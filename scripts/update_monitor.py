"""Build schema-validated, last-good research records from Task 1-3 providers."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __package__:
    from .recommendation import recommend
    from .scoring import score_fund, score_stock
    from .uzi_adapter import publish_uzi_manifest
else:
    from recommendation import recommend
    from scoring import score_fund, score_stock
    from uzi_adapter import publish_uzi_manifest


PIPELINE_VERSION = "4.1"
PIPELINE_SOURCE_URL = "https://github.com/NORIX521/fund-monitor-025209/blob/main/scripts/update_monitor.py"
UZI_SOURCE_URL = "https://github.com/wbh604/UZI-Skill/tree/fce996c33e70eddce8e375f53cd252b549eb3d7c"
SAFE_ASSET_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,119}$")
STATES = {"暂不纳入", "等待确认", "风险偏高", "优先研究", "持续观察"}
STATUS_FIELDS = {"provider", "source_urls", "attempted_at", "retrieved_at", "last_success_at", "stale", "error", "coverage"}
FUND_MARKET_FIELDS = {
    "history",
    "holdings",
    "holding_report_date",
    "size_billion",
    "expense_ratio_pct",
    "age_years",
    "manager_tenure_years",
    "risk_flags",
}
FUND_PRIOR_CORE_FIELDS = {"history", "holdings", "holding_report_date"}


def _as_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        value = asdict(value)
    return dict(value) if isinstance(value, dict) else {}


def _iso(value: Any | None = None) -> str:
    if value in (None, ""):
        return datetime.now(timezone.utc).isoformat()
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.isoformat()


def _status(*, stale: bool, attempted_at: str, provider: str, source_urls: list[str] | None = None, error: str | None = None, retrieved_at: str | None = None, last_success_at: str | None = None, coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"provider": provider, "source_urls": list(source_urls or []), "attempted_at": attempted_at, "retrieved_at": retrieved_at or "", "last_success_at": last_success_at or "", "stale": stale, "error": error or "", "coverage": dict(coverage or {})}
    return result


def _prior_status(previous: dict[str, Any], key: str) -> dict[str, Any]:
    return _as_dict(_as_dict(previous.get("source_status")).get(key))


def _last_success(previous: dict[str, Any], key: str) -> str:
    return str(_prior_status(previous, key).get("last_success_at") or _prior_status(previous, key).get("retrieved_at") or "")


def _holding_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    base = code.split(".", maxsplit=1)[0]
    if re.fullmatch(r"\d{6}", base):
        suffix = "SH" if base.startswith(("5", "6", "9")) else "BJ" if base.startswith(("4", "8")) else "SZ"
        return f"{base}.{suffix}"
    return code


def _merge_holdings(previous: Any, incoming: Any, quote_failed: bool) -> list[dict[str, Any]]:
    earlier = previous if isinstance(previous, list) else []
    refreshed = incoming if isinstance(incoming, list) else []
    by_code = {_holding_code(item.get("code")): item for item in earlier if isinstance(item, dict)}
    merged: list[dict[str, Any]] = []
    for item in refreshed:
        if not isinstance(item, dict):
            continue
        prior = _as_dict(by_code.get(_holding_code(item.get("code"))))
        value = {**prior, **item}
        if prior:
            for field in ("name", "latest_price", "change_pct"):
                if quote_failed or item.get(field) in (None, ""):
                    if field in prior:
                        value[field] = prior[field]
        merged.append(value)
    return merged


def _merge_market(previous: dict[str, Any], incoming: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    market = _as_dict(previous.get("market"))
    for field, value in incoming.items():
        if field not in {"asset", "errors"} and field not in errors and value not in (None, {}, []):
            market[field] = _merge_holdings(market.get("holdings"), value, "quotes" in errors) if field == "holdings" else value
    return market


def _prior_fund_market(previous: dict[str, Any]) -> dict[str, Any]:
    """Keep only provider-supported last-good fund fields, never fixture extras."""
    market = _as_dict(previous.get("market"))
    return {
        field: market[field]
        for field in FUND_PRIOR_CORE_FIELDS
        if field in market and market[field] not in (None, {}, [])
    }


def _merge_fund_market(
    previous: dict[str, Any], incoming: dict[str, Any], errors: dict[str, Any]
) -> dict[str, Any]:
    market = _prior_fund_market(previous)
    for field, value in incoming.items():
        if (
            field not in FUND_MARKET_FIELDS
            or field in errors
            or value in (None, {}, [])
        ):
            continue
        market[field] = (
            _merge_holdings(market.get("holdings"), value, "quotes" in errors)
            if field == "holdings"
            else value
        )
    return market


def _json_compatible(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return not isinstance(value, bool) and math.isfinite(value)
    if isinstance(value, list):
        return all(_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_compatible(item) for key, item in value.items())
    return False


def _url(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _source_urls(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("source URLs must be a list")
    result: list[str] = []
    for item in value:
        if not _url(item):
            raise ValueError("source URLs must contain HTTP(S) URLs")
        normalized = str(item)
        if normalized not in result:
            result.append(normalized)
    return result


def _validate_news(news: dict[str, Any]) -> None:
    if set(news) != {"CN", "INTL"}:
        raise ValueError("news must contain CN and INTL streams")
    for region, items in news.items():
        if not isinstance(items, list):
            raise ValueError("news stream must be a list")
        for item in items:
            required = {"title", "article_url", "source", "source_url", "published_at", "retrieved_at", "region"}
            if not isinstance(item, dict) or set(item) != required or not item["title"] or not item["source"] or item["region"] != region or not _url(item["article_url"]) or not _url(item["source_url"]):
                raise ValueError("invalid news item")
            _iso(item["published_at"])
            _iso(item["retrieved_at"])


def _validate_source_status(source_status: dict[str, Any]) -> None:
    if not isinstance(source_status, dict) or not source_status:
        raise ValueError("source_status is required")
    for name, value in source_status.items():
        if not isinstance(name, str) or not isinstance(value, dict) or set(value) != STATUS_FIELDS:
            raise ValueError("invalid source_status record")
        if not isinstance(value["provider"], str) or not value["provider"] or not isinstance(value["source_urls"], list) or not all(_url(url) for url in value["source_urls"]):
            raise ValueError("invalid source_status provider or URL")
        if type(value["stale"]) is not bool or not isinstance(value["error"], str) or not isinstance(value["attempted_at"], str) or not value["attempted_at"]:
            raise ValueError("invalid source_status fields")
        coverage = value["coverage"]
        if not isinstance(coverage, dict) or (coverage and (set(coverage) != {"covered", "total", "pct"} or not isinstance(coverage["covered"], int) or not isinstance(coverage["total"], int) or coverage["total"] <= 0 or not isinstance(coverage["pct"], (int, float)))):
            raise ValueError("invalid source_status coverage")
        _iso(value["attempted_at"])
        for timestamp in (value["retrieved_at"], value["last_success_at"]):
            if not isinstance(timestamp, str):
                raise ValueError("invalid source_status timestamp")
            if timestamp:
                _iso(timestamp)
        if value["stale"]:
            if not value["error"].strip() and not value["last_success_at"]:
                raise ValueError("stale source_status needs an error or last_success_at")
        elif (
            value["error"]
            or not value["source_urls"]
            or not value["retrieved_at"]
            or not value["last_success_at"]
        ):
            raise ValueError(
                "fresh source_status requires provenance, success timestamps, and no error"
            )


def _validate_detail(detail: dict[str, Any]) -> None:
    required = {"asset", "market", "uzi", "news", "score", "recommendation", "source_status"}
    if set(detail) != required or not SAFE_ASSET_ID.fullmatch(str(detail["asset"].get("id") or "")):
        raise ValueError("invalid or unsafe asset detail schema")
    _validate_news(detail["news"])
    _validate_source_status(detail["source_status"])
    score = _as_dict(detail["score"])
    if score.get("overall") is not None and not isinstance(score.get("overall"), (int, float)):
        raise ValueError("invalid score")
    if not isinstance(score.get("confidence"), (int, float)) or not 0 <= score["confidence"] <= 1:
        raise ValueError("invalid confidence")
    recommendation = _as_dict(detail["recommendation"])
    if recommendation.get("state") not in STATES or not isinstance(recommendation.get("confidence"), (int, float)) or not 0 <= recommendation["confidence"] <= 1:
        raise ValueError("invalid recommendation")
    _iso(recommendation.get("timestamp"))
    if not _json_compatible(detail):
        raise ValueError("detail must contain finite JSON-compatible values")


def _validate_dashboard(dashboard: dict[str, Any]) -> None:
    required = {"generated_at", "pipeline_version", "source_status", "stale_count", "asset_count", "assets"}
    if set(dashboard) != required or not isinstance(dashboard["assets"], list) or dashboard["asset_count"] != len(dashboard["assets"]):
        raise ValueError("invalid dashboard schema")
    _iso(dashboard["generated_at"])
    _validate_source_status(dashboard["source_status"])
    if not _json_compatible(dashboard):
        raise ValueError("dashboard must contain finite JSON-compatible values")
    for item in dashboard["assets"]:
        if not isinstance(item, dict) or not SAFE_ASSET_ID.fullmatch(str(item.get("id") or "")) or item.get("state") not in STATES or not isinstance(item.get("confidence"), (int, float)) or not 0 <= item["confidence"] <= 1:
            raise ValueError("invalid dashboard asset summary")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _component_status(previous: dict[str, Any], key: str, *, attempted_at: str, provider: str, source_urls: list[str], error: str, succeeded: bool, retrieved_at: str, coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    urls = _source_urls(source_urls)
    proven_success = succeeded and bool(urls) and not error
    prior_success = _last_success(previous, key)
    status_error = error or (
        "source_provenance_unavailable" if succeeded and not urls else ""
    )
    if not proven_success and not status_error and not prior_success:
        status_error = f"{key}_no_reliable_update"
    return _status(stale=not proven_success, attempted_at=attempted_at, provider=provider, source_urls=urls, error=status_error, retrieved_at=retrieved_at if proven_success else "", last_success_at=retrieved_at if proven_success else prior_success, coverage=coverage)


def _fund_market(asset: dict[str, Any], previous: dict[str, Any], options: dict[str, Any], now: str) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = options.get("fund_provider")
    if provider is None:
        statuses = {key: _component_status(previous, key, attempted_at=now, provider="eastmoney", source_urls=[], error="fund_provider_unavailable", succeeded=False, retrieved_at="") for key in ("history", "holdings", "quotes")}
        statuses["market"] = _component_status(previous, "market", attempted_at=now, provider="eastmoney", source_urls=[], error="fund_provider_unavailable", succeeded=False, retrieved_at="")
        return _prior_fund_market(previous), statuses
    try:
        result = provider.fetch_fund(asset)
        incoming = _as_dict(getattr(result, "data", result))
        errors = _as_dict(getattr(result, "errors", {}))
        retrieved = _iso(getattr(result, "retrieved_at", now))
        urls = list(getattr(result, "source_urls", []))
        provider_name = type(provider).__name__
        history_ok = "history" in incoming and incoming.get("history") not in (None, [], {}) and "history" not in errors
        holdings_ok = "holdings" in incoming and incoming.get("holdings") not in (None, [], {}) and "holdings" not in errors
        quote_holdings = [holding for holding in incoming.get("holdings", []) if isinstance(holding, dict)]
        covered = sum(1 for holding in quote_holdings if holding.get("latest_price") is not None or holding.get("change_pct") is not None)
        total = len(quote_holdings)
        coverage = {"covered": covered, "total": total, "pct": covered / total * 100} if total else {}
        quote_error = str(errors.get("quotes", "")) or ("quote_no_data" if holdings_ok and covered == 0 else "quote_partial_data" if holdings_ok and covered < total else "")
        quotes_ok = holdings_ok and not quote_error
        merge_errors = {**errors, **({"quotes": quote_error} if quote_error else {})}
        market = _merge_fund_market(previous, incoming, {key: value for key, value in merge_errors.items() if key != "quotes" or value != "quote_partial_data"})
        statuses = {
            "history": _component_status(previous, "history", attempted_at=now, provider=provider_name, source_urls=urls, error=str(errors.get("history", "")), succeeded=history_ok, retrieved_at=retrieved),
            "holdings": _component_status(previous, "holdings", attempted_at=now, provider=provider_name, source_urls=urls, error=str(errors.get("holdings", "")), succeeded=holdings_ok, retrieved_at=retrieved),
            "quotes": _component_status(previous, "quotes", attempted_at=now, provider=provider_name, source_urls=urls, error=quote_error, succeeded=quotes_ok, retrieved_at=retrieved, coverage=coverage),
        }
        any_success = history_ok or holdings_ok or quotes_ok
        statuses["market"] = _component_status(previous, "market", attempted_at=now, provider=provider_name, source_urls=urls, error="; ".join(str(value) for value in merge_errors.values()), succeeded=any_success and not merge_errors, retrieved_at=retrieved)
        return market, statuses
    except Exception as error:
        statuses = {key: _component_status(previous, key, attempted_at=now, provider=type(provider).__name__, source_urls=[], error=str(error), succeeded=False, retrieved_at="") for key in ("history", "holdings", "quotes", "market")}
        return _prior_fund_market(previous), statuses


def _score_dict(result: Any) -> dict[str, Any]:
    return asdict(result) if is_dataclass(result) else _as_dict(result)


def _news_item(item: Any, region: str) -> dict[str, Any]:
    value = asdict(item) if is_dataclass(item) else _as_dict(item)
    value["region"] = value.get("region") or region
    return value


def _news_streams(asset: dict[str, Any], previous: dict[str, Any], options: dict[str, Any], now: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    previous_news = _as_dict(previous.get("news"))
    provider = options.get("news_provider")
    streams: dict[str, list[dict[str, Any]]] = {}
    statuses: dict[str, Any] = {}
    for region in ("CN", "INTL"):
        status_key = f"news_{region}"
        if provider is None:
            streams[region] = list(previous_news.get(region, []))
            statuses[status_key] = _status(stale=True, attempted_at=now, provider="news", error="news_provider_unconfigured", last_success_at=_last_success(previous, status_key))
            continue
        try:
            fetched = [_news_item(item, region) for item in provider(asset, region)]
            streams[region] = fetched or list(previous_news.get(region, []))
            source_urls = _source_urls(
                [item.get("source_url") for item in fetched if isinstance(item, dict)]
            )
            succeeded = bool(fetched) and bool(source_urls)
            statuses[status_key] = _status(stale=not succeeded, attempted_at=now, provider=getattr(provider, "__name__", type(provider).__name__), source_urls=source_urls, error="" if succeeded else "source_provenance_unavailable" if fetched else "no_reliable_update", retrieved_at=now if succeeded else "", last_success_at=now if succeeded else _last_success(previous, status_key))
        except Exception as error:
            streams[region] = list(previous_news.get(region, []))
            statuses[status_key] = _status(stale=True, attempted_at=now, provider=getattr(provider, "__name__", type(provider).__name__), error=str(error), last_success_at=_last_success(previous, status_key))
    return streams, statuses


def _stock_market_from_uzi(
    direct_uzi: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, str]]:
    """Expose only named, normalized UZI evidence as stock score components."""
    market_data: dict[str, dict[str, Any]] = {}
    source_urls: dict[str, list[str]] = {}
    providers: dict[str, str] = {}
    for asset_id, uzi in direct_uzi.items():
        if not isinstance(uzi, dict):
            continue
        evidence: dict[str, Any] = {}
        if uzi.get("fundamental_score") not in (None, ""):
            evidence["quality_valuation"] = uzi["fundamental_score"]
        schools = _as_dict(uzi.get("school_scores"))
        if schools.get("D") not in (None, ""):
            evidence["trend_momentum"] = schools["D"]
        if not evidence:
            continue
        evidence["evidence_origin"] = "uzi_normalized_panel"
        evidence["upstream_commit"] = str(uzi.get("upstream_commit") or "")
        market_data[asset_id] = evidence
        source_urls[asset_id] = [UZI_SOURCE_URL]
        providers[asset_id] = "uzi_normalized_panel"
    return market_data, source_urls, providers


def run_pipeline(watchlist: dict[str, Any], previous: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Refresh enabled assets while preserving valid last-good component evidence."""
    now = _iso(options.get("now"))
    previous_assets = _as_dict(previous.get("assets"))
    records: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []
    for supplied_asset in watchlist.get("assets", []):
        if not isinstance(supplied_asset, dict) or supplied_asset.get("enabled") is False:
            continue
        asset = dict(supplied_asset)
        asset_id = str(asset.get("id") or "")
        if not SAFE_ASSET_ID.fullmatch(asset_id):
            raise ValueError("watchlist asset requires a safe id")
        prior = _as_dict(previous_assets.get(asset_id))
        statuses: dict[str, Any] = {}
        if asset.get("asset_type") == "stock":
            incoming_market = _as_dict(_as_dict(options.get("market_data")).get(asset_id))
            market = _merge_market(prior, incoming_market, {})
            market_urls = _source_urls(
                _as_dict(options.get("market_source_urls")).get(asset_id)
            )
            market_ok = bool(incoming_market) and bool(market_urls)
            market_error = (
                ""
                if market_ok
                else "market_data_unavailable"
                if not incoming_market
                else "market_provenance_unavailable"
            )
            market_provider = str(
                _as_dict(options.get("market_providers")).get(asset_id)
                or "market_data"
            )
            statuses["market"] = _status(stale=not market_ok, attempted_at=now, provider=market_provider, source_urls=market_urls, error=market_error, retrieved_at=now if market_ok else "", last_success_at=now if market_ok else _last_success(prior, "market"))
            incoming_uzi = _as_dict(_as_dict(options.get("uzi")).get(asset_id))
            uzi_ok = incoming_uzi.get("overall") not in (None, "") or incoming_uzi.get("overall_score") not in (None, "")
            uzi = incoming_uzi if uzi_ok else _as_dict(prior.get("uzi"))
            statuses["uzi"] = _status(stale=not uzi_ok, attempted_at=now, provider="uzi", source_urls=[UZI_SOURCE_URL] if uzi_ok else [], error="" if uzi_ok else "direct_uzi_unavailable", retrieved_at=now if uzi_ok else "", last_success_at=now if uzi_ok else _last_success(prior, "uzi"))
            score = _score_dict(score_stock(asset, market, uzi))
        elif asset.get("asset_type") in {"fund", "etf", "lof"}:
            market, fund_statuses = _fund_market(asset, prior, options, now)
            statuses.update(fund_statuses)
            uzi = {}
            holding_uzi = _as_dict(options.get("holding_uzi"))
            score = _score_dict(score_fund(asset, market, holding_uzi))
        else:
            raise ValueError("unsupported asset type")
        news, news_statuses = _news_streams(asset, prior, options, now)
        statuses.update(news_statuses)
        hard_failures = [key for key, status in statuses.items() if status.get("error") == "direct_uzi_unavailable"]
        stale = any(status.get("stale") for status in statuses.values())
        recommendation = recommend(score, {"stale": stale, "hard_failures": hard_failures, "timestamp": now, "evidence": {"source_status": "stale" if stale else "fresh"}})
        detail = {"asset": asset, "market": market, "uzi": uzi, "news": news, "score": score, "recommendation": recommendation, "source_status": statuses}
        _validate_detail(detail)
        records[asset_id] = detail
        summaries.append({"id": asset_id, "code": asset.get("code"), "name": asset.get("name"), "asset_type": asset.get("asset_type"), "state": recommendation["state"], "confidence": recommendation["confidence"], "stale": stale})
    dashboard = {"generated_at": now, "pipeline_version": PIPELINE_VERSION, "source_status": {"pipeline": _status(stale=any(item["stale"] for item in summaries), attempted_at=now, provider="update_monitor", source_urls=[PIPELINE_SOURCE_URL], retrieved_at=now, last_success_at=now)}, "stale_count": sum(1 for item in summaries if item["stale"]), "asset_count": len(summaries), "assets": summaries}
    _validate_dashboard(dashboard)
    if options.get("write"):
        output_dir = Path(options.get("output_dir") or "data")
        for asset_id, detail in records.items():
            _atomic_json(output_dir / "assets" / f"{asset_id}.json", detail)
        _atomic_json(output_dir / "dashboard.json", dashboard)
    return {"dashboard": dashboard, "assets": records}


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _load_previous(data_dir: Path, watchlist: dict[str, Any]) -> dict[str, Any]:
    previous: dict[str, dict[str, Any]] = {}
    for asset in watchlist.get("assets", []):
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("id") or "")
        if not SAFE_ASSET_ID.fullmatch(asset_id):
            raise ValueError("watchlist asset requires a safe id")
        detail_path = data_dir / "assets" / f"{asset_id}.json"
        if detail_path.is_file():
            previous[asset_id] = _load_object(detail_path)
    return {"assets": previous}


