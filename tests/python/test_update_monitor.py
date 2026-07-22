import json
from pathlib import Path


NOW = "2026-07-22T00:00:00+00:00"
FUND = {
    "id": "fund-cn-025209",
    "code": "025209",
    "name": "测试基金",
    "asset_type": "fund",
    "market": "CN",
    "enabled": True,
}


def previous_detail():
    return {
        "asset": FUND,
        "market": {"history": [{"date": "2026-07-20", "nav": 1.23}]},
        "news": [{"title": "last good", "article_url": "https://example.test/article"}],
        "score": {"overall": 61, "confidence": 0.7, "components": {}},
        "recommendation": {"state": "持续观察"},
        "source_status": {"market": {"stale": False}},
    }


class FailedProvider:
    def fetch_fund(self, asset):
        raise RuntimeError("fixture refresh failed")


def test_failed_refresh_preserves_previous_market_and_marks_stale():
    from scripts.update_monitor import run_pipeline

    result = run_pipeline(
        {"assets": [FUND]},
        {"assets": {FUND["id"]: previous_detail()}},
        {
            "now": NOW,
            "fund_provider": FailedProvider(),
            "news_provider": lambda asset, region: [],
            "holding_uzi": {},
            "write": False,
        },
    )

    detail = result["assets"][FUND["id"]]
    assert detail["market"] == previous_detail()["market"]
    assert detail["market"] != {}
    assert detail["source_status"]["market"]["stale"] is True
    assert "fixture refresh failed" in detail["source_status"]["market"]["error"]
    assert result["dashboard"]["stale_count"] == 1


def test_stock_without_direct_uzi_has_explicit_failure():
    from scripts.update_monitor import run_pipeline

    stock = {
        "id": "stock-cn-600519-sh",
        "code": "600519.SH",
        "name": "测试股票",
        "asset_type": "stock",
        "market": "CN",
        "enabled": True,
    }
    result = run_pipeline(
        {"assets": [stock]},
        {},
        {
            "now": NOW,
            "market_data": {stock["id"]: {"quality_valuation": 70}},
            "uzi": {},
            "news_provider": lambda asset, region: [],
            "write": False,
        },
    )

    detail = result["assets"][stock["id"]]
    assert detail["source_status"]["uzi"]["stale"] is True
    assert detail["source_status"]["uzi"]["error"] == "direct_uzi_unavailable"
    assert detail["recommendation"]["state"] == "暂不纳入"


def test_pipeline_writes_valid_versioned_outputs_atomically(tmp_path):
    from scripts.update_monitor import run_pipeline

    previous = previous_detail()
    result = run_pipeline(
        {"assets": [FUND]},
        {"assets": {FUND["id"]: previous}},
        {
            "now": NOW,
            "fund_provider": FailedProvider(),
            "news_provider": lambda asset, region: [],
            "holding_uzi": {},
            "output_dir": tmp_path,
            "write": True,
        },
    )

    dashboard_path = tmp_path / "dashboard.json"
    detail_path = tmp_path / "assets" / f"{FUND['id']}.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    detail = json.loads(detail_path.read_text(encoding="utf-8"))

    assert dashboard["pipeline_version"] == result["dashboard"]["pipeline_version"]
    assert dashboard["asset_count"] == 1
    assert dashboard["assets"] == [result["dashboard"]["assets"][0]]
    assert detail["asset"]["id"] == FUND["id"]
    assert not list(tmp_path.rglob("*.tmp"))
