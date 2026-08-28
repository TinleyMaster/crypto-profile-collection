"""
CoinGlass 净流数据消费者。

功能：
  1. 读取 cg_netflow_latest.json
  2. 产出净流信号（净流入 TOP5、净流出 TOP5、全网净流）
  3. 巨鲸告警用地址库反查标注
  4. 空表优雅降级（"⚠️ 暂不可用"，不用 0 当真 0）

用法：
  from crypto_research.workbench.cg_netflow import get_cg_netflow_signal
  signal = get_cg_netflow_signal(conn)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def get_cg_netflow_data(json_path: str | Path) -> dict | None:
    """
    读取 cg_netflow_latest.json。

    返回:
        dict: CoinGlass 净流数据，包含 main_table, alert_history, netflow_by_exchange_coin, summary
        None: 文件不存在或解析失败
    """
    json_path = Path(json_path)
    if not json_path.exists():
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def get_exchange_wallet_map(conn, chains: list[str] | None = None) -> dict[str, str]:
    """
    获取交易所地址 -> 交易所名称映射（仅 high 置信度）。

    返回:
        dict: {小写地址: 交易所名称}
    """
    import psycopg.rows
    if chains is None:
        chains = ["eth", "bsc", "polygon", "arbitrum", "base", "optimism", "avalanche"]

    result = {}
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for chain in chains:
            cur.execute("""
                SELECT LOWER(address) AS address, exchange_name
                FROM biz.onchain_exchange_wallet
                WHERE chain = %s AND confidence = 'high'
            """, (chain,))
            for row in cur.fetchall():
                result[row["address"]] = row["exchange_name"]
    return result


def get_cg_netflow_signal(
    conn,
    json_path: str | Path = "cg_netflow_latest.json",
    top_n: int = 5,
) -> dict:
    """
    获取 CoinGlass 净流信号。

    返回:
        dict: {
            "available": bool,          # 数据是否可用
            "signal_text": str,         # 格式化的净流信号文本
            "alert_text": str,          # 格式化的巨鲸告警文本
            "summary": dict,            # 汇总数据 (total_inflow_usd, total_outflow_usd, net_usd)
            "top_inflow": list,         # 净流入 TOP N
            "top_outflow": list,        # 净流出 TOP N
            "alerts": list,             # 巨鲸告警列表
            "fetched_at": str,          # 数据抓取时间
        }
    """
    data = get_cg_netflow_data(json_path)
    if data is None:
        return {
            "available": False,
            "signal_text": "⚠️ CoinGlass netflow 数据文件不存在或解析失败。",
            "alert_text": "",
            "summary": {},
            "top_inflow": [],
            "top_outflow": [],
            "alerts": [],
            "fetched_at": "",
        }

    # 检查数据是否为空
    if data.get("main_rows", 0) == 0:
        return {
            "available": False,
            "signal_text": "⚠️ CoinGlass netflow 本周期暂不可用（数据源限流），跳过净流信号。",
            "alert_text": "",
            "summary": data.get("summary", {}),
            "top_inflow": [],
            "top_outflow": [],
            "alerts": data.get("alert_history", []),
            "fetched_at": data.get("fetched_at", ""),
        }

    netflow = data.get("netflow_by_exchange_coin", [])
    summary = data.get("summary", {})

    # 净流入/流出 TOP N
    top_inflow = sorted(netflow, key=lambda x: x["net_usd"], reverse=True)[:top_n]
    top_outflow = sorted(netflow, key=lambda x: x["net_usd"])[:top_n]

    # 格式化净流信号
    total_in = summary.get("total_inflow_usd", 0)
    total_out = summary.get("total_outflow_usd", 0)
    net = summary.get("net_usd", 0)

    lines = []
    lines.append(f"全网交易所净流 ${net/1e6:.1f}M（流入 {total_in/1e6:.1f}M / 流出 {total_out/1e6:.1f}M）")

    if top_inflow:
        in_str = "、".join(
            f"{x['symbol']}@{x['exchange']} +${x['net_usd']/1e6:.1f}M"
            for x in top_inflow if x["net_usd"] > 0
        )
        if in_str:
            lines.append(f"净流入 TOP: {in_str}")

    if top_outflow:
        out_str = "、".join(
            f"{x['symbol']}@{x['exchange']} -${abs(x['net_usd'])/1e6:.1f}M"
            for x in top_outflow if x["net_usd"] < 0
        )
        if out_str:
            lines.append(f"净流出 TOP: {out_str}")

    signal_text = "\n".join(lines)

    # 格式化巨鲸告警
    alerts = data.get("alert_history", [])
    alert_lines = []
    if alerts:
        # 获取地址库用于反查
        exchange_wallets = get_exchange_wallet_map(conn)

        alert_lines.append("巨鲸链上告警:")
        for alert in alerts[:10]:  # 最多显示 10 条
            symbol = alert.get("symbol", "?")
            from_addr = alert.get("from", "?")
            to_addr = alert.get("to", "?")
            qty_display = alert.get("qty_display", "?")
            time_str = alert.get("time", "?")

            # 用地址库反查交易所名
            from_label = from_addr
            to_label = to_addr
            from_lower = from_addr.lower()
            to_lower = to_addr.lower()
            if from_lower in exchange_wallets:
                from_label = f"{exchange_wallets[from_lower]} ({from_addr[:10]}...)"
            if to_lower in exchange_wallets:
                to_label = f"{exchange_wallets[to_lower]} ({to_addr[:10]}...)"

            alert_lines.append(f"  {symbol}: {qty_display} | {from_label} → {to_label} | {time_str}")

    alert_text = "\n".join(alert_lines)

    return {
        "available": True,
        "signal_text": signal_text,
        "alert_text": alert_text,
        "summary": summary,
        "top_inflow": top_inflow,
        "top_outflow": top_outflow,
        "alerts": alerts,
        "fetched_at": data.get("fetched_at", ""),
    }
