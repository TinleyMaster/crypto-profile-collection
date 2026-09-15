#!/usr/bin/env python3
"""历史回填：biz.market_snapshot_daily 从各源表按日期组装（纯 DB，不调外部 API）。

背景：大盘快照表只有 3 条（2026-09-11/14/15），且多数字段为空。
本脚本从已有源数据表按日期组装历史快照，尽量填满字段：
  - btc_price / btc_change_24h_pct  ← biz.asset_market_daily (asset_id=2, cmc)
  - eth_price / eth_change_24h_pct  ← biz.asset_market_daily (asset_id=1209, cmc)
  - fear_greed_value/class          ← biz.fear_greed_daily (metric_date)
  - cefi_index_value                ← biz.cefi_index_daily (metric_date)
  - btc_open_interest_usd           ← biz.btc_oi_daily (metric_date)
  - btc_oi_change_7d_pct            ← biz.btc_oi_daily 窗口计算
  - btc_mvrv_pct_full               ← biz.cm_onchain_percentile_full (cm_symbol='btc')
  - btc_active_addresses            ← biz.cm_asset_onchain_daily (cm_symbol='btc', adr_act_cnt)
  - stablecoin_total_supply_usd     ← biz.stablecoin_supply_daily (total_supply_usd)
  - stablecoin_netflow_7d_usd       ← biz.stablecoin_supply_daily 窗口计算
  - etf_total_flow_7d_usd           ← biz.etf_flow_daily 全币种 7d 合计
  - btc_etf_flow_24h_usd            ← biz.etf_flow_daily BTC 当日

安全策略：
  - 只更新源数据非空的字段，已有值的字段不被覆盖（COALESCE 语义）
  - 幂等：重复运行结果一致
  - 不触碰 total_market_cap / dominance / overall_score 等无历史源的字段

用法：
    python backfill_market_snapshot_history.py                 # 全量回填（默认范围）
    python backfill_market_snapshot_history.py --from 2026-05-27 --to 2026-09-14
    python backfill_market_snapshot_history.py --dry-run       # 预览不写库
    python backfill_market_snapshot_history.py --stats         # 只统计各字段填充情况
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 各源表 asset_id 映射（cm 体系）
BTC_ASSET_ID = 26195   # cm btc（cm_asset_onchain_daily / cm_onchain_percentile_full）
ETH_ASSET_ID = 27116   # cm eth
# asset_market_daily 里 cmc 体系的 BTC/ETH（cmc_historical 覆盖 05-27 起）
AM_BTC_ID = 2
AM_ETH_ID = 1209

# 快照字段 -> (源表, 取数方式)
SNAPSHOT_FIELDS = [
    "btc_price", "btc_change_24h_pct", "eth_price", "eth_change_24h_pct",
    "fear_greed_value", "fear_greed_class",
    "cefi_index_value",
    "btc_open_interest_usd", "btc_funding_rate", "btc_oi_change_7d_pct",
    "btc_mvrv_z_score", "btc_mvrv_pct_full", "btc_active_addresses",
    "btc_realized_price_usd",
    "stablecoin_total_supply_usd", "stablecoin_netflow_7d_usd",
    "etf_total_flow_7d_usd", "btc_etf_flow_24h_usd",
]


def ensure_table(conn) -> None:
    """建表（幂等），与 build_market_snapshot.py 保持一致。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.market_snapshot_daily (
                snapshot_date           DATE           NOT NULL PRIMARY KEY,
                btc_price               NUMERIC(12,2),
                btc_change_24h_pct      NUMERIC(10,4),
                eth_price               NUMERIC(12,2),
                eth_change_24h_pct      NUMERIC(10,4),
                total_market_cap_usd    NUMERIC(24,2),
                btc_dominance_pct       NUMERIC(6,2),
                total_volume_24h_usd    NUMERIC(24,2),
                fear_greed_value        INT,
                fear_greed_class        VARCHAR(20),
                altcoin_season_score    NUMERIC(6,2),
                cefi_index_value        NUMERIC(12,4),
                cefi_change_24h_pct     NUMERIC(10,4),
                btc_open_interest_usd   NUMERIC(24,2),
                btc_funding_rate        NUMERIC(12,6),
                btc_oi_change_7d_pct    NUMERIC(10,4),
                btc_mvrv_z_score        NUMERIC(8,4),
                btc_mvrv_pct_full       NUMERIC(6,4),
                btc_active_addresses    BIGINT,
                btc_realized_price_usd  NUMERIC(12,2),
                stablecoin_total_supply_usd  NUMERIC(24,2),
                stablecoin_netflow_7d_usd    NUMERIC(24,2),
                stablecoin_flow_percentile   NUMERIC(6,4),
                etf_total_flow_7d_usd        NUMERIC(24,2),
                btc_etf_flow_24h_usd         NUMERIC(24,2),
                overall_score               NUMERIC(6,2),
                emotion_subscore            NUMERIC(6,2),
                structure_subscore          NUMERIC(6,2),
                btc_cycle_phase             VARCHAR(30),
                btc_cycle_label             VARCHAR(50),
                cycle_overall_heat          NUMERIC(6,2),
                cycle_phase                 VARCHAR(30),
                cycle_phase_label           VARCHAR(60),
                cycle_consistency_pct       NUMERIC(5,2),
                raw_payload      JSONB,
                fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_snapshot_date
                ON biz.market_snapshot_daily(snapshot_date DESC);
        """)
    conn.commit()


# ========== 源数据加载 ==========

def load_asset_market_daily(conn, asset_id: int) -> dict[date, dict]:
    """asset_market_daily：按日期加载 price/change，cmc 优先于 cmc_historical。"""
    out: dict[date, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT market_date, price_usd, change_24h
            FROM biz.asset_market_daily
            WHERE asset_id = %s
              AND price_usd IS NOT NULL
            ORDER BY market_date ASC
        """, (asset_id,))
        for row in cur.fetchall():
            d, price, chg = row
            if d not in out or out[d].get("src") == "cmc_historical":
                # 同一天多来源：cmc 优先（更新）
                out[d] = {"price": float(price), "change_24h": float(chg) if chg is not None else None}
    return out


