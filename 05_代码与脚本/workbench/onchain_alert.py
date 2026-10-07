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

# ── 榜单噪音过滤 ──
# 默认剔除稳定币（core.asset.asset_type='stablecoin'，不受 symbol 重名污染，
# 且不误杀名字带 USDT/USDC 的 meme 币）。可选额外剔除封装/质押/LP/合成资产。
_STABLE_ASSET_TYPE = "stablecoin"
_NOISE_SECTORS = {"wrapped", "lp_token", "synthetic"}
# 质押/封装/RWA 衍生品噪音（流动性往返无交易信号，且不在 asset_type='stablecoin' 覆盖内）。
# 全部存大写，与 SQL 侧 UPPER(canonical_symbol) 比较保持一致。
_NOISE_SYMBOLS = {
    "STETH", "WSTETH", "CBETH", "CBBTC", "BBSOL", "SOLVBTC", "XAUT", "PAXG",
    "SNDKB", "SNDK", "BTCB", "WBTC", "WETH", "USDB", "EURT",
}

# 盘面信号共振窗口（小时）：榜单/详情展示近 N 小时内 scan_signal 的高置信异动
SIGNAL_LOOKBACK_HOURS = 24
# 链上大额单笔高亮阈值（美元）：超过则流水明细打「大额」徽标
LARGE_TRANSFER_USD = 5_000_000

# 标签新鲜度阈值（小时）：本地热跑每 30min 一次，故 >2h 视为偏旧；>26h 表示漏掉
# 一整轮每日全量（生产上真实发生过 11 天断供，见 scheduler.py 的 enrich_reminder 注释）。
LABEL_AGING_HOURS = 2.0
LABEL_STALE_HOURS = 26.0

# 催化剂标题脏值过滤：上游 kol_news_media_binance_square_11 有 508 条把字面量
# 'null' 写进 title（原帖本身无标题），其中 419 条已被 catalyst_impact 关联。
# 这些记录的方向计数与「最新一条标题」都是噪音，展示层统一剔除。
# 依赖 ac 别名——只用于 JOIN biz.asset_catalyst ac 的查询。
_CATALYST_TITLE_OK = (
    "ac.title IS NOT NULL AND btrim(ac.title) <> '' "
    "AND lower(btrim(ac.title)) <> 'null'"
)


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

    # 稳定币/封装噪音过滤：默认剔除（榜单只留交易型代币）
    exclude_stable = request.args.get("exclude_stable", "1") not in ("0", "false", "False")

    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = min(200, max(1, limit))
    return hours, view, chain, exchange, limit, exclude_stable


def _bucket_span(hours: int) -> int:
    """自适应桶粒度（秒）：短窗口用细桶，否则 1h 窗口只剩 1 根柱子。"""
    return {1: 300, 4: 900}.get(hours, 3600)


def _bucket_label(span: int) -> str:
    return {300: "5 分钟", 900: "15 分钟"}.get(span, "1 小时")


def _unlabeled_stats(cur, hours, chain, *, asset_id=None):
    """本窗口内「两端都没命中交易所标签」的可交易代币转账（去重后）笔数与金额。

    这些行被 clean 层整行剔除 ⇒ 在榜单/流水里完全不可见。标签富化只能在本机跑
    （服务器 IP 被 Cloudflare 拦），一旦本地任务断供，表现就是「榜单静默变空」
    而不是报错。把缺口量显式暴露，让静默缺口可被察觉。

    口径必须与榜单对齐，否则这个数字会被两类噪音放大成假信号：
      - 剔除稳定币与封装/质押类噪音资产。这类资产靠封装/解封往返搬量，与榜单要表达的
        「可交易标的资金流」不是一回事；实测 CBBTC/WBTC 一类占未标注量的 98%。
      - 按 (tx_hash, asset_id) 去重取 MAX(value_usd)。同一笔交易内的多跳 Transfer
        （套利/闪电贷/bridge 路由）会把同一笔钱按跳数重复计入，实测虚增约 50%。

    注意：不受 exchange 筛选影响——它描述的是「完全没有交易所身份」的那部分转账。
    """
    conds, params = _window_conds(hours, chain, asset_id=asset_id, alias="tl")
    cur.execute(f"""
        {_exch_cte()},
        unl AS (
            SELECT tl.asset_id, tl.tx_hash, tl.value_usd
            FROM biz.onchain_transfer_log tl
            JOIN core.asset a ON a.asset_id = tl.asset_id
            LEFT JOIN exch f ON f.address = tl.from_address AND f.chain = tl.chain
            LEFT JOIN exch t ON t.address = tl.to_address   AND t.chain = tl.chain
            WHERE {' AND '.join(conds)}
              AND a.asset_type <> '{_STABLE_ASSET_TYPE}'
              AND UPPER(a.canonical_symbol) <> ALL(%s)
              AND f.exchange_name IS NULL AND t.exchange_name IS NULL
        ),
        one AS (
            SELECT tx_hash, asset_id, MAX(value_usd) AS value_usd
            FROM unl GROUP BY tx_hash, asset_id
        )
        SELECT COUNT(*) AS cnt, COALESCE(SUM(value_usd), 0) AS usd
        FROM one
    """, params + [list(_NOISE_SYMBOLS)])
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


