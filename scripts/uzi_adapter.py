"""Stock-only UZI portfolio creation and publication-safe cache normalization."""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

if __package__:
    from .domain import validate_stock_security_code
else:
    from domain import validate_stock_security_code


UZI_MODEL = "UZI-Skill"
UZI_VERSION = "3.9.2"
UZI_COMMIT = "fce996c33e70eddce8e375f53cd252b549eb3d7c"


def _score_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 0.0 <= number <= 100.0:
        return None
    return number


def _signal_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def build_uzi_portfolio(assets: Iterable[Mapping[str, Any]], path: str | Path) -> Path:
    """Write an equal-weight UZI batch containing enabled stocks and no fund entities."""
    stocks = [
        asset
        for asset in assets
        if asset.get("asset_type") == "stock" and asset.get("enabled", True) is True
    ]
    if not stocks:
        raise ValueError("UZI portfolio requires at least one enabled stock")

    rows: list[dict[str, Any]] = []
    weight = round(1.0 / len(stocks), 10)
    for asset in stocks:
        ticker = validate_stock_security_code(str(asset.get("code") or ""))
        if not ticker:
            raise ValueError("stock asset requires a code")
        rows.append(
            {
                "ticker": ticker,
                "weight": weight,
                "note": str(asset.get("name") or asset.get("note") or "").strip(),
            }
        )

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "weight", "note"])
        writer.writeheader()
        writer.writerows(rows)
    return output


def _bundle_parts(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    synthesis = payload.get("synthesis")
    panel = payload.get("panel")
    return (
        synthesis if isinstance(synthesis, Mapping) else payload,
        panel if isinstance(panel, Mapping) else payload,
    )


def normalize_panel(payload: Mapping[str, Any], ticker: str) -> dict[str, Any]:
    """Extract only verified UZI values, omitting unavailable fields instead of filling them."""
    if not isinstance(payload, Mapping):
        raise ValueError("UZI result must be an object")
    synthesis, panel = _bundle_parts(payload)
    normalized: dict[str, Any] = {
        "ticker": str(ticker).strip().upper(),
        "model": UZI_MODEL,
        "model_version": UZI_VERSION,
        "upstream_commit": UZI_COMMIT,
    }

    primary_overall = synthesis.get("overall_score")
    overall = _score_number(primary_overall)
    if primary_overall in (None, ""):
        overall = _score_number(synthesis.get("overall"))
    if overall is not None:
        normalized["overall"] = overall

    consensus = _score_number(panel.get("panel_consensus"))
    if consensus is not None:
        normalized["panel_consensus"] = consensus

    fundamental = _score_number(synthesis.get("fundamental_score"))
    if fundamental is not None:
        normalized["fundamental_score"] = fundamental

    school_source = panel.get("school_scores") or synthesis.get("school_scores")
    schools: dict[str, float] = {}
    if isinstance(school_source, Mapping):
        for group, value in school_source.items():
            if isinstance(value, Mapping):
                primary_score = value.get("consensus")
                score = _score_number(primary_score)
                if primary_score in (None, ""):
                    score = _score_number(value.get("avg_score"))
            else:
                score = _score_number(value)
            if score is not None:
                schools[str(group)] = score
    if schools:
        normalized["school_scores"] = schools

    signals = panel.get("signal_distribution")
    if isinstance(signals, Mapping):
        safe_signals = {}
        for key, value in signals.items():
            count = _signal_count(value)
            if isinstance(key, str) and count is not None:
                safe_signals[key] = count
        if safe_signals:
            normalized["signal_distribution"] = safe_signals

    verdict = synthesis.get("verdict_label") or synthesis.get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        normalized["verdict"] = verdict.strip()

    risks = synthesis.get("risk_flags") or synthesis.get("risks")
    if isinstance(risks, list):
        safe_risks = [str(risk).strip() for risk in risks if str(risk).strip()]
        if safe_risks:
            normalized["risk_flags"] = safe_risks
    return normalized


def _read_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_uzi_cache(
    cache_dir: str | Path, output_dir: str | Path
) -> dict[str, dict[str, Any]]:
    """Normalize UZI ticker cache directories and write deterministic public JSON files."""
    source = Path(cache_dir)
    destination = Path(output_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"UZI cache directory does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict[str, Any]] = {}
    for ticker_dir in sorted((path for path in source.iterdir() if path.is_dir()), key=lambda p: p.name):
        synthesis = _read_object(ticker_dir / "synthesis.json")
        panel = _read_object(ticker_dir / "panel.json")
        if not synthesis and not panel:
            continue
        ticker = ticker_dir.name.upper()
        result = normalize_panel({"synthesis": synthesis, "panel": panel}, ticker)
        results[ticker] = result
        (destination / f"{ticker}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return results


def publish_uzi_manifest(
    cache_dir: str | Path,
    manifest: Mapping[str, Any],
    output_dir: str | Path,
) -> dict[str, dict[str, Any]]:
    """Atomically rebuild public UZI JSON from one current-run manifest universe."""
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise ValueError("UZI manifest must be a version 1 object")
    entries = manifest.get("tickers")
    if not isinstance(entries, Mapping):
        raise ValueError("UZI manifest tickers must be an object")
    source = Path(cache_dir)
    destination = Path(output_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    published: dict[str, dict[str, Any]] = {}
    upstream = manifest.get("upstream") if isinstance(manifest.get("upstream"), Mapping) else {}
    try:
        for raw_ticker, raw_entry in sorted(entries.items(), key=lambda item: str(item[0])):
            ticker = str(raw_ticker).strip().upper()
            if not ticker or Path(ticker).name != ticker or not all(
                character.isalnum() or character in ".-" for character in ticker
            ):
                raise ValueError("UZI manifest contains an unsafe ticker")
            if not isinstance(raw_entry, Mapping):
                raise ValueError("UZI manifest ticker entry must be an object")
            status = str(raw_entry.get("status") or "")
            if status not in {"refreshed_this_run", "restored_fallback", "failed"}:
                raise ValueError("UZI manifest ticker status is invalid")
            record: dict[str, Any] = {
                "ticker": ticker,
                "model": UZI_MODEL,
                "model_version": UZI_VERSION,
                "upstream_commit": str(upstream.get("commit") or UZI_COMMIT),
            }
            if status != "failed":
                ticker_dir = source / ticker
                synthesis = _read_object(ticker_dir / "synthesis.json")
                panel = _read_object(ticker_dir / "panel.json")
                if synthesis and panel:
                    record.update(
                        normalize_panel(
                            {"synthesis": synthesis, "panel": panel}, ticker
                        )
                    )
            record.update(
                {
                    "attempted_at": str(
                        raw_entry.get("attempted_at") or manifest.get("attempted_at") or ""
                    ),
                    "last_success_at": str(raw_entry.get("last_success_at") or ""),
                    "stale": status != "refreshed_this_run",
                    "error": ""
                    if status == "refreshed_this_run"
                    else str(
                        raw_entry.get("error")
                        or "current_run_output_missing_or_invalid"
                    ),
                    "run": {
                        "id": str(raw_entry.get("run_id") or manifest.get("run_id") or ""),
                        "depth": str(manifest.get("depth") or ""),
                        "status": status,
                    },
                    "upstream": {
                        "repository": str(upstream.get("repository") or "wbh604/UZI-Skill"),
                        "commit": str(upstream.get("commit") or UZI_COMMIT),
                        "model_version": UZI_VERSION,
                    },
                }
            )
            published[ticker] = record
            (temporary / f"{ticker}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )

        backup = destination.with_name(f".{destination.name}-previous")
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(temporary, destination)
        except Exception:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return published
