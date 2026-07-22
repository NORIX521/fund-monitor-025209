from dataclasses import asdict

from scripts.scoring import score_fund, score_stock, weighted_holding_uzi


STOCK = {
    "id": "stock-cn-600519-sh",
    "code": "600519.SH",
    "name": "贵州茅台",
    "asset_type": "stock",
    "market": "CN",
}
FUND = {
    "id": "fund-cn-025209",
    "code": "025209",
    "name": "测试基金",
    "asset_type": "fund",
    "market": "CN",
}


def test_fund_holding_uzi_uses_covered_weight_only():
    score = weighted_holding_uzi(
        [
            {"code": "600001.SH", "weight_pct": 30},
            {"code": "600002.SH", "weight_pct": 20},
            {"code": "600003.SH", "weight_pct": 50},
        ],
        {"600001.SH": {"overall": 80}, "600002.SH": {"overall": 60}},
    )

    assert score["score"] == 72.0
    assert score["coverage_pct"] == 50.0


def test_stock_score_renormalizes_around_unavailable_optional_components():
    result = score_stock(
        STOCK,
        {
            "quality_valuation": 70,
            "risk_signals": 60,
            "risk_flags": ["valuation_watch"],
        },
        {"overall": 80, "risk_flags": ["uzi_review_only"]},
    )

    assert result.overall == 73.75
    assert result.components == {
        "uzi_consensus": 80.0,
        "quality_valuation": 70.0,
        "risk_signals": 60.0,
    }
    assert result.coverage["missing"] == ["trend_momentum", "news_events"]
    assert result.coverage["weight_pct"] == 80.0
    assert result.risk_flags == ["uzi_review_only", "valuation_watch"]
    assert set(asdict(result)) == {
        "overall",
        "components",
        "confidence",
        "model",
        "model_version",
        "coverage",
        "risk_flags",
    }


def test_fund_score_lowers_confidence_when_holding_uzi_coverage_is_below_sixty():
    fund_data = {
        "history": [
            {"date": "2026-01-01", "nav": 1.00},
            {"date": "2026-01-02", "nav": 1.02},
            {"date": "2026-01-03", "nav": 0.99},
            {"date": "2026-01-04", "nav": 1.04},
            {"date": "2026-01-05", "nav": 1.08},
        ],
        "holdings": [
            {"code": "600001.SH", "weight_pct": 30},
            {"code": "600002.SH", "weight_pct": 20},
            {"code": "600003.SH", "weight_pct": 50},
        ],
    }

    result = score_fund(
        FUND,
        fund_data,
        {"600001.SH": {"overall": 80}, "600002.SH": {"overall": 60}},
    )

    assert result.components["holding_uzi"] == 72.0
    assert result.coverage["holding_uzi_pct"] == 50.0
    assert result.confidence == "low"
    assert "low_holding_uzi_coverage" in result.risk_flags
    assert "stability" in result.coverage["missing"]


def test_short_history_is_safe_and_never_turns_unavailable_metrics_into_zero():
    result = score_fund(
        FUND,
        {
            "history": [{"date": "2026-01-01", "nav": 1.0}],
            "holdings": [{"code": "600001.SH", "weight_pct": 25}],
        },
        {},
    )

    assert "risk_adjusted_return" not in result.components
    assert "drawdown_volatility" not in result.components
    assert "trend_recovery" not in result.components
    assert "holding_uzi" not in result.components
    assert set(result.coverage["missing"]) >= {
        "risk_adjusted_return",
        "drawdown_volatility",
        "trend_recovery",
        "stability",
        "holding_uzi",
    }
    assert result.overall is not None
