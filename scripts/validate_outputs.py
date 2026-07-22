"""Deep output validation and deterministic offline browser-demo generation."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "watchlist-mixed.json"
FIXTURE_TIME = "2000-01-01T00:00:00+00:00"
SAFE_ASSET_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,119}$")
ASSET_TYPES = {"stock", "fund", "etf", "lof"}
STATES = {"暂不纳入", "等待确认", "风险偏高", "优先研究", "持续观察"}
STATUS_FIELDS = {
    "provider",
    "source_urls",
    "attempted_at",
    "retrieved_at",
    "last_success_at",
    "stale",
    "error",
    "coverage",
}
DETAIL_FIELDS = {
    "asset",
    "market",
    "uzi",
    "news",
    "score",
    "recommendation",
    "source_status",
}
SUMMARY_FIELDS = {
    "id",
    "code",
    "name",
    "asset_type",
    "state",
    "confidence",
    "stale",
}
DASHBOARD_FIELDS = {
    "generated_at",
    "pipeline_version",
    "source_status",
    "stale_count",
    "asset_count",
    "assets",
}
MAX_DASHBOARD_BYTES = 128_000


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _bounded(value: Any, minimum: float, maximum: float) -> bool:
    return _finite_number(value) and minimum <= float(value) <= maximum


def _safe_url(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _finite_json(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return not isinstance(value, bool) and math.isfinite(float(value))
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json(item) for key, item in value.items())
    return False


def _read_json(path: Path, label: str, errors: list[str]) -> Any:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite constant {value}")
            ),
        )
        if not _finite_json(value):
            raise ValueError("non-finite numeric value")
        return value
    except FileNotFoundError:
        errors.append(f"{label}: missing file {path}")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        errors.append(f"{label}: finite JSON required ({error})")
    return None


def _validate_statuses(
    statuses: Any, label: str, errors: list[str], required: set[str] | None = None
) -> None:
    if not isinstance(statuses, dict) or not statuses:
        errors.append(f"{label}: source_status provenance is required")
        return
    if required:
        for key in sorted(required - set(statuses)):
            errors.append(f"{label}: source_status.{key} is required")
    for key, status in statuses.items():
        prefix = f"{label}: source_status.{key}"
        if not isinstance(key, str) or not isinstance(status, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(status) != STATUS_FIELDS:
            errors.append(f"{prefix} fields must equal {sorted(STATUS_FIELDS)}")
            continue
        if not isinstance(status["provider"], str) or not status["provider"].strip():
            errors.append(f"{prefix}.provider is required")
        urls = status["source_urls"]
        if not isinstance(urls, list) or any(not _safe_url(url) for url in urls):
            errors.append(f"{prefix}.source_urls must contain HTTP(S) URLs")
        if not _aware_timestamp(status["attempted_at"]):
            errors.append(f"{prefix}.attempted_at must be timezone-aware ISO-8601")
        for timestamp_name in ("retrieved_at", "last_success_at"):
            timestamp = status[timestamp_name]
            if not isinstance(timestamp, str) or (
                timestamp and not _aware_timestamp(timestamp)
            ):
                errors.append(
                    f"{prefix}.{timestamp_name} must be empty or timezone-aware ISO-8601"
                )
        if type(status["stale"]) is not bool or not isinstance(status["error"], str):
            errors.append(f"{prefix} stale/error fields are invalid")
        if status.get("stale") is True and not (
            status.get("error") or status.get("last_success_at")
        ):
            errors.append(f"{prefix} stale status needs an error or last_success_at")
        coverage = status["coverage"]
        if not isinstance(coverage, dict):
            errors.append(f"{prefix}.coverage must be an object")
        elif coverage:
            if set(coverage) != {"covered", "total", "pct"}:
                errors.append(f"{prefix}.coverage fields are invalid")
            elif (
                not isinstance(coverage["covered"], int)
                or isinstance(coverage["covered"], bool)
                or not isinstance(coverage["total"], int)
                or isinstance(coverage["total"], bool)
                or coverage["total"] <= 0
                or coverage["covered"] < 0
                or coverage["covered"] > coverage["total"]
                or not _bounded(coverage["pct"], 0, 100)
            ):
                errors.append(f"{prefix}.coverage values are invalid")


def _validate_news(news: Any, label: str, errors: list[str]) -> None:
    if not isinstance(news, dict) or set(news) != {"CN", "INTL"}:
        errors.append(f"{label}: news must contain CN and INTL streams")
        return
    required = {
        "title",
        "article_url",
        "source",
        "source_url",
        "published_at",
        "retrieved_at",
        "region",
    }
    for region, items in news.items():
        if not isinstance(items, list):
            errors.append(f"{label}: news.{region} must be a list")
            continue
        for index, item in enumerate(items):
            prefix = f"{label}: news.{region}[{index}]"
            if not isinstance(item, dict) or set(item) != required:
                errors.append(f"{prefix} has invalid fields")
                continue
            if not isinstance(item["title"], str) or not item["title"].strip():
                errors.append(f"{prefix}.title is required")
            if not isinstance(item["source"], str) or not item["source"].strip():
                errors.append(f"{prefix}.source is required")
            if item["region"] != region:
                errors.append(f"{prefix}.region must equal {region}")
            for url_name in ("article_url", "source_url"):
                if not _safe_url(item[url_name]):
                    errors.append(f"{prefix}.{url_name} must be an HTTP(S) URL")
            for timestamp_name in ("published_at", "retrieved_at"):
                if not _aware_timestamp(item[timestamp_name]):
                    errors.append(
                        f"{prefix}.{timestamp_name} must be timezone-aware ISO-8601"
                    )


def _validate_score(score: Any, label: str, errors: list[str]) -> None:
    if not isinstance(score, dict):
        errors.append(f"{label}: score must be an object")
        return
    overall = score.get("overall")
    if overall is not None and not _bounded(overall, 0, 100):
        errors.append(f"{label}: score.overall must be null or between 0 and 100")
    if not _bounded(score.get("confidence"), 0, 1):
        errors.append(f"{label}: score.confidence must be between 0 and 1")
    components = score.get("components")
    if not isinstance(components, dict) or any(
        not isinstance(name, str) or not _bounded(value, 0, 100)
        for name, value in components.items()
    ):
        errors.append(f"{label}: score.components must contain 0-100 values")


def _validate_recommendation(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label}: recommendation must be an object")
        return
    if value.get("state") not in STATES:
        errors.append(f"{label}: recommendation.state is invalid")
    if not _bounded(value.get("confidence"), 0, 1):
        errors.append(f"{label}: recommendation.confidence must be between 0 and 1")
    if not _aware_timestamp(value.get("timestamp")):
        errors.append(f"{label}: recommendation.timestamp must be timezone-aware ISO-8601")
    for key in ("reasons", "invalidation_rules"):
        if not isinstance(value.get(key), list) or any(
            not isinstance(item, str) or not item.strip() for item in value[key]
        ):
            errors.append(f"{label}: recommendation.{key} must contain text")
    if not isinstance(value.get("risk"), dict):
        errors.append(f"{label}: recommendation.risk must be an object")


def _detail_summary(detail: dict[str, Any]) -> dict[str, Any] | None:
    asset = detail.get("asset")
    recommendation = detail.get("recommendation")
    statuses = detail.get("source_status")
    if not isinstance(asset, dict) or not isinstance(recommendation, dict) or not isinstance(statuses, dict):
        return None
    return {
        "id": asset.get("id"),
        "code": asset.get("code"),
        "name": asset.get("name"),
        "asset_type": asset.get("asset_type"),
        "state": recommendation.get("state"),
        "confidence": recommendation.get("confidence"),
        "stale": any(
            isinstance(status, dict) and status.get("stale") is True
            for status in statuses.values()
        ),
    }


def _validate_detail(
    detail: Any,
    expected_asset: dict[str, Any],
    summary: dict[str, Any] | None,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(detail, dict) or set(detail) != DETAIL_FIELDS:
        errors.append(f"{label}: detail fields must equal {sorted(DETAIL_FIELDS)}")
        return
    asset = detail["asset"]
    if not isinstance(asset, dict):
        errors.append(f"{label}: asset must be an object")
        return
    asset_id = asset.get("id")
    if not isinstance(asset_id, str) or not SAFE_ASSET_ID.fullmatch(asset_id):
        errors.append(f"{label}: unsafe asset id")
    if asset != expected_asset:
        errors.append(f"{label}: watchlist/detail asset parity failed")
    asset_type = asset.get("asset_type")
    if asset_type not in ASSET_TYPES:
        errors.append(f"{label}: unsupported asset_type")
    _validate_news(detail["news"], label, errors)
    required_statuses = {"market", "news_CN", "news_INTL"}
    required_statuses |= {"uzi"} if asset_type == "stock" else {
        "history",
        "holdings",
        "quotes",
    }
    _validate_statuses(detail["source_status"], label, errors, required_statuses)
    _validate_score(detail["score"], label, errors)
    _validate_recommendation(detail["recommendation"], label, errors)
    market = detail["market"]
    if not isinstance(market, dict):
        errors.append(f"{label}: market must be an object")
    elif asset_type in {"fund", "etf", "lof"} and market.get("holdings"):
        if not isinstance(market.get("holding_report_date"), str) or not market[
            "holding_report_date"
        ].strip():
            errors.append(f"{label}: holding_report_date is required when holdings exist")
    if asset_type == "stock":
        uzi = detail["uzi"] if isinstance(detail["uzi"], dict) else {}
        direct = uzi.get("overall")
        if direct in (None, ""):
            direct = uzi.get("overall_score")
        if direct not in (None, "") and not _bounded(direct, 0, 100):
            errors.append(f"{label}: direct UZI score must be between 0 and 100")
        if direct in (None, ""):
            status = detail["source_status"].get("uzi", {})
            if not (
                isinstance(status, dict)
                and status.get("stale") is True
                and isinstance(status.get("error"), str)
                and status["error"].strip()
            ):
                errors.append(f"{label}: explicit direct UZI failure is required")
    actual_summary = _detail_summary(detail)
    if summary is not None and actual_summary != summary:
        errors.append(f"{label}: dashboard/detail summary parity failed")


def validate_outputs(root: str | Path) -> list[str]:
    """Return every structural/provenance error found under one data directory."""
    data_root = Path(root)
    errors: list[str] = []
    watchlist = _read_json(data_root / "watchlist.json", "watchlist", errors)
    dashboard_path = data_root / "dashboard.json"
    dashboard = _read_json(dashboard_path, "dashboard", errors)
    if not isinstance(watchlist, dict) or not isinstance(watchlist.get("assets"), list):
        if watchlist is not None:
            errors.append("watchlist: assets must be a list")
        return errors
    if not isinstance(dashboard, dict):
        return errors
    if dashboard_path.exists() and dashboard_path.stat().st_size > MAX_DASHBOARD_BYTES:
        errors.append(
            f"dashboard: lightweight dashboard exceeds {MAX_DASHBOARD_BYTES} bytes"
        )
    if set(dashboard) != DASHBOARD_FIELDS:
        errors.append(f"dashboard: fields must equal {sorted(DASHBOARD_FIELDS)}")
    if not _aware_timestamp(dashboard.get("generated_at")):
        errors.append("dashboard: generated_at must be timezone-aware ISO-8601")
    _validate_statuses(
        dashboard.get("source_status"), "dashboard", errors, {"pipeline"}
    )
    summaries = dashboard.get("assets")
    if not isinstance(summaries, list):
        errors.append("dashboard: assets must be a list")
        summaries = []
    summary_by_id: dict[str, dict[str, Any]] = {}
    for index, summary in enumerate(summaries):
        if not isinstance(summary, dict) or set(summary) != SUMMARY_FIELDS:
            errors.append(f"dashboard: lightweight summary {index} has invalid fields")
            continue
        asset_id = summary.get("id")
        if not isinstance(asset_id, str) or not SAFE_ASSET_ID.fullmatch(asset_id):
            errors.append(f"dashboard: unsafe summary asset id at index {index}")
            continue
        if asset_id in summary_by_id:
            errors.append(f"dashboard: duplicate asset id {asset_id}")
        summary_by_id[asset_id] = summary
        if summary.get("asset_type") not in ASSET_TYPES:
            errors.append(f"dashboard: invalid asset_type for {asset_id}")
        if summary.get("state") not in STATES:
            errors.append(f"dashboard: invalid state for {asset_id}")
        if not _bounded(summary.get("confidence"), 0, 1):
            errors.append(f"dashboard: invalid confidence for {asset_id}")
        if type(summary.get("stale")) is not bool:
            errors.append(f"dashboard: invalid stale flag for {asset_id}")
    if dashboard.get("asset_count") != len(summaries):
        errors.append("dashboard: asset_count does not match summaries")
    stale_count = sum(
        1 for summary in summaries if isinstance(summary, dict) and summary.get("stale") is True
    )
    if dashboard.get("stale_count") != stale_count:
        errors.append("dashboard: stale_count does not match summaries")

    expected_assets: dict[str, dict[str, Any]] = {}
    for index, asset in enumerate(watchlist["assets"]):
        if not isinstance(asset, dict):
            errors.append(f"watchlist: asset {index} must be an object")
            continue
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not SAFE_ASSET_ID.fullmatch(asset_id):
            errors.append(f"watchlist: unsafe asset id at index {index}")
            continue
        if asset_id in expected_assets:
            errors.append(f"watchlist: duplicate asset id {asset_id}")
        if asset.get("enabled") is not False:
            expected_assets[asset_id] = asset
    expected_ids = set(expected_assets)
    dashboard_ids = set(summary_by_id)
    if dashboard_ids != expected_ids:
        errors.append(
            "watchlist/dashboard parity failed: "
            f"missing={sorted(expected_ids - dashboard_ids)} "
            f"orphan={sorted(dashboard_ids - expected_ids)}"
        )

    assets_dir = data_root / "assets"
    detail_paths = list(assets_dir.glob("*.json")) if assets_dir.is_dir() else []
    detail_ids = {path.stem for path in detail_paths}
    for path in detail_paths:
        if not SAFE_ASSET_ID.fullmatch(path.stem):
            errors.append(f"assets: unsafe detail filename {path.name}")
    for asset_id in sorted(expected_ids - detail_ids):
        errors.append(f"assets: missing detail {asset_id}.json")
    for asset_id in sorted(detail_ids - expected_ids):
        errors.append(f"assets: orphan detail {asset_id}.json")
    for asset_id in sorted(expected_ids & detail_ids):
        detail = _read_json(
            assets_dir / f"{asset_id}.json", f"detail {asset_id}", errors
        )
        if detail is not None:
            _validate_detail(
                detail,
                expected_assets[asset_id],
                summary_by_id.get(asset_id),
                f"detail {asset_id}",
                errors,
            )
    return errors


def _status(
    provider: str,
    source_urls: list[str],
    *,
    stale: bool = False,
    error: str = "",
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "source_urls": source_urls,
        "attempted_at": FIXTURE_TIME,
        "retrieved_at": "" if stale else FIXTURE_TIME,
        "last_success_at": FIXTURE_TIME,
        "stale": stale,
        "error": error,
        "coverage": coverage or {},
    }


def _news(asset_id: str, region: str) -> list[dict[str, Any]]:
    slug = asset_id.replace("-", "/")
    return [
        {
            "title": f"[合成陈旧夹具] {region} 离线趋势示例",
            "article_url": f"https://news.example/{slug}/{region.lower()}",
            "source": "Task 7 deterministic fixture",
            "source_url": f"https://sources.example/{region.lower()}",
            "published_at": FIXTURE_TIME,
            "retrieved_at": FIXTURE_TIME,
            "region": region,
        }
    ]


def _score(
    overall: float | None,
    confidence: float,
    components: dict[str, float],
    *,
    holding_uzi_pct: float | None = None,
    risk_flags: list[str] | None = None,
) -> dict[str, Any]:
    coverage: dict[str, Any] = {
        "available": list(components),
        "missing": [],
        "weight_pct": 100.0 if components else 0.0,
        "confidence_penalties": [],
    }
    if holding_uzi_pct is not None:
        coverage["holding_uzi_pct"] = holding_uzi_pct
    return {
        "overall": overall,
        "components": components,
        "confidence": confidence,
        "model": "task7-deterministic-synthetic-fixture",
        "model_version": "1.0",
        "coverage": coverage,
        "risk_flags": risk_flags or [],
    }


def _recommendation(
    state: str,
    confidence: float,
    *,
    stale: bool = False,
    hard_failures: list[str] | None = None,
    hard_flags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "confidence": confidence,
        "risk": {
            "hard_flags": hard_flags or [],
            "warnings": ["synthetic_stale_fixture"],
            "stale": stale,
            "hard_failures": hard_failures or [],
        },
        "reasons": [
            "FIXTURE ONLY: fixed synthetic/stale evidence for offline QA; not market truth"
        ],
        "invalidation_rules": [
            "Replace this fixture after a verified provider refresh",
            "Never use this synthetic record as investment advice",
        ],
        "timestamp": FIXTURE_TIME,
    }


def _fixture_detail(asset: dict[str, Any]) -> dict[str, Any]:
    asset_id = asset["id"]
    news = {region: _news(asset_id, region) for region in ("CN", "INTL")}
    news_status = {
        f"news_{region}": _status(
            "task7-deterministic-news-fixture",
            [f"https://sources.example/{region.lower()}"],
        )
        for region in ("CN", "INTL")
    }
    fixture_notice = "SYNTHETIC STALE FIXTURE: not live market data"
    if asset_id == "stock-cn-600519-sh":
        market = {
            "fixture_notice": fixture_notice,
            "quality_valuation": 88,
            "trend_momentum": 84,
            "risk_signals": 82,
            "news_events": 80,
        }
        uzi = {
            "overall": 90,
            "version": "fixture-only",
            "fixture_notice": fixture_notice,
        }
        score = _score(
            86.5,
            0.9,
            {
                "uzi_consensus": 90,
                "quality_valuation": 88,
                "trend_momentum": 84,
                "risk_signals": 82,
                "news_events": 80,
            },
        )
        recommendation = _recommendation("优先研究", 0.9)
        statuses = {
            "market": _status(
                "task7-deterministic-market-fixture",
                [f"https://market.example/{asset_id}"],
            ),
            "uzi": _status(
                "task7-deterministic-uzi-fixture",
                [f"https://uzi.example/{asset_id}"],
            ),
            **news_status,
        }
    elif asset_id == "stock-cn-000001-sz":
        market = {"fixture_notice": fixture_notice}
        uzi = {"fixture_notice": fixture_notice}
        score = _score(None, 0.0, {})
        recommendation = _recommendation(
            "暂不纳入",
            0.0,
            stale=True,
            hard_failures=["direct_uzi_unavailable"],
        )
        statuses = {
            "market": _status(
                "task7-deterministic-market-fixture",
                [f"https://market.example/{asset_id}"],
            ),
            "uzi": _status(
                "task7-deterministic-uzi-fixture",
                [f"https://uzi.example/{asset_id}"],
                stale=True,
                error="direct_uzi_unavailable",
            ),
            **news_status,
        }
    else:
        is_risk = asset.get("asset_type") == "etf"
        market = {
            "fixture_notice": fixture_notice,
            "history": [
                {"date": "1999-12-30", "nav": 1.0},
                {"date": "1999-12-31", "nav": 0.8 if is_risk else 1.04},
                {"date": "2000-01-01", "nav": 0.65 if is_risk else 1.1},
            ],
            "holdings": [
                {
                    "code": "600519.SH",
                    "name": "合成持仓 A",
                    "weight_pct": 35,
                    "latest_price": 1,
                    "change_pct": 0,
                },
                {
                    "code": "000001.SZ",
                    "name": "合成持仓 B",
                    "weight_pct": 30,
                    "latest_price": 1,
                    "change_pct": 0,
                },
            ],
            "holding_report_date": "SYNTHETIC-2000-Q1",
            "size_billion": 1,
            "expense_ratio_pct": 0.5,
            "age_years": 1,
            "manager_tenure_years": 1,
        }
        uzi = {}
        if is_risk:
            score = _score(
                32,
                0.78,
                {
                    "risk_adjusted_return": 20,
                    "drawdown_volatility": 18,
                    "trend_recovery": 25,
                    "stability": 45,
                    "concentration": 50,
                    "holding_uzi": 38,
                },
                holding_uzi_pct=65,
                risk_flags=["large_drawdown"],
            )
            recommendation = _recommendation(
                "风险偏高", 0.78, hard_flags=["large_drawdown"]
            )
        else:
            score = _score(
                80,
                0.82,
                {
                    "risk_adjusted_return": 82,
                    "drawdown_volatility": 80,
                    "trend_recovery": 86,
                    "stability": 72,
                    "concentration": 75,
                    "holding_uzi": 84,
                },
                holding_uzi_pct=65,
            )
            recommendation = _recommendation("优先研究", 0.82)
        provider = "task7-deterministic-fund-fixture"
        source_url = f"https://fund.example/{asset_id}"
        statuses = {
            "market": _status(provider, [source_url]),
            "history": _status(provider, [f"{source_url}/history"]),
            "holdings": _status(provider, [f"{source_url}/holdings"]),
            "quotes": _status(
                provider,
                [f"{source_url}/quotes"],
                coverage={"covered": 2, "total": 2, "pct": 100.0},
            ),
            **news_status,
        }
    return {
        "asset": asset,
        "market": market,
        "uzi": uzi,
        "news": news,
        "score": score,
        "recommendation": recommendation,
        "source_status": statuses,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_fixture_data(
    data_root: Path, watchlist: dict[str, Any], *, force_stale: bool = False
) -> None:
    assets = [
        dict(asset)
        for asset in watchlist.get("assets", [])
        if isinstance(asset, dict) and asset.get("enabled") is not False
    ]
    records = {asset["id"]: _fixture_detail(asset) for asset in assets}
    if force_stale:
        for detail in records.values():
            for status in detail["source_status"].values():
                status["stale"] = True
                status["retrieved_at"] = ""
                status["error"] = status["error"] or "synthetic_fixture_stale"
            detail["recommendation"] = _recommendation(
                "等待确认", detail["score"]["confidence"], stale=True
            )
    summaries = [_detail_summary(records[asset["id"]]) for asset in assets]
    stale_count = sum(1 for summary in summaries if summary and summary["stale"])
    pipeline_error = (
        "synthetic_fixture_stale"
        if force_stale
        else "contains_intentional_fixture_error"
        if stale_count
        else ""
    )
    dashboard = {
        "generated_at": FIXTURE_TIME,
        "pipeline_version": "task7-fixture-1.0",
        "source_status": {
            "pipeline": _status(
                "task7-deterministic-fixture-generator",
                ["https://fixture.example/task7"],
                stale=bool(stale_count),
                error=pipeline_error,
            )
        },
        "stale_count": stale_count,
        "asset_count": len(summaries),
        "assets": summaries,
    }
    _write_json(data_root / "watchlist.json", watchlist)
    _write_json(data_root / "dashboard.json", dashboard)
    assets_dir = data_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    expected_names = {f"{asset_id}.json" for asset_id in records}
    for path in assets_dir.glob("*.json"):
        if path.name not in expected_names:
            path.unlink()
    for asset_id, detail in records.items():
        _write_json(assets_dir / f"{asset_id}.json", detail)


def generate_fixture_site(
    site_root: str | Path, watchlist_path: str | Path = DEFAULT_FIXTURE
) -> Path:
    """Create a complete offline-served static site without touching repository data."""
    destination = Path(site_root)
    fixture = Path(watchlist_path)
    watchlist = json.loads(
        fixture.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite constant {value}")
        ),
    )
    destination.mkdir(parents=True, exist_ok=True)
    for relative in ("index.html", "manifest.webmanifest", "robots.txt", "sw.js"):
        shutil.copy2(REPO_ROOT / relative, destination / relative)
    target_assets = destination / "assets"
    target_assets.mkdir(parents=True, exist_ok=True)
    for relative in ("app.js", "core.js", "styles.css", "icon.svg"):
        shutil.copy2(REPO_ROOT / "assets" / relative, target_assets / relative)
    data_root = destination / "data"
    _write_fixture_data(data_root, watchlist)
    return data_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate generated monitor outputs or build an offline fixture site."
    )
    parser.add_argument("root", nargs="?", type=Path)
    parser.add_argument("--generate-demo", type=Path, metavar="SITE_ROOT")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)
    if args.generate_demo:
        data_root = generate_fixture_site(args.generate_demo, args.fixture)
        print(f"DEMO_SITE={args.generate_demo.resolve()}")
    else:
        if args.root is None:
            parser.error("root is required unless --generate-demo is used")
        data_root = args.root
    errors = validate_outputs(data_root)
    if errors:
        for error in errors:
            print(f"OUTPUT_ERROR: {error}")
        return 1
    print("OUTPUTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
