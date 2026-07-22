"""Secure parsers for pasted, CSV, and Issue-based watchlist imports."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__:
    from .domain import normalize_asset
else:
    from domain import normalize_asset


MAX_IMPORT = 50
MAX_INPUT_BYTES = 20_000
ISSUE_RE = re.compile(r"<!--\s*WATCHLIST_IMPORT_V1\s*(\{.*?\})\s*-->", re.S)


def _bounded_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("import payload must be text")
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError(f"import payload exceeds {MAX_INPUT_BYTES} bytes")
    return text


def _ensure_import_limit(rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise ValueError("import contains no assets")
    if len(rows) > MAX_IMPORT:
        raise ValueError(f"an import may contain at most {MAX_IMPORT} assets")


def _normalize_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    _ensure_import_limit(rows)
    return [normalize_asset(row) for row in rows]


def _parse_text_rows(text: str) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for values in csv.reader(io.StringIO(text)):
        if not values or not any(value.strip() for value in values):
            continue
        if len(values) > 5:
            raise ValueError("text rows may contain at most five columns")
        padded = values + [""] * (5 - len(values))
        rows.append(
            {
                "code": padded[0],
                "name": padded[1],
                "asset_type": padded[2],
                "sector": padded[3],
                "note": padded[4],
            }
        )
    return rows


def _parse_csv_rows(text: str) -> list[Mapping[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV import requires a header row")
    return [row for row in reader if any((value or "").strip() for value in row.values())]


def parse_rows(text: str, format_hint: str) -> list[dict[str, Any]]:
    """Parse at most fifty pasted or CSV assets into their normalized schema."""
    text = _bounded_text(text)
    hint = str(format_hint).strip().lower()
    if hint == "text":
        return _normalize_rows(_parse_text_rows(text))
    if hint == "csv":
        return _normalize_rows(_parse_csv_rows(text))
    raise ValueError(f"unsupported import format: {format_hint}")


def parse_issue_body(body: str) -> list[dict[str, Any]]:
    """Extract the only accepted machine-readable Issue payload."""
    match = ISSUE_RE.search(_bounded_text(body))
    if not match:
        raise ValueError("Issue body must contain a WATCHLIST_IMPORT_V1 block")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError("WATCHLIST_IMPORT_V1 block contains invalid JSON") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("assets"), list):
        raise ValueError("WATCHLIST_IMPORT_V1 block must contain an assets array")
    assets = payload["assets"]
    if not all(isinstance(asset, Mapping) for asset in assets):
        raise ValueError("WATCHLIST_IMPORT_V1 assets must be objects")
    return _normalize_rows(assets)


def merge_watchlist(current: Mapping[str, Any], incoming: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge normalized imports by stable ID while preserving watchlist order."""
    if not isinstance(current, Mapping):
        raise ValueError("watchlist must be an object")
    version = current.get("version", 1)
    if version != 1:
        raise ValueError(f"unsupported watchlist version: {version}")
    existing = current.get("assets", [])
    if not isinstance(existing, list) or not all(isinstance(asset, Mapping) for asset in existing):
        raise ValueError("watchlist assets must be an array of objects")

    merged = [normalize_asset(asset) for asset in existing]
    positions = {asset["id"]: index for index, asset in enumerate(merged)}
    seen_incoming: set[str] = set()
    for raw_asset in incoming:
        asset = normalize_asset(raw_asset)
        if asset["id"] in seen_incoming:
            continue
        seen_incoming.add(asset["id"])
        position = positions.get(asset["id"])
        if position is None:
            positions[asset["id"]] = len(merged)
            merged.append(asset)
        else:
            merged[position] = asset

    result: dict[str, Any] = {"version": 1, "assets": merged}
    if "updated_at" in current:
        result["updated_at"] = current["updated_at"]
    return result


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def import_issue_file(
    issue_body_file: str | Path,
    watchlist_file: str | Path,
    summary_file: str | Path,
) -> dict[str, Any]:
    """Parse one Issue body and atomically merge it into the canonical watchlist."""
    body_path = Path(issue_body_file)
    watchlist_path = Path(watchlist_file)
    summary_path = Path(summary_file)
    body = body_path.read_text(encoding="utf-8")
    current = json.loads(watchlist_path.read_text(encoding="utf-8"))
    if not isinstance(current, Mapping):
        raise ValueError("watchlist must be an object")
    incoming = parse_issue_body(body)
    existing = current.get("assets", [])
    if not isinstance(existing, list):
        raise ValueError("watchlist assets must be an array")
    existing_ids = {
        normalize_asset(asset)["id"]
        for asset in existing
        if isinstance(asset, Mapping)
    }
    incoming_ids = {asset["id"] for asset in incoming}
    merged = merge_watchlist(current, incoming)
    summary = {
        "status": "success",
        "imported_count": len(incoming_ids),
        "added_count": len(incoming_ids - existing_ids),
        "updated_count": len(incoming_ids & existing_ids),
        "total_count": len(merged["assets"]),
    }
    _atomic_json(watchlist_path, merged)
    _atomic_json(summary_path, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-body-file", type=Path, required=True)
    parser.add_argument("--watchlist", type=Path, default=Path("data/watchlist.json"))
    parser.add_argument("--summary-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = import_issue_file(
            args.issue_body_file,
            args.watchlist,
            args.summary_file,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
