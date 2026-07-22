"""Build schema-validated, last-good research records from Task 1-3 providers."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.recommendation import recommend
from scripts.scoring import score_fund, score_stock


PIPELINE_VERSION = "4.1"
SAFE_ASSET_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,119}$")
STATES = {"暂不纳入", "等待确认", "风险偏高", "优先研究", "持续观察"}


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


def _status(*, stale: bool, attempted_at: str, provider: str, source_urls: list[str] | None = None, error: str | None = None, retrieved_at: str | None = None, last_success_at: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"provider": provider, "source_urls": list(source_urls or []), "attempted_at": attempted_at, "retrieved_at": retrieved_at or "", "last_success_at": last_success_at or "", "stale": stale, "error": error or ""}
    return result


def _prior_status(previous: dict[str, Any], key: str) -> dict[str, Any]:
    return _as_dict(_as_dict(previous.get("source_status")).get(key))


def _last_success(previous: dict[str, Any], key: str) -> str:
    return str(_prior_status(previous, key).get("last_success_at") or _prior_status(previous, key).get("retrieved_at") or "")


def _merge_market(previous: dict[str, Any], incoming: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    market = _as_dict(previous.get("market"))
    for field, value in incoming.items():
        if field not in {"asset", "errors"} and field not in errors and value not in (None, {}, []):
            market[field] = value
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


def _validate_detail(detail: dict[str, Any]) -> None:
    required = {"asset", "market", "uzi", "news", "score", "recommendation", "source_status"}
    if set(detail) != required or not SAFE_ASSET_ID.fullmatch(str(detail["asset"].get("id") or "")):
        raise ValueError("invalid or unsafe asset detail schema")
    _validate_news(detail["news"])
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


def _fund_market(asset: dict[str, Any], previous: dict[str, Any], options: dict[str, Any], now: str) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = options.get("fund_provider")
    if provider is None:
        return _as_dict(previous.get("market")), _status(stale=True, attempted_at=now, provider="eastmoney", error="fund_provider_unavailable", last_success_at=_last_success(previous, "market"))
    try:
        result = provider.fetch_fund(asset)
        incoming = _as_dict(getattr(result, "data", result))
        errors = _as_dict(getattr(result, "errors", {}))
        market = _merge_market(previous, incoming, errors)
        retrieved = _iso(getattr(result, "retrieved_at", now))
        return market, _status(stale=bool(errors), attempted_at=now, provider=type(provider).__name__, source_urls=list(getattr(result, "source_urls", [])), error="; ".join(str(value) for value in errors.values()), retrieved_at=retrieved, last_success_at=retrieved if market else _last_success(previous, "market"))
    except Exception as error:
        return _as_dict(previous.get("market")), _status(stale=True, attempted_at=now, provider=type(provider).__name__, error=str(error), last_success_at=_last_success(previous, "market"))


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
            statuses[status_key] = _status(stale=not bool(fetched), attempted_at=now, provider=getattr(provider, "__name__", type(provider).__name__), error="" if fetched else "no_reliable_update", retrieved_at=now if fetched else "", last_success_at=now if fetched else _last_success(previous, status_key))
        except Exception as error:
            streams[region] = list(previous_news.get(region, []))
            statuses[status_key] = _status(stale=True, attempted_at=now, provider=getattr(provider, "__name__", type(provider).__name__), error=str(error), last_success_at=_last_success(previous, status_key))
    return streams, statuses


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
            statuses["market"] = _status(stale=not bool(incoming_market), attempted_at=now, provider="market_data", error="" if incoming_market else "market_data_unavailable", retrieved_at=now if incoming_market else "", last_success_at=now if incoming_market else _last_success(prior, "market"))
            incoming_uzi = _as_dict(_as_dict(options.get("uzi")).get(asset_id))
            uzi_ok = incoming_uzi.get("overall") not in (None, "") or incoming_uzi.get("overall_score") not in (None, "")
            uzi = incoming_uzi if uzi_ok else _as_dict(prior.get("uzi"))
            statuses["uzi"] = _status(stale=not uzi_ok, attempted_at=now, provider="uzi", error="" if uzi_ok else "direct_uzi_unavailable", retrieved_at=now if uzi_ok else "", last_success_at=now if uzi_ok else _last_success(prior, "uzi"))
            score = _score_dict(score_stock(asset, market, uzi))
        elif asset.get("asset_type") in {"fund", "etf", "lof"}:
            market, statuses["market"] = _fund_market(asset, prior, options, now)
            uzi = {}
            holding_uzi = _as_dict(options.get("holding_uzi"))
            score = _score_dict(score_fund(asset, market, holding_uzi))
        else:
            raise ValueError("unsupported asset type")
        news, news_statuses = _news_streams(asset, prior, options, now)
        statuses.update(news_statuses)
        hard_failures = [key for key, status in statuses.items() if status.get("error") in {"direct_uzi_unavailable", "market_data_unavailable"}]
        stale = any(status.get("stale") for status in statuses.values())
        recommendation = recommend(score, {"stale": stale, "hard_failures": hard_failures, "timestamp": now, "evidence": {"source_status": "stale" if stale else "fresh"}})
        detail = {"asset": asset, "market": market, "uzi": uzi, "news": news, "score": score, "recommendation": recommendation, "source_status": statuses}
        _validate_detail(detail)
        records[asset_id] = detail
        summaries.append({"id": asset_id, "code": asset.get("code"), "name": asset.get("name"), "asset_type": asset.get("asset_type"), "state": recommendation["state"], "confidence": recommendation["confidence"], "stale": stale})
    dashboard = {"generated_at": now, "pipeline_version": PIPELINE_VERSION, "source_status": {"pipeline": _status(stale=any(item["stale"] for item in summaries), attempted_at=now, provider="update_monitor", retrieved_at=now, last_success_at=now)}, "stale_count": sum(1 for item in summaries if item["stale"]), "asset_count": len(summaries), "assets": summaries}
    _validate_dashboard(dashboard)
    if options.get("write"):
        output_dir = Path(options.get("output_dir") or "data")
        for asset_id, detail in records.items():
            _atomic_json(output_dir / "assets" / f"{asset_id}.json", detail)
        _atomic_json(output_dir / "dashboard.json", dashboard)
    return {"dashboard": dashboard, "assets": records}
