#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CoinGlass 网页端 netflow 抓取器（绕过 capi 加密响应）— Python 版。

思路：用真实浏览器渲染 https://www.coinglass.com/InflowAndOutflow，
     前端 JS 自动解密并渲染到 DOM，直接读 DOM 精准数值（aria-label）。

与 JS 版（cg_netflow_scraper.js）等价，改用 Python Playwright，
复用容器已有的 playwright + chromium（无需 Node.js / 本机 Chrome）。

输出：结构化 JSON（主表逐笔 + 链上告警 + 按交易所/币种聚合净流）
  同时打印到 stdout（可被管道消费）并写入 cg_netflow_latest.json

用法：
  python cg_netflow_scraper.py [--no-write]

环境变量：
  CG_URL        - 覆盖默认 CoinGlass 页面 URL（离线测试用）
  CHROME_EXEC   - 覆盖 chromium 可执行文件路径（不设则用 playwright 自带）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
URL = os.environ.get("CG_URL") or "https://www.coinglass.com/InflowAndOutflow"
CHROME_EXEC = os.environ.get("CHROME_EXEC") or None

BACKOFF = [0, 30, 60]  # 秒；首次不睡，之后 30s / 60s 冷却
WAIT_STABLE_MAX = 45000  # 等待稳定最长 45s
WAIT_STABLE_TICK = 2000  # 每 2s 检查一次
WAIT_STABLE_THRESHOLD = 3  # 连续 3 次不变 => 渲染完成


def _num_attr(el) -> float | None:
    """从 DOM 元素中提取 .Number 的 aria-label 精确值。"""
    if el is None:
        return None
    n = el.query_selector(".Number")
    if n is None:
        return None
    label = n.get_attribute("aria-label")
    if not label:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", label)
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_page(page) -> dict:
    """解析主表 + 告警表。"""
    tables = page.query_selector_all("table")
    main, alert = [], []

    for t in tables:
        headers = [th.inner_text().strip() for th in t.query_selector_all("thead th")]
        is_main = "Side" in headers and "Exchanges" in headers
        is_alert = "From" in headers and "To" in headers
        
        # 调试日志
        print(f"[parse] table headers={headers[:5]}, is_main={is_main}, is_alert={is_alert}", file=sys.stderr)

        for r in t.query_selector_all('tr[data-row-key]'):
            tds = r.query_selector_all("td")
            sym_el = r.query_selector(".symbol-name")
            if sym_el is not None:
                symbol = sym_el.inner_text().strip()
            elif tds:
                symbol = re.sub(r"\s+", " ", tds[0].inner_text()).strip()
            else:
                symbol = ""

            if is_main:
                main.append({
                    "symbol": symbol,
                    "exchange": tds[1].inner_text().strip() if len(tds) > 1 else "",
                    "side": tds[2].inner_text().strip() if len(tds) > 2 else "",
                    "qty": _num_attr(tds[3]) if len(tds) > 3 else None,
                    "qty_display": tds[3].inner_text().strip() if len(tds) > 3 else "",
                    "value": _num_attr(tds[4]) if len(tds) > 4 else None,
                    "value_display": tds[4].inner_text().strip() if len(tds) > 4 else "",
                    "time": tds[5].inner_text().strip() if len(tds) > 5 else "",
                })
            elif is_alert:
                alert.append({
                    "symbol": symbol,
                    "from": tds[1].inner_text().strip() if len(tds) > 1 else "",
                    "to": tds[2].inner_text().strip() if len(tds) > 2 else "",
                    "qty": _num_attr(tds[3]) if len(tds) > 3 else None,
                    "qty_display": tds[3].inner_text().strip() if len(tds) > 3 else "",
                    "time": tds[4].inner_text().strip() if len(tds) > 4 else "",
                })
    
    print(f"[parse] main_rows={len(main)}, alert_rows={len(alert)}", file=sys.stderr)
    return {"main": main, "alert": alert}


def aggregate_netflow(main_rows: list[dict]) -> dict:
    """按 (exchange, symbol) 聚合净流。"""
    agg: dict[str, dict] = {}
    for r in main_rows:
        side = (r.get("side") or "").lower()
        if side not in ("inflow", "outflow"):
            continue
        key = f"{r.get('exchange')}||{r.get('symbol')}"
        a = agg.setdefault(key, {
            "exchange": r.get("exchange"), "symbol": r.get("symbol"),
            "inflow_qty": 0.0, "outflow_qty": 0.0, "inflow_usd": 0.0, "outflow_usd": 0.0,
            "tx": 0,
        })
        a["tx"] += 1
        if side == "inflow":
            a["inflow_qty"] += r.get("qty") or 0
            a["inflow_usd"] += r.get("value") or 0
        else:
            a["outflow_qty"] += r.get("qty") or 0
            a["outflow_usd"] += r.get("value") or 0

    netflow = []
    for a in agg.values():
        netflow.append({
            "exchange": a["exchange"], "symbol": a["symbol"], "tx": a["tx"],
            "inflow_qty": round(a["inflow_qty"], 4), "outflow_qty": round(a["outflow_qty"], 4),
            "net_qty": round(a["inflow_qty"] - a["outflow_qty"], 4),
            "inflow_usd": round(a["inflow_usd"], 2), "outflow_usd": round(a["outflow_usd"], 2),
            "net_usd": round(a["inflow_usd"] - a["outflow_usd"], 2),
        })
    netflow.sort(key=lambda x: x["net_usd"], reverse=True)
    return netflow


