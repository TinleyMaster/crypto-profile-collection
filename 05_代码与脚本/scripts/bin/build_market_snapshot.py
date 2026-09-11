"""大盘每日快照生成脚本。

调用 macro_market.get_market_overview() 计算大盘全景，抽平核心指标写入
biz.market_snapshot_daily 表。完整 JSON 存 raw_payload（可选，默认存）。

幂等：同一天重复运行会更新而非重复插入。

用法：
    python build_market_snapshot.py              # 生成当日快照
    python build_market_snapshot.py --no-raw     # 不存完整 JSON（省空间）
    python build_market_snapshot.py --dry-run    # 预览，不写入
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
WORKBENCH_DIR = SCRIPT_DIR.parent.parent / "workbench"

for p in (str(PROJECT_SRC), str(WORKBENCH_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

sys.stdout.reconfigure(line_buffering=True)


def ensure_table(conn) -> None:
    """建表（幂等）。"""
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
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_snapshot_score
                ON biz.market_snapshot_daily(overall_score);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_snapshot_cycle
                ON biz.market_snapshot_daily(btc_cycle_phase);
        """)
    conn.commit()


def _get(obj, *keys, default=None):
    """安全嵌套取值：_get(d, 'a', 'b', 'c')。"""
    cur = obj
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int):
            cur = cur[k] if k < len(cur) else None
        else:
            return default
    return cur if cur is not None else default


def _to_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _to_int(v, default=None):
    if v is None:
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def extract_snapshot(overview: dict) -> dict:
    """从 overview 结果中抽平核心指标。"""
    # 价格
    btc_klines = _get(overview, "dimensions", "2盘面", "data", "btc") or {}
    eth_klines = _get(overview, "dimensions", "2盘面", "data", "eth") or {}
    btc_price = _to_float(btc_klines.get("price"))
    btc_change_24h = _to_float(btc_klines.get("change_24h_pct"))
    eth_price = _to_float(eth_klines.get("price"))
    eth_change_24h = _to_float(eth_klines.get("change_24h_pct"))

    # 市值
    gm = _get(overview, "dimensions", "1体量", "data") or {}
    total_mcap = _to_float(gm.get("total_market_cap"))
    btc_dom = _to_float(gm.get("btc_dominance"))
    total_vol = _to_float(gm.get("total_volume_24h"))

    # 情绪
    emo_data = _get(overview, "dimensions", "3情绪", "data") or {}
    fg = emo_data.get("fear_greed") or {}
    fg_value = _to_int(fg.get("value"))
    fg_class = fg.get("value_classification") or fg.get("classification")
    alt_season = _to_float(_get(emo_data, "altcoin_season", "score"))
    cefi = emo_data.get("cefi") or {}
    cefi_val = _to_float(cefi.get("value"))
    cefi_change = _to_float(cefi.get("change_24h_pct"))

    # 衍生品
    der = _get(overview, "dimensions", "3衍生品", "data") or {}
    btc_oi = _to_float(der.get("btc_open_interest"))
    btc_funding = _to_float(der.get("btc_funding_rate"))
    btc_oi_change_7d = _to_float(der.get("btc_oi_change_7d_pct"))

    # 链上
    btc_onchain = _get(overview, "dimensions", "6链上", "data") or {}
    mvrv_z = _to_float(btc_onchain.get("mvrv_z_score"))
    mvrv_pct = _to_float(btc_onchain.get("mvrv_percentile_full"))
    active_addr = _to_int(btc_onchain.get("active_addresses"))
    realized_price = _to_float(btc_onchain.get("realized_price"))

    # 稳定币
    sc_data = _get(overview, "dimensions", "5板块", "data") or {}
    sc_total_supply = None
    sc_netflow_7d = None
    sc_flow_pct = sc_data.get("stablecoin_flow_percentile")
    sc_supply_hist = sc_data.get("stablecoin_supply_history") or []
    if sc_supply_hist:
        sc_total_supply = _to_float(sc_supply_hist[-1])
    # 从 netflow 历史算 7d
    sc_flow_hist = sc_data.get("stablecoin_flow_history") or []
    if sc_flow_hist and len(sc_flow_hist) >= 1:
        last_7 = sc_flow_hist[-7:] if len(sc_flow_hist) >= 7 else sc_flow_hist
        sc_netflow_7d = sum(_to_float(x, 0) for x in last_7)

    # ETF
    etf_data = _get(overview, "dimensions", "4机构", "data") or {}
    etf_flow_7d = _to_float(etf_data.get("total_flow_7d"))
    btc_etf_flow_24h = _to_float(etf_data.get("btc_flow_24h"))

    # 综合评分
    summary = overview.get("summary") or {}
    overall = None
    emo_sub = None
    struct_sub = None
    # 从 score 的 components 里反推
    # overview 里可能直接有，这里兼容多种情况
    if isinstance(overview.get("overall_score"), (int, float)):
        overall = _to_float(overview["overall_score"])
    emo_sub = _to_float(summary.get("emotion_subscore"))
    struct_sub = _to_float(summary.get("structure_subscore"))

    # 如果没有 overall，尝试从 scoring 结果里拿
    if overall is None and _get(overview, "scoring", "score") is not None:
        overall = _to_float(_get(overview, "scoring", "score"))

    # 周期
    cycle = overview.get("btc_cycle") or {}
    cycle_phase = cycle.get("phase")
    cycle_label = cycle.get("phase_label")

    # 大盘周期热力图
    dash = overview.get("cycle_dashboard") or {}
    cycle_overall_heat = _to_float(dash.get("overall_heat"))
    dash_phase = dash.get("phase")
    dash_phase_label = dash.get("phase_label")
    cycle_consistency = _to_float(dash.get("consistency_pct"))

    return {
        "btc_price": btc_price,
        "btc_change_24h_pct": btc_change_24h,
        "eth_price": eth_price,
        "eth_change_24h_pct": eth_change_24h,
        "total_market_cap_usd": total_mcap,
        "btc_dominance_pct": btc_dom,
        "total_volume_24h_usd": total_vol,
        "fear_greed_value": fg_value,
        "fear_greed_class": fg_class,
        "altcoin_season_score": alt_season,
        "cefi_index_value": cefi_val,
        "cefi_change_24h_pct": cefi_change,
        "btc_open_interest_usd": btc_oi,
        "btc_funding_rate": btc_funding,
        "btc_oi_change_7d_pct": btc_oi_change_7d,
        "btc_mvrv_z_score": mvrv_z,
        "btc_mvrv_pct_full": mvrv_pct,
        "btc_active_addresses": active_addr,
        "btc_realized_price_usd": realized_price,
        "stablecoin_total_supply_usd": sc_total_supply,
        "stablecoin_netflow_7d_usd": sc_netflow_7d,
        "stablecoin_flow_percentile": sc_flow_pct,
        "etf_total_flow_7d_usd": etf_flow_7d,
        "btc_etf_flow_24h_usd": btc_etf_flow_24h,
        "overall_score": overall,
        "emotion_subscore": emo_sub,
        "structure_subscore": struct_sub,
        "btc_cycle_phase": cycle_phase,
        "btc_cycle_label": cycle_label,
        "cycle_overall_heat": cycle_overall_heat,
        "cycle_phase": dash_phase,
        "cycle_phase_label": dash_phase_label,
        "cycle_consistency_pct": cycle_consistency,
    }


