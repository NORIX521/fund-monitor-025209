"""Traceable RSS and Atom news retrieval for research records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import requests


TIMEOUT = (5, 25)
USER_AGENT = "UZI research monitor/1.0 (+https://github.com/openai/uzi-monitor)"
DEFAULT_FEEDS = {
    "CN": (),
    "INTL": (),
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


def _text(element: ElementTree.Element | None, name: str) -> str:
    if element is None:
        return ""
    child = element.find(name)
    return (child.text or "").strip() if child is not None else ""


def _atom_link(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    for link in element.findall("{*}link"):
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return str(link.get("href"))
    first = element.find("{*}link")
    return str(first.get("href", "")) if first is not None else ""


def _rss_items(root: ElementTree.Element, region: str, retrieved_at: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    for item in root.findall("./channel/item"):
        source_element = item.find("source")
        article_url = _text(item, "link")
        source_url = str(source_element.get("url", "")) if source_element is not None else ""
        items.append(
            NewsItem(
                title=_text(item, "title"),
                article_url=article_url,
                source=(source_element.text or "").strip() if source_element is not None else "",
                source_url=source_url,
                published_at=_rss_timestamp(_text(item, "pubDate")),
                retrieved_at=retrieved_at,
                region=region,
            )
        )
    return items


def _rss_timestamp(value: str) -> str:
    """Normalize RFC 822 timestamps while retaining an unparseable source value."""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def _atom_items(root: ElementTree.Element, region: str, retrieved_at: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    for entry in root.findall("{*}entry"):
        source = entry.find("{*}source")
        items.append(
            NewsItem(
                title=_text(entry, "{*}title"),
                article_url=_atom_link(entry),
                source=_text(source, "{*}title"),
                source_url=_atom_link(source),
                published_at=_text(entry, "{*}published") or _text(entry, "{*}updated"),
                retrieved_at=retrieved_at,
                region=region,
            )
        )
    return items


def _parse_feed(text: str, region: str, retrieved_at: str) -> list[NewsItem]:
    root = ElementTree.fromstring(text)
    if root.tag.endswith("rss"):
        return _rss_items(root, region, retrieved_at)
    if root.tag.endswith("feed"):
        return _atom_items(root, region, retrieved_at)
    raise ValueError("unsupported news feed format")


def fetch_news(
    asset: dict[str, Any],
    region: str,
    *,
    session: requests.Session | Any | None = None,
    feeds: list[str] | tuple[str, ...] | None = None,
    retrieved_at: str | None = None,
) -> list[NewsItem]:
    """Fetch and URL-dedupe traceable feed items; never infer news facts."""
    del asset  # Feed selection is intentionally caller-controlled and evidence-only.
    normalized_region = str(region).upper()
    if normalized_region not in DEFAULT_FEEDS:
        raise ValueError("region must be CN or INTL")
    client = session or requests.Session()
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat()
    sources = tuple(feeds) if feeds is not None else DEFAULT_FEEDS[normalized_region]
    seen_urls: set[str] = set()
    results: list[NewsItem] = []
    for feed_url in sources:
        response = client.get(feed_url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        for item in _parse_feed(response.text, normalized_region, timestamp):
            if not item.article_url or item.article_url in seen_urls:
                continue
            seen_urls.add(item.article_url)
            results.append(item)
    return results
