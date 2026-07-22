"""Guarded, stock-only orchestration entrypoint for the pinned UZI package."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .domain import normalize_asset
    from .uzi_adapter import UZI_COMMIT, build_uzi_portfolio, normalize_panel
else:
    from domain import normalize_asset
    from uzi_adapter import UZI_COMMIT, build_uzi_portfolio, normalize_panel


DEFAULT_UZI_ROOT = Path(r"C:\Users\智汇云\Documents\A股选股策略\tools\UZI-Skill")
DEPTHS = ("lite", "medium", "deep")
MEDIUM_BATCH_SIZE = 10
SAFE_ASSET_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,119}$")
CN_HOLDING = re.compile(r"\d{6}\.(?:SH|SZ|BJ)$")


def _configure_utf8() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _run_uzi_portfolio(
    uzi_root: str | Path, portfolio_csv: str | Path, depth: str = "lite"
) -> dict[str, Any]:
    """Load UZI's own timeout guard, preserve its runner API, and return its result."""
    if depth not in DEPTHS:
        raise ValueError(f"unsupported UZI depth: {depth}")
    _configure_utf8()
    os.environ.setdefault("UZI_CLI_ONLY", "1")
    os.environ.setdefault("UZI_NO_AUTO_OPEN", "1")
    os.environ.setdefault("UZI_HTTP_TIMEOUT", "15")

    root = Path(uzi_root).expanduser().resolve()
    scripts_dir = root / "skills" / "deep-analysis" / "scripts"
    if not scripts_dir.is_dir():
        raise FileNotFoundError(f"UZI scripts directory does not exist: {scripts_dir}")
    portfolio = Path(portfolio_csv).expanduser().resolve()
    if not portfolio.is_file():
        raise FileNotFoundError(f"UZI portfolio does not exist: {portfolio}")
    scripts_path = str(scripts_dir)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)

    importlib.import_module("lib.net_timeout_guard")
    profile_module = importlib.import_module("lib.analysis_profile")
    runner_module = importlib.import_module("lib.portfolio_runner")
    profile_module.apply_profile_to_env(profile_module.get_profile(depth))
    result = runner_module.run_portfolio(portfolio, depth=depth, auto_open=False)
    if not isinstance(result, dict):
        raise RuntimeError("UZI portfolio runner returned a non-object result")
    return result


