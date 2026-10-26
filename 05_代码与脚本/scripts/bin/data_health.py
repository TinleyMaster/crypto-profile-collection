#!/usr/bin/env python3
"""投研数据体检模块（P1 阶段）：资产定位 + 数据域新鲜度/覆盖率矩阵。

职责：
1. resolve_asset()：把用户输入（symbol / name / asset_id）解析成 (asset_id, symbol, name, sector)。
2. check_data_health()：对目标资产各数据域做新鲜度 + 覆盖率检查，输出绿/黄/红矩阵。

被 run_research.py 调用；也可单独运行 `py -3.12 data_health.py --symbol BTC` 快速体检。

退出码（HC-006 契约，上层必须据此区分，不得一律当失败）：
    0 = 体检完成
    1 = 资产不存在（库可达、已查证）
    3 = 数据库不可达（本次无法判定，≠ 数据不可得）
    4 = 环境缺依赖 / 解释器不对（先修环境）
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 路径约定（与 bin 下其他脚本一致）───────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
_scripts_src = os.path.join(_code_root, "scripts", "src")
for cand in (_scripts_src, os.path.join(_code_root, "workbench"), "/app"):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

# ── HC-006：环境/依赖缺失不得伪装成「数据不可得」────────────────────
# 本机裸 `python` = WorkBuddy 内置 3.13（无 psycopg），正确解释器为 `py -3.12`。
# 若直接 import，缺依赖时脚本会在 import 阶段崩成 ModuleNotFoundError 堆栈，
# 上层只看到「stdout 一片空白」，极易被解读成「库里没有数据」——2026-10-26 真实踩过。
# 故此处捕获并降级为显式退出码 4（ENV_BROKEN），绝不静默、绝不伪装。
_IMPORT_ERROR: BaseException | None = None
try:
    import psycopg  # noqa: E402
    import psycopg_pool  # noqa: E402
    from crypto_research.config import get_settings  # noqa: E402
    from crypto_research.db.conn import get_connection  # noqa: E402
except ModuleNotFoundError as _ie:  # noqa: N816
    # 被其他脚本 import（run_research.py / build_factors.py 等）时必须原样抛出：
    # 让调用方看到真实的缺依赖栈，而不是拿到一个 None 后在别处变成
    # 「TypeError: 'NoneType' object is not callable」这种更难排查的假象。
    if __name__ != "__main__":
        raise
    _IMPORT_ERROR = _ie
    psycopg = None  # type: ignore[assignment]
    psycopg_pool = None  # type: ignore[assignment]
    get_settings = None  # type: ignore[assignment]
    get_connection = None  # type: ignore[assignment]

# ── 退出码契约（HC-006）：任何上层脚本/agent 必须能区分这三种情况 ──────
EXIT_OK = 0
EXIT_NOT_FOUND = 1            # 资产确实不存在（库可达、查过了、没有）
EXIT_DB_UNREACHABLE = 3       # 库连不上 → 本次无法判定，不是「数据不可得」
EXIT_ENV_BROKEN = 4           # 解释器缺依赖 / 环境坏了 → 先修环境

# 资产解析用（public 表存在历史遗留，优先 biz 层）
ASSET_LOOKUP_SQL = """
SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type,
       cb.cmc_id, COALESCE(a.primary_sector, 'unknown') AS sector,
       a.market_cap_rank
FROM core.asset a
LEFT JOIN biz.coin_basic cb ON cb.asset_id = a.asset_id
WHERE a.status = 'active'
  AND (
        (%(by_id)s AND a.asset_id = %(id_val)s)
        OR (NOT %(by_id)s AND (
              UPPER(a.canonical_symbol) = %(sym_upper)s
              OR UPPER(a.canonical_name) = %(sym_upper)s
              OR EXISTS (SELECT 1 FROM biz.asset_binance_pair p
                         WHERE p.asset_id = a.asset_id AND UPPER(p.base_symbol) = %(sym_upper)s)))
      )
