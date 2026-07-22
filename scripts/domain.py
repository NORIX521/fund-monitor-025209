"""Canonical watchlist asset helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


ASSET_TYPES = {"stock", "fund", "etf", "lof"}
_TYPE_ALIASES = {
    "股票": "stock",
    "a股": "stock",
    "a 股": "stock",
    "基金": "fund",
    "公募基金": "fund",
    "交易型开放式指数基金": "etf",
}
_CN_SECURITIES = re.compile(r"\d{6}\.(?:SH|SZ|BJ)$")
_HK_SECURITIES = re.compile(r"\d{5}\.HK$")
_US_SECURITIES = re.compile(r"[A-Z][A-Z0-9.-]{0,9}$")
_FUND_CODE = re.compile(r"\d{6}$")
_MAX_TEXT_LENGTH = 200


def clean(value: Any) -> str:
    """Return bounded, display-safe plain text without control characters."""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError("asset text fields must be scalar values")
    plain = "".join(character for character in str(value) if character >= " " and character != "\x7f")
    return " ".join(plain.split())[:_MAX_TEXT_LENGTH]


def _value(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _asset_type(raw_type: Any, code: str) -> str:
    kind = clean(raw_type).lower()
    kind = _TYPE_ALIASES.get(kind, kind)
    if not kind:
        kind = "fund" if re.fullmatch(r"0\d{5}", code) else "stock"
    if kind not in ASSET_TYPES:
        raise ValueError(f"unsupported asset type: {kind}")
    return kind


def _normalize_code(code: str, kind: str) -> str:
    normalized = code.strip().upper()
    if any(character.isspace() for character in normalized):
        raise ValueError("invalid code: whitespace is not allowed")

    if kind != "stock":
        if not _FUND_CODE.fullmatch(normalized):
            raise ValueError(f"invalid {kind} code: {normalized}")
        return normalized

    if re.fullmatch(r"\d{6}", normalized):
        suffix = "SH" if normalized.startswith(("5", "6", "9")) else "BJ" if normalized.startswith(("4", "8")) else "SZ"
        normalized = f"{normalized}.{suffix}"

    if not (_CN_SECURITIES.fullmatch(normalized) or _HK_SECURITIES.fullmatch(normalized) or _US_SECURITIES.fullmatch(normalized)):
        raise ValueError(f"invalid code: {normalized}")
    return normalized


def _market(code: str, kind: str) -> str:
    if kind != "stock" or code.endswith((".SH", ".SZ", ".BJ")):
        return "CN"
    if code.endswith(".HK"):
        return "HK"
    return "US"


def _enabled(raw: Mapping[str, Any]) -> bool:
    value = raw.get("enabled", True)
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return True
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no"}:
        return False
    raise ValueError("enabled must be a boolean")


def asset_id(asset: Mapping[str, Any]) -> str:
    """Produce the stable identifier used to deduplicate normalized assets."""
    kind = clean(asset.get("asset_type")).lower()
    market = clean(asset.get("market")).upper()
    code = clean(asset.get("code")).lower()
    if not kind or not market or not code:
        raise ValueError("asset id requires asset_type, market, and code")
    slug = re.sub(r"[^a-z0-9]+", "-", code).strip("-")
    return f"{kind}-{market.lower()}-{slug}"


def normalize_asset(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one untrusted asset record and emit the complete contract."""
    if not isinstance(raw, Mapping):
        raise ValueError("asset must be an object")

    raw_code = _value(raw, "code", "ticker", "代码")
    if raw_code is None:
        raise ValueError("asset code is required")
    code = clean(raw_code).upper()
    if not code:
        raise ValueError("asset code is required")

    kind = _asset_type(_value(raw, "asset_type", "type", "类型"), code)
    code = _normalize_code(code, kind)
    market = _market(code, kind)
    asset = {
        "code": code,
        "name": clean(_value(raw, "name", "名称")),
        "asset_type": kind,
        "market": market,
        "sector": clean(_value(raw, "sector", "板块")),
        "note": clean(_value(raw, "note", "备注")),
        "enabled": _enabled(raw),
    }
    return {"id": asset_id(asset), **asset}
