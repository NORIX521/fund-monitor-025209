"""Build durable, versioned research dashboard records from Task 1-3 providers."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from scripts.recommendation import recommend
from scripts.scoring import score_fund, score_stock


PIPELINE_VERSION = "4.0"


def _as_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        value = asdict(value)
    return dict(value) if isinstance(value, dict) else {}


def _status(*, stale: bool, error: str | None = None, timestamp: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"stale": stale, "timestamp": timestamp}
    if error:
        result["error"] = error
    return result


def _previous_component(previous: dict[str, Any], name: str, fallback: Any) -> Any:
    value = previous.get(name)
    return value if value not in (None, {}, []) else fallback


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _validate_detail(detail: dict[str, Any]) -> None:
    required = {"asset", "market", "news", "score", "recommendation", "source_status"}
    if set(detail) != required or not detail["asset"].get("id"):
        raise ValueError("invalid asset detail schema")


def _validate_dashboard(dashboard: dict[str, Any]) -> None:
    required = {"generated_at", "pipeline_version", "source_status", "stale_count", "asset_count", "assets"}
    if set(dashboard) != required or dashboard["asset_count"] != len(dashboard["assets"]):
        raise ValueError("invalid dashboard schema")


def _fund_market(asset: dict[str, Any], previous: dict[str, Any], options: dict[str, Any], now: str) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = options.get("fund_provider")
    if provider is None:
        return _previous_component(previous, "market", {}), _status(stale=True, error="fund_provider_unavailable", timestamp=now)
    try:
        result = provider.fetch_fund(asset)
        payload = _as_dict(getattr(result, "data", result))
        payload.pop("asset", None)
        errors = _as_dict(getattr(result, "errors", {}))
        if not payload:
            raise RuntimeError("fund_provider_returned_no_data")
        if errors:
            payload["errors"] = errors
        return payload, _status(stale=bool(errors), error="; ".join(errors.values()) or None, timestamp=str(getattr(result, "retrieved_at", now)))
    except Exception as error:
        return _previous_component(previous, "market", {}), _status(stale=True, error=str(error), timestamp=now)


def _score_dict(result: Any) -> dict[str, Any]:
    return asdict(result) if is_dataclass(result) else _as_dict(result)


def run_pipeline(watchlist: dict[str, Any], previous: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Refresh enabled assets without replacing a failed component with an empty value."""
    now = str(options.get("now") or "")
    previous_assets = previous.get("assets", {}) if isinstance(previous, dict) else {}
    asset_records: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []
    for asset in watchlist.get("assets", []):
        if not isinstance(asset, dict) or asset.get("enabled") is False:
            continue
        asset = dict(asset)
        asset_id = str(asset.get("id") or "")
        if not asset_id:
            raise ValueError("watchlist asset requires id")
        prior = _as_dict(previous_assets.get(asset_id))
        statuses: dict[str, Any] = {}
        if asset.get("asset_type") == "stock":
            market_data = _as_dict(options.get("market_data", {}).get(asset_id))
            market = market_data or _previous_component(prior, "market", {})
            statuses["market"] = _status(stale=not bool(market_data), error=None if market_data else "market_data_unavailable", timestamp=now)
            uzi = _as_dict(options.get("uzi", {}).get(asset_id))
            uzi_ok = bool(uzi) and (uzi.get("overall") not in (None, "") or uzi.get("overall_score") not in (None, ""))
            statuses["uzi"] = _status(stale=not uzi_ok, error=None if uzi_ok else "direct_uzi_unavailable", timestamp=now)
            score = _score_dict(score_stock(asset, market, uzi))
        elif asset.get("asset_type") in {"fund", "etf", "lof"}:
            market, statuses_market = _fund_market(asset, prior, options, now)
            statuses["market"] = statuses_market
            holding_uzi = options.get("holding_uzi", {})
            score = _score_dict(score_fund(asset, market, holding_uzi if isinstance(holding_uzi, dict) else {}))
        else:
            raise ValueError("unsupported asset type")
        region = "CN" if asset.get("market") == "CN" else "INTL"
        try:
            news = list(options.get("news_provider", lambda *_: [])(asset, region))
            statuses["news"] = _status(stale=False, timestamp=now)
        except Exception as error:
            news = _previous_component(prior, "news", [])
            statuses["news"] = _status(stale=True, error=str(error), timestamp=now)
        hard_failures = [name for name, status in statuses.items() if status.get("error") in {"direct_uzi_unavailable", "market_data_unavailable"}]
        stale = any(status.get("stale") for status in statuses.values())
        recommendation = recommend(score, {"stale": stale, "hard_failures": hard_failures, "timestamp": now, "evidence": {"source_status": "stale" if stale else "fresh"}})
        detail = {"asset": asset, "market": market, "news": news, "score": score, "recommendation": recommendation, "source_status": statuses}
        _validate_detail(detail)
        asset_records[asset_id] = detail
        summaries.append({"id": asset_id, "code": asset.get("code"), "name": asset.get("name"), "asset_type": asset.get("asset_type"), "state": recommendation["state"], "confidence": recommendation["confidence"], "stale": stale})
    stale_count = sum(1 for summary in summaries if summary["stale"])
    dashboard = {"generated_at": now, "pipeline_version": PIPELINE_VERSION, "source_status": {"pipeline": _status(stale=bool(stale_count), timestamp=now)}, "stale_count": stale_count, "asset_count": len(summaries), "assets": summaries}
    _validate_dashboard(dashboard)
    if options.get("write"):
        output_dir = Path(options.get("output_dir") or "data")
        for asset_id, detail in asset_records.items():
            _atomic_json(output_dir / "assets" / f"{asset_id}.json", detail)
        _atomic_json(output_dir / "dashboard.json", dashboard)
    return {"dashboard": dashboard, "assets": asset_records}
