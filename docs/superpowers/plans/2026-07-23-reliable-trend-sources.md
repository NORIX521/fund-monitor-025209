# Reliable Trend Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace empty or low-quality domestic/international trend streams with traceable, recent records from an official source pool.

**Architecture:** Keep the existing `NewsItem` and pipeline contracts. Make the default news provider build localized multi-feed queries, directly read the MIIT operating-data list for a domestic first-party fallback, and gate production items by trusted host, relevance, freshness, deduplication, and a six-item limit. Explicit test feeds remain isolated from network-only production additions.

**Tech Stack:** Python 3.12, requests 2.32.4, BeautifulSoup4 4.13.4, pytest 8.4.1, GitHub Actions, GitHub Pages.

## Global Constraints

- Every displayed record must contain article URL, publisher URL, published timestamp, retrieval timestamp, and region.
- Domestic trusted hosts are limited to MIIT, NDRC, gov.cn, CSRC, SSE, and SZSE hostnames listed in the design.
- International trusted hosts are limited to SEMI, WSTS, U.S. Commerce, and U.S. BIS hostnames listed in the design.
- Default production items must be no older than 120 days and must match sector relevance terms.
- A failed feed must not discard successful results from another feed.
- An empty refresh must preserve the previous trusted stream through the existing pipeline behavior.
- No API key may be stored in frontend files or the repository.

---

### Task 1: Localized queries and production trust gate

**Files:**
- Modify: `scripts/providers/news.py`
- Create: `tests/python/test_news_provider.py`

**Interfaces:**
- Consumes: `asset: dict[str, Any]`, `region: str`, RSS/Atom `NewsItem` values.
- Produces: `default_feeds(asset, region) -> tuple[str, ...]`, `_trusted_item(item, region, reference_time) -> bool`, and `fetch_news(...) -> list[NewsItem]`.

- [ ] **Step 1: Write failing tests for localized queries, trusted hosts, freshness, relevance, per-feed isolation, sorting, and limit**

```python
def test_international_default_feed_uses_english_sector_terms_not_chinese_fund_name():
    feeds = default_feeds(FUND, "INTL")
    decoded = " ".join(unquote(url) for url in feeds)
    assert "semiconductor" in decoded
    assert "DRAM" in decoded
    assert FUND["name"] not in decoded


def test_default_fetch_filters_untrusted_stale_and_irrelevant_items_and_survives_one_feed_failure():
    session = SequencedSession([
        requests.ConnectionError("first feed failed"),
        rss_response([
            rss_item("SEMI memory equipment outlook", "https://news.google.com/1", "SEMI", "https://www.semi.org", "Tue, 14 Jul 2026 07:00:00 GMT"),
            rss_item("Untrusted semiconductor post", "https://news.google.com/2", "Blog", "https://blog.example", "Tue, 14 Jul 2026 07:00:00 GMT"),
            rss_item("Unrelated official item", "https://news.google.com/3", "SEMI", "https://www.semi.org", "Tue, 14 Jul 2026 07:00:00 GMT"),
            rss_item("Old semiconductor item", "https://news.google.com/4", "SEMI", "https://www.semi.org", "Mon, 01 Jan 2024 07:00:00 GMT"),
        ]),
    ])
    items = fetch_news(FUND, "INTL", session=session, retrieved_at="2026-07-23T00:00:00+00:00")
    assert [item.title for item in items] == ["SEMI memory equipment outlook"]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/python/test_news_provider.py -q`

Expected: failures showing the international query still contains the Chinese fund name and untrusted/stale items are not filtered.

- [ ] **Step 3: Implement localized query building and trust gate**

