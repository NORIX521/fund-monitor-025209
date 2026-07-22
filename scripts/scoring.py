"""Deterministic, explainable stock and fund research scoring."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


STOCK_WEIGHTS = {
    "uzi_consensus": 0.45,
    "quality_valuation": 0.20,
    "trend_momentum": 0.15,
    "risk_signals": 0.15,
    "news_events": 0.05,
}
FUND_WEIGHTS = {
    "risk_adjusted_return": 0.20,
    "drawdown_volatility": 0.20,
    "trend_recovery": 0.15,
    "stability": 0.10,
    "concentration": 0.15,
    "holding_uzi": 0.20,
}


@dataclass(frozen=True)
class ScoreResult:
    overall: float | None
    components: dict[str, float]
    confidence: float
    model: str
    model_version: str
    coverage: dict[str, Any]
    risk_flags: list[str]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _score(value: Any) -> float | None:
    number = _number(value)
    if number is None or not 0.0 <= number <= 100.0:
        return None
    return round(number, 2)


def _clamp(value: float) -> float:
    return min(100.0, max(0.0, value))


def weighted_average(parts: Iterable[tuple[Any, float]]) -> float | None:
    """Average available values only; absent values never contribute zero weight."""
    usable = [
        (number, float(weight))
        for value, weight in parts
        if (number := _number(value)) is not None and float(weight) > 0
    ]
    if not usable:
        return None
    return sum(value * weight for value, weight in usable) / sum(weight for _, weight in usable)


def _unique_flags(*sources: Any) -> list[str]:
    flags: list[str] = []
    for source in sources:
        if not isinstance(source, (list, tuple, set)):
            continue
        for value in source:
            flag = str(value).strip()
            if flag and flag not in flags:
                flags.append(flag)
    return flags


def _component_coverage(
    components: Mapping[str, float], weights: Mapping[str, float]
) -> dict[str, Any]:
    available = [name for name in weights if name in components]
    missing = [name for name in weights if name not in components]
    return {
        "available": available,
        "missing": missing,
        "weight_pct": round(sum(weights[name] for name in available) * 100.0, 2),
    }


def _assemble_score(
    components: Mapping[str, Any],
    weights: Mapping[str, float],
    *,
    confidence_factor: float,
    confidence_cap: float,
    model: str,
    model_version: str,
    risk_flags: list[str],
    coverage_extra: Mapping[str, Any] | None = None,
) -> ScoreResult:
    usable = {
        name: score
        for name in weights
        if name in components and (score := _score(components[name])) is not None
    }
    overall = weighted_average((usable.get(name), weight) for name, weight in weights.items())
    coverage = _component_coverage(usable, weights)
    if coverage_extra:
        coverage.update(coverage_extra)
    confidence = min(
        max(0.0, confidence_cap),
        coverage["weight_pct"] / 100.0 * max(0.0, confidence_factor),
    )
    return ScoreResult(
        overall=round(overall, 2) if overall is not None else None,
        components=usable,
        confidence=round(min(1.0, confidence), 4),
        model=model,
        model_version=model_version,
        coverage=coverage,
        risk_flags=risk_flags,
    )


def _first_score(source: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _score(source.get(key))
        if value is not None:
            return value
    return None


def _direct_uzi_score(source: Mapping[str, Any]) -> float | None:
    """Use the first present UZI score field without masking a malformed primary value."""
    primary = source.get("overall")
    if primary not in (None, ""):
        return _score(primary)
    return _score(source.get("overall_score"))


def _combined_score(source: Mapping[str, Any], direct: str, *parts: str) -> float | None:
    value = _first_score(source, direct)
    if value is not None:
        return value
    return weighted_average((_first_score(source, part), 1.0) for part in parts)


def _quality_adjustment(*sources: Mapping[str, Any]) -> tuple[float, list[str]]:
    """Return deterministic confidence penalties for stale data and source errors."""
    factor = 1.0
    penalties: list[str] = []
    if any(source.get("stale") is True for source in sources):
        factor *= 0.8
        penalties.append("stale_data")
    if any(source.get("errors") for source in sources):
        factor *= 0.9
        penalties.append("source_errors")
    return factor, penalties


def score_stock(
    asset: Mapping[str, Any], market: Mapping[str, Any], uzi: Mapping[str, Any]
) -> ScoreResult:
    """Blend direct UZI stock evidence with available market evidence for research only."""
    if asset.get("asset_type") != "stock":
        raise ValueError("score_stock requires a stock asset")
    components = {
        "uzi_consensus": _direct_uzi_score(uzi),
        "quality_valuation": _combined_score(
            market, "quality_valuation", "quality", "valuation"
        ),
        "trend_momentum": _combined_score(market, "trend_momentum", "trend", "momentum"),
        "risk_signals": _combined_score(market, "risk_signals", "risk", "trap_safety"),
        "news_events": _combined_score(market, "news_events", "news", "events"),
    }
    flags = _unique_flags(uzi.get("risk_flags"), market.get("risk_flags"))
    confidence_factor, penalties = _quality_adjustment(market, uzi)
    if components["uzi_consensus"] is None:
        confidence_factor *= 0.6
        penalties.append("direct_uzi_unavailable")
    return _assemble_score(
        components,
        STOCK_WEIGHTS,
        confidence_factor=confidence_factor,
        confidence_cap=1.0,
        model="stock-uzi-review-composite",
        model_version="1.0",
        risk_flags=flags,
        coverage_extra={"confidence_penalties": penalties},
    )


def weighted_holding_uzi(
    holdings: Iterable[Mapping[str, Any]], holding_uzi: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Weight UZI scores over covered fund weight only and report uncovered weight."""
    weighted_sum = 0.0
    covered_weight = 0.0
    covered_codes: list[str] = []
    uncovered_codes: list[str] = []
    for holding in holdings:
        code = str(holding.get("code") or "").strip().upper()
        weight = _number(holding.get("weight_pct"))
        if not code or weight is None or weight <= 0:
            continue
        uzi = holding_uzi.get(code)
        overall = _direct_uzi_score(uzi) if isinstance(uzi, Mapping) else None
        if overall is None:
            uncovered_codes.append(code)
            continue
        weighted_sum += overall * weight
        covered_weight += weight
        covered_codes.append(code)
    return {
        "score": round(weighted_sum / covered_weight, 2) if covered_weight else None,
        "coverage_pct": round(min(100.0, covered_weight), 2),
        "covered_codes": covered_codes,
        "uncovered_codes": uncovered_codes,
    }


