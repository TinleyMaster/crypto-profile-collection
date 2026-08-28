#!/usr/bin/env python3
"""
CoinGlass 净流抓取器 Python 封装。

功能：
  1. 调用 node cg_netflow_scraper.js 抓取 CoinGlass 净流数据
  2. 读取 cg_netflow_latest.json
  3. 输出结构化净流信号（净流入 TOP5、净流出 TOP5、全网净流）

用法：
  python run_cg_netflow.py              # 抓取并输出净流信号
  python run_cg_netflow.py --fetch-only # 仅抓取，不输出信号
  python run_cg_netflow.py --json       # 输出原始 JSON
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRAPER_JS = SCRIPT_DIR / "cg_netflow_scraper.js"
OUTPUT_JSON = SCRIPT_DIR / "cg_netflow_latest.json"


def run_scraper(timeout: int = 120) -> int:
    """调用 node cg_netflow_scraper.js 抓取数据。"""
    cmd = ["node", str(SCRAPER_JS)]
    print(f"[fetch] 执行: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=str(SCRIPT_DIR), timeout=timeout)
        if result.returncode != 0:
            print(f"[ERROR] 抓取器返回非零退出码: {result.returncode}", file=sys.stderr)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"[ERROR] 抓取器超时 ({timeout}s)", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("[ERROR] 未找到 node 或 cg_netflow_scraper.js", file=sys.stderr)
        return 1


def read_output() -> dict | None:
    """读取 cg_netflow_latest.json。"""
    if not OUTPUT_JSON.exists():
        print(f"[WARN] 输出文件不存在: {OUTPUT_JSON}", file=sys.stderr)
        return None
    try:
        with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[WARN] JSON 解析失败: {e}", file=sys.stderr)
        return None


def format_netflow_signal(data: dict) -> str:
    """格式化净流信号文本。"""
    if data["main_rows"] == 0:
        return "⚠️ CoinGlass netflow 本周期暂不可用（数据源限流），跳过净流信号。"

    netflow = data.get("netflow_by_exchange_coin", [])
    summary = data.get("summary", {})

    if not netflow:
        return "⚠️ CoinGlass netflow 无有效交易数据。"

    # 净流入 TOP5
    top_in = sorted(netflow, key=lambda x: x["net_usd"], reverse=True)[:5]
    # 净流出 TOP5
    top_out = sorted(netflow, key=lambda x: x["net_usd"])[:5]

    total_in = summary.get("total_inflow_usd", 0)
    total_out = summary.get("total_outflow_usd", 0)
    net = summary.get("net_usd", 0)

    lines = []
    lines.append(f"全网交易所净流 ${net/1e6:.1f}M（流入 {total_in/1e6:.1f}M / 流出 {total_out/1e6:.1f}M）")

    if top_in:
        in_str = "、".join(
            f"{x['symbol']}@{x['exchange']} +${x['net_usd']/1e6:.1f}M"
            for x in top_in if x["net_usd"] > 0
        )
        if in_str:
            lines.append(f"净流入 TOP: {in_str}")

    if top_out:
        out_str = "、".join(
            f"{x['symbol']}@{x['exchange']} -${abs(x['net_usd'])/1e6:.1f}M"
            for x in top_out if x["net_usd"] < 0
        )
        if out_str:
            lines.append(f"净流出 TOP: {out_str}")

    return "\n".join(lines)


def format_alert_signal(data: dict, exchange_wallets: dict[str, str] | None = None) -> str:
    """格式化巨鲸告警信号文本。"""
    alerts = data.get("alert_history", [])
    if not alerts:
        return ""

    lines = ["巨鲸链上告警:"]
    for alert in alerts[:10]:  # 最多显示 10 条
        symbol = alert.get("symbol", "?")
        from_addr = alert.get("from", "?")
        to_addr = alert.get("to", "?")
        qty_display = alert.get("qty_display", "?")
        time_str = alert.get("time", "?")

        # 用地址库反查交易所名
        from_label = from_addr
        to_label = to_addr
        if exchange_wallets:
            from_lower = from_addr.lower()
            to_lower = to_addr.lower()
            if from_lower in exchange_wallets:
                from_label = f"{exchange_wallets[from_lower]} ({from_addr[:10]}...)"
            if to_lower in exchange_wallets:
                to_label = f"{exchange_wallets[to_lower]} ({to_addr[:10]}...)"

        lines.append(f"  {symbol}: {qty_display} | {from_label} → {to_label} | {time_str}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="CoinGlass 净流抓取器")
    parser.add_argument("--fetch-only", action="store_true", help="仅抓取，不输出信号")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    parser.add_argument("--timeout", type=int, default=120, help="抓取超时秒数")
    args = parser.parse_args()

    # 抓取
    rc = run_scraper(timeout=args.timeout)
    if rc != 0 and not OUTPUT_JSON.exists():
        print("❌ 抓取失败且无输出文件", file=sys.stderr)
        return rc

    # 读取
    data = read_output()
    if data is None:
        print("❌ 无法读取输出文件", file=sys.stderr)
        return 1

    if args.fetch_only:
        print(f"✅ 抓取完成，输出: {OUTPUT_JSON}")
        return 0

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    # 输出净流信号
    print("\n" + "=" * 60)
    print("CoinGlass 净流信号")
    print("=" * 60)
    print(f"数据时间: {data.get('fetched_at', '?')}")
    print(f"主表行数: {data.get('main_rows', 0)}")
    print(f"告警行数: {data.get('alert_rows', 0)}")
    print()

    signal = format_netflow_signal(data)
    print(signal)

    alert_signal = format_alert_signal(data)
    if alert_signal:
        print("\n" + alert_signal)

    print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