```python
TRUSTED_HOSTS = {
    "CN": frozenset({"www.miit.gov.cn", "wap.miit.gov.cn", "www.ndrc.gov.cn", "zfxxgk.ndrc.gov.cn", "www.gov.cn", "www.csrc.gov.cn", "www.sse.com.cn", "www.szse.cn"}),
    "INTL": frozenset({"semi.org", "www.semi.org", "wsts.org", "www.wsts.org", "commerce.gov", "www.commerce.gov", "bis.gov", "www.bis.gov"}),
}
RELEVANCE_TERMS = {
    "CN": ("半导体", "集成电路", "存储芯片", "芯片", "电子信息制造业"),
    "INTL": ("semiconductor", "memory chip", "dram", "nand", "hbm", "chip"),
}

def _sector_query(asset: dict[str, Any], region: str) -> str:
    sector = str(asset.get("sector") or "")
    if "半导体" in sector or "存储" in sector:
        return "半导体 集成电路 存储芯片" if region == "CN" else "semiconductor memory chip DRAM NAND HBM"
    if region == "CN":
        return " ".join(value for value in (str(asset.get("name") or "").strip(), str(asset.get("code") or "").strip(), sector.strip()) if value)
    ascii_terms = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9 .&+-]*", " ".join((str(asset.get("name") or ""), str(asset.get("code") or ""), sector))))
    return ascii_terms.strip() or "global financial markets"
```

Implement `_trusted_item` using exact source host, parsed aware timestamp, 120-day cutoff, and case-insensitive title-term matching. Catch request/XML errors per feed, keep successful feeds, sort newest first, deduplicate, and return at most six.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/python/test_news_provider.py -q`

Expected: all provider tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add scripts/providers/news.py tests/python/test_news_provider.py
git commit -m "feat: gate trend feeds by trusted sources"
```

### Task 2: Direct MIIT domestic official-source adapter

**Files:**
- Modify: `scripts/providers/news.py`
- Modify: `tests/python/test_news_provider.py`

**Interfaces:**
- Consumes: `requests.Session`, MIIT list HTML, relevant article HTML, retrieval timestamp.
- Produces: `_fetch_miit_items(client, retrieved_at) -> list[NewsItem]` with direct MIIT article URLs.

- [ ] **Step 1: Write a failing MIIT parsing and isolation test**

```python
def test_default_cn_fetch_adds_direct_miit_operating_update_with_article_timestamp():
    session = RouteSession({
        MIIT_INDEX_URL: html_response('<a href="/jgsj/yxj/xxfb/art/2026/a.html">2026年1—5月电子信息制造业运行情况</a>'),
        "https://wap.miit.gov.cn/jgsj/yxj/xxfb/art/2026/a.html": html_response('<meta name="ArticleTitle" content="2026年1—5月电子信息制造业运行情况"><meta name="PubDate" content="2026-07-01 14:27">'),
    }, default=rss_response([]))
    items = fetch_news(FUND, "CN", session=session, retrieved_at="2026-07-23T00:00:00+00:00")
    miit = [item for item in items if item.source == "工业和信息化部"]
    assert miit[0].article_url == "https://wap.miit.gov.cn/jgsj/yxj/xxfb/art/2026/a.html"
    assert miit[0].published_at == "2026-07-01T14:27:00+08:00"
```

- [ ] **Step 2: Run the MIIT test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/python/test_news_provider.py::test_default_cn_fetch_adds_direct_miit_operating_update_with_article_timestamp -q`

Expected: FAIL because `_fetch_miit_items` and the direct record do not exist.

- [ ] **Step 3: Implement the MIIT adapter**

```python
MIIT_INDEX_URL = "https://wap.miit.gov.cn/jgsj/yxj/index.html"
MIIT_SOURCE_URL = "https://www.miit.gov.cn"

