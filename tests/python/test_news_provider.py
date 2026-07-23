from urllib.parse import parse_qs, urlparse

import requests


class FeedResponse:
    def __init__(self, body: str):
        self.content = body.encode("utf-8")
        # Google News can be decoded incorrectly by requests on Windows when
        # callers trust the guessed response encoding instead of the XML bytes.
        self.text = self.content.decode("latin-1")

    def raise_for_status(self):
        return None


class FailingThenFeedSession:
    def __init__(self, body: str):
        self.body = body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            raise requests.RequestException("one discovery feed is unavailable")
        return FeedResponse(self.body)


def test_international_default_queries_use_english_sector_terms_only():
    from scripts.providers.news import default_feeds

    asset = {"code": "025209", "name": "永赢先锋半导体智选混合发起A", "sector": "半导体/存储"}
    feeds = default_feeds(asset, "INTL")
    queries = [parse_qs(urlparse(url).query)["q"][0].lower() for url in feeds]

    assert len(feeds) >= 2
    assert all("semiconductor" in query for query in queries)
    assert any("memory chip" in query for query in queries)
    assert all(asset["name"] not in query and asset["code"] not in query for query in queries)
    assert all("hl=en-US" in url and "ceid=US%3Aen" in url for url in feeds)


def test_default_discovery_survives_one_feed_failure_and_keeps_only_fresh_relevant_trusted_items():
    from scripts.providers.news import fetch_news

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item><title>SEMI raises semiconductor equipment forecast</title><link>https://news.google.com/rss/articles/newer</link><pubDate>Tue, 21 Jul 2026 09:00:00 +0000</pubDate><source url="https://www.semi.org/en/news-media-press-releases">SEMI</source></item>
      <item><title>Commerce announces semiconductor investment</title><link>https://news.google.com/rss/articles/older</link><pubDate>Mon, 20 Jul 2026 08:00:00 +0000</pubDate><source url="https://www.commerce.gov/news">U.S. Department of Commerce</source></item>
      <item><title>Semiconductor market report from an aggregator</title><link>https://news.google.com/rss/articles/untrusted</link><pubDate>Wed, 22 Jul 2026 08:00:00 +0000</pubDate><source url="https://example.com/news">Aggregator</source></item>
      <item><title>SEMI hosts charity fun run</title><link>https://news.google.com/rss/articles/irrelevant</link><pubDate>Wed, 22 Jul 2026 07:00:00 +0000</pubDate><source url="https://www.semi.org/en/news">SEMI</source></item>
      <item><title>Old semiconductor forecast</title><link>https://news.google.com/rss/articles/stale</link><pubDate>Thu, 01 Jan 2026 08:00:00 +0000</pubDate><source url="https://www.semi.org/en/news">SEMI</source></item>
    </channel></rss>"""
    session = FailingThenFeedSession(xml)

    items = fetch_news(
        {"code": "025209", "name": "永赢先锋半导体智选混合发起A", "sector": "半导体/存储"},
        "INTL",
        session=session,
        retrieved_at="2026-07-23T00:00:00+00:00",
    )

    assert [item.title for item in items] == [
        "SEMI raises semiconductor equipment forecast",
        "Commerce announces semiconductor investment",
    ]
    assert len(session.calls) >= 2
    assert all("é" not in item.title for item in items)
