"""
链上异动告警（CoinGlass On-Chain Alert 风格）蓝图。

功能：
  - GET /onchain-alert                 渲染独立页面
  - GET /api/onchain-alert/ranking     代币级净流榜单（视图 tab × 时间窗 × 链/交易所筛选）
  - GET /api/onchain-alert/token       选中代币的流入流出统计序列 + 转账流水明细
  - GET /api/onchain-alert/meta        筛选项（链 + 交易所家族名）

设计要点（与既有管道的边界）：
  - 复用 biz.onchain_transfer_log（chain_transfer_monitor 守护进程已生产级写入），不新增采集、不新增表。
  - 归因采用「读侧 union」：同时匹配 biz.onchain_exchange_wallet(confidence='high')
    与 biz.onchain_address_label(label_type='exchange')，在查询层实时重新判定
    from/to 是否为交易所钱包。这样不改动写入 daemon、不引入误标风险。
  - 方向语义对齐 CoinGlass：Inflow = 转入交易所（潜在抛压）；Outflow = 从交易所转出（提币）。
  - 视图口径：net_inflow = inflow - outflow（正=净充提至交易所）；
              net_outflow = outflow - inflow（正=净提币离场）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
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

# 链展示名（给筛选用）
_CHAIN_LABEL = {
    "eth": "Ethereum", "ethereum": "Ethereum",
    "bsc": "BSC", "bnb": "BSC",
    "arbitrum": "Arbitrum", "base": "Base", "optimism": "Optimism",
    "polygon": "Polygon", "avax": "Avalanche", "avalanche": "Avalanche",
    "solana": "Solana", "tron": "Tron", "ton": "TON", "sui": "Sui", "aptos": "Aptos",
}

# 视图 -> 展示名
VIEW_LABELS = {
    "net_inflow": "净流入",
    "inflow": "流入",
    "net_outflow": "净流出",
    "outflow": "流出",
}

# 交易哈希的测试数据前缀（参数化传入，避免裸 % 被当成占位符）
_TEST_TX_PREFIX = "0xtest%"

# 交易所家族判定：先按冒号拆（Binance: Hot Wallet 20 → Binance），再按空格拆（Binance 14 → Binance）
_FAMILY_SQL = "split_part(split_part({col}, ':', 1), ' ', 1)"

# 标签新鲜度阈值（小时）：本地热跑每 30min 一次，故 >2h 视为偏旧；>26h 表示漏掉
# 一整轮每日全量（生产上真实发生过 11 天断供，见 scheduler.py 的 enrich_reminder 注释）。
LABEL_AGING_HOURS = 2.0
LABEL_STALE_HOURS = 26.0


def _explorer_url(chain: str, tx_hash: str) -> str | None:
    tpl = _EXPLORER_TX.get((chain or "").lower())
    if not tpl or not tx_hash:
        return None
    return tpl.format(tx=tx_hash)


def _chain_disp(chain: str) -> str:
    return _CHAIN_LABEL.get((chain or "").lower(), (chain or "?").upper())


def _short_addr(addr: str | None) -> str:
    """地址缩写展示（对齐 CoinGlass 的 0xa8c_20b 风格）。"""
    if not addr:
        return "—"
    a = str(addr)
    if len(a) <= 14:
        return a
    return f"{a[:6]}_{a[-4:]}"


def _pick_label(exchange: str | None, names, address: str | None) -> str:
    """From/To 展示优先级：交易所名 > 首个标签名 > 缩写地址。"""
    if exchange:
        return exchange
    if names:
        for n in names:
            if n:
                return str(n)
    return _short_addr(address)


def _exch_cte() -> str:
    """交易所地址集合：high 置信度钱包库 + exchange 标签地址库（high/medium）。

    读侧 union，不写入任何表。
    外层按 (address, chain) 收敛为一行——若同一地址同时存在于两张表、或带两个
    不同 exchange_name，UNION 的三元组去重不成立，LEFT JOIN 会让行数翻倍、
    使 per-asset 的 SUM(value_usd) 放大。
    """
    return """
        WITH exch_raw AS (
            SELECT address, chain, exchange_name
            FROM biz.onchain_exchange_wallet
            WHERE confidence = 'high'
            UNION
            SELECT address, chain, label_name
            FROM biz.onchain_address_label
            WHERE label_type = 'exchange'
              AND confidence IN ('high', 'medium')
        ),
        exch AS (
            SELECT address, chain, min(exchange_name) AS exchange_name
            FROM exch_raw
            GROUP BY address, chain
        )
    """


def _window_conds(hours, chain, *, asset_id=None, alias="src"):
    """窗口 / 链 / 脏数据过滤条件，ranking、chart、history、漏报量共用。

    返回 (条件列表, 参数列表)——条件顺序即占位符顺序，调用方按序拼接不得重排。
    """
    conds = [
        f"{alias}.block_timestamp >= NOW() - (%s * INTERVAL '1 hour')",
        f"{alias}.asset_id IS NOT NULL",
        f"{alias}.value_usd IS NOT NULL AND {alias}.value_usd > 0",
        f"{alias}.is_suspect IS NOT TRUE",
        f"{alias}.tx_hash NOT LIKE %s",
    ]
    params: list = [hours, _TEST_TX_PREFIX]
    if chain:
        conds.append(f"{alias}.chain = %s")
        params.append(chain)
    if asset_id is not None:
        conds.append(f"{alias}.asset_id = %s")
        params.append(asset_id)
    return conds, params


def _base_cte(hours, chain, exchange, *, asset_id=None):
    """四层 CTE：tl（窗口/链/脏数据）→ j（JOIN exch）→ clean（剔同所互转）
    → flagged（算 is_in / is_out）。ranking / chart / history 共用。

    返回 (sql 片段, 参数列表)。筛选条件按需拼接，不做 `%s IS NULL OR ...` 的参数体操。
    """
    conds, params = _window_conds(hours, chain, asset_id=asset_id, alias="src")

    # j 层已把 exchange_name 投影为 f_ex / t_ex，家族投影为 f_fam / t_fam；
    # clean / flagged 层只能引用这些投影列，不能再写 f.xxx / t.xxx（会丢 FROM 别名）。
    # 同所家族互转剔除：必须保留 NULL 守卫（NOT(f=t) 会因 NULL 比较误杀单端命中的正常流入流出）
    clean_conds = [
        "(f_ex IS NOT NULL OR t_ex IS NOT NULL)",
        "(f_ex IS NULL OR t_ex IS NULL OR f_fam <> t_fam)",
    ]

    # 交易所筛选：按家族等值匹配；选 E 时 inflow 只认「转入 E」，outflow 只认「转出 E」
    if exchange:
        in_flag = "(t_ex IS NOT NULL AND t_fam = %s)"
        out_flag = "(f_ex IS NOT NULL AND f_fam = %s)"
        clean_conds.append("(f_fam = %s OR t_fam = %s)")
        flag_params = [exchange, exchange, exchange, exchange]
    else:
        in_flag = "(t_ex IS NOT NULL)"
        out_flag = "(f_ex IS NOT NULL)"
        flag_params = []

    sql = f"""
        {_exch_cte()},
        tl AS (
            SELECT src.asset_id, src.chain, src.block_timestamp, src.value, src.value_usd,
                   src.from_address, src.to_address, src.tx_hash,
                   src.from_label_names, src.to_label_names
            FROM biz.onchain_transfer_log src
            WHERE {' AND '.join(conds)}
        ),
        j AS (
            SELECT tl.*,
                   f.exchange_name AS f_ex,
                   t.exchange_name AS t_ex,
                   {_FAMILY_SQL.format(col='f.exchange_name')} AS f_fam,
                   {_FAMILY_SQL.format(col='t.exchange_name')} AS t_fam
            FROM tl
            LEFT JOIN exch f ON f.address = tl.from_address AND f.chain = tl.chain
            LEFT JOIN exch t ON t.address = tl.to_address   AND t.chain = tl.chain
        ),
        clean AS (
            SELECT * FROM j
            WHERE {' AND '.join(clean_conds)}
        ),
        flagged AS (
            SELECT clean.*, {in_flag} AS is_in, {out_flag} AS is_out
            FROM clean
        )
    """
    return sql, params + flag_params


def _view_metric(view, inflow_usd, outflow_usd, inflow_qty, outflow_qty):
    """按视图派生 (美元指标, 数量指标)。"""
    if view == "inflow":
        return inflow_usd, inflow_qty
    if view == "outflow":
        return outflow_usd, outflow_qty
    if view == "net_outflow":
        return outflow_usd - inflow_usd, outflow_qty - inflow_qty
    # net_inflow（默认）
    return inflow_usd - outflow_usd, inflow_qty - outflow_qty


def _parse_common_args():
    """解析 ranking / token 的公共查询参数。"""
    try:
        hours = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    hours = min(24, max(1, hours))
    if hours not in (1, 4, 24):
        hours = 24

    view = (request.args.get("view") or "net_inflow").strip().lower()
    if view not in VIEW_LABELS:
        view = "net_inflow"

    chain = (request.args.get("chain") or "").strip() or None
    exchange = (request.args.get("exchange") or "").strip() or None

    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = min(200, max(1, limit))
    return hours, view, chain, exchange, limit


def _bucket_span(hours: int) -> int:
    """自适应桶粒度（秒）：短窗口用细桶，否则 1h 窗口只剩 1 根柱子。"""
    return {1: 300, 4: 900}.get(hours, 3600)


def _bucket_label(span: int) -> str:
    return {300: "5 分钟", 900: "15 分钟"}.get(span, "1 小时")


def _unlabeled_stats(cur, hours, chain, *, asset_id=None):
    """本窗口内「两端都没命中交易所标签」的转账笔数与金额（漏报量）。

    这些行被 clean 层整行剔除 ⇒ 在榜单/流水里完全不可见。标签富化只能在本机跑
    （服务器 IP 被 Cloudflare 拦），一旦本地任务断供，表现就是「榜单静默变空」
    而不是报错。把漏报量显式暴露，让静默缺口可被察觉。

    注意：不受 exchange 筛选影响——它描述的是「完全没有交易所身份」的那部分转账。
    """
    conds, params = _window_conds(hours, chain, asset_id=asset_id, alias="tl")
    cur.execute(f"""
        {_exch_cte()},
        unl AS (
            SELECT tl.asset_id, tl.chain, tl.value_usd, tl.from_address, tl.to_address
            FROM biz.onchain_transfer_log tl
            WHERE {' AND '.join(conds)}
        )
        SELECT COUNT(*) AS cnt, COALESCE(SUM(unl.value_usd), 0) AS usd
        FROM unl
        LEFT JOIN exch f ON f.address = unl.from_address AND f.chain = unl.chain
        LEFT JOIN exch t ON t.address = unl.to_address   AND t.chain = unl.chain
        WHERE f.exchange_name IS NULL AND t.exchange_name IS NULL
    """, params)
    r = cur.fetchone()
    return {"count": int(r["cnt"] or 0), "value_usd": float(r["usd"] or 0)}


def _label_freshness(cur):
    """标签库最新写入时间与年龄（页面「标签新鲜度」提示）。

    取两张标签表的 MAX(时间列)：onchain_address_label.updated_at / onchain_exchange_wallet.added_at。
    看护阈值按「是否漏掉一整轮本地富化」设定：
      - 正常：本地热跑每 30min 一次 ⇒ 年龄通常 < 1h
      - 偏旧 aging：> 2h（本地任务被跳过或拖延）
      - 停更 stale：> 26h（漏掉每日全量一轮 ⇒ 已在生产发生过一次 11 天断供）
    """
    cur.execute("SELECT MAX(updated_at) AS ts FROM biz.onchain_address_label")
    a = cur.fetchone()["ts"]
    cur.execute("SELECT MAX(added_at) AS ts FROM biz.onchain_exchange_wallet")
    b = cur.fetchone()["ts"]

    latest = max([t for t in (a, b) if t is not None], default=None)
    if latest is None:
        return {
            "updated_at": None, "age_hours": None, "level": "unknown",
            "aging_hours": LABEL_AGING_HOURS, "stale_hours": LABEL_STALE_HOURS,
        }

    age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0
    if age_hours >= LABEL_STALE_HOURS:
        level = "stale"
    elif age_hours >= LABEL_AGING_HOURS:
        level = "aging"
    else:
        level = "fresh"
    return {
        "updated_at": latest.isoformat(),
        "age_hours": round(age_hours, 2),
        "level": level,
        "aging_hours": LABEL_AGING_HOURS,
        "stale_hours": LABEL_STALE_HOURS,
    }


@onchain_alert_bp.route("/onchain-alert")
def onchain_alert_page():
    return render_template("onchain_alert.html")


@onchain_alert_bp.route("/api/onchain-alert/ranking")
def onchain_alert_ranking():
    """代币级净流榜单：按当前视图的美元指标降序。"""
    try:
        hours, view, chain, exchange, limit = _parse_common_args()

        with _get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("SET TIME ZONE 'UTC'")
                cte, params = _base_cte(hours, chain, exchange)
                cur.execute(f"""
                    {cte}
                    SELECT asset_id,
                           COALESCE(SUM(value_usd) FILTER (WHERE is_in), 0)  AS inflow_usd,
                           COALESCE(SUM(value)     FILTER (WHERE is_in), 0)  AS inflow_qty,
                           COALESCE(SUM(value_usd) FILTER (WHERE is_out), 0) AS outflow_usd,
                           COALESCE(SUM(value)     FILTER (WHERE is_out), 0) AS outflow_qty,
                           COUNT(*) FILTER (WHERE is_in OR is_out)           AS cnt
                    FROM flagged
                    GROUP BY asset_id
                    HAVING COUNT(*) FILTER (WHERE is_in OR is_out) > 0
                """, params)
                rows = [dict(r) for r in cur.fetchall()]
                unlabeled = _unlabeled_stats(cur, hours, chain)

                # 按视图派生指标并排序（跨币种数量不可比 → 一律按美元指标排名）
                ranked = []
                for r in rows:
                    inflow_usd = float(r["inflow_usd"] or 0)
                    outflow_usd = float(r["outflow_usd"] or 0)
                    inflow_qty = float(r["inflow_qty"] or 0)
                    outflow_qty = float(r["outflow_qty"] or 0)
                    metric_usd, metric_qty = _view_metric(
                        view, inflow_usd, outflow_usd, inflow_qty, outflow_qty)
                    ranked.append({
                        "asset_id": r["asset_id"],
                        "inflow_usd": inflow_usd,
                        "outflow_usd": outflow_usd,
                        "netflow_usd": inflow_usd - outflow_usd,
                        "value_usd": metric_usd,
                        "quantity": metric_qty,
                        "count": int(r["cnt"] or 0),
                    })
                ranked = [x for x in ranked if x["value_usd"] != 0]
                ranked.sort(key=lambda x: x["value_usd"], reverse=True)
                ranked = ranked[:limit]

                # 补 symbol / name
                ids = [x["asset_id"] for x in ranked]
                meta = {}
                if ids:
                    cur.execute(
                        "SELECT asset_id, canonical_symbol, canonical_name"
                        " FROM core.asset WHERE asset_id = ANY(%s)", (ids,))
                    for a in cur.fetchall():
                        meta[a["asset_id"]] = a
                for i, x in enumerate(ranked, 1):
                    a = meta.get(x["asset_id"]) or {}
                    x["rank"] = i
                    x["symbol"] = a.get("canonical_symbol") or f"#{x['asset_id']}"
                    x["name"] = a.get("canonical_name") or ""

        return jsonify({
            "ok": True,
            "view": view,
            "view_label": VIEW_LABELS[view],
            "hours": hours,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": {"chain": chain, "exchange": exchange, "limit": limit},
            "total_count": len(ranked),
            "unlabeled": unlabeled,
            "ranking": ranked,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@onchain_alert_bp.route("/api/onchain-alert/token")
def onchain_alert_token():
    """选中代币：流入流出统计序列（发散柱图）+ 转账流水明细。"""
    try:
        hours, view, chain, exchange, limit = _parse_common_args()
        try:
            asset_id = int(request.args.get("asset_id", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "asset_id 参数无效"}), 400
        if asset_id <= 0:
            return jsonify({"ok": False, "error": "asset_id 必填"}), 400

        span = _bucket_span(hours)

        with _get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("SET TIME ZONE 'UTC'")
                cte, params = _base_cte(hours, chain, exchange, asset_id=asset_id)

                # ── 统计序列（chart）──
                cur.execute(f"""
                    {cte}
                    SELECT to_timestamp(
                               floor(extract(epoch FROM block_timestamp)::double precision / %s) * %s
                           ) AS bucket,
                           COALESCE(SUM(value_usd) FILTER (WHERE is_in), 0)  AS inflow_usd,
                           COALESCE(SUM(value_usd) FILTER (WHERE is_out), 0) AS outflow_usd
                    FROM flagged
                    GROUP BY 1
                    ORDER BY 1
                """, params + [span, span])
                raw = {r["bucket"]: r for r in cur.fetchall()}

                # ── 零填充（UTC 对齐，保证柱图连续）──
                now_epoch = int(datetime.now(timezone.utc).timestamp())
                cur_bucket = now_epoch - now_epoch % span          # 当前（进行中）桶的起点
                n_buckets = (hours * 3600) // span                 # 1h→12×5min / 4h→16×15min / 24h→24×1h
                start_epoch = cur_bucket - (n_buckets - 1) * span

                chart = []
                for i in range(n_buckets):
                    b = datetime.fromtimestamp(start_epoch + i * span, tz=timezone.utc)
                    rec = raw.get(b)
                    chart.append({
                        "bucket": b.isoformat().replace("+00:00", "Z"),
                        "inflow_usd": float(rec["inflow_usd"]) if rec else 0.0,
                        "outflow_usd": float(rec["outflow_usd"]) if rec else 0.0,
                    })

                # ── 流水明细（history）──
                cur.execute(f"""
                    {cte}
                    SELECT block_timestamp, chain, tx_hash,
                           from_address, to_address, value, value_usd,
                           f_ex, t_ex, is_in, is_out,
                           from_label_names, to_label_names
                    FROM flagged
                    WHERE (is_in OR is_out)
                    ORDER BY block_timestamp DESC
                    LIMIT %s
                """, params + [limit])
                hist_rows = cur.fetchall()

                # ── 代币基础信息 ──
                cur.execute(
                    "SELECT canonical_symbol, canonical_name FROM core.asset WHERE asset_id = %s",
                    (asset_id,))
                a = cur.fetchone() or {}
                symbol = a.get("canonical_symbol") or f"#{asset_id}"
                name = a.get("canonical_name") or ""

        history = []
        for r in hist_rows:
            direction = "inflow" if (r["is_in"] and not r["is_out"]) else (
                "outflow" if (r["is_out"] and not r["is_in"]) else "internal")
            history.append({
                "ts": r["block_timestamp"].isoformat(),
                "symbol": symbol,
                "from": _pick_label(r["f_ex"], r["from_label_names"], r["from_address"]),
                "to": _pick_label(r["t_ex"], r["to_label_names"], r["to_address"]),
                "from_is_exchange": r["f_ex"] is not None,
                "to_is_exchange": r["t_ex"] is not None,
                "quantity": float(r["value"]) if r["value"] is not None else 0.0,
                "value_usd": float(r["value_usd"]) if r["value_usd"] is not None else 0.0,
                "direction": direction,
                "chain_disp": _chain_disp(r["chain"]),
                "tx_hash": r["tx_hash"],
                "explorer_url": _explorer_url(r["chain"], r["tx_hash"]),
            })

        totals_in = sum(b["inflow_usd"] for b in chart)
        totals_out = sum(b["outflow_usd"] for b in chart)
        return jsonify({
            "ok": True,
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "view": view,
            "view_label": VIEW_LABELS[view],
            "hours": hours,
            "bucket_span_sec": span,
            "bucket_label": _bucket_label(span),
            "totals": {
                "inflow_usd": totals_in,
                "outflow_usd": totals_out,
                "netflow_usd": totals_in - totals_out,
                "count": len(history),
            },
            "chart": chart,
            "history": history,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@onchain_alert_bp.route("/api/onchain-alert/meta")
def onchain_alert_meta():
    """下拉筛选用：可用链 + 交易所家族名（读侧 union 的投影）。"""
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
                # 返回家族名：与查询层的家族等值匹配保持一致
                # （下拉若是 "Binance 14"/"Binance: Hot Wallet 20" 这类子钱包名，匹配会对不上）
                cur.execute(f"""
                    SELECT DISTINCT {_FAMILY_SQL.format(col='exchange_name')} AS family
                    FROM (
                        SELECT exchange_name FROM biz.onchain_exchange_wallet WHERE confidence='high'
                        UNION
                        SELECT label_name AS exchange_name FROM biz.onchain_address_label
                        WHERE label_type='exchange' AND confidence IN ('high','medium')
                    ) s
                    WHERE exchange_name IS NOT NULL
                    ORDER BY 1
                """)
                exchanges = [r["family"] for r in cur.fetchall() if r["family"]]
                freshness = _label_freshness(cur)
        return jsonify({
            "ok": True, "chains": chains, "exchanges": exchanges,
            "label_freshness": freshness,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500