def upsert_snapshot(conn, snapshot_date, snap: dict, raw_payload: dict | None) -> None:
    """插入或更新快照。"""
    fields = list(snap.keys())
    placeholders = ", ".join(["%s"] * len(fields))
    col_names = ", ".join(fields)
    updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields)

    raw_col = ", raw_payload" if raw_payload else ""
    raw_ph = ", %s" if raw_payload else ""
    raw_upd = ", raw_payload = EXCLUDED.raw_payload" if raw_payload else ""

    sql = f"""
        INSERT INTO biz.market_snapshot_daily
            (snapshot_date, {col_names}{raw_col}, fetched_at, updated_at)
        VALUES (%s, {placeholders}{raw_ph}, NOW(), NOW())
        ON CONFLICT (snapshot_date) DO UPDATE
        SET {updates}{raw_upd}, updated_at = NOW()
    """

    vals = [snapshot_date] + [snap[f] for f in fields]
    if raw_payload:
        vals.append(json.dumps(raw_payload, ensure_ascii=False))

    with conn.cursor() as cur:
        cur.execute(sql, vals)
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="大盘每日快照生成")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    parser.add_argument("--no-raw", action="store_true", help="不存完整 JSON（省空间）")
    args = parser.parse_args()

    # 延迟 import，先建表
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

    print("[market_snapshot] 正在计算大盘 overview ...")
    from macro_market import get_market_overview
    overview = get_market_overview()

    snap = extract_snapshot(overview)
    today = datetime.now(tz=timezone.utc).date()

    print(f"[market_snapshot] 快照日期: {today}")
    print(f"[market_snapshot] BTC: ${snap['btc_price']:,.0f}" if snap["btc_price"] else "[market_snapshot] BTC: N/A")
    print(f"[market_snapshot] 恐贪: {snap['fear_greed_value']}" if snap["fear_greed_value"] else "[market_snapshot] 恐贪: N/A")
    print(f"[market_snapshot] 综合评分: {snap['overall_score']}" if snap["overall_score"] else "[market_snapshot] 综合评分: N/A")
    print(f"[market_snapshot] 周期: {snap['btc_cycle_phase']} / {snap['btc_cycle_label']}")
    print(f"[market_snapshot] 大盘周期热度: {snap['cycle_overall_heat']} / {snap['cycle_phase_label']}"
          if snap["cycle_overall_heat"] else "[market_snapshot] 大盘周期热度: N/A")

    if args.dry_run:
        print("[market_snapshot] DRY RUN: 不写入")
        return

    with get_connection(settings.database_url) as conn:
        raw = None if args.no_raw else overview
        upsert_snapshot(conn, today, snap, raw)
        print(f"[market_snapshot] 已写入 {today} 的快照 ✓")


if __name__ == "__main__":
    main()
