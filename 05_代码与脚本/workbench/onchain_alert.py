"""
链上异动告警（CoinGlass On-Chain Alert 风格）蓝图。

功能：
  - GET /onchain-alert              渲染独立页面
  - GET /api/onchain-alert/stream   跨资产大额转账实时流 + 24h 统计 + 交易所榜单

设计要点（与既有管道的边界）：
  - 复用 biz.onchain_transfer_log（chain_transfer_monitor 守护进程已生产级写入），不新增采集。
  - 归因采用「读侧 union」：同时匹配 biz.onchain_exchange_wallet(confidence='high')
    与 biz.onchain_address_label(label_type='exchange')，在查询层实时重新判定
    from/to 是否为交易所钱包。这样不改动写入 daemon、不引入误标风险，
    页面覆盖率立即吃到已入库的交易所标签地址（含用户那上万条）。
  - 方向语义对齐 CoinGlass：Inflow = 转入交易所（潜在抛压）；Outflow = 从交易所转出（提币）。
"""

from __future__ import annotations

import os
from flask import Blueprint, render_template, jsonify, request

try:
    from task_manager import _get_db  # 复用 app.py 的取连接函数
except Exception:  # 兜底：直接走环境变量
    from psycopg import connect as _pg_connect
    from contextlib import contextmanager

    @contextmanager
    def _get_db():
        c = _pg_connect(os.environ["DATABASE_URL"], connect_timeout=30)
        try:
            yield c
        finally:
            c.close()

import psycopg.rows

onchain_alert_bp = Blueprint("onchain_alert", __name__)

# 链 -> 区块浏览器交易 URL 模板（用于前端跳转核实）
_EXPLORER_TX = {
    "eth": "https://etherscan.io/tx/{tx}",
    "ethereum": "https://etherscan.io/tx/{tx}",
    "bsc": "https://bscscan.com/tx/{tx}",
    "bnb": "https://bscscan.com/tx/{tx}",
    "arbitrum": "https://arbiscan.io/tx/{tx}",
    "base": "https://basescan.org/tx/{tx}",
    "optimism": "https://optimistic.etherscan.io/tx/{tx}",
    "polygon": "https://polygonscan.com/tx/{tx}",
    "avax": "https://snowtrace.io/tx/{tx}",
    "avalanche": "https://snowtrace.io/tx/{tx}",
    "solana": "https://solscan.io/tx/{tx}",
    "tron": "https://tronscan.org/#/transaction/{tx}",
    "ton": "https://tonscan.org/tx/{tx}",
    "sui": "https://suiscan.xyz/mainnet/tx/{tx}",
    "aptos": "https://explorer.aptoslabs.com/txn/{tx}",
}

# 链展示名（给交易所榜单/筛选用）
_CHAIN_LABEL = {
    "eth": "Ethereum", "ethereum": "Ethereum",
    "bsc": "BSC", "bnb": "BSC",
    "arbitrum": "Arbitrum", "base": "Base", "optimism": "Optimism",
    "polygon": "Polygon", "avax": "Avalanche", "avalanche": "Avalanche",
    "solana": "Solana", "tron": "Tron", "ton": "TON", "sui": "Sui", "aptos": "Aptos",
}


def _explorer_url(chain: str, tx_hash: str) -> str | None:
    tpl = _EXPLORER_TX.get((chain or "").lower())
    if not tpl or not tx_hash:
        return None
    return tpl.format(tx=tx_hash)


def _chain_disp(chain: str) -> str:
    return _CHAIN_LABEL.get((chain or "").lower(), (chain or "?".upper()))


def _build_exch_cte() -> str:
    """交易所地址集合：high 置信度钱包库 + exchange 标签地址库（high/medium）。

    读侧 union，不写入任何表。
    """
    return """
        WITH exch AS (
            SELECT address, chain, exchange_name, 'wallet' AS src
            FROM biz.onchain_exchange_wallet
            WHERE confidence = 'high'
            UNION
            SELECT address, chain, label_name AS exchange_name, 'label' AS src
            FROM biz.onchain_address_label
            WHERE label_type = 'exchange'
              AND confidence IN ('high', 'medium')
        )
    """


