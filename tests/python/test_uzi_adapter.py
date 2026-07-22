import csv
import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("code", ["025209", "025209.SZ", "510300.SH", "161725.SZ"])
def test_uzi_portfolio_rejects_codes_outside_cn_equity_namespaces(tmp_path, code):
    with pytest.raises(ValueError, match="CN equity"):
        build_uzi_portfolio(
            [{"code": code, "name": "mislabeled", "asset_type": "stock", "enabled": True}],
            tmp_path / "portfolio.csv",
        )


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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 100.1])
def test_normalize_panel_rejects_nonfinite_or_out_of_range_scores(value):
    result = normalize_panel(
        {
            "synthesis": {"overall_score": value},
            "panel": {"panel_consensus": value, "school_scores": {"D": {"consensus": value}}},
        },
        "600519.SH",
    )

    assert "overall" not in result
    assert "panel_consensus" not in result
    assert "school_scores" not in result
    json.dumps(result, allow_nan=False)


def test_normalize_panel_accepts_only_nonnegative_integral_signal_counts():
    result = normalize_panel(
        {
            "synthesis": {"overall_score": 68.4},
            "panel": {
                "signal_distribution": {
                    "bullish": 2.0,
                    "neutral": 1.9,
                    "bearish": -1,
                    "skip": float("nan"),
                }
            },
        },
        "600519.SH",
    )

    assert result["signal_distribution"] == {"bullish": 2}
    assert all(
        isinstance(value, int) and value >= 0
        for value in result["signal_distribution"].values()
    )


def test_invalid_primary_uzi_scores_do_not_fall_back_to_other_fields():
    result = normalize_panel(
        {
            "synthesis": {"overall_score": float("nan"), "overall": 88},
            "panel": {
                "school_scores": {
                    "D": {"consensus": 101, "avg_score": 61},
                }
            },
        },
        "600519.SH",
    )

    assert "overall" not in result
    assert "school_scores" not in result


def test_weighted_holding_uzi_rejects_invalid_scores_without_coverage():
    from scripts.scoring import weighted_holding_uzi

    result = weighted_holding_uzi(
        [{"code": "600001.SH", "weight_pct": 50}],
        {"600001.SH": {"overall": float("nan"), "overall_score": 80}},
    )

    assert result["score"] is None
    assert result["coverage_pct"] == 0.0


def test_manifest_publication_is_atomic_current_universe_only_and_truthful(tmp_path):
    from scripts.uzi_adapter import publish_uzi_manifest

    cache = tmp_path / "cache"
    for ticker, score in (("600519.SH", 78), ("000001.SZ", 66)):
        target = cache / ticker
        target.mkdir(parents=True)
        (target / "synthesis.json").write_text(
            json.dumps({"overall_score": score}), encoding="utf-8"
        )
        (target / "panel.json").write_text(
            json.dumps({"panel_consensus": score}), encoding="utf-8"
        )
    public = tmp_path / "public"
    public.mkdir()
    (public / "ORPHAN.json").write_text('{"overall":99}', encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "status": "partial",
        "attempted_at": "2026-07-22T08:00:00+00:00",
        "depth": "lite",
        "run_id": "run-42",
        "upstream": {"repository": "wbh604/UZI-Skill", "commit": "fce996c33e70eddce8e375f53cd252b549eb3d7c"},
        "tickers": {
            "600519.SH": {"status": "refreshed_this_run", "attempted_at": "2026-07-22T08:00:00+00:00", "last_success_at": "2026-07-22T08:00:00+00:00", "stale": False, "error": "", "run_id": "run-42"},
            "000001.SZ": {"status": "restored_fallback", "attempted_at": "2026-07-22T08:00:00+00:00", "last_success_at": "2026-07-20T08:00:00+00:00", "stale": True, "error": "current_run_output_missing_or_invalid", "run_id": "run-42"},
            "300750.SZ": {"status": "failed", "attempted_at": "2026-07-22T08:00:00+00:00", "last_success_at": "", "stale": True, "error": "current_run_output_missing_or_invalid", "run_id": "run-42"},
        },
    }

    published = publish_uzi_manifest(cache, manifest, public)

    assert set(published) == {"600519.SH", "000001.SZ", "300750.SZ"}
    assert {path.stem for path in public.glob("*.json")} == set(published)
    assert published["600519.SH"]["overall"] == 78
    assert published["600519.SH"]["stale"] is False
    assert published["000001.SZ"]["overall"] == 66
    assert published["000001.SZ"]["stale"] is True
    assert published["000001.SZ"]["error"] == "current_run_output_missing_or_invalid"
    assert "overall" not in published["300750.SZ"]
    assert published["300750.SZ"]["run"] == {
        "id": "run-42",
        "depth": "lite",
        "status": "failed",
    }
    assert published["600519.SH"]["upstream"]["commit"] == manifest["upstream"]["commit"]
    assert not list(tmp_path.glob(".public-*"))