def _fetch_miit_items(client: Any, retrieved_at: str) -> list[NewsItem]:
    response = client.get(MIIT_INDEX_URL, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    result: list[NewsItem] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href*="/jgsj/yxj/xxfb/art/"]'):
        title = anchor.get_text(" ", strip=True)
        article_url = urljoin(MIIT_INDEX_URL, anchor.get("href", ""))
        if article_url in seen or not any(term in title for term in RELEVANCE_TERMS["CN"]):
            continue
        seen.add(article_url)
        article = client.get(article_url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        article.raise_for_status()
        article_soup = BeautifulSoup(article.text, "html.parser")
        title_meta = article_soup.select_one('meta[name="ArticleTitle"]')
        date_meta = article_soup.select_one('meta[name="PubDate"]')
        published = str(date_meta.get("content", "")).strip() if date_meta else ""
        if not published:
            continue
        published_at = datetime.strptime(published, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
        result.append(NewsItem(str(title_meta.get("content", title)).strip() if title_meta else title, article_url, "工业和信息化部", MIIT_SOURCE_URL, published_at, retrieved_at, "CN"))
        if len(result) == 3:
            break
    return result
```

Call the adapter only when `feeds is None and region == "CN"`; catch its errors so RSS results still survive.

- [ ] **Step 4: Run all provider tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/python/test_news_provider.py -q`

Expected: all provider tests pass, including a case where MIIT fails but trusted RSS succeeds.

- [ ] **Step 5: Commit Task 2**

```powershell
git add scripts/providers/news.py tests/python/test_news_provider.py
git commit -m "feat: add MIIT trend fallback"
```

### Task 3: Refresh, deploy, and verify both streams

**Files:**
- Modify: `data/assets/fund-cn-025209.json`
- Modify: `data/dashboard.json`
- Modify: `C:\Users\智汇云\Documents\日常工作\基金监控网站\03-开发记录.md`
- Modify: `C:\Users\智汇云\Documents\日常工作\基金监控网站\04-踩坑日志.md`
- Modify: `C:\Users\智汇云\Documents\日常工作\基金监控网站\05-项目SOP.md`

**Interfaces:**
- Consumes: production `fetch_news`, current watchlist, current verified data, GitHub Actions/Pages.
- Produces: refreshed trend JSON, deployed site, recorded verification evidence.

- [ ] **Step 1: Run the focused and full local gates**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm test
.\.venv\Scripts\python.exe scripts\validate_outputs.py data
.\.venv\Scripts\python.exe -c "import pathlib,yaml; yaml.safe_load(pathlib.Path('.github/workflows/update-and-deploy.yml').read_text(encoding='utf-8')); print('YAML_OK')"
```

Expected: Python and Node tests pass, then `OUTPUTS_OK` and `YAML_OK`.

- [ ] **Step 2: Run a real provider probe before changing production data**

```powershell
@'
from urllib.parse import urlparse
from scripts.providers.news import TRUSTED_HOSTS, fetch_news
asset = {"name": "永赢先锋半导体智选混合发起C", "code": "025209", "sector": "半导体/存储", "market": "CN", "asset_type": "fund"}
for region in ("CN", "INTL"):
    items = fetch_news(asset, region)
    print(region, len(items))
    assert items
    for item in items:
        host = (urlparse(item.source_url).hostname or "").lower()
        print(item.published_at, host, item.title)
        assert host in TRUSTED_HOSTS[region]
'@ | .\.venv\Scripts\python.exe -
```

Expected: both streams contain at least one recent trusted item.

- [ ] **Step 3: Refresh production data and validate it**

```powershell
.\.venv\Scripts\python.exe scripts\update_monitor.py --watchlist data\watchlist.json --data-dir data --uzi-cache C:\Users\智汇云\Documents\A股选股策略\tools\UZI-Skill\skills\deep-analysis\scripts\.cache --stage prepare
.\.venv\Scripts\python.exe scripts\validate_outputs.py data
```

Expected: `data/assets/fund-cn-025209.json` contains non-empty `news.CN` and `news.INTL`; both news source statuses are fresh.

- [ ] **Step 4: Commit refreshed data and project documentation**

```powershell
git add data/assets/fund-cn-025209.json data/dashboard.json
git commit -m "data: publish trusted trend streams"
```

Use `apply_patch` to record root cause, sources, tests, deployment run, and remaining limitations in the project workspace documents.

- [ ] **Step 5: Replace the young conflicting full refresh safely**

Cancel Actions run `29971600460` before pushing because it started from the pre-fix commit and would otherwise block or later conflict with the new data commit. Confirm its status is cancelled, then push `main`. After the push Pages run succeeds, trigger a new `workflow_dispatch depth=lite`; the prior run had only a short amount of work and had not reached its save checkpoint.

- [ ] **Step 6: Verify GitHub Pages and live JSON**

Check the push Actions run conclusion, Pages `build_type=workflow`, HTTPS enforcement, HTTP 200, cache-busted live JSON counts/source hosts, and absence of `no_reliable_update` for both news streams.

- [ ] **Step 7: Verify rendered UI in a fresh browser session**

Confirm both trend columns display linked records with timestamps, the old empty-state copy is absent for the current asset, and site console error/warn is empty. Finalize browser tabs after inspection.

- [ ] **Step 8: Final repository check**

Run `git status --short` and `git diff --check`.

Expected: clean repository and no whitespace errors.
