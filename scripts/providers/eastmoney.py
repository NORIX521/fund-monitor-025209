"""Eastmoney-backed fund history, holdings, and quote retrieval."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


TIMEOUT = (5, 25)
USER_AGENT = "UZI fund monitor/1.0 (+https://github.com/openai/uzi-monitor)"
HISTORY_URL = "https://api.fund.eastmoney.com/f10/lsjz"
HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
QUOTES_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
PLACEHOLDER_NAMES = {"行情", "详情", "查看", "--", "-"}


@dataclass
class ProviderResult:
    data: dict[str, Any]
    source_urls: list[str]
    retrieved_at: str
    errors: dict[str, str]


def normalize_cn_stock(code: str) -> str:
    """Return an Eastmoney six-digit mainland stock code with its exchange."""
    raw = str(code).strip().upper()
    base = raw.split(".", maxsplit=1)[0]
    return f"{base}.SH" if base.startswith(("5", "6", "9")) else f"{base}.BJ" if base.startswith(("4", "8")) else f"{base}.SZ"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _security_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name or name in PLACEHOLDER_NAMES or re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", name):
        return ""
    return name


class EastmoneyProvider:
    """Retrieve Chinese fund information while retaining successful components."""

    def __init__(self, session: requests.Session | Any | None = None) -> None:
        self.session = session or requests.Session()
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"}

    def _source_url(self, base_url: str, params: dict[str, Any]) -> str:
        return f"{base_url}?{urlencode(params, doseq=True)}"

    def _request(
        self,
        base_url: str,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, str]:
        source_url = self._source_url(base_url, params)
        request_headers = {**self.headers, **(headers or {})}
        response = self.session.get(source_url, headers=request_headers, timeout=TIMEOUT)
        response.raise_for_status()
        return response, source_url

    @staticmethod
    def _fund_page_url(fund_code: str) -> str:
        return f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html"

    def _fetch_history(self, fund_code: str) -> tuple[list[dict[str, Any]], str]:
        response, source_url = self._request(
            HISTORY_URL,
            {"fundCode": fund_code, "pageIndex": 1, "pageSize": 180},
            {"Referer": self._fund_page_url(fund_code)},
        )
        payload = response.json()
        rows = ((payload.get("Data") or {}).get("LSJZList") or [])
        history = []
        for row in rows:
            nav = _number(row.get("DWJZ")) if isinstance(row, dict) else None
            date = row.get("FSRQ") if isinstance(row, dict) else None
            if nav is not None and isinstance(date, str) and date:
                history.append(
                    {"date": date, "nav": nav, "change_pct": _number(row.get("JZZZL"))}
                )
        history.sort(key=lambda item: item["date"])
        if not history:
            raise RuntimeError("Eastmoney returned no NAV history")
        return history, source_url

    def _fetch_holdings(self, fund_code: str) -> tuple[list[dict[str, Any]], str | None, str]:
        response, source_url = self._request(
            HOLDINGS_URL,
            {"type": "jjcc", "code": fund_code, "topline": 10, "year": "", "month": ""},
            {"Referer": self._fund_page_url(fund_code)},
        )
        page = html.unescape(self._decode_embedded_html(response.text))
        soup = BeautifulSoup(page, "html.parser")
        report_date = self._report_date(soup.get_text(" ", strip=True))
        holdings: list[dict[str, Any]] = []
        seen_codes: set[str] = set()
        for row in soup.select("tbody tr, table tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
            code = next(
                (match.group(0) for cell in cells if (match := re.search(r"(?<!\d)\d{6}(?!\d)", cell))),
                None,
            )
            if not code:
                continue
            normalized_code = normalize_cn_stock(code)
            if normalized_code in seen_codes:
                continue
            seen_codes.add(normalized_code)
            labels = [_security_name(link.get_text(" ", strip=True)) for link in row.select("a")]
            name = next((label for label in labels if label), "")
            if not name:
                code_index = next((index for index, cell in enumerate(cells) if code in cell), -1)
                if code_index >= 0 and code_index + 1 < len(cells):
                    name = _security_name(cells[code_index + 1])
            percentage = next(
                (
                    _number(match.group(1))
                    for cell in reversed(cells)
                    if (match := re.fullmatch(r"(-?\d+(?:\.\d+)?)%", cell))
                ),
                None,
            )
            holdings.append(
                {"code": normalized_code, "name": name, "weight_pct": percentage, "change_pct": None}
            )
            if len(holdings) == 10:
                break
        if not holdings:
            raise RuntimeError("Could not parse fund holdings")
        return holdings, report_date, source_url

    @staticmethod
    def _decode_embedded_html(text: str) -> str:
        match = re.search(r'content:"(.*)",arryear:', text, flags=re.S)
        if not match:
            return text
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            return match.group(1).replace(r"\\\"", '"').replace(r"\\/", "/")

    @staticmethod
    def _report_date(text: str) -> str | None:
        match = re.search(r"(20\d{2})年(?:第)?([一二三四1234])季度", text)
        if not match:
            return None
        quarter = {"一": "1", "二": "2", "三": "3", "四": "4"}.get(match.group(2), match.group(2))
        return f"{match.group(1)} Q{quarter}"

    def _fetch_quotes(self, codes: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
        normalized_codes = [normalize_cn_stock(code) for code in codes]
        secids = ",".join(
            f"1.{code[:6]}" if code.endswith(".SH") else f"0.{code[:6]}" for code in normalized_codes
        )
        response, source_url = self._request(
            QUOTES_URL,
            {
                "secids": secids,
                "fields": "f12,f14,f2,f3",
                "fltt": 2,
                "invt": 2,
            },
        )
        payload = response.json()
        rows = ((payload.get("data") or {}).get("diff") or [])
        quotes: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("f12") in (None, ""):
                continue
            code = normalize_cn_stock(str(row["f12"]))
            quotes[code] = {
                "name": _security_name(row.get("f14")),
                "latest_price": _number(row.get("f2")),
                "change_pct": _number(row.get("f3")),
            }
        return quotes, source_url

    def fetch_quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch the latest quote fields keyed by normalized Chinese stock code."""
        if not codes:
            return {}
        quotes, _ = self._fetch_quotes(codes)
        return quotes

    def fetch_fund(self, asset: dict[str, Any]) -> ProviderResult:
        """Fetch independent fund components without discarding earlier successes."""
        if asset.get("asset_type") not in {"fund", "etf", "lof"}:
            raise ValueError("EastmoneyProvider.fetch_fund requires a fund asset")
        fund_code = str(asset.get("code", "")).strip()
        if not re.fullmatch(r"\d{6}", fund_code):
            raise ValueError("fund asset requires a six-digit code")

        data: dict[str, Any] = {"asset": dict(asset)}
        source_urls: list[str] = []
        errors: dict[str, str] = {}
        try:
            history, source_url = self._fetch_history(fund_code)
            data["history"] = history
            source_urls.append(source_url)
        except Exception as error:
            errors["history"] = str(error)

        try:
            holdings, report_date, source_url = self._fetch_holdings(fund_code)
            data["holdings"] = holdings
            data["holding_report_date"] = report_date
            source_urls.append(source_url)
        except Exception as error:
            errors["holdings"] = str(error)
            holdings = []

        if holdings:
            try:
                quotes, source_url = self._fetch_quotes([holding["code"] for holding in holdings])
                source_urls.append(source_url)
                if not quotes:
                    raise RuntimeError("Eastmoney returned no holding quotes")
                for holding in holdings:
                    quote = quotes.get(holding["code"], {})
                    quote_name = _security_name(quote.get("name"))
                    if quote_name:
                        holding["name"] = quote_name
                    holding["name"] = _security_name(holding.get("name"))
                    holding["latest_price"] = quote.get("latest_price")
                    holding["change_pct"] = quote.get("change_pct")
            except Exception as error:
                errors["quotes"] = str(error)

        return ProviderResult(
            data=data,
            source_urls=source_urls,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            errors=errors,
        )