def fund_metrics(history: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Derive return, annualized volatility, drawdown, and recovery from valid NAV rows."""
    rows: list[tuple[str, float]] = []
    for row in history:
        nav = _number(row.get("nav")) if isinstance(row, Mapping) else None
        if nav is not None and nav > 0:
            rows.append((str(row.get("date") or ""), nav))
    rows.sort(key=lambda item: item[0])
    values = [nav for _, nav in rows]
    if len(values) < 2:
        return {}

    returns = [current / previous - 1.0 for previous, current in zip(values, values[1:])]
    total_return = (values[-1] / values[0] - 1.0) * 100.0
    peak = values[0]
    trough_after_peak = values[0]
    max_drawdown = 0.0
    for nav in values:
        if nav >= peak:
            peak = nav
            trough_after_peak = nav
        else:
            trough_after_peak = min(trough_after_peak, nav)
            max_drawdown = min(max_drawdown, (nav / peak - 1.0) * 100.0)
    recovery = 100.0
    if peak > trough_after_peak:
        recovery = (values[-1] - trough_after_peak) / (peak - trough_after_peak) * 100.0

    metrics = {
        "total_return_pct": total_return,
        "max_drawdown_pct": max_drawdown,
        "recovery_pct": _clamp(recovery),
    }
    if len(returns) >= 2:
        metrics["volatility_pct"] = statistics.pstdev(returns) * math.sqrt(252) * 100.0
    return metrics


def _score_risk_adjusted(metrics: Mapping[str, float]) -> float | None:
    total_return = _number(metrics.get("total_return_pct"))
    volatility = _number(metrics.get("volatility_pct"))
    if total_return is None or volatility is None:
        return None
    return _clamp(50.0 + total_return * 2.0 - volatility * 0.5)


def _score_drawdown(metrics: Mapping[str, float]) -> float | None:
    drawdown = _number(metrics.get("max_drawdown_pct"))
    volatility = _number(metrics.get("volatility_pct"))
    if drawdown is None or volatility is None:
        return None
    return _clamp(100.0 - abs(drawdown) * 1.5 - volatility * 0.75)


def _score_trend(metrics: Mapping[str, float]) -> float | None:
    total_return = _number(metrics.get("total_return_pct"))
    recovery = _number(metrics.get("recovery_pct"))
    if total_return is None or recovery is None:
        return None
    trend = _clamp(50.0 + total_return * 2.0)
    return (trend + _clamp(recovery)) / 2.0


def _score_stability(fund_data: Mapping[str, Any]) -> float | None:
    nested = fund_data.get("stability")
    stability = nested if isinstance(nested, Mapping) else {}

    def value(*keys: str) -> float | None:
        for source in (fund_data, stability):
            for key in keys:
                number = _number(source.get(key))
                if number is not None:
                    return number
        return None

    scale = value("size_billion", "scale_billion", "aum_billion")
    fee = value("expense_ratio_pct", "fee_rate_pct")
    age = value("age_years")
    tenure = value("manager_tenure_years")
    return weighted_average(
        [
            (_clamp(40.0 + scale * 3.0) if scale is not None else None, 1.0),
            (_clamp(100.0 - fee * 40.0) if fee is not None else None, 1.0),
            (_clamp(age / 5.0 * 100.0) if age is not None else None, 1.0),
            (_clamp(tenure / 5.0 * 100.0) if tenure is not None else None, 1.0),
        ]
    )


def _score_concentration(holdings: Iterable[Mapping[str, Any]]) -> float | None:
    weights = [
        weight
        for holding in holdings
        if (weight := _number(holding.get("weight_pct"))) is not None and weight > 0
    ]
    if not weights:
        return None
    disclosed_total = min(100.0, sum(weights))
    largest = max(weights)
    return _clamp(
        100.0 - max(0.0, disclosed_total - 40.0) - max(0.0, largest - 10.0) * 2.0
    )


def score_fund(
    asset: Mapping[str, Any],
    fund_data: Mapping[str, Any],
    holding_uzi: Mapping[str, Mapping[str, Any]],
) -> ScoreResult:
    """Apply the fund-only six-factor model without sending the fund itself to UZI."""
    if asset.get("asset_type") not in {"fund", "etf", "lof"}:
        raise ValueError("score_fund requires a fund, ETF, or LOF asset")
    history = fund_data.get("history")
    holdings_value = fund_data.get("holdings")
    history = history if isinstance(history, list) else []
    holdings = holdings_value if isinstance(holdings_value, list) else []
    metrics = fund_metrics(history)
    holding_score = weighted_holding_uzi(holdings, holding_uzi)
    components = {
        "risk_adjusted_return": _score_risk_adjusted(metrics),
        "drawdown_volatility": _score_drawdown(metrics),
        "trend_recovery": _score_trend(metrics),
        "stability": _score_stability(fund_data),
        "concentration": _score_concentration(holdings),
        "holding_uzi": holding_score["score"],
    }

    flags: list[str] = []
    holding_coverage = holding_score["coverage_pct"]
    if holding_coverage < 60.0:
        flags.append("low_holding_uzi_coverage")
    if metrics.get("max_drawdown_pct", 0.0) <= -25.0:
        flags.append("large_drawdown")
    if metrics.get("volatility_pct", 0.0) >= 35.0:
        flags.append("high_volatility")
    valid_weights = [
        weight
        for holding in holdings
        if (weight := _number(holding.get("weight_pct"))) is not None and weight > 0
    ]
    if valid_weights and sum(valid_weights) >= 70.0:
        flags.append("high_concentration")
    flags = _unique_flags(flags, fund_data.get("risk_flags"))

    confidence_factor, penalties = _quality_adjustment(fund_data)
    confidence_cap = 1.0
    if holding_coverage < 60.0:
        confidence_cap = 0.44
        penalties.append("holding_uzi_coverage_below_60pct")
    return _assemble_score(
        components,
        FUND_WEIGHTS,
        confidence_factor=confidence_factor,
        confidence_cap=confidence_cap,
        model="fund-transparent-six-factor",
        model_version="1.0",
        risk_flags=flags,
        coverage_extra={
            "holding_uzi_pct": holding_coverage,
            "confidence_penalties": penalties,
        },
    )