def _classify(rows):
    """根据 from/to 交易所命中，给每行定方向并计算展示字段。"""
    out = []
    for r in rows:
        from_exch = r.get("from_exch")
        to_exch = r.get("to_exch")
        if to_exch and not from_exch:
            direction = "inflow"          # 转入交易所
        elif from_exch and not to_exch:
            direction = "outflow"         # 从交易所转出
        elif from_exch and to_exch:
            direction = "internal"        # 交易所内部划转
        else:
            direction = "unknown"
        r["direction"] = direction
        # 展示用的交易所名（优先 from，其次 to）
        r["exchange"] = to_exch or from_exch
        out.append(r)
    return out


@onchain_alert_bp.route("/onchain-alert")
def onchain_alert_page():
    return render_template("onchain_alert.html")


@onchain_alert_bp.route("/api/onchain-alert/stream")
def onchain_alert_stream():
    try:
        # 参数
        try:
            hours = max(1, min(168, int(request.args.get("hours", 24))))
        except (TypeError, ValueError):
            hours = 24
        chain = (request.args.get("chain") or "").strip() or None
        exchange = (request.args.get("exchange") or "").strip() or None
        direction = (request.args.get("direction") or "all").strip().lower()
        if direction not in ("inflow", "outflow", "internal", "all"):
            direction = "all"
        symbol = (request.args.get("symbol") or "").strip() or None
        try:
            limit = max(1, min(500, int(request.args.get("limit", 150))))
        except (TypeError, ValueError):
            limit = 150

        with _get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                exch_cte = _build_exch_cte()

                # ── 主查询：跨资产大额转账流（至少一端命中交易所）──
                feed_conds = [
                    "(tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)",
                    "tl.tx_hash NOT LIKE '0xtest%%'",
                    "tl.block_timestamp >= NOW() - (%s * INTERVAL '1 hour')",
                    "(f.exchange_name IS NOT NULL OR t.exchange_name IS NOT NULL)",
                    "(f.exchange_name IS NULL OR t.exchange_name IS NULL OR split_part(split_part(f.exchange_name, ':', 1), ' ', 1) <> split_part(split_part(t.exchange_name, ':', 1), ' ', 1))",
                ]
                feed_params = [hours]
                if chain:
                    feed_conds.append("tl.chain = %s")
                    feed_params.append(chain)
                if symbol:
                    feed_conds.append("(a.canonical_symbol ILIKE %s OR a.canonical_name ILIKE %s)")
                    feed_params.append(f"%{symbol}%")
                    feed_params.append(f"%{symbol}%")
                if exchange:
                    feed_conds.append("(f.exchange_name ILIKE %s OR t.exchange_name ILIKE %s)")
                    feed_params.append(f"%{exchange}%")
                    feed_params.append(f"%{exchange}%")

                feed_sql = f"""
                    {exch_cte}
                    SELECT
                        tl.log_id, tl.asset_id, tl.chain, tl.contract_address,
                        tl.tx_hash, tl.from_address, tl.to_address,
                        tl.value, tl.value_usd,
                        tl.block_number, tl.block_timestamp,
                        tl.from_label, tl.to_label,
                        a.canonical_symbol, a.canonical_name,
                        f.exchange_name AS from_exch,
                        t.exchange_name AS to_exch
                    FROM biz.onchain_transfer_log tl
                    LEFT JOIN core.asset a ON a.asset_id = tl.asset_id
                    LEFT JOIN exch f ON f.address = tl.from_address AND f.chain = tl.chain
                    LEFT JOIN exch t ON t.address = tl.to_address AND t.chain = tl.chain
                    WHERE {' AND '.join(feed_conds)}
                    ORDER BY tl.block_timestamp DESC
                    LIMIT %s
                """
                cur.execute(feed_sql, feed_params + [limit])
                feed_rows = cur.fetchall()
                feed = _classify([dict(r) for r in feed_rows])

                if direction != "all":
                    feed = [x for x in feed if x["direction"] == direction]

                # ── 24h 统计（按小时桶，Inflow vs Outflow）──
                stats_conds = [
                    "tl.block_timestamp >= NOW() - INTERVAL '24 hours'",
                    "(tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)",
                    "tl.tx_hash NOT LIKE '0xtest%%'",
                    "(f.exchange_name IS NOT NULL OR t.exchange_name IS NOT NULL)",
                    "(f.exchange_name IS NULL OR t.exchange_name IS NULL OR split_part(split_part(f.exchange_name, ':', 1), ' ', 1) <> split_part(split_part(t.exchange_name, ':', 1), ' ', 1))",
                ]
                stats_params = []
                if chain:
                    stats_conds.append("tl.chain = %s")
                    stats_params.append(chain)
                if exchange:
                    stats_conds.append("(f.exchange_name ILIKE %s OR t.exchange_name ILIKE %s)")
                    stats_params.append(f"%{exchange}%")
                    stats_params.append(f"%{exchange}%")

                stats_sql = f"""
                    {exch_cte}
                    SELECT
                        date_trunc('hour', tl.block_timestamp) AS bucket,
                        SUM(CASE WHEN t.exchange_name IS NOT NULL AND f.exchange_name IS NULL
                                 THEN COALESCE(tl.value_usd, 0) ELSE 0 END) AS inflow_usd,
                        SUM(CASE WHEN f.exchange_name IS NOT NULL AND t.exchange_name IS NULL
                                 THEN COALESCE(tl.value_usd, 0) ELSE 0 END) AS outflow_usd
                    FROM biz.onchain_transfer_log tl
                    LEFT JOIN core.asset a ON a.asset_id = tl.asset_id
                    LEFT JOIN exch f ON f.address = tl.from_address AND f.chain = tl.chain
                    LEFT JOIN exch t ON t.address = tl.to_address AND t.chain = tl.chain
                    WHERE {' AND '.join(stats_conds)}
                    GROUP BY 1
                    ORDER BY 1
                """
                cur.execute(stats_sql, stats_params)
                hourly_raw = {
                    r["bucket"].strftime("%Y-%m-%d %H:00:00"): r
                    for r in cur.fetchall()
                }

                # 补全 24 个整点桶（无数据补 0），保证柱图连续
                from datetime import datetime, timedelta, timezone
                now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                hourly = []
                for i in range(23, -1, -1):
                    b = now - timedelta(hours=i)
                    key = b.strftime("%Y-%m-%d %H:00:00")
                    rec = hourly_raw.get(key)
                    hourly.append({
                        "bucket": b.strftime("%Y-%m-%dT%H:00:00Z"),
                        "inflow_usd": float(rec["inflow_usd"]) if rec else 0.0,
                        "outflow_usd": float(rec["outflow_usd"]) if rec else 0.0,
                    })

                # ── 总览 + 交易所榜单（按当前 hours 窗口）──
                sum_conds = [
                    "tl.block_timestamp >= NOW() - (%s * INTERVAL '1 hour')",
                    "(tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)",
                    "tl.tx_hash NOT LIKE '0xtest%%'",
                    "(f.exchange_name IS NOT NULL OR t.exchange_name IS NOT NULL)",
                    "(f.exchange_name IS NULL OR t.exchange_name IS NULL OR split_part(split_part(f.exchange_name, ':', 1), ' ', 1) <> split_part(split_part(t.exchange_name, ':', 1), ' ', 1))",
                ]
                sum_params = [hours]
                if chain:
                    sum_conds.append("tl.chain = %s")
                    sum_params.append(chain)
                if exchange:
                    sum_conds.append("(f.exchange_name ILIKE %s OR t.exchange_name ILIKE %s)")
                    sum_params.append(f"%{exchange}%")
                    sum_params.append(f"%{exchange}%")

                sum_sql = f"""
                    {exch_cte}
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN t.exchange_name IS NOT NULL AND f.exchange_name IS NULL
                                 THEN 1 ELSE 0 END) AS inflow_cnt,
                        SUM(CASE WHEN f.exchange_name IS NOT NULL AND t.exchange_name IS NULL
                                 THEN 1 ELSE 0 END) AS outflow_cnt,
                        SUM(CASE WHEN t.exchange_name IS NOT NULL AND f.exchange_name IS NULL
                                 THEN COALESCE(tl.value_usd, 0) ELSE 0 END) AS inflow_usd,
                        SUM(CASE WHEN f.exchange_name IS NOT NULL AND t.exchange_name IS NULL
                                 THEN COALESCE(tl.value_usd, 0) ELSE 0 END) AS outflow_usd
                    FROM biz.onchain_transfer_log tl
                    LEFT JOIN core.asset a ON a.asset_id = tl.asset_id
                    LEFT JOIN exch f ON f.address = tl.from_address AND f.chain = tl.chain
                    LEFT JOIN exch t ON t.address = tl.to_address AND t.chain = tl.chain
                    WHERE {' AND '.join(sum_conds)}
                """
                cur.execute(sum_sql, sum_params)
                s = cur.fetchone() or {}
                summary = {
                    "total_count": int(s.get("total") or 0),
                    "inflow_count": int(s.get("inflow_cnt") or 0),
                    "outflow_count": int(s.get("outflow_cnt") or 0),
                    "inflow_usd": float(s.get("inflow_usd") or 0),
                    "outflow_usd": float(s.get("outflow_usd") or 0),
                    "window_hours": hours,
                }

                # 交易所榜单
                lb_sql = f"""
                    {exch_cte}
                    SELECT COALESCE(t.exchange_name, f.exchange_name) AS exchange,
                           SUM(CASE WHEN t.exchange_name IS NOT NULL AND f.exchange_name IS NULL
                                    THEN COALESCE(tl.value_usd, 0) ELSE 0 END) AS inflow_usd,
                           SUM(CASE WHEN f.exchange_name IS NOT NULL AND t.exchange_name IS NULL
                                    THEN COALESCE(tl.value_usd, 0) ELSE 0 END) AS outflow_usd,
                           COUNT(*) AS cnt
                    FROM biz.onchain_transfer_log tl
                    LEFT JOIN core.asset a ON a.asset_id = tl.asset_id
                    LEFT JOIN exch f ON f.address = tl.from_address AND f.chain = tl.chain
                    LEFT JOIN exch t ON t.address = tl.to_address AND t.chain = tl.chain
                    WHERE tl.block_timestamp >= NOW() - (%s * INTERVAL '1 hour')
                      AND (tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)
                      AND tl.tx_hash NOT LIKE '0xtest%%'
                      AND (f.exchange_name IS NOT NULL OR t.exchange_name IS NOT NULL)
                      AND (f.exchange_name IS NULL OR t.exchange_name IS NULL OR split_part(split_part(f.exchange_name, ':', 1), ' ', 1) <> split_part(split_part(t.exchange_name, ':', 1), ' ', 1))
                    GROUP BY 1
                    ORDER BY (SUM(COALESCE(tl.value_usd, 0))) DESC
                    LIMIT 15
                """
                cur.execute(lb_sql, [hours])
                exchanges = [
                    {
                        "exchange": r["exchange"],
                        "inflow_usd": float(r["inflow_usd"] or 0),
                        "outflow_usd": float(r["outflow_usd"] or 0),
                        "count": int(r["cnt"] or 0),
                    }
                    for r in cur.fetchall()
                ]

                # 给 feed 行补展示字段
                for x in feed:
                    x["symbol"] = x.get("canonical_symbol") or "?"
                    x["name"] = x.get("canonical_name") or ""
                    x["chain_disp"] = _chain_disp(x.get("chain", ""))
                    x["value"] = float(x["value"]) if x.get("value") is not None else 0.0
                    x["value_usd"] = float(x["value_usd"]) if x.get("value_usd") is not None else 0.0
                    x["block_timestamp"] = str(x["block_timestamp"]) if x.get("block_timestamp") else None
                    x["explorer_url"] = _explorer_url(x.get("chain", ""), x.get("tx_hash", ""))
                    # 清理内部字段
                    x.pop("canonical_symbol", None)
                    x.pop("canonical_name", None)
                    x.pop("from_exch", None)
                    x.pop("to_exch", None)

        return jsonify({
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": {
                "hours": hours, "chain": chain, "exchange": exchange,
                "direction": direction, "symbol": symbol, "limit": limit,
            },
            "summary": summary,
            "hourly": hourly,
            "exchanges": exchanges,
            "transfers": feed,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@onchain_alert_bp.route("/api/onchain-alert/meta")
def onchain_alert_meta():
    """下拉筛选用：可用链 + 交易所列表（读侧 union 的投影）。"""
    try:
        with _get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT DISTINCT chain FROM (
                        SELECT chain FROM biz.onchain_exchange_wallet WHERE confidence='high'
                        UNION
                        SELECT chain FROM biz.onchain_address_label
                        WHERE label_type='exchange' AND confidence IN ('high','medium')
                    ) s ORDER BY 1
                """)
                chains = [r["chain"] for r in cur.fetchall()]
                cur.execute("""
                    SELECT DISTINCT exchange_name FROM (
                        SELECT exchange_name FROM biz.onchain_exchange_wallet WHERE confidence='high'
                        UNION
                        SELECT label_name AS exchange_name FROM biz.onchain_address_label
                        WHERE label_type='exchange' AND confidence IN ('high','medium')
                    ) s ORDER BY 1
                """)
                exchanges = [r["exchange_name"] for r in cur.fetchall()]
        return jsonify({"ok": True, "chains": chains, "exchanges": exchanges})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