def _load_watchlist(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() != ".json":
        raise ValueError("UZI input must be a canonical watchlist JSON file")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("watchlist must be a version 1 object")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ValueError("watchlist assets must be a list")
    canonical: list[dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("watchlist assets must be objects")
        if asset.get("asset_type") not in {"stock", "fund", "etf", "lof"}:
            raise ValueError("watchlist asset_type is not canonical")
        if not isinstance(asset.get("code"), str) or not asset["code"].strip():
            raise ValueError("watchlist asset requires a code")
        if not isinstance(asset.get("enabled", True), bool):
            raise ValueError("watchlist enabled must be boolean")
        canonical.append(normalize_asset(asset))
    return canonical


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _valid_cache(path: Path, ticker: str) -> bool:
    synthesis = _read_object(path / "synthesis.json")
    panel = _read_object(path / "panel.json")
    if not synthesis or not panel:
        return False
    return "overall" in normalize_panel(
        {"synthesis": synthesis, "panel": panel}, ticker
    )


def _cache_success_at(path: Path) -> str:
    candidates = [path / "synthesis.json", path / "panel.json"]
    mtimes = [candidate.stat().st_mtime for candidate in candidates if candidate.is_file()]
    return datetime.fromtimestamp(max(mtimes), timezone.utc).isoformat() if mtimes else ""


def _holding_assets(assets: list[dict[str, Any]], details_dir: Path) -> list[dict[str, Any]]:
    holdings: list[dict[str, Any]] = []
    for asset in assets:
        if asset.get("enabled", True) is not True or asset.get("asset_type") not in {"fund", "etf", "lof"}:
            continue
        asset_id = str(asset.get("id") or "")
        if not SAFE_ASSET_ID.fullmatch(asset_id):
            raise ValueError("fund watchlist asset requires a safe id")
        detail = _read_object(details_dir / f"{asset_id}.json")
        market = detail.get("market") if isinstance(detail.get("market"), dict) else {}
        disclosed = market.get("holdings") if isinstance(market.get("holdings"), list) else []
        for holding in disclosed:
            if not isinstance(holding, dict):
                continue
            code = str(holding.get("code") or "").strip().upper()
            if not CN_HOLDING.fullmatch(code):
                continue
            holdings.append(
                {
                    "code": code,
                    "name": str(holding.get("name") or code).strip(),
                    "asset_type": "stock",
                    "enabled": True,
                }
            )
    return holdings


def _uzi_universe(assets: list[dict[str, Any]], details_dir: Path) -> list[dict[str, Any]]:
    direct = [
        asset
        for asset in assets
        if asset.get("asset_type") == "stock" and asset.get("enabled", True) is True
    ]
    universe: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in [*direct, *_holding_assets(assets, details_dir)]:
        code = str(asset.get("code") or "").strip().upper()
        if code and code not in seen:
            seen.add(code)
            universe.append(asset)
    return universe


def run_watchlist(
    uzi_root: str | Path,
    watchlist_json: str | Path,
    depth: str = "lite",
    *,
    details_dir: str | Path = "data/assets",
    run_id: str = "local",
) -> dict[str, Any]:
    """Refresh the direct-stock plus disclosed-holding universe with cache isolation."""
    if depth not in DEPTHS:
        raise ValueError(f"unsupported UZI depth: {depth}")
    assets = _load_watchlist(watchlist_json)
    universe = _uzi_universe(assets, Path(details_dir))
    attempted_at = datetime.now(timezone.utc).isoformat()
    base_manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "skipped_no_targets" if not universe else "failed",
        "attempted_at": attempted_at,
        "depth": depth,
        "run_id": str(run_id),
        "upstream": {
            "repository": "wbh604/UZI-Skill",
            "commit": UZI_COMMIT,
        },
        "tickers": {},
        "batches": [],
    }
    if not universe:
        return base_manifest
    root = Path(uzi_root).expanduser().resolve()
    cache_root = root / "skills" / "deep-analysis" / "scripts" / ".cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uzi-stock-portfolio-") as temp_dir:
        temporary = Path(temp_dir)
        backups = temporary / "restored-cache"
        checkpoints = temporary / "refreshed-cache"
        prior_success: dict[str, str] = {}
        tickers = [str(asset["code"]).strip().upper() for asset in universe]
        for ticker in tickers:
            target = cache_root / ticker
            if target.is_dir():
                prior_success[ticker] = _cache_success_at(target)
                backups.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backups / ticker))
        entries: dict[str, dict[str, Any]] = {}
        refreshed = 0
        fallback = 0
        batch_size = MEDIUM_BATCH_SIZE if depth == "medium" else len(universe)
        chunks = [
            universe[index : index + batch_size]
            for index in range(0, len(universe), batch_size)
        ]
        try:
            for batch_index, batch_assets in enumerate(chunks, start=1):
                portfolio = build_uzi_portfolio(
                    batch_assets, temporary / f"stocks-{batch_index:03d}.csv"
                )
                batch_tickers = [
                    str(asset["code"]).strip().upper() for asset in batch_assets
                ]
                runner_error = ""
                try:
                    _run_uzi_portfolio(root, portfolio, depth)
                except (OSError, RuntimeError, ValueError) as error:
                    runner_error = str(error)

                batch_refreshed = 0
                batch_fallback = 0
                for ticker in batch_tickers:
                    current = cache_root / ticker
                    backup = backups / ticker
                    checkpoint = checkpoints / ticker
                    common = {
                        "ticker": ticker,
                        "attempted_at": attempted_at,
                        "run_id": str(run_id),
                        "upstream_commit": UZI_COMMIT,
                    }
                    if current.is_dir() and _valid_cache(current, ticker):
                        checkpoints.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(current), str(checkpoint))
                        refreshed += 1
                        batch_refreshed += 1
                        entries[ticker] = {
                            **common,
                            "status": "refreshed_this_run",
                            "last_success_at": attempted_at,
                            "stale": False,
                            "error": "",
                        }
                    elif backup.is_dir() and _valid_cache(backup, ticker):
                        fallback += 1
                        batch_fallback += 1
                        if current.exists():
                            shutil.rmtree(current)
                        entries[ticker] = {
                            **common,
                            "status": "restored_fallback",
                            "last_success_at": prior_success.get(ticker, ""),
                            "stale": True,
                            "error": "current_run_output_missing_or_invalid",
                        }
                    else:
                        if current.exists():
                            shutil.rmtree(current)
                        entries[ticker] = {
                            **common,
                            "status": "failed",
                            "last_success_at": "",
                            "stale": True,
                            "error": "current_run_output_missing_or_invalid",
                        }
                if runner_error and not batch_refreshed:
                    batch_status = "failed"
                elif batch_refreshed == len(batch_tickers):
                    batch_status = "completed"
                elif batch_refreshed or batch_fallback:
                    batch_status = "partial"
                else:
                    batch_status = "failed"
                base_manifest["batches"].append(
                    {
                        "index": batch_index,
                        "tickers": batch_tickers,
                        "status": batch_status,
                        "error": runner_error,
                    }
                )
        finally:
            for ticker in tickers:
                current = cache_root / ticker
                checkpoint = checkpoints / ticker
                backup = backups / ticker
                if current.exists():
                    shutil.rmtree(current)
                if checkpoint.is_dir() and _valid_cache(checkpoint, ticker):
                    shutil.move(str(checkpoint), str(current))
                elif backup.is_dir() and _valid_cache(backup, ticker):
                    shutil.move(str(backup), str(current))
        base_manifest["tickers"] = entries
        if refreshed == len(tickers):
            base_manifest["status"] = "completed"
        elif refreshed or fallback:
            base_manifest["status"] = "partial"
        else:
            base_manifest["status"] = "failed"
        return base_manifest


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("watchlist_json", type=Path)
    parser.add_argument(
        "--uzi-root",
        type=Path,
        default=Path(os.environ.get("UZI_ROOT", DEFAULT_UZI_ROOT)),
    )
    parser.add_argument("--depth", choices=DEPTHS, default="lite")
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--details-dir", type=Path, default=Path("data/assets"))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    args = parser.parse_args(argv)
    try:
        result = run_watchlist(
            args.uzi_root,
            args.watchlist_json,
            args.depth,
            details_dir=args.details_dir,
            run_id=args.run_id,
        )
    except (OSError, RuntimeError, ValueError) as error:
        if args.result_file:
            _write_result(
                args.result_file,
                {"status": "error", "error": str(error), "failed": []},
            )
        print(str(error), file=sys.stderr)
        return 2
    if args.result_file:
        _write_result(args.result_file, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"completed", "partial", "skipped_no_targets"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
