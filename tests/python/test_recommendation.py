import pytest


NOW = "2026-07-22T00:00:00+00:00"


def test_low_confidence_returns_waiting_with_invalidation():
    from scripts.recommendation import recommend

    result = recommend(
        {
            "overall": 82,
            "confidence": 0.35,
            "components": {"uzi_consensus": 80, "trend_momentum": 88},
            "risk_flags": [],
        },
        {"stale": False, "timestamp": NOW},
    )

    assert result["state"] == "等待确认"
    assert result["confidence"] == 0.35
    assert result["invalidation_rules"]
    assert result["timestamp"] == NOW


@pytest.mark.parametrize(
    ("score", "quality", "expected"),
    [
        ({"overall": 90, "confidence": 0.9}, {"hard_failures": ["identity"]}, "暂不纳入"),
        ({"overall": 90, "confidence": 0.9}, {"stale": True}, "等待确认"),
        (
            {"overall": 90, "confidence": 0.9, "risk_flags": ["large_drawdown"]},
            {},
            "风险偏高",
        ),
        ({"overall": 39, "confidence": 0.9}, {}, "风险偏高"),
        ({"overall": 75, "confidence": 0.65}, {}, "优先研究"),
        ({"overall": 62, "confidence": 0.8}, {}, "持续观察"),
    ],
)
def test_recommendation_priority_is_deterministic(score, quality, expected):
    from scripts.recommendation import recommend

    quality = {**quality, "timestamp": NOW}
    result = recommend(score, quality)

    assert result["state"] == expected
    assert result["invalidation_rules"]
    assert "收益" not in " ".join(result["reasons"] + result["invalidation_rules"])


def test_reasons_reference_only_named_components_or_evidence():
    from scripts.recommendation import recommend

    result = recommend(
        {
            "overall": 78,
            "confidence": 0.8,
            "components": {"uzi_consensus": 81, "trend_momentum": 72},
            "risk_flags": [],
        },
        {"timestamp": NOW, "evidence": {"news_traceability": "complete"}},
    )

    joined = " ".join(result["reasons"])
    assert "uzi_consensus" in joined
    assert "trend_momentum" in joined
    assert "news_traceability" in joined
