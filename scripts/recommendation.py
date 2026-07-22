"""Deterministic research states; never a trading instruction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


HARD_RISK_FLAGS = {"large_drawdown", "high_volatility", "high_concentration", "fraud_risk", "liquidity_crisis"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def recommend(score: Mapping[str, Any], quality: Mapping[str, Any]) -> dict[str, Any]:
    confidence = round(max(0.0, min(1.0, _number(score.get("confidence")))), 4)
    overall = _number(score.get("overall"), -1.0)
    supplied = [str(flag) for flag in score.get("risk_flags", []) if str(flag)]
    hard = [flag for flag in [str(flag) for flag in score.get("hard_risk_flags", supplied)] if flag in HARD_RISK_FLAGS]
    warnings = [str(flag) for flag in score.get("warnings", []) if str(flag)] + [flag for flag in supplied if flag not in HARD_RISK_FLAGS]
    hard_failures = [str(item) for item in quality.get("hard_failures", []) if str(item)]
    stale = quality.get("stale") is True
    if hard_failures:
        state = "暂不纳入"
    elif stale or confidence < 0.45:
        state = "等待确认"
    elif hard or overall < 40:
        state = "风险偏高"
    elif overall >= 75 and confidence >= 0.65:
        state = "优先研究"
    else:
        state = "持续观察"
    reasons = [f"component {key}={value}" for key, value in (score.get("components") or {}).items()]
    reasons.extend(f"evidence {key}={value}" for key, value in (quality.get("evidence") or {}).items())
    return {"state": state, "confidence": confidence, "risk": {"hard_flags": hard, "warnings": warnings, "stale": stale, "hard_failures": hard_failures}, "reasons": reasons or ["evidence availability=limited"], "invalidation_rules": ["confidence < 0.45", "stale evidence requires confirmation", "hard_risk_flags or overall < 40 requires renewed research review"], "timestamp": quality.get("timestamp") or ""}
