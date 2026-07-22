"""Deterministic research state selection; this module never makes trading calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _reasons(score: Mapping[str, Any], quality: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    components = score.get("components")
    if isinstance(components, Mapping):
        for name, value in components.items():
            reasons.append(f"component {name}={value}")
    evidence = quality.get("evidence")
    if isinstance(evidence, Mapping):
        for name, value in evidence.items():
            reasons.append(f"evidence {name}={value}")
    return reasons or ["evidence availability=limited"]


def recommend(score: Mapping[str, Any], quality: Mapping[str, Any]) -> dict[str, Any]:
    """Return an explainable research priority from supplied scoring evidence only."""
    confidence = round(max(0.0, min(1.0, _number(score.get("confidence")))), 4)
    overall = _number(score.get("overall"), -1.0)
    risk_flags = [str(flag) for flag in score.get("risk_flags", []) if str(flag)]
    hard_failures = [str(item) for item in quality.get("hard_failures", []) if str(item)]
    stale = quality.get("stale") is True
    if hard_failures:
        state = "暂不纳入"
    elif stale or confidence < 0.45:
        state = "等待确认"
    elif risk_flags or overall < 40:
        state = "风险偏高"
    elif overall >= 75 and confidence >= 0.65:
        state = "优先研究"
    else:
        state = "持续观察"
    return {
        "state": state,
        "confidence": confidence,
        "risk": {"flags": risk_flags, "stale": stale, "hard_failures": hard_failures},
        "reasons": _reasons(score, quality),
        "invalidation_rules": [
            "confidence < 0.45",
            "stale evidence requires confirmation",
            "risk_flags or overall < 40 requires renewed research review",
        ],
        "timestamp": quality.get("timestamp") or "",
    }
