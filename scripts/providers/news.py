"""Traceable, allowlisted RSS and Atom news retrieval for research records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from xml.etree import ElementTree

import requests


TIMEOUT = (5, 25)
USER_AGENT = "fund-monitor-025209/1.0 (+https://github.com/NORIX521/fund-monitor-025209)"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
TRACKING_PARAMETERS = {"gclid", "fbclid", "ref", "source", "from"}
MAX_ITEMS = 6
MAX_AGE = timedelta(days=120)
TRUSTED_HOSTS = {
    "CN": frozenset(
        {
            "www.miit.gov.cn",
            "wap.miit.gov.cn",
            "www.ndrc.gov.cn",
            "zfxxgk.ndrc.gov.cn",
            "www.gov.cn",
            "www.csrc.gov.cn",
            "www.sse.com.cn",
            "www.szse.cn",
        }
    ),
    "INTL": frozenset(
        {
            "semi.org",
            "www.semi.org",
            "wsts.org",
            "www.wsts.org",
            "commerce.gov",
            "www.commerce.gov",
            "bis.gov",
            "www.bis.gov",
        }
    ),
}
RELEVANCE_TERMS = {
    "CN": ("半导体", "集成电路", "存储芯片", "芯片", "电子信息制造业"),
    "INTL": ("semiconductor", "memory chip", "dram", "nand", "hbm", "chip"),
}


@dataclass(frozen=True)
class NewsItem:
    title: str
    article_url: str
    source: str
    source_url: str
    published_at: str
    retrieved_at: str
    region: str


def default_feeds(asset: dict[str, Any], region: str) -> tuple[str, ...]:
    """Build official-source Google News discovery feeds for the asset sector."""
    if region not in {"CN", "INTL"}:
        raise ValueError("region must be CN or INTL")
    if region == "CN":
        queries = (
            '(半导体 OR 集成电路 OR 存储芯片 OR 电子信息制造业) (site:miit.gov.cn OR site:ndrc.gov.cn OR site:gov.cn)',
            '(半导体 OR 集成电路 OR 存储芯片) (site:csrc.gov.cn OR site:sse.com.cn OR site:szse.cn)',
        )
        locale = {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    else:
        queries = (
            'semiconductor OR "memory chip" OR DRAM OR NAND OR HBM (site:semi.org OR site:wsts.org)',
            'semiconductor OR "memory chip" OR DRAM OR NAND OR HBM (site:commerce.gov OR site:bis.gov)',
        )
        locale = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    return tuple(
        f"{GOOGLE_NEWS_RSS}?{urlencode({'q': query, **locale})}" for query in queries
    )


def _text(element: ElementTree.Element | None, name: str) -> str:
    child = element.find(name) if element is not None else None
    return (child.text or "").strip() if child is not None else ""


def _atom_link(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    for link in element.findall("{*}link"):
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return str(link.get("href"))
    first = element.find("{*}link")
    return str(first.get("href", "")) if first is not None else ""


def _rss_timestamp(value: str) -> str:
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def _rss_items(root: ElementTree.Element, region: str, retrieved_at: str) -> list[NewsItem]:
    result: list[NewsItem] = []
    for item in root.findall("./channel/item"):
        source = item.find("source")
        result.append(NewsItem(_text(item, "title"), _text(item, "link"), (source.text or "").strip() if source is not None else "", str(source.get("url", "")) if source is not None else "", _rss_timestamp(_text(item, "pubDate")), retrieved_at, region))
    return result


def _atom_items(root: ElementTree.Element, region: str, retrieved_at: str) -> list[NewsItem]:
    result: list[NewsItem] = []
    for entry in root.findall("{*}entry"):
        source = entry.find("{*}source")
        result.append(NewsItem(_text(entry, "{*}title"), _atom_link(entry), _text(source, "{*}title"), _atom_link(source), _text(entry, "{*}published") or _text(entry, "{*}updated"), retrieved_at, region))
    return result


def _parse_feed(text: str, region: str, retrieved_at: str) -> list[NewsItem]:
    root = ElementTree.fromstring(text)
    if root.tag.endswith("rss"):
        return _rss_items(root, region, retrieved_at)
    if root.tag.endswith("feed"):
        return _atom_items(root, region, retrieved_at)
    raise ValueError("unsupported news feed format")


def _response_text(response: Any) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode("utf-8-sig")
    return str(response.text)


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _trusted_relevant_fresh(item: NewsItem, region: str, retrieved_at: str) -> bool:
    host = (urlparse(item.source_url).hostname or "").lower().rstrip(".")
    if host not in TRUSTED_HOSTS[region]:
        return False
    title = item.title.lower()
    if not any(term.lower() in title for term in RELEVANCE_TERMS[region]):
        return False
    published = _timestamp(item.published_at)
    retrieved = _timestamp(retrieved_at)
    return bool(
        published
        and retrieved
        and timedelta(0) <= retrieved - published <= MAX_AGE
    )


def _canonical_url(value: str) -> str:
    parsed = urlparse(value)
    params = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, urlencode(params, doseq=True), ""))


def _title(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value).lower()


def _duplicate(item: NewsItem, selected: list[NewsItem]) -> bool:
    canonical = _canonical_url(item.article_url)
    normalized = _title(item.title)
    for earlier in selected:
        if canonical and canonical == _canonical_url(earlier.article_url):
            return True
        previous_title = _title(earlier.title)
        if normalized and previous_title and (normalized == previous_title or SequenceMatcher(None, normalized, previous_title).ratio() >= 0.92):
            return True
    return False


def fetch_news(asset: dict[str, Any], region: str, *, session: requests.Session | Any | None = None, feeds: list[str] | tuple[str, ...] | None = None, retrieved_at: str | None = None) -> list[NewsItem]:
    """Fetch asset-query news, keeping exact first selected URLs for traceability."""
    normalized_region = str(region).upper()
    if normalized_region not in {"CN", "INTL"}:
        raise ValueError("region must be CN or INTL")
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat()
    production_discovery = feeds is None
    sources = tuple(feeds) if feeds is not None else default_feeds(asset, normalized_region)
    client = session or requests.Session()
    selected: list[NewsItem] = []
    for feed_url in sources:
        try:
            response = client.get(feed_url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            items = _parse_feed(_response_text(response), normalized_region, timestamp)
        except (requests.RequestException, ElementTree.ParseError, UnicodeError, ValueError):
            continue
        for item in items:
            if (
                item.article_url
                and (not production_discovery or _trusted_relevant_fresh(item, normalized_region, timestamp))
                and not _duplicate(item, selected)
            ):
                selected.append(item)
    if production_discovery:
        selected.sort(key=lambda item: _timestamp(item.published_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return selected[:MAX_ITEMS]
    return selected