def wait_stable(page) -> int:
    """等待数据行数连续 3 次（~6s）不变才返回，避免首屏提前退出。"""
    last = -1
    stable = 0
    waited = 0
    while waited < WAIT_STABLE_MAX:
        rc = page.evaluate("() => document.querySelectorAll('tr[data-row-key]').length")
        if rc > 0 and rc == last:
            stable += 1
        else:
            stable = 0
        last = rc
        if rc > 0 and stable >= WAIT_STABLE_THRESHOLD:
            return rc
        page.wait_for_timeout(WAIT_STABLE_TICK)
        waited += WAIT_STABLE_TICK
    return last


def write_output(out: dict, no_write: bool) -> None:
    json_str = json.dumps(out, ensure_ascii=False, indent=2)
    if not no_write:
        fp = SCRIPT_DIR / "cg_netflow_latest.json"
        fp.write_text(json_str, encoding="utf-8")
        print(f"[written] {fp}", file=sys.stderr)
    print(json_str)


def run(no_write: bool = False) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_EXEC,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--no-proxy-server",
            ],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
            extra_http_headers={
                "accept-language": "zh-CN,zh;q=0.9",
                "referer": "https://www.coinglass.com/",
            },
        )
        page = context.new_page()
        
        print(f"[fetch] 访问 {URL}", file=sys.stderr)
        resp = page.goto(URL, wait_until="load", timeout=60000)
        status = resp.status if resp else None
        print(f"[fetch] HTTP={status}", file=sys.stderr)
        row_count = wait_stable(page)
        print(f"[fetch] 初始行数={row_count}", file=sys.stderr)

        # 空表兜底：reload + 冷却退避（capi 高频请求会节流，立即连 reload 越打越死）
        tries = 0
        while row_count == 0 and tries < len(BACKOFF):
            if tries > 0:
                print(f"[retry {tries}] 数据行 0，冷却 {BACKOFF[tries]}s 后 reload…", file=sys.stderr)
                page.wait_for_timeout(BACKOFF[tries] * 1000)
            page.reload(wait_until="load", timeout=60000)
            row_count = wait_stable(page)
            tries += 1

        if row_count == 0:
            diag = page.evaluate(
                "() => ({ title: document.title, bodyLen: document.body.innerText.length,"
                " hasAntTable: !!document.querySelector('.ant-table'),"
                " bodyHead: document.body.innerText.slice(0, 400) })"
            )
            print(f"FATAL: 数据行未渲染。HTTP={status} {json.dumps(diag, ensure_ascii=False)}", file=sys.stderr)
            # 写降级 JSON（消费侧正确降级，而非读旧数据）
            degraded = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": URL,
                "http_status": status,
                "main_rows": 0,
                "alert_rows": 0,
                "main_table": [],
                "alert_history": [],
                "netflow_by_exchange_coin": [],
                "summary": {"total_inflow_usd": 0, "total_outflow_usd": 0, "net_usd": 0, "distinct_coins": 0},
                "degraded": True,
                "degrade_reason": "数据行未渲染（可能限流）",
            }
            if not no_write:
                (SCRIPT_DIR / "cg_netflow_latest.json").write_text(
                    json.dumps(degraded, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[written degraded] {SCRIPT_DIR / 'cg_netflow_latest.json'}", file=sys.stderr)
            browser.close()
            return 2

        parsed = parse_page(page)
        browser.close()

    netflow = aggregate_netflow(parsed["main"])
    total_in = sum((r.get("value") or 0) for r in parsed["main"]
                   if (r.get("side") or "").lower() == "inflow")
    total_out = sum((r.get("value") or 0) for r in parsed["main"]
                    if (r.get("side") or "").lower() == "outflow")

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "http_status": status,
        "main_rows": len(parsed["main"]),
        "alert_rows": len(parsed["alert"]),
        "main_table": parsed["main"],
        "alert_history": parsed["alert"],
        "netflow_by_exchange_coin": netflow,
        "summary": {
            "total_inflow_usd": round(total_in, 2),
            "total_outflow_usd": round(total_out, 2),
            "net_usd": round(total_in - total_out, 2),
            "distinct_coins": len(netflow),
        },
    }

    # 页面渲染出行但无主表数据（数据 XHR 被节流/未加载）=> 同样降级，避免消费侧误读
    if len(parsed["main"]) == 0:
        out["degraded"] = True
        out["degrade_reason"] = "主表无数据行（数据源限流或未加载）"

    write_output(out, no_write)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CoinGlass netflow 抓取器（Python 版）")
    parser.add_argument("--no-write", action="store_true", help="不写文件，仅 stdout")
    args = parser.parse_args()
    try:
        return run(no_write=args.no_write)
    except Exception as e:
        print(f"FATAL {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())