ORDER BY a.market_cap_rank NULLS LAST, a.asset_id
LIMIT 20
"""


def resolve_asset(conn, query: str | int) -> list[dict]:
    """解析资产输入 → 候选列表。输入可为 asset_id(int)/symbol/name。

    返回按市值排名排序的候选（多个同名符号时由调用方决定）。
    """
    by_id = isinstance(query, int)
    if by_id:
        sym = ""
        sym_upper = ""
    else:
        q = str(query).strip().upper()
        # 去掉常见后缀（USDT/PERP 等），BTCUSDT → BTC
        for suffix in ("USDT", "USDC", "PERP", "USD"):
            if q.endswith(suffix) and len(q) > len(suffix):
                q = q[: -len(suffix)]
                break
        sym = q
        sym_upper = q

    with conn.cursor() as cur:
        cur.execute(
            ASSET_LOOKUP_SQL,
            {"by_id": by_id, "id_val": query if by_id else 0,
             "sym_upper": sym_upper},
        )
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


# ── 数据域健康检查定义 ──────────────────────────────────────────────
# 每项：(键名, 中文名, 查询SQL, 容忍滞后天数, 覆盖阈值)
# SQL 中可用 {aid}（asset_id）、{sym}（binance symbol 如 BTCUSDT）。
# 返回 (最新时间, 记录数)；无记录 → latest=None, cnt=0。
HEALTH_CHECKS = [
    ("market_daily", "行情日线(CMC)",
     "SELECT MAX(market_date), COUNT(*) FROM biz.asset_market_daily WHERE asset_id={aid}", 2, 30),
    ("klines_1h", "K线1h(Binance)",
     "SELECT MAX(open_time), COUNT(*) FROM biz.asset_klines WHERE symbol='{sym}' AND interval='1h'", 1, 100),
    ("klines_1d", "K线日线(Binance)",
     "SELECT MAX(open_time), COUNT(*) FROM biz.asset_klines WHERE symbol='{sym}' AND interval='1d'", 2, 100),
    ("derivatives", "合约快照(Coinglass)",
     "SELECT MAX(fetched_at), COUNT(*) FROM biz.asset_derivatives WHERE asset_id={aid}", 1, 1),
    ("funding_hist", "资金费率历史",
     "SELECT MAX(funding_time), COUNT(*) FROM biz.funding_rate_hist WHERE symbol='{sym}'", 2, 5),
    ("lsr", "多空比",
     "SELECT MAX(ts), COUNT(*) FROM biz.long_short_ratio WHERE symbol='{sym}'", 3, 5),
    ("liq_snapshot", "爆仓快照",
     "SELECT MAX(ts), COUNT(*) FROM biz.liquidation_snapshot WHERE symbol='{sym}'", 1, 5),
    ("etf_flow", "ETF资金流",
     "SELECT MAX(flow_date), COUNT(*) FROM biz.etf_flow_daily WHERE symbol ILIKE '{sym_base}'", 5, 5),
    ("fear_greed", "恐贪指数",
     "SELECT MAX(metric_date), COUNT(*) FROM biz.fear_greed_daily", 2, 30),
    ("stablecoin", "稳定币供应",
     "SELECT MAX(metric_date), COUNT(*) FROM biz.stablecoin_supply_daily", 3, 30),
    ("macro", "宏观日线",
     "SELECT MAX(metric_date), COUNT(*) FROM biz.macro_market_daily", 5, 30),
    ("netflow_hourly", "交易所净流(小时)",
     "SELECT MAX(bucket_hour), COUNT(*) FROM biz.onchain_netflow_hourly WHERE asset_id={aid}", 2, 24),
    ("cm_onchain", "链上指标(CoinMetrics)",
     "SELECT MAX(metric_date), COUNT(*) FROM biz.cm_asset_onchain_daily WHERE asset_id={aid}", 3, 30),
    ("factor_elasticity", "因子-价格弹性",
     "SELECT MAX(as_of), COUNT(*) FROM biz.asset_price_elasticity WHERE asset_id={aid}", 7, 1),
    ("factor_unlock_reaction", "因子-解锁反应",
     "SELECT MAX(as_of), COUNT(*) FROM biz.asset_unlock_reaction WHERE asset_id={aid}", 7, 0),
    ("factor_flow", "因子-资金流",
     "SELECT MAX(as_of), COUNT(*) FROM biz.asset_flow_factor WHERE asset_id={aid}", 7, 1),
    ("doc_entries", "文档入口链接",
     "SELECT MAX(created_at), COUNT(*) FROM biz.doc_source_entry WHERE asset_id={aid}", 90, 1),
    ("doc_assets", "文档资产",
     "SELECT MAX(created_at), COUNT(*) FROM biz.doc_asset WHERE asset_id={aid}", 90, 1),
    ("whitepaper_summary", "白皮书摘要",
     "SELECT MAX(created_at), COUNT(*) FROM biz.doc_whitepaper_summary WHERE asset_id={aid}", 90, 0),
    ("github", "GitHub活跃",
     "SELECT MAX(fetched_at), COUNT(*) FROM biz.github_repo_activity", 30, 1),
    ("tokenomics", "代币经济学",
     "SELECT MAX(updated_at), COUNT(*) FROM biz.asset_tokenomics WHERE asset_id={aid}", 60, 0),
    ("unlock_events", "解锁事件",
     "SELECT MAX(updated_at), COUNT(*) FROM biz.asset_unlock_event WHERE asset_id={aid}", 60, 0),
    ("token_unlocks", "解锁总览(Tokenomist)",
     "SELECT MAX(updated_at), COUNT(*) FROM biz.asset_token_unlocks WHERE asset_id={aid}", 60, 0),
    ("social_heat", "社交热度",
     "SELECT MAX(fetched_at), COUNT(*) FROM biz.asset_social_heat WHERE asset_id={aid}", 3, 1),
    ("catalyst", "催化剂新闻",
     "SELECT MAX(published_at), COUNT(*) FROM biz.asset_catalyst WHERE asset_id={aid}", 3, 1),
    ("raises", "融资记录",
     "SELECT MAX(updated_at), COUNT(*) FROM biz.asset_raises WHERE asset_id={aid}", 180, 0),
    ("contract_security", "合约安全",
     "SELECT MAX(scanned_at), COUNT(*) FROM biz.asset_contract_security WHERE asset_id={aid}", 90, 0),
    ("liquidity", "流动性(DEX)",
     "SELECT MAX(scanned_at), COUNT(*) FROM biz.asset_liquidity WHERE asset_id={aid}", 90, 0),
    ("risk_labels", "风险标签",
     "SELECT MAX(computed_at), COUNT(*) FROM biz.asset_risk_labels WHERE asset_id={aid}", 30, 0),
    ("perf_daily", "ATH/ATL表现",
     "SELECT MAX(perf_date), COUNT(*) FROM biz.asset_perf_daily WHERE asset_id={aid}", 3, 5),
]


def _binance_symbol(conn, aid: int) -> str:
    """asset_id → Binance 永续 symbol（BTCUSDT），用于 klines/费率/多空比等表。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM biz.asset_binance_pair WHERE asset_id=%s ORDER BY match_kind LIMIT 1",
            (aid,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        # 兜底：直接用 canonical_symbol
        cur.execute("SELECT canonical_symbol FROM core.asset WHERE asset_id=%s", (aid,))
        row = cur.fetchone()
        return (row[0] if row else "") + "USDT"


_TABLE_RE = re.compile(r"FROM\s+(biz|core)\.(\w+)", re.IGNORECASE)


def _table_global_count(conn, sql_tpl: str) -> int | None:
    """0 条时探表级全局行数：区分「本资产确无数据」与「表级异常/空表」。

    背景（2026-10-23 实测）：晨间 XMR/ZEC 体检中 CM 链上指标报"0 条"，被误读为
    "结构性无数据"；实为外部批量载入事务在体检之后才提交（入库竞态）。查询本身无误，
    但矩阵读者无法从"0 条"区分三种情况。表名从 HEALTH_CHECKS 的固定 SQL 常量中提取
    （非外部输入，正则仅匹配 schema.单词表名），提取/查询失败返回 None，不影响主流程。
    """
    m = _TABLE_RE.search(sql_tpl)
    if not m:
        return None
    schema, table = m.group(1).lower(), m.group(2)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {schema}.{table}")
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def check_data_health(conn, asset_id: int, binance_symbol: str | None = None) -> dict:
    """对目标资产执行全部健康检查。

    返回：
    {
      "asset_id": ...,
      "binance_symbol": ...,
      "checked_at": iso,
      "items": [ {key,label,status,latest,count,detail} ... ],
      "green": n, "yellow": n, "red": n,
      "coverage_score": 0-100,
    }
    status: green(新鲜且覆盖) / yellow(滞后或覆盖不足) / red(缺失或严重滞后)
    """
    if not binance_symbol:
        binance_symbol = _binance_symbol(conn, asset_id)
    sym_base = binance_symbol.replace("USDT", "").replace("USDC", "")

    items = []
    now = datetime.now(timezone.utc)
    for key, name, sql_tpl, max_lag_days, min_count in HEALTH_CHECKS:
        sql = sql_tpl.format(aid=asset_id, sym=binance_symbol, sym_base=sym_base)
        latest = None
        cnt = 0
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                if row:
                    latest, cnt = row[0], (row[1] or 0)
        except Exception as e:
            items.append({
                "key": key, "label": name, "status": "red",
                "latest": None, "count": 0,
                "detail": f"查询失败: {str(e)[:80]}",
            })
            continue

        # 判定状态
        status = "red"
        detail = []
        if latest is not None:
            # latest 可能是 datetime 或 date；datetime 可能带/不带 tz，统一成带 tz 的 datetime
            if isinstance(latest, datetime):
                lag_ts = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
            else:
                lag_ts = datetime(latest.year, latest.month, latest.day, tzinfo=timezone.utc)
            lag = (now - lag_ts).days
            if lag <= max_lag_days:
                status = "green"
                detail.append(f"最新 {latest:%Y-%m-%d}" + (" " + latest.strftime("%H:%M") if isinstance(latest, datetime) else "") + " UTC")
            else:
                status = "yellow"
                detail.append(f"滞后 {lag} 天（阈值 {max_lag_days}）")
        else:
            detail.append("无数据")

        if cnt < min_count and status != "red":
            status = "yellow"
        detail.append(f"{cnt} 条")
        if min_count > 0 and cnt < min_count:
            detail.append(f"少于阈值 {min_count}")
        if cnt == 0:
            # 0 条歧义治理（2026-10-23）：探表级全局行数，让矩阵读者一眼区分
            # 「本资产无」/「空表」/「表有数据（他资产）」——防止再把入库竞态
            # 或表级异常误读成"该资产结构性无数据"。
            g = _table_global_count(conn, sql_tpl)
            if g is not None:
                if g > 0:
                    detail.append(f"该表全局 {g:,} 行（其他资产有数据，本资产确无）")
                else:
                    detail.append("该表全局 0 行（空表）")

        items.append({
            "key": key, "label": name, "status": status,
            "latest": latest.isoformat() if latest else None,
            "count": cnt,
            "detail": "；".join(detail),
        })

    green = sum(1 for i in items if i["status"] == "green")
    yellow = sum(1 for i in items if i["status"] == "yellow")
    red = sum(1 for i in items if i["status"] == "red")
    total = len(items)
    coverage_score = round(100 * (green + 0.5 * yellow) / total) if total else 0

    return {
        "asset_id": asset_id,
        "binance_symbol": binance_symbol,
        "checked_at": now.isoformat(),
        "items": items,
        "green": green, "yellow": yellow, "red": red,
        "coverage_score": coverage_score,
    }


def render_health_md(health: dict) -> str:
    """健康矩阵 → Markdown 表格。"""
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    lines = [
        "### 数据可信度矩阵（P1 数据体检）",
        "",
        f"> 检查时间：{health['checked_at'][:16]} UTC ｜ 数据覆盖率评分：**{health['coverage_score']}/100**"
        f"（🟢{health['green']} 🟡{health['yellow']} 🔴{health['red']}）",
        "",
        "| 数据域 | 状态 | 详情 |",
        "|---|---|---|",
    ]
    for i in health["items"]:
        lines.append(f"| {i['label']} | {emoji.get(i['status'], '?')} | {i['detail']} |")
    return "\n".join(lines)


def _explain_db_unreachable(e: BaseException) -> None:
    """库不可达的统一出口：明确区分「库挂了」与「数据缺失」（HC-006）。

    背景：get_connection() 的重试日志全部走 stderr，且要等 20s+（池自身 60s 超时）
    才抛异常。若调用方只读 stdout，看到的是一片空白 —— 这正是把「库连不上」
    误判成「数据不可得」的成因。
    """
    print("", file=sys.stderr)
    print("[health] ❌ DB_UNREACHABLE — 数据库不可达，本次体检未完成", file=sys.stderr)
    print(f"[health]    异常: {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
    print("[health]    ⚠️ 这不代表数据缺失。禁止据此判定「数据不可得」。", file=sys.stderr)
    print("[health]    排查顺序:", file=sys.stderr)
    print("[health]      1) tailscale status → 节点 active 且 rx > 0（rx=0 即数据面不通）",
          file=sys.stderr)
    print("[health]      2) nslookup derp9.tailscale.com → 返回 198.18.x 即 Clash fake-ip 劫持",
          file=sys.stderr)
    print("[health]      3) curl -w '%{remote_ip}' https://app.tokenomics.com → 是否 127.0.0.1",
          file=sys.stderr)
    print("[health]      4) 管理员: Stop-Service FlClashHelperService; Disable-NetAdapter okz",
          file=sys.stderr)
    print("[health]      5) 一键三层自检: py -3.12 diag_net.py（退出码 0=全通 2=隧道 3=DB）",
          file=sys.stderr)


def main() -> int:
    import argparse

    if _IMPORT_ERROR is not None:
        _name = getattr(_IMPORT_ERROR, "name", None) or str(_IMPORT_ERROR)
        print(f"[health] ❌ ENV_BROKEN — 当前解释器缺依赖: {_name}", file=sys.stderr)
        print(f"[health]    实际解释器: {sys.executable}", file=sys.stderr)
        print("[health]    正确用法: py -3.12 bin/data_health.py --symbol BTC", file=sys.stderr)
        print("[health]    ⚠️ 这不代表数据缺失。", file=sys.stderr)
        return EXIT_ENV_BROKEN

    parser = argparse.ArgumentParser(description="投研数据体检（P1）")
    parser.add_argument("--symbol", required=True, help="币种符号或名称，如 BTC / BTCUSDT / Bitcoin")
    args = parser.parse_args()

    try:
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            raw_query = args.symbol
            q = int(raw_query) if raw_query.isdigit() else raw_query
            candidates = resolve_asset(conn, q)
            if not candidates:
                print(f"[health] 未找到资产: {args.symbol}（库可达，已查证不存在）")
                return EXIT_NOT_FOUND
            # 取市值排名最高者
            cand = candidates[0]
            print(f"[health] 命中: {cand['asset_id']} {cand['canonical_symbol']} "
                  f"{cand['canonical_name']} "
                  f"(rank={cand['market_cap_rank']}, sector={cand['sector']})")
            health = check_data_health(conn, cand["asset_id"])
            print(render_health_md(health))
            return EXIT_OK
    except (psycopg_pool.PoolTimeout, psycopg.OperationalError, RuntimeError) as e:
        # 只捕获这三类，不用 except Exception：
        #   conn.py 只抛 PoolTimeout / OperationalError / RuntimeError("无法从连接池取得连接")
        # 业务层若将来引入 RuntimeError，仍需保留可观测性（不静默吞异常）。
        _explain_db_unreachable(e)
        return EXIT_DB_UNREACHABLE



if __name__ == "__main__":
    raise SystemExit(main())