def load_fear_greed(conn) -> dict[date, dict]:
    out: dict[date, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, value, value_classification
            FROM biz.fear_greed_daily
            WHERE value IS NOT NULL
            ORDER BY metric_date ASC
        """)
        for d, v, cls in cur.fetchall():
            out[d] = {"value": int(v), "class": cls}
    return out


def load_cefi(conn) -> dict[date, float]:
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, value FROM biz.cefi_index_daily
            WHERE value IS NOT NULL ORDER BY metric_date ASC
        """)
        for d, v in cur.fetchall():
            out[d] = float(v)
    return out


def load_btc_oi(conn) -> dict[date, float]:
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, open_interest FROM biz.btc_oi_daily
            WHERE open_interest IS NOT NULL ORDER BY metric_date ASC
        """)
        for d, v in cur.fetchall():
            out[d] = float(v)
    return out


def load_btc_funding(conn) -> dict[date, float]:
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, funding_rate FROM biz.btc_funding_daily
            WHERE funding_rate IS NOT NULL ORDER BY metric_date ASC
        """)
        for d, v in cur.fetchall():
            out[d] = float(v)
    return out


def load_mvrv_pct(conn) -> dict[date, float]:
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, mvrv_pct_full FROM biz.cm_onchain_percentile_full
            WHERE cm_symbol = 'btc' AND mvrv_pct_full IS NOT NULL
            ORDER BY metric_date ASC
        """)
        for d, v in cur.fetchall():
            out[d] = float(v)
    return out


def load_mvrv_value(conn) -> dict[date, float]:
    """BTC MVRV 比值（cap_mvrv_cur）和价格，用于计算 z-score 与 realized price。"""
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, cap_mvrv_cur, price_usd FROM biz.cm_asset_onchain_daily
            WHERE cm_symbol = 'btc' AND cap_mvrv_cur IS NOT NULL
            ORDER BY metric_date ASC
        """)
        for d, mvrv, price in cur.fetchall():
            out[d] = {"mvrv": float(mvrv), "price": float(price) if price is not None else None}
    return out


