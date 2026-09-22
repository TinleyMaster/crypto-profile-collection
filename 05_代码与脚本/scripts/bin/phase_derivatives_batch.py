"""
衍生品资金面批量采集脚本。
按市值从高到低遍历资产，批量采集 Binance / OKX / Bybit / Bitget / Gate 五家交易所的
资金费率、未平仓合约 OI、CVD 等衍生品数据，写入 biz.asset_derivatives 表。

用法:
    python phase_derivatives_batch.py --limit 100          # 采集 top 100 市值资产
    python phase_derivatives_batch.py --limit 0            # 全量（所有有 symbol 的资产）
    python phase_derivatives_batch.py --limit 50 --force   # 强制刷新，跳过 15 分钟缓存
    python phase_derivatives_batch.py --limit 50 --delay 0.2  # 每币间隔 0.2s

O1（2026-09-22 工单）：摄取 universe 原为「有市值排名 top-N」，而信号 universe 是全部
Binance USDT 永续（含 meme/小市值）⇒ 近 47% 高置信主池信号无 `funding_rate`。默认把
**近 `--signal-days`(7) 天出现在主池/BRK 信号里、且尚无 `asset_derivatives` 行**的
资产优先纳入采集（`--signal-days 0` 可关闭），从源头对齐两个 universe。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"


def _find_workbench_dir() -> Path:
    """探测 derivatives_client.py 所在目录，兼容本地与多种部署结构。

    容器内按 Dockerfile 扁平复制：05_代码与脚本/workbench/*.py 会落到 /app/ 根目录，
    因此部署环境的真实路径就是 /app（而非任何 workbench 子目录）。逐一探测命中即返回。
    """
    candidates = [
        # Dockerfile 扁平复制：workbench/*.py 直接落到 /app/
        Path("/app"),
        # Zeabur 完整仓库挂载
        Path("/app/05_代码与脚本/workbench"),
        # 本地开发：scripts/bin 与 workbench 同级
        SCRIPT_DIR.parent.parent / "workbench",
        # 本地开发（仓库根为 05_代码与脚本）
        SCRIPT_DIR.parent.parent.parent / "workbench",
    ]
    for c in candidates:
        if (c / "derivatives_client.py").exists():
            return c
    # 兜底：容器环境优先用 /app 根目录，本地回退到 scripts/bin 同级的 workbench
    return Path("/app") if os.path.exists("/app") else (SCRIPT_DIR.parent.parent / "workbench")


WORKBENCH_DIR = _find_workbench_dir()

for _p in (str(SRC_DIR), str(WORKBENCH_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.db.upsert import load_sql, fetch_one

from derivatives_client import EXCHANGE_CLIENTS  # noqa: E402

CACHE_TTL = 15 * 60  # 15 分钟缓存，与 db_stats.py 对齐


def ensure_table(conn) -> None:
    """确保 biz.asset_derivatives 表存在。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.asset_derivatives (
                asset_id INTEGER PRIMARY KEY REFERENCES core.asset(asset_id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                funding_rate NUMERIC(12,8),
                funding_rate_pct NUMERIC(8,4),
                next_funding_time TIMESTAMPTZ,
                funding_rate_7d_avg NUMERIC(12,8),
                funding_rate_30d_avg NUMERIC(12,8),
                total_oi_usd NUMERIC(20,2),
                oi_change_24h_pct NUMERIC(8,2),
                cvd_24h_usd NUMERIC(20,2),
                cvd_ratio_24h NUMERIC(8,4),
                exchanges_json JSONB,
                available_exchanges TEXT[],
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_asset_derivatives_symbol
            ON biz.asset_derivatives(symbol)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_asset_derivatives_fetched
            ON biz.asset_derivatives(fetched_at)
        """)


def get_pending_assets(conn, limit: int, force: bool = False) -> list[dict]:
    """获取待采集资产列表（按市值从高到低）。

    force=False 时跳过 15 分钟内已有缓存的资产。
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if force:
            cur.execute(
                """
                SELECT asset_id, canonical_symbol AS symbol, canonical_name AS name,
                       market_cap_rank
                FROM core.asset
                WHERE canonical_symbol IS NOT NULL
                  AND market_cap_rank IS NOT NULL
                ORDER BY market_cap_rank ASC
                LIMIT %s
                """,
                (limit,),
            )
        else:
            cur.execute(
                """
                SELECT a.asset_id, a.canonical_symbol AS symbol,
                       a.canonical_name AS name, a.market_cap_rank
                FROM core.asset a
                WHERE a.canonical_symbol IS NOT NULL
                  AND a.market_cap_rank IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM biz.asset_derivatives d
                      WHERE d.asset_id = a.asset_id
                        AND d.fetched_at > NOW() - INTERVAL '%s seconds'
                  )
                ORDER BY a.market_cap_rank ASC
                LIMIT %s
                """,
                (CACHE_TTL, limit),
            )
        return cur.fetchall()


def get_total_pending(conn, force: bool = False) -> int:
    with conn.cursor() as cur:
        if force:
            cur.execute(
                "SELECT COUNT(*) FROM core.asset WHERE canonical_symbol IS NOT NULL "
                "AND market_cap_rank IS NOT NULL"
            )
        else:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM core.asset a
                WHERE a.canonical_symbol IS NOT NULL
                  AND a.market_cap_rank IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM biz.asset_derivatives d
                      WHERE d.asset_id = a.asset_id
                        AND d.fetched_at > NOW() - INTERVAL '%s seconds'
                  )
                """,
                (CACHE_TTL,),
            )
        return cur.fetchone()[0]


def _signal_symbol_candidates(symbol: str) -> list[str]:
    """合约符号 → 本库可能使用的裸符号候选（原样 → 去 USDT → 去倍数前缀）。

    与 `scan_daemon._symbol_candidates` 同口径：`scan_signal.symbol` 存合约符号
    （'B2USDT' / '1000FLOKIUSDT'），而 `core.asset.canonical_symbol` 存裸符号
    （'B2' / 'FLOKI'）；不归一会让 O1 缺口查询恒空。
    """
    s = (symbol or "").upper()
    base = s[:-4] if s.endswith("USDT") and len(s) > 4 else s
    out = [s, base]
    for pre in ("1000000", "1000"):
        if base.startswith(pre) and len(base) > len(pre):
            out.append(base[len(pre):])
            break
    return list(dict.fromkeys(out))


def get_signal_gap_assets(conn, days: int) -> list[dict]:
    """O1 缺口资产：近期出现在主池/BRK 信号、且**符号级**尚无资金费率的资产。

    - 作用域 = `pool='main' OR scenario='BRK'`（资金费率的**消费方**；squeeze 池不用 funding）；
    - **按符号级判定覆盖**：`_load_funding_map` 是按**裸符号**注册候选的 ⇒ 只要
      `biz.asset_derivatives` 里该符号的**任一行**（任一 asset_id）有 `funding_rate`，
      信号即可解析出费率 ⇒ 该符号不算缺口；
    - **按符号去重**：`core.asset.canonical_symbol` 不唯一（实测 9.9% 重复、单符号最多 18 行），
      逐行取会导致 DOGE/BTC/SOL 等被算成十几个「缺口」并重复入库 ⇒ 用
      `DISTINCT ON (upper(canonical_symbol))` + 按 `market_cap_rank NULLS LAST, asset_id`
      只取**最优那条**（与 `scan_daemon._get_asset_id` 的选择一致）；
    - 无 `core.asset` 行的 symbol（如 BROCCOLI714）本函数无法覆盖（属主数据治理缺口）。
    """
    if days <= 0:
        return []
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.scan_signal "
            "WHERE signal_ts > NOW() - make_interval(days => %s) "
            "AND (pool = 'main' OR scenario = 'BRK')",
            (days,),
        )
        sig_syms = [r["symbol"] for r in cur.fetchall()]
        if not sig_syms:
            return []
        # 已覆盖符号 = 已有资金费率的 asset_derivatives.symbol 及其全部候选别名
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.asset_derivatives "
            "WHERE funding_rate IS NOT NULL"
        )
        covered: set[str] = set()
        for r in cur.fetchall():
            s = r["symbol"] or ""
            covered.add(s)
            covered.add(s.upper())
            covered.update(_signal_symbol_candidates(s))

    # 未覆盖的候选符号（任一别名已覆盖 ⇒ 该信号非缺口）
    uncovered: set[str] = set()
    for s in sig_syms:
        cands = _signal_symbol_candidates(s)
        if not any(c in covered for c in cands):
            uncovered.update(cands)
    if not uncovered:
        return []

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (upper(a.canonical_symbol))
                   a.asset_id, a.canonical_symbol AS symbol,
                   a.canonical_name AS name, a.market_cap_rank
            FROM core.asset a
            WHERE a.canonical_symbol IS NOT NULL
              AND upper(a.canonical_symbol) = ANY(%s)
            ORDER BY upper(a.canonical_symbol),
                     a.market_cap_rank NULLS LAST, a.asset_id
            """,
            (sorted(uncovered),),
        )
        return cur.fetchall()


def merge_pending(ranked: list[dict], gap_assets: list[dict], limit: int) -> list[dict]:
    """合并「市值 top-N 待采」与「O1 信号缺口」，缺口置顶且不被 `limit` 截断。

    - 缺口资产正是 O1 根因，必须当轮采到 ⇒ `cap = max(limit, len(gap))`；
    - `limit <= 0`（全量）时不截断；
    - 按 asset_id 去重，缺口优先。
    """
    gap_ids = {a["asset_id"] for a in gap_assets}
    merged = list(gap_assets) + [a for a in ranked if a["asset_id"] not in gap_ids]
    if limit > 0:
        merged = merged[:max(limit, len(gap_assets))]
    return merged


def _aggregate_funding(available: list[str], details: dict) -> dict:
    """跨交易所聚合资金费率（纯函数，便于离线单测）。

    有 OI 价值 ⇒ 按 OI 价值加权（原口径不变）；无 OI 价值 ⇒ 等权简单平均
    （**O1 连带修复**：原实现把费率累加嵌在 `if oi_val` 内，OI 缺失时费率被整体
    丢弃 ⇒ 小市值/meme 永远补不上 funding）。两者都无 → None。
    """
    total_oi_value = 0.0
    weighted = {1: 0.0, 7: 0.0, 30: 0.0}
    simple = {1: 0.0, 7: 0.0, 30: 0.0}
    cnt = {1: 0, 7: 0, 30: 0}
    next_funding_ts = None

    for ex in available:
        d = details.get(ex, {})
        oi_val = d.get("open_interest_value") or 0
        fr = d.get("funding_rate")
        if fr is not None:
            simple[1] += fr
            cnt[1] += 1
            if oi_val:
                total_oi_value += oi_val
                weighted[1] += fr * oi_val
            for key, fkey in ((7, "funding_rate_7d_avg"), (30, "funding_rate_30d_avg")):
                v = d.get(fkey)
                if v is not None:
                    simple[key] += v
                    cnt[key] += 1
                    if oi_val:
                        weighted[key] += v * oi_val
        if d.get("next_funding_time"):
            if next_funding_ts is None or d["next_funding_time"] < next_funding_ts:
                next_funding_ts = d["next_funding_time"]

    def _avg(key: int) -> float | None:
        if total_oi_value > 0:
            return weighted[key] / total_oi_value
        return (simple[key] / cnt[key]) if cnt[key] > 0 else None

    return {
        "avg_funding": _avg(1),
        "avg_funding_7d": _avg(7),
        "avg_funding_30d": _avg(30),
        "next_funding_ts": next_funding_ts,
        "total_oi_value": total_oi_value if total_oi_value > 0 else None,
    }


def fetch_one_asset(symbol: str) -> dict:
    """采集单个资产的衍生品数据（5 家交易所并发），返回聚合结果 dict。

    若所有交易所都没有该合约，ok=True 但 available_exchanges 为空。
    """
    symbol = symbol.upper()
    exchanges_detail = {}
    available = []

    def _fetch_exchange(ex_name: str, client) -> dict:
        try:
            sym = client.format_symbol(symbol)
            fr = client.get_funding_rate(sym)
            if not fr:
                return {"exchange": ex_name, "available": False}

            result = {"exchange": ex_name, "symbol": sym, "available": True}
            result["funding_rate"] = fr.funding_rate
            result["next_funding_time"] = fr.next_funding_time
            result["mark_price"] = fr.mark_price

            # 历史资金费率（90 条 ≈ 30 天，每 8h 一次）
            try:
                fr_hist = client.get_funding_rate_history(sym, limit=90)
                if fr_hist:
                    rates = [f.funding_rate for f in fr_hist]
                    result["funding_history_count"] = len(rates)
                    result["funding_rate_7d_avg"] = (
                        sum(rates[:21]) / min(21, len(rates)) if rates else None
                    )
                    result["funding_rate_30d_avg"] = (
                        sum(rates) / len(rates) if rates else None
                    )
            except Exception:
                pass

            # OI
            try:
                oi = client.get_open_interest(sym)
                if oi:
                    result["open_interest"] = oi.open_interest
                    result["open_interest_value"] = oi.open_interest_value
                    if not oi.open_interest_value and fr.mark_price:
                        result["open_interest_value"] = oi.open_interest * fr.mark_price
            except Exception:
                pass

            # OI 历史（24h 变化）
            try:
                oi_hist = client.get_open_interest_history(sym, period="1h", limit=24)
                if oi_hist and len(oi_hist) >= 2:
                    first_oi = oi_hist[0].open_interest_value or oi_hist[0].open_interest
                    last_oi = oi_hist[-1].open_interest_value or oi_hist[-1].open_interest
                    if first_oi and last_oi and first_oi > 0:
                        result["oi_change_24h_pct"] = (
                            (last_oi - first_oi) / first_oi * 100
                        )
            except Exception:
                pass

            # 最近成交（CVD）
            try:
                trades = client.get_recent_trades(sym, limit=500)
                if trades:
                    buy_volume = sum(t.quote_qty for t in trades if not t.is_buyer_maker)
                    sell_volume = sum(t.quote_qty for t in trades if t.is_buyer_maker)
                    total_volume = buy_volume + sell_volume
                    cvd = buy_volume - sell_volume
                    result["cvd_recent"] = cvd
                    result["total_volume_recent"] = total_volume
                    result["cvd_ratio"] = cvd / total_volume if total_volume > 0 else 0
                    result["trades_count"] = len(trades)
            except Exception:
                pass

            return result
        except Exception as e:
            return {"exchange": ex_name, "available": False, "error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_exchange, name, client): name
            for name, client in EXCHANGE_CLIENTS.items()
        }
        for future in concurrent.futures.as_completed(futures):
            ex_name = futures[future]
            try:
                result = future.result()
                exchanges_detail[ex_name] = result
                if result.get("available"):
                    available.append(ex_name)
            except Exception:
                exchanges_detail[ex_name] = {"exchange": ex_name, "available": False}

    # ── 聚合计算（资金费率优先按 OI 价值加权）──
    # ⚠️ O1 连带修复（2026-09-22）：原实现把资金费率累加**嵌在 `if oi_val` 内** ⇒ 当
    # 交易所未返回 OI 价值（小市值/meme 常见）时，**即便费率可取也被整体丢弃**、
    # `avg_funding=None` ⇒ 该类符号永远补不上 funding（实测 MAGMA/UNI/XEC/MIRA/VTHO
    # 补采后仍 NULL）。聚合逻辑见 `_aggregate_funding`：有 OI 价值仍按 OI 加权
    # （口径不变），无 OI 价值退化为**等权简单平均**（有费率即可）。
    _fund = _aggregate_funding(available, exchanges_detail)
    avg_funding = _fund["avg_funding"]
    avg_funding_7d = _fund["avg_funding_7d"]
    avg_funding_30d = _fund["avg_funding_30d"]
    next_funding_ts = _fund["next_funding_ts"]
    total_oi_value = _fund["total_oi_value"] or 0.0

    # OI 24h 变化
    total_oi_change_weighted = 0.0
    oi_change_total_weight = 0.0
    for ex in available:
        d = exchanges_detail[ex]
        oi_val = d.get("open_interest_value") or 0
        oi_chg = d.get("oi_change_24h_pct")
        if oi_val and oi_chg is not None:
            total_oi_change_weighted += oi_chg * oi_val
            oi_change_total_weight += oi_val

    oi_change_24h = (
        total_oi_change_weighted / oi_change_total_weight
        if oi_change_total_weight > 0
        else None
    )

    # CVD 聚合
    total_cvd = 0.0
    total_volume = 0.0
    for ex in available:
        d = exchanges_detail[ex]
        if d.get("cvd_recent") is not None:
            total_cvd += d["cvd_recent"]
        if d.get("total_volume_recent") is not None:
            total_volume += d["total_volume_recent"]

    cvd_ratio = total_cvd / total_volume if total_volume > 0 else None

    return {
        "ok": True,
        "symbol": symbol,
        "available_exchanges": available,
        "exchanges_detail": exchanges_detail,
        "avg_funding": avg_funding,
        "avg_funding_7d": avg_funding_7d,
        "avg_funding_30d": avg_funding_30d,
        "next_funding_ts": next_funding_ts,
        "total_oi_value": total_oi_value if total_oi_value > 0 else None,
        "oi_change_24h": oi_change_24h,
        "total_cvd": total_cvd if total_cvd else None,
        "cvd_ratio": cvd_ratio,
    }


def save_result(conn, asset_id: int, result: dict) -> None:
    """写入/更新 biz.asset_derivatives。"""
    symbol = result["symbol"]
    avg_funding = result["avg_funding"]
    next_ts = result["next_funding_ts"]
    next_funding_time = (
        datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc) if next_ts else None
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biz.asset_derivatives
                (asset_id, symbol, funding_rate, funding_rate_pct, next_funding_time,
                 funding_rate_7d_avg, funding_rate_30d_avg,
                 total_oi_usd, oi_change_24h_pct,
                 cvd_24h_usd, cvd_ratio_24h,
                 exchanges_json, available_exchanges, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (asset_id) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                funding_rate = EXCLUDED.funding_rate,
                funding_rate_pct = EXCLUDED.funding_rate_pct,
                next_funding_time = EXCLUDED.next_funding_time,
                funding_rate_7d_avg = EXCLUDED.funding_rate_7d_avg,
                funding_rate_30d_avg = EXCLUDED.funding_rate_30d_avg,
                total_oi_usd = EXCLUDED.total_oi_usd,
                oi_change_24h_pct = EXCLUDED.oi_change_24h_pct,
                cvd_24h_usd = EXCLUDED.cvd_24h_usd,
                cvd_ratio_24h = EXCLUDED.cvd_ratio_24h,
                exchanges_json = EXCLUDED.exchanges_json,
                available_exchanges = EXCLUDED.available_exchanges,
                fetched_at = NOW()
            """,
            (
                asset_id,
                symbol,
                avg_funding,
                round(avg_funding * 100, 4) if avg_funding is not None else None,
                next_funding_time,
                result["avg_funding_7d"],
                result["avg_funding_30d"],
                round(result["total_oi_value"], 2) if result["total_oi_value"] else None,
                round(result["oi_change_24h"], 2) if result["oi_change_24h"] is not None else None,
                round(result["total_cvd"], 2) if result["total_cvd"] else None,
                round(result["cvd_ratio"], 4) if result["cvd_ratio"] is not None else None,
                json.dumps(result["exchanges_detail"], default=str),
                result["available_exchanges"],
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="衍生品资金面批量采集")
    parser.add_argument("--limit", type=int, default=100,
                        help="采集数量（按市值从高到低），0=全量，默认 100")
    parser.add_argument("--force", action="store_true",
                        help="强制刷新，跳过 15 分钟缓存")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="每币之间间隔秒数，默认 0.2s")
    parser.add_argument("--timeout", type=int, default=30,
                        help="单币采集超时秒数（预留）")
    parser.add_argument("--signal-days", type=int, default=7,
                        help="把近 N 天出现在主池/BRK 信号、且尚无衍生品行的 symbol "
                             "优先纳入采集（O1 缺口对齐），0=关闭，默认 7")
    args = parser.parse_args()

    settings = get_settings()
    t0 = time.time()
    total_success = 0
    total_fail = 0
    total_with_data = 0  # 至少有 1 家交易所数据的资产数

    # ingest_run 审计记录
    insert_ingest_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_sql = load_sql("sys/finish_ingest_run.sql")
    run_id = None
    workflow_name = "WF_DERIVATIVES_BATCH"

    # ingest_run 审计记录：独立连接写入，避免写入失败污染主事务（与 P1-3 同源问题）。
    try:
        with get_connection(settings.database_url) as wconn:
            run_row = fetch_one(
                wconn,
                insert_ingest_sql,
                (
                    "derivatives",
                    "batch_collect",
                    workflow_name,
                    json.dumps(
                        {"limit": args.limit, "force": args.force,
                         "delay": args.delay, "signal_days": args.signal_days},
                        ensure_ascii=False,
                    ),
                    f"top{args.limit}" if args.limit > 0 else "all",
                ),
            )
            run_id = run_row["run_id"]
    except Exception as e:
        print(f"[WARN] ingest_run 记录写入失败（不影响采集）: {e}")

    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        total_pending = get_total_pending(conn, force=args.force)
        # O1：信号 universe 缺口（近 N 天主池/BRK 出现过、且从无衍生品行）优先纳入。
        gap_assets = get_signal_gap_assets(conn, args.signal_days)

        # 市值 top-N 待采（--limit 0 = 全量待采）
        ranked_limit = args.limit if args.limit > 0 else total_pending
        ranked = (get_pending_assets(conn, ranked_limit, force=args.force)
                  if ranked_limit > 0 else [])

        # 缺口资产置顶且**不受 --limit 截断**（它们正是 O1 的根因，必须当轮采到）；
        # 其余按市值顺序补齐并去重。--limit 0（全量）时不截断。
        assets = merge_pending(ranked, gap_assets, args.limit)

        print(f"待采集总数: {total_pending}（市值 top-N）+ {len(gap_assets)}（信号 universe 缺口）"
              f"，本次处理: {len(assets)}")
        print(f"交易所: {', '.join(EXCHANGE_CLIENTS.keys())}")
        if not assets:
            print("无待采集资产（全部已有缓存且未 --force，且无信号缺口）")
            _finish_ingest(settings, run_id, "success", 0, 0, 0, "无待采集资产")
            return 0

        for i, asset in enumerate(assets, 1):
            asset_id = asset["asset_id"]
            symbol = asset.get("symbol", "?")
            rank = asset.get("market_cap_rank", "?")
            print(f"  [{i}/{len(assets)}] #{rank} asset_id={asset_id} {symbol} ... ",
                  end="", flush=True)

            try:
                result = fetch_one_asset(symbol)
                save_result(conn, asset_id, result)
                n_ex = len(result["available_exchanges"])
                total_success += 1
                if n_ex > 0:
                    total_with_data += 1
                    print(f"OK ({n_ex} 交易所)")
                else:
                    print("OK (无合约)")
            except Exception as e:
                total_fail += 1
                print(f"FAIL ({e})")

            if i < len(assets) and args.delay > 0:
                time.sleep(args.delay)

    elapsed = time.time() - t0
    total_processed = total_success + total_fail
    if total_processed == 0:
        status = "success"
        error_msg = "无待采集资产"
    elif total_fail == 0:
        status = "success"
        error_msg = None
    elif total_success == 0:
        status = "failed"
        error_msg = f"全部失败 ({total_fail}/{total_processed})"
    else:
        status = "partial"
        error_msg = f"部分失败 ({total_fail}/{total_processed})"

    _finish_ingest(settings, run_id, status, total_processed,
                   total_with_data, total_fail, error_msg)

    print("\n" + "=" * 60)
    print(f"全部完成，耗时 {elapsed:.1f}s")
    print(f"总计: 成功 {total_success}, 失败 {total_fail}, 有合约 {total_with_data}")
    print(f"状态: {status}")
    print("=" * 60)

    print(json.dumps({
        "status": status,
        "success": total_success,
        "fail": total_fail,
        "with_data": total_with_data,
        "elapsed_s": round(elapsed, 1),
    }, ensure_ascii=False))

    return 1 if status == "failed" else 0


def _finish_ingest(settings, run_id: str | None, status: str,
                   total: int, success: int, fail: int, error_msg: str | None) -> None:
    if not run_id:
        return
    try:
        with get_connection(settings.database_url) as conn:
            fetch_one(
                conn,
                load_sql("sys/finish_ingest_run.sql"),
                (status, 200 if status != "failed" else 500,
                 total, success, fail, error_msg, run_id),
            )
    except Exception as e:
        print(f"[WARN] ingest_run 结束记录写入失败: {e}")


if __name__ == "__main__":
    sys.exit(main())
