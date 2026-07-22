import json
from pathlib import Path

import pytest


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


def test_pipeline_calls_and_stores_both_news_regions_and_serializes_items(tmp_path):
    from scripts.providers.news import NewsItem
    from scripts.update_monitor import run_pipeline

    calls = []
    def news(asset, region):
        calls.append(region)
        return [NewsItem("traceable", f"https://example.test/{region}", "source", "https://source.test", NOW, NOW, region)]

    result = run_pipeline(
        {"assets": [FUND]},
        {},
        {"now": NOW, "news_provider": news, "write": True, "output_dir": tmp_path},
    )
    detail = result["assets"][FUND["id"]]
    assert calls == ["CN", "INTL"]
    assert set(detail["news"]) == {"CN", "INTL"}
    assert detail["news"]["CN"][0]["article_url"] == "https://example.test/CN"
    assert json.loads((tmp_path / "assets" / f"{FUND['id']}.json").read_text(encoding="utf-8"))["news"]["INTL"]


def test_failed_stock_uzi_retains_previous_evidence_but_excludes_asset():
    from scripts.update_monitor import run_pipeline

    stock = {"id": "stock-cn-600519-sh", "code": "600519.SH", "name": "测试股票", "asset_type": "stock", "market": "CN", "enabled": True}
    previous = {"assets": {stock["id"]: {"asset": stock, "market": {"quality_valuation": 70}, "uzi": {"overall": 80}, "news": {"CN": [], "INTL": []}, "score": {"overall": 80, "confidence": .8}, "recommendation": {"state": "优先研究"}, "source_status": {}}}}
    detail = run_pipeline({"assets": [stock]}, previous, {"now": NOW, "market_data": {stock["id"]: {"quality_valuation": 70}}, "uzi": {}, "news_provider": lambda *_: []})["assets"][stock["id"]]
    assert detail["uzi"] == {"overall": 80}
    assert detail["source_status"]["uzi"]["stale"] is True
    assert detail["recommendation"]["state"] == "暂不纳入"


def test_pipeline_generates_timezone_timestamp_and_rejects_unsafe_asset_id():
    from scripts.update_monitor import run_pipeline

    result = run_pipeline({"assets": [FUND]}, {}, {"news_provider": lambda *_: []})
    assert result["dashboard"]["generated_at"].endswith("+00:00")
    unsafe = {**FUND, "id": "../unsafe"}
    with pytest.raises(ValueError, match="safe"):
        run_pipeline({"assets": [unsafe]}, {}, {"news_provider": lambda *_: []})


def test_partial_fund_merge_keeps_prior_holdings_and_component_provenance():
    from scripts.providers.eastmoney import ProviderResult
    from scripts.update_monitor import run_pipeline

    class PartialProvider:
        def fetch_fund(self, asset):
            return ProviderResult(data={"asset": asset, "history": [{"date": "2026-07-21", "nav": 1.5}]}, source_urls=["https://provider.test/history"], retrieved_at=NOW, errors={"holdings": "holdings unavailable"})

    before = previous_detail()
    before["market"]["holdings"] = [{"code": "600519.SH", "weight_pct": 20}]
    result = run_pipeline({"assets": [FUND]}, {"assets": {FUND["id"]: before}}, {"now": NOW, "fund_provider": PartialProvider(), "holding_uzi": {}, "news_provider": lambda *_: []})
    detail = result["assets"][FUND["id"]]
    assert detail["market"]["holdings"] == before["market"]["holdings"]
    assert detail["source_status"]["market"]["source_urls"] == ["https://provider.test/history"]
    assert detail["source_status"]["market"]["attempted_at"] == NOW


def test_checked_in_dashboard_satisfies_lightweight_schema_and_nan_is_rejected():
    from scripts.update_monitor import _validate_dashboard, run_pipeline

    dashboard = json.loads((Path(__file__).parents[2] / "data" / "dashboard.json").read_text(encoding="utf-8"))
    _validate_dashboard(dashboard)
    stock = {"id": "stock-cn-600519-sh", "code": "600519.SH", "name": "测试股票", "asset_type": "stock", "market": "CN", "enabled": True}
    with pytest.raises(ValueError, match="finite"):
        run_pipeline({"assets": [stock]}, {}, {"now": NOW, "market_data": {stock["id"]: {"quality_valuation": float("nan")}}, "uzi": {stock["id"]: {"overall": 80}}, "news_provider": lambda *_: []})


