import csv
import json
from pathlib import Path

from scripts.uzi_adapter import build_uzi_portfolio, normalize_panel, normalize_uzi_cache


FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_uzi_panel_normalizes_without_inventing_missing_scores():
    result = normalize_panel(load_fixture("uzi_panel.json"), "600519.SH")

    assert result["overall"] == 68.4
    assert result["school_scores"]["D"] == 61.0
    assert "missing" not in json.dumps(result)


def test_uzi_portfolio_contains_stocks_only(tmp_path):
    path = tmp_path / "portfolio.csv"
    assets = [
        {"code": "600519.SH", "name": "贵州茅台", "asset_type": "stock", "enabled": True},
        {"code": "025209", "name": "半导体基金", "asset_type": "fund", "enabled": True},
        {"code": "510300", "name": "沪深300ETF", "asset_type": "etf", "enabled": True},
        {"code": "161725", "name": "白酒LOF", "asset_type": "lof", "enabled": True},
        {"code": "AAPL", "name": "Apple", "asset_type": "stock", "enabled": False},
    ]

    result_path = build_uzi_portfolio(assets, path)

    assert result_path == path
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [{"ticker": "600519.SH", "weight": "1.0", "note": "贵州茅台"}]


def test_normalize_uzi_cache_writes_publication_safe_results(tmp_path):
    fixture = load_fixture("uzi_panel.json")
    ticker_cache = tmp_path / "cache" / "600519.SH"
    ticker_cache.mkdir(parents=True)
    (ticker_cache / "synthesis.json").write_text(
        json.dumps(fixture["synthesis"], ensure_ascii=False), encoding="utf-8"
    )
    (ticker_cache / "panel.json").write_text(
        json.dumps(fixture["panel"], ensure_ascii=False), encoding="utf-8"
    )

    results = normalize_uzi_cache(tmp_path / "cache", tmp_path / "public")

    assert results["600519.SH"]["overall"] == 68.4
    assert json.loads((tmp_path / "public" / "600519.SH.json").read_text(encoding="utf-8")) == results[
        "600519.SH"
    ]