def _contract_symbol_candidates(symbol: str) -> list[str]:
    """裸符号 → 合约符号候选（scan_signal.symbol 存 Binance 永续合约名，如
    'B2USDT' / '1000FLOKIUSDT'；core.asset 存裸符号 'B2' / 'FLOKI'）。

    与 scan_daemon._symbol_candidates 反向：从裸符号生成「最贴切→最宽松」候选，
    供页面共振标注跨表匹配（不做归一化会让盘面信号恒空，见审计 P0-1）。
    """
    s = (symbol or "").upper().strip()
    if not s:
        return []
    cands = [s, s + "USDT"]
    if s not in ("BTC", "ETH", "XRP", "DOGE"):
        cands.append("1000" + s + "USDT")
    return cands


def _fetch_scan_signals(cur, asset_ids: list[int], hours: int = SIGNAL_LOOKBACK_HOURS) -> dict[int, list[dict]]:
    """批量取近 N 小时盘面信号（high/medium），按 asset_id 归组。

    返回 {asset_id: [{pool, scenario, confidence, signal_ts, p_dir, oi_dir}]}，
    只保留每条 symbol 的**最新**一条信号（同币 12h 告警冷却，避免卡片刷屏）。
    """
    if not asset_ids:
        return {}
    # 先取裸符号，再生成合约候选
    cur.execute(
        "SELECT asset_id, canonical_symbol FROM core.asset WHERE asset_id = ANY(%s)",
        (asset_ids,))
    sym_map = {r["asset_id"]: r["canonical_symbol"] for r in cur.fetchall()}
    cand_map: dict[str, int] = {}      # 合约候选符号 -> asset_id
    for aid, sym in sym_map.items():
        for c in _contract_symbol_candidates(sym):
            cand_map.setdefault(c, aid)
    if not cand_map:
        return {}

    cur.execute(f"""
        SELECT symbol, pool, scenario, confidence, signal_ts, p_dir, oi_dir
        FROM (
            SELECT symbol, pool, scenario, confidence, signal_ts, p_dir, oi_dir,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY signal_ts DESC) AS rn
            FROM biz.scan_signal
            WHERE symbol = ANY(%s)
              AND confidence IN ('high', 'medium')
              AND signal_ts > NOW() - make_interval(hours => %s)
        ) s
        WHERE rn = 1
    """, (list(cand_map.keys()), hours))
    out: dict[int, list[dict]] = {}
    for r in cur.fetchall():
        aid = cand_map.get(r["symbol"])
        if aid is None:
            continue
        out.setdefault(aid, []).append({
            "pool": r["pool"], "scenario": r["scenario"],
            "confidence": r["confidence"],
            "signal_ts": r["signal_ts"].isoformat() if r["signal_ts"] else None,
            "p_dir": r["p_dir"], "oi_dir": r["oi_dir"],
        })
    return out


