import json
from pathlib import Path

import pytest
import requests

from scripts.providers.eastmoney import EastmoneyProvider


FIXTURES = Path(__file__).parents[1] / "fixtures"
ASSET = {
    "id": "fund-cn-025209",
    "code": "025209",
    "name": "永赢先锋半导体智选混合发起C",
    "asset_type": "fund",
    "market": "CN",
    "sector": "半导体/存储",
    "note": "",
    "enabled": True,
}


class FixtureResponse:
    def __init__(self, payload, *, is_json=True):
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self._is_json = is_json

    def json(self):
        if not self._is_json:
            raise ValueError("not JSON")
        return self._payload

    def raise_for_status(self):
        return None


class FixtureSession:
    def __init__(self, *, quote_failure=False):
        self.quote_failure = quote_failure
        self.calls = []
        self.history = json.loads((FIXTURES / "fund_history.json").read_text(encoding="utf-8"))
        self.holdings = (FIXTURES / "fund_holdings.html").read_text(encoding="utf-8")
        self.quotes = json.loads((FIXTURES / "quotes.json").read_text(encoding="utf-8"))

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "lsjz" in url:
            return FixtureResponse(self.history)
        if "FundArchivesDatas" in url:
            return FixtureResponse(self.holdings, is_json=False)
        if "ulist.np" in url:
            if self.quote_failure:
                raise requests.RequestException("quote service unavailable")
            return FixtureResponse(self.quotes)
        raise AssertionError(f"unexpected URL: {url}")


def test_fetch_fund_parses_history_holdings_and_replaces_placeholder_name():
    session = FixtureSession()

    result = EastmoneyProvider(session=session).fetch_fund(ASSET)

    assert result.data["history"][-1]["nav"] == 2.4767
    assert result.data["holdings"][0]["code"] == "603986.SH"
    assert result.data["holdings"][0]["name"] == "兆易创新"
    assert result.data["holdings"][0]["name"] != "行情"
    assert result.data["holding_report_date"] == "2026 Q2"
    assert result.errors == {}
    assert len(result.source_urls) == 3
    assert all(kwargs["timeout"] == (5, 25) for _, kwargs in session.calls)
    assert all("UZI fund monitor" in kwargs["headers"]["User-Agent"] for _, kwargs in session.calls)


def test_fetch_fund_keeps_last_good_history_when_quote_request_fails():
    result = EastmoneyProvider(session=FixtureSession(quote_failure=True)).fetch_fund(ASSET)

    assert result.data["history"][-1]["nav"] == 2.4767
    assert result.data["holdings"][0]["code"] == "603986.SH"
    assert "quotes" in result.errors
    assert "quote service unavailable" in result.errors["quotes"]


def test_fetch_quotes_normalizes_cn_stock_codes():
    quotes = EastmoneyProvider(session=FixtureSession()).fetch_quotes(["603986"])

    assert quotes["603986.SH"]["name"] == "兆易创新"
