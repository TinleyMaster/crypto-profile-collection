#!/usr/bin/env python3
"""截面联动因子挖掘 · 第 2 步：领先-滞后事件研究（只读）。

背景（审计 2026-10-02）：第 1 步相关矩阵已产出干净的高联动对（ρ≥0.6，同实体
去重后）。本步验证这些"联动"是否有**方向性/时序性**——是真联动（A 异动后
B 确实跟涨），还是只是同涨同跌（无先后关系，对预测无用）。

方法（日频事件研究）：
- 取高联动对（复刻第 1 步的去重 + 矩阵构建，ρ ≥ CORR_THRESHOLD）
- 对每个对：找 A 的异动日 T（|excess_ret_A(T)| ≥ THRESH）
- 测三组同向率：
    同期   : B(T) 与 A(T) 同向   → 联动强度（无时序）
    滞后1  : B(T+1) 与 A(T) 同向 → 领先-滞后（A 领先 B 一天）
    滞后2  : B(T+2) 与 A(T) 同向 → 领先-滞后（A 领先 B 两天）
- 对照基线：无条件日期 B 的 excess_ret 正负分布同向率（理论 ~50%）
- 结论：若 滞后1/滞后2 同向率显著 > 基线，说明存在可预测的方向性联动

收益口径：日收益 = change_24h 扣 BTC 超额（同第 1 步，消除"一起涨"伪联动）。

用法：
    python scan_lead_lag_validation.py --corr 0.6 --thresh 5.0
    python scan_lead_lag_validation.py --corr 0.7 --thresh 10.0 --top 30

只读，不落库。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import numpy as np
import psycopg
import psycopg.rows

from crypto_research.config import get_settings  # noqa: E402

MIN_DAYS = 60
MIN_MED_VOL = 1e6
CORR_THRESHOLD = 0.60
MOVE_THRESHOLD = 5.0   # A 异动阈值（|excess_ret| %）
TOP_N = 30


def get_conn():
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        keepalives=1,
        keepalives_idle=15,
        keepalives_interval=5,
        keepalives_count=3,
    )


def root_entity(name: str) -> str:
    """同第 1 步：从资产名提取根实体（剥包装词），合并同标的形态。"""
    import re as _re
    s = (name or "").strip()
    if not s:
        return ""
    s = s.lower()
    s = _re.sub(r"\([^)]*\)", " ", s)
    for kw in ("tokenized", "prestock", "pre-stock", "b-stock", "bstock",
               "xstock", "wrapped", "bridged", "peg", "derivative",
               "tokenized stock", "tokenized etf"):
        s = s.replace(kw, " ")
    s = _re.sub(r"\s+", " ", s).strip(" -:.")
    for tail in (" stock", " etf", " token", " coin", " perpetuals", " lp",
                 " (binance)", " binance"):
        if s.endswith(tail):
            s = s[: -len(tail)].strip()
    if s == "":
        return ""
    return s[:40]


def load_panel(conn, min_days: int) -> tuple[dict[int, dict], list, dict[int, str]]:
    """加载去重后的资产面板 + 板块映射（复刻第 1 步逻辑）。

    Returns:
        panel: {asset_id: {date: excess_ret}}
        dates: 升序日期列表
        sector_map: {asset_id: sector}
    """
    btc_id = conn.execute(
        "SELECT asset_id FROM core.asset WHERE canonical_symbol='BTC' "
        "AND status='active' ORDER BY market_cap DESC NULLS LAST LIMIT 1"
    ).fetchone()["asset_id"]

    assets = conn.execute("""
        WITH stats AS (
            SELECT asset_id, count(*) AS n_days,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY volume_24h) AS med_vol
            FROM biz.v_asset_market_daily_primary WHERE change_24h IS NOT NULL
            GROUP BY asset_id
        )
        SELECT asset_id FROM stats WHERE n_days >= %s AND med_vol >= %s
    """, (min_days, MIN_MED_VOL)).fetchall()
    asset_ids = [r["asset_id"] for r in assets]

    meta = conn.execute("""
        SELECT asset_id, canonical_symbol, canonical_name, market_cap
        FROM core.asset WHERE asset_id = ANY(%s)
    """, (asset_ids,)).fetchall()
    meta_map = {r["asset_id"]: r for r in meta}

    # 同实体去重
    keep_ids, seen_root = [], {}
    for aid in asset_ids:
        m = meta_map.get(aid)
        if not m:
            continue
        sym = m["canonical_symbol"] or ""
        name = m["canonical_name"] or sym
        low = name.lower()
        wrapped = any(k in low for k in ("tokenized", "xstock", "bstock",
                      "b-stock", "wrapped", "bridged", "prestock", "derivative"))
        if wrapped:
            root = re.sub(r"(on|x|b|w|e|z)$", "", sym, flags=re.I)
            if len(root) < 2:
                root = root_entity(name)
        else:
            root = root_entity(name)
        cap = m["market_cap"] or 0.0
        if not root:
            keep_ids.append(aid)
            continue
        if root in seen_root:
            kept = seen_root[root]
            if cap > (meta_map[kept]["market_cap"] or 0.0):
                keep_ids.remove(kept)
                seen_root[root] = aid
                keep_ids.append(aid)
            # 否则丢弃当前
        else:
            seen_root[root] = aid
            keep_ids.append(aid)
    asset_ids = keep_ids

    # 收益面板
    ids = asset_ids + ([btc_id] if btc_id not in asset_ids else [])
    rows = conn.execute("""
        SELECT asset_id, market_date, change_24h
        FROM biz.v_asset_market_daily_primary
        WHERE asset_id = ANY(%s) AND change_24h IS NOT NULL ORDER BY market_date
    """, (ids,)).fetchall()
    panel: dict[int, dict] = {}
    for r in rows:
        panel.setdefault(r["asset_id"], {})[r["market_date"]] = float(r["change_24h"])
    btc_ret = panel.get(btc_id, {})
    for aid in asset_ids:
        for d in list(panel[aid]):
            base = btc_ret.get(d)
            if base is not None:
                panel[aid][d] -= base
    dates = sorted(set().union(*[set(panel[a]) for a in asset_ids]) | set(btc_ret))

    sector_map: dict[int, str] = {}
    for r in conn.execute(
        "SELECT asset_id, sector FROM biz.asset_sector WHERE is_primary = true"
    ).fetchall():
        sector_map.setdefault(r["asset_id"], r["sector"])
    for aid in asset_ids:
        sector_map.setdefault(aid, "unknown")

    return panel, dates, sector_map, btc_id


def high_corr_pairs(corr: np.ndarray, idx_map: dict, thr: float) -> list[tuple]:
    pairs = []
    for i in range(len(idx_map)):
        for j in range(i + 1, len(idx_map)):
            r = corr[i, j]
            if not np.isnan(r) and r >= thr:
                pairs.append((list(idx_map.keys())[i], list(idx_map.keys())[j], float(r)))
    return sorted(pairs, key=lambda x: -x[2])


def event_study(pair: tuple, panel: dict, dates: list, thresh: float) -> dict | None:
    aid_a, aid_b, rho = pair
    ret_a = panel.get(aid_a, {})
    ret_b = panel.get(aid_b, {})
    # 对齐日期序列
    common = sorted(set(ret_a) & set(ret_b))
    if len(common) < MIN_DAYS:
        return None
    idx = {d: i for i, d in enumerate(common)}
    arr_a = np.array([ret_a[d] for d in common])
    arr_b = np.array([ret_b[d] for d in common])

    # 基线：无条件 B 正收益比例
    base_pos = float((arr_b > 0).mean())

    # A 异动日（且前后都有足够数据）
    moves = [i for i, x in enumerate(arr_a) if abs(x) >= thresh and 1 <= i <= len(common) - 3]
    if len(moves) < 5:
        return None

    same_period, lag1_same, lag2_same = [], [], []
    for i in moves:
        same_period.append(int((arr_a[i] > 0) == (arr_b[i] > 0)))
        lag1_same.append(int((arr_a[i] > 0) == (arr_b[i + 1] > 0)))
        lag2_same.append(int((arr_a[i] > 0) == (arr_b[i + 2] > 0)))

    return {
        "rho": rho,
        "n_move": len(moves),
        "base_pos": base_pos,
        "same": np.mean(same_period),
        "lag1": np.mean(lag1_same),
        "lag2": np.mean(lag2_same),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="领先-滞后事件研究（只读）")
    parser.add_argument("--min-days", type=int, default=MIN_DAYS)
    parser.add_argument("--corr", type=float, default=CORR_THRESHOLD)
    parser.add_argument("--thresh", type=float, default=MOVE_THRESHOLD)
    parser.add_argument("--top", type=int, default=TOP_N)
    parser.add_argument("--write", action="store_true",
                        help="将验证通过的对写入 biz.asset_linkage_factor（默认只读）")
    args = parser.parse_args()

    conn = get_conn()
    try:
        panel, dates, sector_map, btc_id = load_panel(conn, args.min_days)
        asset_ids = list(panel.keys())
        print(f"资产 {len(asset_ids)} | 日期 {len(dates)} 天 | 异动阈值 {args.thresh}% | ρ≥{args.corr}")

        # 相关矩阵（只用上三角）
        n = len(asset_ids)
        idx_map = {a: i for i, a in enumerate(asset_ids)}
        corr = np.full((n, n), np.nan)
        for i, aid in enumerate(asset_ids):
            s = np.array([panel[aid].get(d, np.nan) for d in dates])
            corr[i, i] = 1.0
            for j in range(i + 1, n):
                s2 = np.array([panel[asset_ids[j]].get(d, np.nan) for d in dates])
                mask = ~(np.isnan(s) | np.isnan(s2))
                if mask.sum() >= 30:
                    r = np.corrcoef(s[mask], s2[mask])[0, 1]
                    if not np.isnan(r):
                        corr[i, j] = corr[j, i] = r

        pairs = high_corr_pairs(corr, idx_map, args.corr)
        print(f"高联动对（ρ≥{args.corr}）: {len(pairs)}")

        results = []
        for p in pairs:
            es = event_study(p, panel, dates, args.thresh)
            if es:
                results.append((p, es))

        if not results:
            print("无足够样本（A 异动日 ≥5 的对），尝试调低阈值或 --corr")
            return 0

        # 预取 symbol 映射（避免循环内逐对查库）
        sym_map = {}
        for r in conn.execute(
            "SELECT asset_id, canonical_symbol FROM core.asset WHERE asset_id = ANY(%s)",
            (asset_ids,),
        ).fetchall():
            sym_map[r["asset_id"]] = r["canonical_symbol"] or str(r["asset_id"])

        print("\n" + "=" * 78)
        print(f"事件研究结果（A 异动≥{args.thresh}% → B 后续同向率，n≥5 异动日）")
        print("=" * 78)
        print(f"{'对':<26}{'ρ':>5}{'异动n':>6}{'基线%':>7}{'同期%':>7}{'滞后1d%':>8}{'滞后2d%':>8}  '^'=滞后1显著>基线")

        def sym(aid):
            return sym_map.get(aid, str(aid))

        sig_rows = []
        for (a, b, rho), es in results:
            label = f"{sym(a)}/{sym(b)}"
            flag = " *" if (es["lag1"] - es["base_pos"]) > 0.08 and es["lag1"] > 0.55 else ""
            print(f"{label:<26}{rho:>5.2f}{es['n_move']:>6}{es['base_pos']*100:>6.1f}%"
                  f"{es['same']*100:>6.1f}%{es['lag1']*100:>7.1f}%{es['lag2']*100:>7.1f}%  {flag}")
            if flag:
                sig_rows.append((label, es))

        print("\n" + "=" * 78)
        print("显著领先-滞后对（滞后1d 同向率 > 基线 +8pp 且 >55%）：")
        print("=" * 78)
        for label, es in sig_rows:
            print(f"  {label:<26} 同期{es['same']*100:.0f}% 滞后1d{es['lag1']*100:.0f}% 滞后2d{es['lag2']*100:.0f}% 基线{es['base_pos']*100:.0f}%")

        # 聚合：按板块分类看
        print("\n" + "=" * 78)
        print("按「对的方向性」分类（全部高联动对）")
        print("=" * 78)
        n_lag = sum(1 for _, es in results if es["lag1"] > es["base_pos"] + 0.08)
        n_same_only = sum(1 for _, es in results if es["lag1"] <= es["base_pos"] + 0.08 and es["same"] > 0.6)
        print(f"  有领先-滞后效应(滞后1d显著): {n_lag}/{len(results)}")
        print(f"  仅同期联动(无时序,同涨同跌): {n_same_only}/{len(results)}")

        # ---- 落库（--write）：验证通过的对写入 biz.asset_linkage_factor ----
        if args.write:
            # 建表（幂等）
            ddl_path = SCRIPT_DIR.parent / "sql" / "biz" / "asset_linkage_factor.sql"
            ddl = ddl_path.read_text(encoding="utf-8")
            conn.execute(ddl)
            # 清空旧数据（本表为全量重算快照）
            conn.execute("DELETE FROM biz.asset_linkage_factor")
            inserted = 0
            for (a, b, rho), es in results:
                if not (es["lag1"] - es["base_pos"] > 0.08 and es["lag1"] > 0.55):
                    continue
                conn.execute("""
                    INSERT INTO biz.asset_linkage_factor
                        (asset_id_a, asset_id_b, correlation, lag1_rate, lag2_rate,
                         base_rate, n_move)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (a, b, rho, es["lag1"], es["lag2"], es["base_pos"], es["n_move"]))
                inserted += 1
            conn.commit()
            print("\n" + "=" * 78)
            print(f"落库完成：biz.asset_linkage_factor 写入 {inserted} 对（验证通过：滞后1d 显著）")
            print("=" * 78)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
