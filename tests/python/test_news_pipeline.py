from pathlib import Path


FIXTURES = Path(__file__).parents[1] / "fixtures"


class FixtureResponse:
    def __init__(self, text):
        self.text = text
        self.status_checked = False

    def raise_for_status(self):
        self.status_checked = True


class FixtureSession:
    def __init__(self):
        self.calls = []
        self.responses = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        fixture = "news-cn.xml" if "cn-feed" in url else "news-us.xml"
        response = FixtureResponse((FIXTURES / fixture).read_text(encoding="utf-8"))
        self.responses.append(response)
        return response


def test_rss_dedupe_preserves_exact_article_and_source_urls():
    from scripts.providers.news import fetch_news

    session = FixtureSession()
    items = fetch_news(
        {"code": "600519.SH", "name": "研究标的"},
        "CN",
        session=session,
        feeds=["https://feeds.example/cn-feed"],
        retrieved_at="2026-07-22T00:00:00+00:00",
    )

    assert len(items) == 1
    assert items[0].article_url == "https://articles.example.cn/notices/600519?from=rss&id=1"
    assert items[0].source == "公司公告"
    assert items[0].source_url == "https://source.example.cn/company"
    assert items[0].published_at == "2026-07-21T08:00:00+08:00"
    assert items[0].retrieved_at == "2026-07-22T00:00:00+00:00"
    assert items[0].region == "CN"
    assert session.calls[0][1]["timeout"]
    assert "User-Agent" in session.calls[0][1]["headers"]
    assert session.responses[0].status_checked is True


def test_atom_source_and_article_urls_remain_traceable():
    from scripts.providers.news import fetch_news

    items = fetch_news(
        {"code": "ABC", "name": "Example"},
        "INTL",
        session=FixtureSession(),
        feeds=["https://feeds.example/us-feed"],
        retrieved_at="2026-07-22T00:00:00+00:00",
    )

    assert [item.region for item in items] == ["INTL"]
    assert items[0].article_url == "https://articles.example.com/filings/abc?ref=rss&item=7"
    assert items[0].source_url == "https://source.example.com/issuer"


def test_fetch_news_rejects_unknown_region_without_network():
    import pytest

    from scripts.providers.news import fetch_news

    session = FixtureSession()
    with pytest.raises(ValueError, match="CN or INTL"):
        fetch_news({}, "US", session=session)
    assert session.calls == []