def _fetch_event_watchlist(cur, symbol: str, limit: int = 5) -> list[dict]:
    """事件预置层共振：解锁 / 链上大额转账观察名单（与 scan_daemon 同口径）。

    biz.event_watchlist.symbol 存裸符号（含少数合约符号残留），候选展开匹配；
    事件本身**方向**：解锁=新增流通（抛压）⇒ 利空；链上转账方向不明 ⇒ 中性。
    """
    if not symbol:
        return []
    cands = _contract_symbol_candidates(symbol)
    cur.execute("""
        SELECT event_type, event_date, event_pct, detail, updated_at
        FROM biz.event_watchlist
        WHERE symbol = ANY(%s)
        ORDER BY event_date DESC NULLS LAST, id DESC
        LIMIT %s
    """, (cands, limit))
    out = []
    for r in cur.fetchall():
        out.append({
            "type": r["event_type"],               # unlock / onchain_transfer
            "date": str(r["event_date"])[:10] if r["event_date"] else None,
            "event_pct": float(r["event_pct"]) if r["event_pct"] is not None else None,
            "detail": str(r["detail"] or "").strip(),
            "direction": "bearish" if r["event_type"] == "unlock" else "neutral",
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return out


def _stable_flow_summary(cur, hours, chain, exchange, limit: int = 5) -> dict | None:
    """稳定币大额转账汇总：即使榜单默认剔除稳定币，也把「稳定币↔交易所」
    的净流入作为独立信号返回（资金入所待命 = 潜在买盘，链上活跃度前置指标）。

    与 ranking 同口径（_base_cte），只对 core.asset.asset_type='stablecoin'
    的资产聚合；返回 {net_inflow_usd, per: [{symbol, inflow_usd, outflow_usd, net}]}。
    全部稳定币净流出时仍返回（net 为负），供前端提示「提币离场」。
    """
    cte, params = _base_cte(hours, chain, exchange)
    cur.execute(f"""
        {cte}
        SELECT a.canonical_symbol,
               COALESCE(SUM(value_usd) FILTER (WHERE is_in), 0)  AS inflow_usd,
               COALESCE(SUM(value_usd) FILTER (WHERE is_out), 0) AS outflow_usd,
               COUNT(*) FILTER (WHERE is_in OR is_out)           AS cnt
        FROM flagged
        JOIN core.asset a ON a.asset_id = flagged.asset_id
        WHERE a.asset_type = '{_STABLE_ASSET_TYPE}'
        GROUP BY a.asset_id, a.canonical_symbol
        HAVING COUNT(*) FILTER (WHERE is_in OR is_out) > 0
        ORDER BY (COALESCE(SUM(value_usd) FILTER (WHERE is_in), 0)
                - COALESCE(SUM(value_usd) FILTER (WHERE is_out), 0)) DESC
        LIMIT %s
    """, params + [limit])
    rows = cur.fetchall()
    if not rows:
        return None
    per = []
    net_total = 0.0
    for r in rows:
        inf = float(r["inflow_usd"] or 0)
        outf = float(r["outflow_usd"] or 0)
        net_total += inf - outf
        per.append({
            "symbol": r["canonical_symbol"],
            "inflow_usd": inf, "outflow_usd": outf, "net_usd": inf - outf,
            "count": int(r["cnt"] or 0),
        })
    return {"net_inflow_usd": net_total, "per": per}


def _fetch_catalyst_direction(cur, asset_id: int, days: int = 7) -> dict:
    """近 N 天催化剂方向构成（bullish/bearish/neutral 计数 + 最新一条标题）。

    剔除 title 为 NULL/空/字面量 'null' 的脏记录（见 _CATALYST_TITLE_OK），
    否则「利好/利空」计数会被无标题帖的方向判定污染、卡片标题会显示成 "null"。
    """
    cur.execute(f"""
        SELECT ci.impact_direction AS d, COUNT(*) AS cnt
        FROM biz.catalyst_impact ci
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = ci.catalyst_id
        WHERE ci.asset_id = %s AND ac.published_at > NOW() - make_interval(days => %s)
          AND {_CATALYST_TITLE_OK}
        GROUP BY 1
    """, (asset_id, days))
    dirs = {"bullish": 0, "bearish": 0, "neutral": 0}
    for r in cur.fetchall():
        d = (r["d"] or "neutral").lower()
        if d in dirs:
            dirs[d] = int(r["cnt"] or 0)
    cur.execute(f"""
        SELECT ac.title, ac.published_at
        FROM biz.catalyst_impact ci
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = ci.catalyst_id
        WHERE ci.asset_id = %s AND ac.published_at > NOW() - make_interval(days => %s)
          AND {_CATALYST_TITLE_OK}
        ORDER BY ac.published_at DESC LIMIT 1
    """, (asset_id, days))
    latest = cur.fetchone()
    return {
        "days": days,
        "dirs": dirs,
        "latest": {
            "title": latest["title"] if latest else None,
            "published_at": (latest["published_at"].isoformat()
                             if latest and latest["published_at"] else None),
        } if latest else None,
    }


@onchain_alert_bp.route("/onchain-alert")
def onchain_alert_page():
    return render_template("onchain_alert.html")


@onchain_alert_bp.route("/api/onchain-alert/ranking")
def onchain_alert_ranking():
    """代币级净流榜单：按当前视图的美元指标降序。"""
    try:
        hours, view, chain, exchange, limit, exclude_stable = _parse_common_args()

        with _get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("SET TIME ZONE 'UTC'")
                cte, params = _base_cte(hours, chain, exchange)

                # 噪音过滤：稳定币默认剔除；可选再剔除封装/质押/LP/合成（asset_type 判定）
                noise_filter = ""
                if exclude_stable:
                    noise_filter = (
                        "AND asset_id NOT IN (SELECT asset_id FROM core.asset "
                        f"WHERE asset_type = '{_STABLE_ASSET_TYPE}')"
                        " AND asset_id NOT IN (SELECT asset_id FROM core.asset"
                        " WHERE UPPER(canonical_symbol) = ANY(%s))"
                    )
                    params = params + [list(_NOISE_SYMBOLS)]

                cur.execute(f"""
                    {cte}
                    SELECT asset_id,
                           COALESCE(SUM(value_usd) FILTER (WHERE is_in), 0)  AS inflow_usd,
                           COALESCE(SUM(value)     FILTER (WHERE is_in), 0)  AS inflow_qty,
                           COALESCE(SUM(value_usd) FILTER (WHERE is_out), 0) AS outflow_usd,
                           COALESCE(SUM(value)     FILTER (WHERE is_out), 0) AS outflow_qty,
                           COUNT(*) FILTER (WHERE is_in OR is_out)           AS cnt
                    FROM flagged
                    WHERE asset_id IS NOT NULL {noise_filter}
                    GROUP BY asset_id
                    HAVING COUNT(*) FILTER (WHERE is_in OR is_out) > 0
                """, params)
                rows = [dict(r) for r in cur.fetchall()]
                unlabeled = _unlabeled_stats(cur, hours, chain)

                # 稳定币资金流独立信号：榜单剔除稳定币时，仍汇总「稳定币↔交易所」
                # 净流入（潜在买盘），避免把这块数据完全藏掉。
                stable_summary = None
                if exclude_stable:
                    stable_summary = _stable_flow_summary(cur, hours, chain, exchange)

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

                # 补 symbol / name / 24h 涨跌幅 / 市值
                ids = [x["asset_id"] for x in ranked]
                meta = {}
                if ids:
                    cur.execute(
                        "SELECT asset_id, canonical_symbol, canonical_name"
                        " FROM core.asset WHERE asset_id = ANY(%s)", (ids,))
                    for a in cur.fetchall():
                        meta[a["asset_id"]] = a
                # 最近一天行情（change_24h / market_cap）：LATERAL 取每条最新日
                mkt = {}
                if ids:
                    cur.execute("""
                        SELECT m.asset_id, m.change_24h, m.market_cap, m.market_date
                        FROM biz.asset_market_daily m
                        JOIN LATERAL (
                            SELECT MAX(market_date) AS d
                            FROM biz.asset_market_daily
                            WHERE asset_id = m.asset_id
                              AND market_date <= CURRENT_DATE
                        ) lat ON lat.d = m.market_date
                        WHERE m.asset_id = ANY(%s) AND m.market_date = lat.d
                    """, (ids,))
                    for a in cur.fetchall():
                        if a["asset_id"] not in mkt or (
                                a["market_date"] and mkt[a["asset_id"]]["market_date"] is None):
                            mkt[a["asset_id"]] = a
                # 盘面信号共振（近 24h high/medium）
                sig_map = _fetch_scan_signals(cur, ids)
                for i, x in enumerate(ranked, 1):
                    a = meta.get(x["asset_id"]) or {}
                    m = mkt.get(x["asset_id"]) or {}
                    x["rank"] = i
                    x["symbol"] = a.get("canonical_symbol") or f"#{x['asset_id']}"
                    x["name"] = a.get("canonical_name") or ""
                    x["change_24h"] = (float(m["change_24h"])
                                       if m.get("change_24h") is not None else None)
                    x["market_cap"] = (float(m["market_cap"])
                                       if m.get("market_cap") is not None else None)
                    x["signals"] = sig_map.get(x["asset_id"], [])

        return jsonify({
            "ok": True,
            "view": view,
            "view_label": VIEW_LABELS[view],
            "hours": hours,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": {"chain": chain, "exchange": exchange, "limit": limit,
                        "exclude_stable": exclude_stable},
            "total_count": len(ranked),
            "unlabeled": unlabeled,
            "stable_summary": stable_summary,
            "ranking": ranked,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@onchain_alert_bp.route("/api/onchain-alert/token")
def onchain_alert_token():
    """选中代币：流入流出统计序列（发散柱图）+ 转账流水明细 + 共振上下文。"""
    try:
        hours, view, chain, exchange, limit, _ = _parse_common_args()
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

                # ── 共振上下文：盘面信号 + 事件预置（解锁/链上大额）+ 催化剂方向 ──
                signals = _fetch_scan_signals(cur, [asset_id]).get(asset_id, [])
                events = _fetch_event_watchlist(cur, symbol)
                catalysts = _fetch_catalyst_direction(cur, asset_id)

        history = []
        for r in hist_rows:
            direction = "inflow" if (r["is_in"] and not r["is_out"]) else (
                "outflow" if (r["is_out"] and not r["is_in"]) else "internal")
            value_usd = float(r["value_usd"]) if r["value_usd"] is not None else 0.0
            history.append({
                "ts": r["block_timestamp"].isoformat(),
                "symbol": symbol,
                "from": _pick_label(r["f_ex"], r["from_label_names"], r["from_address"]),
                "to": _pick_label(r["t_ex"], r["to_label_names"], r["to_address"]),
                "from_is_exchange": r["f_ex"] is not None,
                "to_is_exchange": r["t_ex"] is not None,
                "quantity": float(r["value"]) if r["value"] is not None else 0.0,
                "value_usd": value_usd,
                "is_large": value_usd >= LARGE_TRANSFER_USD,
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
            "resonance": {
                "signals": signals,
                "events": events,
                "catalysts": catalysts,
            },
            "chart": chart,
            "history": history,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@onchain_alert_bp.route("/api/onchain-alert/holders")
def onchain_alert_holders():
    """某币持仓大户趋势：最近 N 个快照的 top 持仓变化（地址级）。

    数据源 biz.onchain_holder_snapshot（每日调度采集，见 scheduler.py 的
    chain_holder_snapshot_* 任务）：每 (asset_id, chain, snapshot_date) 一条，
    top_holders_json 为 [{rank, address, label, amount, pct}] 地址级明细。

    相邻快照对比口径：
      - 新进 top10：最新快照有、上一个快照无（首次进入榜单）
      - 增持/减持：最新 vs 上一个快照的 pct 差
    取链规则：不传 chain 时自动选「快照最新」的链（同一资产多链分别快照）。
    """
    try:
        try:
            asset_id = int(request.args.get("asset_id", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "asset_id 参数无效"}), 400
        if asset_id <= 0:
            return jsonify({"ok": False, "error": "asset_id 必填"}), 400
        chain = (request.args.get("chain") or "").strip() or None
        try:
            days = int(request.args.get("days", 14))
        except (TypeError, ValueError):
            days = 14
        days = min(60, max(3, days))
        top_n = 10

        with _get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 资产基础信息
                cur.execute(
                    "SELECT canonical_symbol, canonical_name FROM core.asset WHERE asset_id = %s",
                    (asset_id,))
                a = cur.fetchone() or {}
                symbol = a.get("canonical_symbol") or f"#{asset_id}"
                name = a.get("canonical_name") or ""

                # 未指定链 → 取快照最新的一条链（同资产多链分别快照）
                if not chain:
                    cur.execute("""
                        SELECT chain FROM biz.onchain_holder_snapshot
                        WHERE asset_id = %s
                        ORDER BY snapshot_date DESC, fetched_at DESC LIMIT 1
                    """, (asset_id,))
                    r = cur.fetchone()
                    if not r:
                        return jsonify({
                            "ok": True, "asset_id": asset_id, "symbol": symbol,
                            "name": name, "chain": None, "snapshots": [], "top": [],
                        })
                    chain = r["chain"]

                # 最近 N 个快照（快照非每日连续，按日期倒序取 N 条）
                cur.execute("""
                    SELECT snapshot_date, total_holders, top10_concentration,
                           whale_balance_change_7d_pct, whale_balance_change_30d_pct,
                           top_holders_json
                    FROM biz.onchain_holder_snapshot
                    WHERE asset_id = %s AND chain = %s
                    ORDER BY snapshot_date DESC
                    LIMIT %s
                """, (asset_id, chain, days))
                snaps = cur.fetchall()
                if not snaps:
                    return jsonify({
                        "ok": True, "asset_id": asset_id, "symbol": symbol,
                        "name": name, "chain": chain, "snapshots": [], "top": [],
                    })
                snaps = list(reversed(snaps))  # 时间升序

                snapshots = []
                for s in snaps:
                    top = s["top_holders_json"] or []
                    snapshots.append({
                        "date": str(s["snapshot_date"]),
                        "total_holders": s["total_holders"],
                        "top10_concentration": (float(s["top10_concentration"])
                                                if s["top10_concentration"] is not None else None),
                        "whale_7d_pct": (float(s["whale_balance_change_7d_pct"])
                                         if s["whale_balance_change_7d_pct"] is not None else None),
                        "whale_30d_pct": (float(s["whale_balance_change_30d_pct"])
                                          if s["whale_balance_change_30d_pct"] is not None else None),
                        "top_n": len(top),
                    })

                # 相邻对比：最新 vs 上一个快照
                latest_top = snaps[-1]["top_holders_json"] or []
                prev_top_map = {}
                if len(snaps) >= 2:
                    for h in (snaps[-2]["top_holders_json"] or []):
                        prev_top_map[str(h.get("address") or "").lower()] = h

                top = []
                seen_addrs = set()
                for h in latest_top[:top_n]:
                    addr_raw = str(h.get("address") or "").strip()
                    addr_key = addr_raw.lower()
                    if not addr_key or addr_key in seen_addrs:
                        continue
                    seen_addrs.add(addr_key)
                    prev = prev_top_map.get(addr_key)
                    cur_pct = float(h.get("pct") or 0)
                    prev_pct = float(prev.get("pct") or 0) if prev else None
                    top.append({
                        "rank": int(h.get("rank") or 0),
                        "address": addr_raw,
                        "amount": str(h.get("amount") or ""),
                        "pct": cur_pct,
                        "delta_pct": round(cur_pct - prev_pct, 4) if prev_pct is not None else None,
                        "is_new": prev is None,          # 上一快照无此地址 = 新进 top
                    })

                # 批量补地址身份（exchange > market_maker > dex > 其它，按置信度取优）
                if top:
                    addrs = [t["address"].lower() for t in top]
                    cur.execute("""
                        SELECT address, label_type, label_name, confidence
                        FROM biz.onchain_address_label
                        WHERE address = ANY(%s)
                          AND label_type IN ('exchange', 'market_maker', 'dex', 'whale', 'mev_bot')
                          AND confidence IN ('high', 'medium')
                        ORDER BY CASE label_type
                            WHEN 'exchange' THEN 0 WHEN 'market_maker' THEN 1
                            WHEN 'dex' THEN 2 ELSE 3 END,
                            confidence DESC
                    """, (addrs,))
                    labels = {}
                    for r in cur.fetchall():
                        labels.setdefault(str(r["address"]).lower(), r)
                    for t in top:
                        lb = labels.get(t["address"].lower())
                        t["label"] = (lb["label_name"] if lb and lb["label_name"] else
                                      lb["label_type"] if lb else None)
                        t["label_type"] = lb["label_type"] if lb else None

        return jsonify({
            "ok": True,
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "chain": chain,
            "snapshots": snapshots,
            "top": top,
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