def compute_mvrv_z_scores(mvrv_map: dict[date, dict], window: int = 365) -> dict[date, float]:
    """滚动窗口 MVRV z-score：(当前值 - 窗口均值) / 窗口标准差。"""
    dates = sorted(mvrv_map.keys())
    vals = [mvrv_map[d]["mvrv"] for d in dates]
    out: dict[date, float] = {}
    for i, d in enumerate(dates):
        lo = max(0, i - window + 1)
        seg = vals[lo:i + 1]
        if len(seg) < 30:
            continue
        mean = sum(seg) / len(seg)
        var = sum((x - mean) ** 2 for x in seg) / len(seg)
        std = var ** 0.5
        if std <= 1e-12:
            continue
        out[d] = (vals[i] - mean) / std
    return out


def load_btc_active_addresses(conn) -> dict[date, int]:
    out: dict[date, int] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, adr_act_cnt FROM biz.cm_asset_onchain_daily
            WHERE cm_symbol = 'btc' AND adr_act_cnt IS NOT NULL
            ORDER BY metric_date ASC
        """)
        for d, v in cur.fetchall():
            out[d] = int(v)
    return out


def load_stablecoin_supply(conn) -> dict[date, float]:
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric_date, total_supply_usd FROM biz.stablecoin_supply_daily
            WHERE total_supply_usd IS NOT NULL ORDER BY metric_date ASC
        """)
        for d, v in cur.fetchall():
            out[d] = float(v)
    return out


