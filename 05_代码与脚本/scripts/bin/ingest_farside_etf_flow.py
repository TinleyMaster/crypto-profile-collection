#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Farside Investors ETF 日频资金流入库（BTC/ETH 全历史）。

背景（2026-10-03 投研）：cryptoetf.today 免费档只返回最近 30 天窗口
（windowDays=30），无法补齐 ETF 上市以来历史。Farside 是行业标准 ETF
资金流数据源，all-data 页面含自上市日起逐日净流（BTC 2024-01-11、
ETH 2024-07-23）。本脚本用 cloudscraper 突破 Cloudflare 反爬，解析
HTML 表格 upsert 到 biz.etf_flow_daily（source_code='farside'，
与 cryptoetf 源按主键 (symbol, flow_date, source_code) 共存）。

数据口径：
  - 页面净流单位 US$m（百万美元）；入库转完整 USD（net_flow_usd = m × 1e6）
  - 负数显示为括号："(123.4)" → -123.4；"-" = 该基金未上市/无数据 → NULL
  - 只取 Total 列（全部基金当日净流合计，ETH 表 Total 已含质押类 ETHB）
  - aum / inflow / outflow 该页面不提供，留空

用法：
    python ingest_farside_etf_flow.py             # 抓取 + upsert（幂等可重跑）
    python ingest_farside_etf_flow.py --dry-run   # 预览不写入
    python ingest_farside_etf_flow.py --cross-check  # 与 cryptoetf 源重叠期交叉验证
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

import cloudscraper

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# ── 页面与表结构 ──────────────────────────────────────────────
PAGES = {
    "BTC": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "ETH": "https://farside.co.uk/ethereum-etf-flow-all-data/",
}
TOTAL_COL_IDX = {"BTC": 13, "ETH": 12}   # 表头中 Total 所在列（2026-10-03 验证）

UPSERT_SQL = """
INSERT INTO biz.etf_flow_daily (
    symbol, flow_date, net_flow_usd, net_flow_usd_m, aum_usd,
    total_inflow_usd, total_outflow_usd, source_code, fetched_at, updated_at
) VALUES (
    %(symbol)s, %(flow_date)s, %(net_flow_usd)s, %(net_flow_usd_m)s, NULL,
    NULL, NULL, 'farside', NOW(), NOW()
)
ON CONFLICT (symbol, flow_date, source_code) DO UPDATE SET
    net_flow_usd = EXCLUDED.net_flow_usd,
    net_flow_usd_m = EXCLUDED.net_flow_usd_m,
    updated_at = NOW()
"""

CROSSCHECK_SQL = """
SELECT f.flow_date, f.net_flow_usd_m AS farside_m, c.net_flow_usd_m AS cryptoetf_m
FROM biz.etf_flow_daily f
JOIN biz.etf_flow_daily c
  ON c.symbol = f.symbol AND c.flow_date = f.flow_date AND c.source_code = 'cryptoetf'
WHERE f.source_code = 'farside' AND f.symbol = %s
ORDER BY f.flow_date
"""


def parse_flow_value(s: str) -> float | None:
    """解析 Farside 表格数值。'(123.4)'→-123.4；'-'/空→None；去掉千分位与星号。"""
    s = s.strip()
    if not s or s == "-":
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace(",", "").replace("*", "").replace("$", "").replace("%", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_date_cell(s: str) -> date | None:
    """解析 '11 Jan 2024' 格式日期；非日期行（Fee/Seed/Total/Average…）返回 None。"""
    s = s.strip()
    try:
        return datetime.strptime(s, "%d %b %Y").date()
    except ValueError:
        return None


def fetch_table_rows(scraper, url: str) -> list[list[str]]:
    """抓取页面并返回所有 <tr> 的单元格文本（去 HTML 标签）。"""
    resp = scraper.get(url, timeout=30)
    resp.raise_for_status()
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if cells:
            rows.append(cells)
    return rows


def extract_flows(rows: list[list[str]], total_idx: int) -> list[dict]:
    """从表格行提取 {flow_date, net_flow_usd_m}。Total 列越界/无日期行跳过。"""
    out: list[dict] = []
    for cells in rows:
        if len(cells) <= total_idx:
            continue
        d = parse_date_cell(cells[0])
        if d is None:
            continue
        total_m = parse_flow_value(cells[total_idx])
        if total_m is None:
            # Total 为 '-' 罕见（当日全无数据），跳过避免污染
            continue
        out.append({"flow_date": d, "net_flow_usd_m": total_m})
    out.sort(key=lambda r: r["flow_date"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Farside ETF 日频资金流入库（BTC/ETH 全历史）")
    ap.add_argument("--dry-run", action="store_true", help="预览不写入")
    ap.add_argument("--cross-check", action="store_true",
                    help="与 cryptoetf 源重叠期交叉验证（打印差异）")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False})

    print("Farside ETF 资金流入库（全历史）")
    print(f"dry-run: {args.dry_run} | cross-check: {args.cross_check}")
    print("=" * 60)

    with get_connection(settings.database_url) as conn:
        for symbol, url in PAGES.items():
            rows = fetch_table_rows(scraper, url)
            flows = extract_flows(rows, TOTAL_COL_IDX[symbol])
            if not flows:
                print(f"[{symbol}] 解析失败：无有效行")
                return 1
            print(f"\n[{symbol}] {url}")
            print(f"  解析 {len(flows)} 个交易日：{flows[0]['flow_date']} ~ {flows[-1]['flow_date']}")

            # 一致性检查：净流日应为交易日（周末应无数据）
            weekend = [f for f in flows if f["flow_date"].weekday() >= 5]
            if weekend:
                print(f"  [warn] 出现 {len(weekend)} 个周末日期（首个 {weekend[0]['flow_date']}）")

            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.executemany(UPSERT_SQL, [
                        {"symbol": symbol, **f} | {
                            "net_flow_usd": round(f["net_flow_usd_m"] * 1_000_000, 2)}
                        for f in flows])
                conn.commit()
                print(f"  已 upsert {len(flows)} 行（source_code='farside'）")

            if args.cross_check:
                with conn.cursor() as cur:
                    cur.execute(CROSSCHECK_SQL, (symbol,))
                    pairs = cur.fetchall()
                diffs = [(d, a, b) for d, a, b in pairs
                         if a is not None and b is not None and abs(a - b) > 1.0]
                print(f"  交叉验证（vs cryptoetf）：重叠 {len(pairs)} 日，"
                      f"差异>1M 共 {len(diffs)} 日")
                for d, a, b in diffs[:8]:
                    print(f"    {d}: farside={a:+.1f}M vs cryptoetf={b:+.1f}M "
                          f"(差 {a - b:+.1f}M)")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
