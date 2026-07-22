"""Traceable, allowlisted RSS and Atom news retrieval for research records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    """Build one Google News RSS search URL from the supplied asset identity."""
    if region not in {"CN", "INTL"}:
        raise ValueError("region must be CN or INTL")
    terms = [str(asset.get(key) or "").strip() for key in ("name", "code", "sector")]
    query = " ".join(term for term in terms if term) or "financial research"
    parameters = {"q": query, "hl": "zh-CN" if region == "CN" else "en-US", "gl": "CN" if region == "CN" else "US", "ceid": "CN:zh-Hans" if region == "CN" else "US:en"}
    return (f"{GOOGLE_NEWS_RSS}?{urlencode(parameters)}",)


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
    sources = tuple(feeds) if feeds is not None else default_feeds(asset, normalized_region)
    client = session or requests.Session()
    selected: list[NewsItem] = []
    for feed_url in sources:
        response = client.get(feed_url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        for item in _parse_feed(response.text, normalized_region, timestamp):
            if item.article_url and not _duplicate(item, selected):
                selected.append(item)
    return selected