def load_etf_flows(conn) -> tuple[dict[date, float], dict[date, float]]:
    """返回 (全币种每日合计, BTC 每日净流)。单位换算为 USD。"""
    all_total: dict[date, float] = {}
    btc_flow: dict[date, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, flow_date, net_flow_usd FROM biz.etf_flow_daily
            WHERE net_flow_usd IS NOT NULL ORDER BY flow_date ASC
        """)
        for sym, d, v in cur.fetchall():
            val = float(v)
            all_total[d] = all_total.get(d, 0.0) + val
            if sym == "BTC":
                btc_flow[d] = val
    return all_total, btc_flow


# ========== 组装快照 ==========

def _pct_change(a: float, b: float) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / b * 100, 2)


def build_daily_snapshot(d: date, ctx: dict) -> dict:
    """为单个日期组装快照 dict（只含可确定的字段）。"""
    snap: dict = {}

    # 价格（asset_market_daily）
    btc_m = ctx["am_btc"].get(d)
    eth_m = ctx["am_eth"].get(d)
    if btc_m and btc_m.get("price"):
        snap["btc_price"] = round(btc_m["price"], 2)
        if btc_m.get("change_24h") is not None:
            snap["btc_change_24h_pct"] = round(btc_m["change_24h"], 4)
    if eth_m and eth_m.get("price"):
        snap["eth_price"] = round(eth_m["price"], 2)
        if eth_m.get("change_24h") is not None:
            snap["eth_change_24h_pct"] = round(eth_m["change_24h"], 4)

    # 恐贪
    fg = ctx["fear_greed"].get(d)
    if fg:
        snap["fear_greed_value"] = fg["value"]
        if fg.get("class"):
            snap["fear_greed_class"] = fg["class"]

    # CEFI
    if d in ctx["cefi"]:
        snap["cefi_index_value"] = round(ctx["cefi"][d], 4)

    # BTC OI + 7d 变化
    oi_dates = sorted(ctx["btc_oi"].keys())
    if d in ctx["btc_oi"]:
        snap["btc_open_interest_usd"] = round(ctx["btc_oi"][d], 2)
        # 7 天前（最近 <= 7d 的日期）
        prev7 = d - timedelta(days=7)
        cand = [x for x in oi_dates if x <= prev7]
        if cand:
            p = ctx["btc_oi"][cand[-1]]
            chg = _pct_change(ctx["btc_oi"][d], p)
            if chg is not None:
                snap["btc_oi_change_7d_pct"] = round(chg, 4)

    # BTC 资金费率
    if d in ctx["btc_funding"]:
        snap["btc_funding_rate"] = round(ctx["btc_funding"][d], 8)

    # 链上
    if d in ctx["mvrv_pct"]:
        snap["btc_mvrv_pct_full"] = round(ctx["mvrv_pct"][d], 4)
    if d in ctx["btc_active"]:
        snap["btc_active_addresses"] = ctx["btc_active"][d]
    # MVRV z-score（滚动 365d 窗口）与 realized price（= price / MVRV）
    if d in ctx["mvrv_z"]:
        snap["btc_mvrv_z_score"] = round(ctx["mvrv_z"][d], 4)
    mv = ctx["mvrv_value"].get(d)
    if mv and mv.get("mvrv") and mv.get("price"):
        realized = mv["price"] / mv["mvrv"]
        snap["btc_realized_price_usd"] = round(realized, 2)

    # 稳定币
    sc_dates = sorted(ctx["stablecoin"].keys())
    if d in ctx["stablecoin"]:
        snap["stablecoin_total_supply_usd"] = round(ctx["stablecoin"][d], 2)
        # 7d 净流 = 当日 - 7 天前
        prev7 = d - timedelta(days=7)
        cand = [x for x in sc_dates if x <= prev7]
        if cand:
            snap["stablecoin_netflow_7d_usd"] = round(
                ctx["stablecoin"][d] - ctx["stablecoin"][cand[-1]], 2)

    # ETF
    if d in ctx["etf_all"]:
        # 7d 合计（含当日）
        etf_dates = sorted(ctx["etf_all"].keys())
        window = [x for x in etf_dates if d - timedelta(days=6) <= x <= d]
        if window:
            snap["etf_total_flow_7d_usd"] = round(
                sum(ctx["etf_all"][x] for x in window), 2)
    if d in ctx["etf_btc"]:
        snap["btc_etf_flow_24h_usd"] = round(ctx["etf_btc"][d], 2)

    return snap


def load_all_sources(conn) -> dict:
    """加载所有源数据到内存。"""
    mvrv_value = load_mvrv_value(conn)
    return {
        "am_btc": load_asset_market_daily(conn, AM_BTC_ID),
        "am_eth": load_asset_market_daily(conn, AM_ETH_ID),
        "fear_greed": load_fear_greed(conn),
        "cefi": load_cefi(conn),
        "btc_oi": load_btc_oi(conn),
        "btc_funding": load_btc_funding(conn),
        "mvrv_pct": load_mvrv_pct(conn),
        "mvrv_value": mvrv_value,
        "mvrv_z": compute_mvrv_z_scores(mvrv_value),
        "btc_active": load_btc_active_addresses(conn),
        "stablecoin": load_stablecoin_supply(conn),
        "etf_all": load_etf_flows(conn)[0],
        "etf_btc": load_etf_flows(conn)[1],
    }


def upsert_snapshot(conn, d: date, snap: dict, dry_run: bool = False) -> bool:
    """插入或更新快照：只覆盖源数据非空的字段，已有值保留（COALESCE 语义）。

    返回是否产生了写入。
    """
    if not snap:
        return False

    cols = list(snap.keys())
    # 构造 CASE WHEN EXCLUDED.x IS NOT NULL THEN EXCLUDED.x ELSE 原值 END
    set_clauses = ", ".join(
        f"{c} = CASE WHEN EXCLUDED.{c} IS NOT NULL THEN EXCLUDED.{c} ELSE biz.market_snapshot_daily.{c} END"
        for c in cols
    )
    placeholders = ", ".join(["%s"] * len(cols))
    col_names = ", ".join(cols)
    vals = [d] + [snap[c] for c in cols]

    sql = f"""
        INSERT INTO biz.market_snapshot_daily
            (snapshot_date, {col_names}, fetched_at, updated_at)
        VALUES (%s, {placeholders}, NOW(), NOW())
        ON CONFLICT (snapshot_date) DO UPDATE
        SET {set_clauses}, updated_at = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, vals)
    if not dry_run:
        conn.commit()
    return True