def _load_public_uzi(path: Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    if not path.is_dir():
        return results
    for source in sorted(path.glob("*.json")):
        value = _load_object(source)
        ticker = str(value.get("ticker") or source.stem).strip().upper()
        if ticker:
            results[ticker] = value
    return results


def _select_current_uzi(
    watchlist: dict[str, Any],
    published: dict[str, dict[str, Any]],
    manifest: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Select only scores whose manifest and public record prove a current refresh."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tickers"), dict):
        return {}, {}
    refreshed = {
        str(ticker).strip().upper(): record
        for ticker, record in published.items()
        if isinstance(record, dict)
        and record.get("stale") is False
        and _as_dict(record.get("run")).get("status") == "refreshed_this_run"
        and _as_dict(manifest["tickers"].get(str(ticker).strip().upper())).get("status")
        == "refreshed_this_run"
    }
    stocks = [
        asset
        for asset in watchlist.get("assets", [])
        if isinstance(asset, dict)
        and asset.get("asset_type") == "stock"
        and asset.get("enabled", True) is True
    ]
    direct = {
        str(asset["id"]): refreshed[str(asset.get("code") or "").strip().upper()]
        for asset in stocks
        if str(asset.get("code") or "").strip().upper() in refreshed
    }
    return direct, refreshed


