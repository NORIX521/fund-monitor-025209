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


def test_cn_stream_reads_recent_electronics_updates_directly_from_miit():
    from scripts.providers.news import MIIT_INDEX_URL, fetch_news

    index = """<html><body>
      <a href="/jgsj/yxj/xxfb/art/2026/art_one.html">2026年1—5月电子信息制造业运行情况</a>
      <a href="/jgsj/yxj/xxfb/art/2026/art_two.html">半导体产业运行更新</a>
      <a href="/jgsj/yxj/xxfb/art/2026/art_three.html">集成电路行业月度数据</a>
      <a href="/jgsj/yxj/xxfb/art/2026/art_four.html">芯片制造业后续更新</a>
      <a href="/jgsj/yxj/xxfb/art/2026/art_other.html">通信业运行情况</a>
    </body></html>"""

    class RouteSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url == MIIT_INDEX_URL:
                return FeedResponse(index)
            if "/art/2026/art_" in url:
                slug = url.rsplit("_", 1)[-1].removesuffix(".html")
                titles = {
                    "one": "2026年1—5月电子信息制造业运行情况",
                    "two": "半导体产业运行更新",
                    "three": "集成电路行业月度数据",
                }
                return FeedResponse(
                    f'<html><head><meta name="ArticleTitle" content="{titles[slug]}">'
                    '<meta name="PubDate" content="2026-07-01 14:27"></head></html>'
                )
            return FeedResponse("<rss><channel></channel></rss>")

    session = RouteSession()
    items = fetch_news(
        {"code": "025209", "name": "永赢先锋半导体智选混合发起A", "sector": "半导体/存储"},
        "CN",
        session=session,
        retrieved_at="2026-07-23T00:00:00+00:00",
    )

    assert len(items) == 3
    assert items[0].title == "2026年1—5月电子信息制造业运行情况"
    assert items[0].published_at == "2026-07-01T14:27:00+08:00"
    assert items[0].source == "工业和信息化部"
    assert items[0].source_url == "https://www.miit.gov.cn"
    assert items[0].article_url.startswith("https://wap.miit.gov.cn/jgsj/yxj/xxfb/art/")
    assert len([url for url, _ in session.calls if "/art/2026/art_" in url]) == 3