def print_stats(conn) -> None:
    """统计各字段填充率。"""
    fields = SNAPSHOT_FIELDS
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM biz.market_snapshot_daily")
        total = cur.fetchone()[0]
        cur.execute("SELECT MIN(snapshot_date), MAX(snapshot_date) FROM biz.market_snapshot_daily")
        mn, mx = cur.fetchone()
    print(f"\n=== 快照表填充情况（共 {total} 条，{mn} ~ {mx}）===")
    with conn.cursor() as cur:
        for f in fields:
            cur.execute(f"SELECT COUNT({f}) FROM biz.market_snapshot_daily WHERE {f} IS NOT NULL")
            cnt = cur.fetchone()[0]
            pct = cnt / total * 100 if total else 0
            mark = "✅" if pct >= 90 else ("🟡" if pct >= 30 else "🔴")
            print(f"  {mark} {f:32s}: {cnt:5d}/{total} ({pct:5.1f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(description="大盘快照历史回填（从源表组装）")
    parser.add_argument("--from", dest="from_date", type=str, default="2026-05-27",
                        help="起始日期 YYYY-MM-DD（默认 2026-05-27）")
    parser.add_argument("--to", dest="to_date", type=str, default=None,
                        help="结束日期 YYYY-MM-DD（默认昨天）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写库")
    parser.add_argument("--stats", action="store_true", help="只统计填充率")
    parser.add_argument("--limit", type=int, default=None, help="只回填最近 N 天（调试用）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        if args.stats:
            print_stats(conn)
            return

        from_date = date.fromisoformat(args.from_date)
        to_date = date.fromisoformat(args.to_date) if args.to_date else date.today() - timedelta(days=1)
        if args.limit:
            from_date = to_date - timedelta(days=args.limit - 1)

        print(f"[snapshot] 回填范围: {from_date} ~ {to_date}")

        # 加载源数据
        print("[snapshot] 加载源数据 ...")
        ctx = load_all_sources(conn)
        print(f"  am_btc={len(ctx['am_btc'])}天 am_eth={len(ctx['am_eth'])}天 "
              f"恐贪={len(ctx['fear_greed'])}天 CEFI={len(ctx['cefi'])}天 "
              f"OI={len(ctx['btc_oi'])}天 资金费率={len(ctx['btc_funding'])}天 "
              f"MVRV%={len(ctx['mvrv_pct'])}天 MVRVz={len(ctx['mvrv_z'])}天 "
              f"活跃={len(ctx['btc_active'])}天 稳定币={len(ctx['stablecoin'])}天 "
              f"ETF合计={len(ctx['etf_all'])}天")

        # 逐日组装 + upsert
        d = from_date
        total_days = (to_date - from_date).days + 1
        written = 0
        filled_days = 0
        while d <= to_date:
            snap = build_daily_snapshot(d, ctx)
            if snap:
                filled_days += 1
                if upsert_snapshot(conn, d, snap, dry_run=args.dry_run):
                    written += 1
            d += timedelta(days=1)

        print(f"\n[snapshot] 完成：{filled_days}/{total_days} 天有数据可填"
              f"{'（DRY RUN 未写库）' if args.dry_run else f'，写入 {written} 条'}")
        print_stats(conn)


if __name__ == "__main__":
    main()
