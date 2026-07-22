#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "dashboard.json"
NEW_ALERTS_PATH = ROOT / "data" / "new_alerts.json"
FUND_CODE = "025209"
TZ = ZoneInfo("Asia/Shanghai")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Referer": f"https://fundf10.eastmoney.com/jjjz_{FUND_CODE}.html",
}
TIMEOUT = 25


def load_previous() -> dict[str, Any]:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def fetch_fund_history() -> tuple[list[dict[str, Any]], str]:
    payload = fetch_json(
        "https://api.fund.eastmoney.com/f10/lsjz",
        {"fundCode": FUND_CODE, "pageIndex": 1, "pageSize": 180},
    )
    rows = ((payload.get("Data") or {}).get("LSJZList") or [])
    history = []
    for row in rows:
        try:
            history.append({
                "date": row["FSRQ"],
                "nav": float(row["DWJZ"]),
                "change_pct": float(row["JZZZL"]) if row.get("JZZZL") not in (None, "") else None,
            })
        except (KeyError, TypeError, ValueError):
            continue
    history.sort(key=lambda item: item["date"])
    if not history:
        raise RuntimeError("Eastmoney returned no NAV history")
    return history, "live"


def decode_js_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw.replace(r"\"", '"').replace(r"\/", "/").replace(r"\r", "").replace(r"\n", "")