def run_from_paths(
    watchlist_path: str | Path,
    data_dir: str | Path,
    uzi_cache: str | Path,
    uzi_manifest: str | Path | None = None,
    stage: str = "final",
) -> dict[str, Any]:
    """Load local state, refresh sourced evidence, and atomically publish data files."""
    if __package__:
        from .providers.eastmoney import EastmoneyProvider
        from .providers.news import fetch_news
    else:
        from providers.eastmoney import EastmoneyProvider
        from providers.news import fetch_news

    watchlist_file = Path(watchlist_path)
    output_dir = Path(data_dir)
    watchlist = _load_object(watchlist_file)
    if watchlist.get("version") != 1 or not isinstance(watchlist.get("assets"), list):
        raise ValueError("watchlist must be a version 1 object with an assets array")
    if stage not in {"prepare", "final"}:
        raise ValueError("stage must be prepare or final")
    previous = _load_previous(output_dir, watchlist)
    cache_path = Path(uzi_cache)
    direct_uzi: dict[str, dict[str, Any]] = {}
    holding_uzi: dict[str, dict[str, Any]] = {}
    if stage == "final":
        manifest_path = Path(uzi_manifest) if uzi_manifest else None
        if manifest_path is None or not manifest_path.is_file():
            raise ValueError("final stage requires --uzi-manifest")
        manifest = _load_object(manifest_path)
        published = publish_uzi_manifest(cache_path, manifest, output_dir / "uzi")
        direct_uzi, holding_uzi = _select_current_uzi(watchlist, published, manifest)
    market_data, market_source_urls, market_providers = _stock_market_from_uzi(
        direct_uzi
    )
    result = run_pipeline(
        watchlist,
        previous,
        {
            "fund_provider": EastmoneyProvider(),
            "news_provider": fetch_news,
            "market_data": market_data,
            "market_source_urls": market_source_urls,
            "market_providers": market_providers,
            "uzi": direct_uzi,
            "holding_uzi": holding_uzi,
            "output_dir": output_dir,
            "write": True,
        },
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist", type=Path, default=Path("data/watchlist.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--uzi-cache", type=Path, required=True)
    parser.add_argument("--uzi-manifest", type=Path)
    parser.add_argument("--stage", choices=("prepare", "final"), default="final")
    args = parser.parse_args(argv)
    try:
        result = run_from_paths(
            args.watchlist,
            args.data_dir,
            args.uzi_cache,
            args.uzi_manifest,
            args.stage,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "success",
                "stage": args.stage,
                "asset_count": result["dashboard"]["asset_count"],
                "stale_count": result["dashboard"]["stale_count"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