def test_quote_failure_preserves_prior_quote_fields_but_accepts_disclosure():
    from scripts.providers.eastmoney import ProviderResult
    from scripts.update_monitor import run_pipeline

    class QuoteFailure:
        def fetch_fund(self, asset):
            return ProviderResult(data={"asset": asset, "holdings": [{"code": "600519", "name": "new disclosure", "weight_pct": 11, "latest_price": None, "change_pct": None}], "holding_report_date": "2026 Q3"}, source_urls=["https://provider.test/holdings"], retrieved_at=NOW, errors={"quotes": "quote refresh failed"})

    before = previous_detail()
    before["market"]["holdings"] = [{"code": "600519.SH", "name": "prior quote name", "weight_pct": 10, "latest_price": 100, "change_pct": 1}]
    detail = run_pipeline({"assets": [FUND]}, {"assets": {FUND["id"]: before}}, {"now": NOW, "fund_provider": QuoteFailure(), "news_provider": lambda *_: [], "holding_uzi": {}})["assets"][FUND["id"]]
    holding = detail["market"]["holdings"][0]
    assert holding["weight_pct"] == 11
    assert holding["name"] == "prior quote name"
    assert holding["latest_price"] == 100
    assert holding["change_pct"] == 1
    assert detail["market"]["holding_report_date"] == "2026 Q3"
    assert detail["source_status"]["quotes"]["stale"] is True
    assert detail["source_status"]["holdings"]["last_success_at"] == NOW


def test_total_provider_failure_retains_component_last_success_and_mixed_statuses():
    from scripts.update_monitor import run_pipeline

    before = previous_detail()
    before["source_status"] = {
        "market": {"last_success_at": "2026-07-20T00:00:00+00:00"},
        "history": {"last_success_at": "2026-07-19T00:00:00+00:00"},
        "holdings": {"last_success_at": "2026-07-18T00:00:00+00:00"},
        "quotes": {"last_success_at": "2026-07-17T00:00:00+00:00"},
    }
    detail = run_pipeline({"assets": [FUND]}, {"assets": {FUND["id"]: before}}, {"now": NOW, "fund_provider": FailedProvider(), "news_provider": lambda *_: [], "holding_uzi": {}})["assets"][FUND["id"]]
    assert detail["source_status"]["market"]["last_success_at"] == "2026-07-20T00:00:00+00:00"
    assert detail["source_status"]["history"]["last_success_at"] == "2026-07-19T00:00:00+00:00"
    assert detail["source_status"]["history"]["attempted_at"] == NOW
    assert detail["source_status"]["holdings"]["stale"] is True


@pytest.mark.parametrize("status", [
    {"provider": "p", "source_urls": ["ftp://bad.test"], "attempted_at": NOW, "retrieved_at": "", "last_success_at": "", "stale": True, "error": "x"},
    {"provider": "p", "source_urls": [], "attempted_at": NOW, "retrieved_at": "", "last_success_at": "not-a-time", "stale": True, "error": "x"},
])
def test_detail_rejects_invalid_source_status_urls_and_timestamps(status):
    from scripts.update_monitor import _validate_detail

    detail = {"asset": FUND, "market": {}, "uzi": {}, "news": {"CN": [], "INTL": []}, "score": {"overall": None, "confidence": 0.0}, "recommendation": {"state": "等待确认", "confidence": 0.0, "timestamp": NOW}, "source_status": {"market": status}}
    with pytest.raises(ValueError):
        _validate_detail(detail)


def test_quote_no_data_retains_prior_fields_without_advancing_quote_success():
    from scripts.providers.eastmoney import ProviderResult
    from scripts.update_monitor import run_pipeline

    class NoQuoteData:
        def fetch_fund(self, asset):
            return ProviderResult(data={"asset": asset, "holdings": [{"code": "600519.SH", "weight_pct": 12, "name": "", "latest_price": None, "change_pct": None}]}, source_urls=["https://provider.test/holdings"], retrieved_at=NOW, errors={})

    before = previous_detail()
    before["market"]["holdings"] = [{"code": "600519.SH", "weight_pct": 10, "name": "prior", "latest_price": 100, "change_pct": 1}]
    before["source_status"] = {"quotes": {"last_success_at": "2026-07-20T00:00:00+00:00"}}
    detail = run_pipeline({"assets": [FUND]}, {"assets": {FUND["id"]: before}}, {"now": NOW, "fund_provider": NoQuoteData(), "news_provider": lambda *_: [], "holding_uzi": {}})["assets"][FUND["id"]]
    holding = detail["market"]["holdings"][0]
    assert (holding["name"], holding["latest_price"], holding["change_pct"]) == ("prior", 100, 1)
    assert detail["source_status"]["quotes"]["stale"] is True
    assert detail["source_status"]["quotes"]["error"] == "quote_no_data"
    assert detail["source_status"]["quotes"]["last_success_at"] == "2026-07-20T00:00:00+00:00"