def fetch_holdings() -> tuple[list[dict[str, Any]], str | None]:
    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    response = requests.get(url, params={"type": "jjcc", "code": FUND_CODE, "topline": 10, "year": "", "month": ""}, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    text = response.text
    match = re.search(r'content:"(.*)",arryear:', text, flags=re.S)
    content = decode_js_string(match.group(1)) if match else text
    content = html.unescape(content)
    soup = BeautifulSoup(content, "html.parser")
    report_text = soup.get_text(" ", strip=True)
    report_match = re.search(r"(20\d{2})年(?:第)?([一二三四1234])季度", report_text)
    report_date = None
    if report_match:
        qmap = {"一": "1", "二": "2", "三": "3", "四": "4"}
        report_date = f"{report_match.group(1)} Q{qmap.get(report_match.group(2), report_match.group(2))}"

    holdings: list[dict[str, Any]] = []
    for tr in soup.select("tbody tr, table tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.select("td")]
        if len(cells) < 3:
            continue
        code = next((m.group(0) for cell in cells for m in [re.search(r"(?<!\d)\d{6}(?!\d)", cell)] if m), None)
        if not code:
            continue
        name = None
        for link in tr.select("a"):
            label = link.get_text(" ", strip=True)
            if label and not re.fullmatch(r"\d{6}", label) and not re.fullmatch(r"\d+(?:\.\d+)?%?", label):
                name = label
        if not name:
            code_index = next((i for i, cell in enumerate(cells) if code in cell), 0)
            name = cells[code_index + 1] if code_index + 1 < len(cells) else code
        percentages = []
        for cell in cells:
            m = re.fullmatch(r"(-?\d+(?:\.\d+)?)%", cell)
            if m:
                percentages.append(float(m.group(1)))
        weight = percentages[-1] if percentages else None
        if not any(item["code"] == code for item in holdings):
            holdings.append({"code": code, "name": name, "weight_pct": weight, "change_pct": None})
        if len(holdings) >= 10:
            break
    if not holdings:
        raise RuntimeError("Could not parse fund holdings")
    return holdings, report_date


def eastmoney_secid(code: str) -> str:
    return f"1.{code}" if code.startswith(("5", "6", "9")) else f"0.{code}"


def enrich_stock_quotes(holdings: list[dict[str, Any]]) -> None:
    secids = ",".join(eastmoney_secid(item["code"]) for item in holdings)
    payload = fetch_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        {"secids": secids, "fields": "f12,f14,f2,f3,f4,f5,f6,f8,f15,f16,f17,f18", "fltt": 2, "invt": 2},
    )
    rows = ((payload.get("data") or {}).get("diff") or [])
    quotes = {str(row.get("f12")): row for row in rows}
    for item in holdings:
        quote = quotes.get(item["code"], {})
        value = quote.get("f3")
        item["change_pct"] = float(value) if isinstance(value, (int, float)) else None
        item["latest_price"] = quote.get("f2") if isinstance(quote.get("f2"), (int, float)) else None


def extract_range(text: str, keyword: str) -> list[int] | None:
    patterns = [
        rf"{keyword}.{{0,100}}?(?:季增|上涨|上升|increase(?:s|d)?(?: by)?)[^\d-]*?(\d+)\s*[-–—~至]\s*(\d+)\s*%",
        rf"{keyword}.{{0,100}}?(\d+)\s*[-–—~至]\s*(\d+)\s*%",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return [int(match.group(1)), int(match.group(2))]
    return None


def trend_label(current: list[int] | None, previous: list[int] | None) -> str:
    if not current:
        return "暂无量化区间"
    midpoint = sum(current) / 2
    if midpoint < 0:
        return "价格转为下跌"
    if not previous:
        return "价格仍处上行"
    previous_midpoint = sum(previous) / 2
    if midpoint <= previous_midpoint - 8:
        return "价格仍涨，涨幅显著收敛"
    if midpoint <= previous_midpoint - 3:
        return "价格仍涨，涨幅温和收敛"
    if midpoint >= previous_midpoint + 5:
        return "涨价斜率再次增强"
    return "涨价斜率大致持平"


def fetch_storage_signal(previous: dict[str, Any]) -> dict[str, Any]:
    bases = ["https://www.trendforce.cn/presscenter", "https://www.trendforce.com/presscenter"]
    selected_url = None
    selected_title = None
    for base in bases:
        try:
            response = requests.get(base, headers=HEADERS, timeout=TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            candidates = []
            for link in soup.select('a[href*="/presscenter/news/"]'):
                title = " ".join(link.get_text(" ", strip=True).split())
                href = link.get("href")
                if href and re.search(r"DRAM|NAND|存储|記憶|memory", title, flags=re.I):
                    candidates.append((urljoin(base, href), title))
            if candidates:
                selected_url, selected_title = candidates[0]
                break
        except Exception:
            continue
    if not selected_url:
        raise RuntimeError("No TrendForce memory article found")

    article = requests.get(selected_url, headers=HEADERS, timeout=TIMEOUT)
    article.raise_for_status()
    soup = BeautifulSoup(article.text, "html.parser")
    article_text = " ".join(soup.get_text(" ", strip=True).split())
    dram = extract_range(article_text, r"(?:一般型|Conventional\s+)?DRAM")
    nand = extract_range(article_text, r"NAND(?:\s+Flash)?")
    old_storage = previous.get("storage") or {}
    dram_trend = trend_label(dram, old_storage.get("dram_qoq_range"))
    nand_trend = trend_label(nand, old_storage.get("nand_qoq_range"))
    combined = f"{dram_trend}；{nand_trend}。"
    if "下跌" in combined:
        signal = "产业价格拐点转弱"
    elif "显著收敛" in combined:
        signal = "高景气但斜率放缓"
    elif "增强" in combined:
        signal = "涨价动能增强"
    else:
        signal = "景气延续"
    return {
        "period": datetime.now(TZ).strftime("%Y-%m"),
        "dram_qoq_range": dram or old_storage.get("dram_qoq_range"),
        "nand_qoq_range": nand or old_storage.get("nand_qoq_range"),
        "dram_trend": dram_trend,
        "nand_trend": nand_trend,
        "signal": signal,
        "summary": f"最新行业信息显示：{combined}存储价格方向仍需与终端需求、库存和新增供给共同验证。",
        "source_title": selected_title,
        "source_url": selected_url,
    }


def make_alerts(data: dict[str, Any], previous: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    alerts: list[dict[str, Any]] = []
    history = data["fund"]["history"]
    latest = history[-1]
    nav = latest["nav"]
    peak = data["fund"]["peak_nav"]
    drawdown = (nav / peak - 1) * 100
    date = latest["date"]
    if 2.25 <= nav <= 2.35:
        alerts.append({"id": f"watch-{date}", "level": "warning", "title": "净值进入2.25–2.35观察区", "message": f"最新净值 {nav:.4f}，进入重点观察区。", "date": date})
    if nav < 2.25:
        alerts.append({"id": f"break-225-{date}", "level": "danger", "title": "净值跌破2.25元", "message": f"最新净值 {nav:.4f}，低于风险线。", "date": date})
    last_three = [row["nav"] for row in history[-3:]]
    if nav >= 2.50 and len(last_three) == 3 and last_three[0] < last_three[1] < last_three[2]:
        alerts.append({"id": f"recover-250-{date}", "level": "info", "title": "净值站上2.50元并连续修复", "message": f"最近三期净值连续回升，最新为 {nav:.4f}。", "date": date})
    if drawdown <= -30:
        alerts.append({"id": f"dd30-{date}", "level": "danger", "title": "回撤扩大至30%", "message": f"相对 {peak:.4f} 高点回撤 {drawdown:.2f}%。", "date": date})
    elif drawdown <= -25:
        alerts.append({"id": f"dd25-{date}", "level": "warning", "title": "回撤扩大至25%", "message": f"相对 {peak:.4f} 高点回撤 {drawdown:.2f}%。", "date": date})

    changes = [x.get("change_pct") for x in data.get("holdings", []) if isinstance(x.get("change_pct"), (int, float))]
    if changes:
        up = sum(v > 0 for v in changes)
        down = sum(v < 0 for v in changes)
        avg = sum(changes) / len(changes)
        if up >= 7 and avg >= 2:
            alerts.append({"id": f"breadth-up-{date}", "level": "info", "title": "重仓股集体反弹", "message": f"前十大重仓股 {up} 只上涨，平均涨幅 {avg:.2f}%。", "date": date})
        if down >= 7 and avg <= -2:
            alerts.append({"id": f"breadth-down-{date}", "level": "danger", "title": "重仓股集体走弱", "message": f"前十大重仓股 {down} 只下跌，平均跌幅 {avg:.2f}%。", "date": date})

    storage_signal = data.get("storage", {}).get("signal", "")
    old_signal = previous.get("storage", {}).get("signal", "")
    if storage_signal and storage_signal != old_signal and any(key in storage_signal for key in ("转弱", "放缓", "增强")):
        alerts.append({"id": f"storage-{re.sub(r'[^a-zA-Z0-9]+','-',storage_signal)}-{date}", "level": "warning" if "放缓" in storage_signal else "danger" if "转弱" in storage_signal else "info", "title": f"存储信号：{storage_signal}", "message": data.get("storage", {}).get("summary", "行业信号发生变化。"), "date": date})

    old_ids = {a.get("id") for a in previous.get("alerts", []) if a.get("id")}
    new_alerts = [a for a in alerts if a.get("id") not in old_ids]
    return alerts, new_alerts


def main() -> int:
    previous = load_previous()
    data = previous.copy() if previous else {}
    errors = []
    mode = "live"

    try:
        history, _ = fetch_fund_history()
        peak_nav = max(max(row["nav"] for row in history), 3.0864)
        data["fund"] = {"code": FUND_CODE, "name": "永赢先锋半导体智选混合发起C", "peak_nav": peak_nav, "history": history}
    except Exception as exc:
        errors.append(f"fund_history: {exc}")
        mode = "cached"

    try:
        holdings, report_date = fetch_holdings()
        try:
            enrich_stock_quotes(holdings)
        except Exception as exc:
            errors.append(f"stock_quotes: {exc}")
        data["holdings"] = holdings
        data["holding_report_date"] = report_date
    except Exception as exc:
        errors.append(f"holdings: {exc}")
        mode = "cached"

    try:
        data["storage"] = fetch_storage_signal(previous)
    except Exception as exc:
        errors.append(f"storage: {exc}")
        mode = "cached" if mode == "cached" else "partial"

    if not data.get("fund", {}).get("history"):
        print("No fund history available; refusing to overwrite dashboard.", file=sys.stderr)
        return 1

    alerts, new_alerts = make_alerts(data, previous)
    data["alerts"] = alerts
    data["generated_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    data["status"] = {"data_mode": mode, "message": "; ".join(errors) if errors else "All data sources updated."}
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    NEW_ALERTS_PATH.write_text(json.dumps(new_alerts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": mode, "new_alerts": len(new_alerts), "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
