"""Guarded, stock-only orchestration entrypoint for the pinned UZI package."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__:
    from .uzi_adapter import build_uzi_portfolio
else:
    from uzi_adapter import build_uzi_portfolio


DEFAULT_UZI_ROOT = Path(r"C:\Users\智汇云\Documents\A股选股策略\tools\UZI-Skill")
DEPTHS = ("lite", "medium", "deep")


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
        canonical.append(asset)
    return canonical


def run_watchlist(
    uzi_root: str | Path, watchlist_json: str | Path, depth: str = "lite"
) -> dict[str, Any]:
    """Build a temporary stock-only portfolio from canonical typed watchlist assets."""
    assets = _load_watchlist(watchlist_json)
    with tempfile.TemporaryDirectory(prefix="uzi-stock-portfolio-") as temp_dir:
        portfolio = build_uzi_portfolio(assets, Path(temp_dir) / "stocks.csv")
        return _run_uzi_portfolio(uzi_root, portfolio, depth)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("watchlist_json", type=Path)
    parser.add_argument(
        "--uzi-root",
        type=Path,
        default=Path(os.environ.get("UZI_ROOT", DEFAULT_UZI_ROOT)),
    )
    parser.add_argument("--depth", choices=DEPTHS, default="lite")
    args = parser.parse_args(argv)
    try:
        result = run_watchlist(args.uzi_root, args.watchlist_json, args.depth)
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
