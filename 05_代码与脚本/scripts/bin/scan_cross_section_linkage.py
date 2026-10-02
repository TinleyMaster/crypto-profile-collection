#!/usr/bin/env python3
"""资产截面联动因子挖掘 · 第 1 步：同期相关矩阵（只读）。

背景（审计 2026-10-02）：系统缺「资产×资产」联动强度量化。catalyst_second_order
只做 sector 静态映射、未经相关性验证。本脚本用日频收益（扣 BTC 超额）构建
同期相关矩阵，产出联动全景，为后续因子化（领先-滞后 / 事件验证）打底。

口径：
- 收益：`v_asset_market_daily_primary.change_24h`（日涨跌%），扣 BTC 超额
  （ret − ret_btc）消除"一起涨只是都在涨"的伪联动
- 样本：覆盖 ≥ MIN_DAYS 天的资产（默认 60），且日均成交量 ≥ 阈值过滤死资产
- 度量：Pearson 相关（同一 market_date 对齐，两两配对算）
- 输出：
    ① 总体分布（平均相关、高相关对数量）
    ② 同板块 vs 跨板块平均相关（验证板块标签是否真的对应联动）
    ③ Top 联动对（按 |ρ| 排序，附板块）
    ④ 板块内平均相关排名（找联动最强的板块）
- 只读：不落库

用法：
    python scan_cross_section_linkage.py --days 60
    python scan_cross_section_linkage.py --days 60 --min-days 45 --top 500
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

MIN_DAYS = 60             # 资产最少覆盖天数
MIN_MED_VOL = 1e6         # 日均成交量下限（过滤死资产）
CORR_THRESHOLD = 0.60     # 高联动对阈值
TOP_N = 25                # top 输出条数


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
    """从资产名提取「根标的实体」，合并同标的的多形态资产。

    背景（审计 2026-10-02 第 1 轮）：相关矩阵 Top 联动对被「同标的代币化/包装
    形态」污染（CRCLX/CRCLon/CRCLB、MSTRX/MSTRB、各链 Bridged SOL 等 ρ≈0.9~1.0），
    这些不是真联动。修法：剥掉包装词，映射到根实体，同实体只保留市值最大的主资产。

    包装词清单（按常见度）：bStocks/xStock/Tokenized/Bridged/Wrapped/Peg/
    (Ondo)/(Base)/(BNB Chain)/(Near Protocol)/(Eclipse)/(Sui)/Prestock/
    Derivative/wrapped 等，以及括号后缀。
    """
    import re as _re
    s = (name or "").strip()
    if not s:
        return ""
    # 统一转小写处理（原实现用原始大小写 replace，导致 "Tokenized"/"bStocks"
    # 等大写包装词漏剥、根实体不一致——见 MUB/MUon 未合并的 bug）
    s = s.lower()
    # 剥括号后缀（…(base)、(ondo)、(near protocol) 等）
    s = _re.sub(r"\([^)]*\)", " ", s)
    # 剥包装关键词
    for kw in ("tokenized", "prestock", "pre-stock", "b-stock", "bstock",
               "xstock", "wrapped", "bridged", "peg", "derivative",
               "tokenized stock", "tokenized etf"):
        s = s.replace(kw, " ")
    # 去冗余空格、尾标
    s = _re.sub(r"\s+", " ", s).strip(" -:.")
    # 去掉常见噪声尾词
    for tail in (" stock", " etf", " token", " coin", " perpetuals", " lp",
                 " (binance)", " binance"):
        if s.endswith(tail):
            s = s[: -len(tail)].strip()
    # 特例：桥接 SOL 族 → Solana（按 canonical_symbol 兜底在调用方处理）
    if s == "":
        return ""
    return s[:40]


def load_data(conn, min_days: int) -> tuple[dict, list[str], dict[int, str]]:
    """加载资产收益面板 + 板块映射。

    Returns:
        (panel, dates, sector_map)
        panel: {asset_id: {date: excess_ret}}  扣 BTC 超额后的日收益
        dates: 按升序的日期列表（全市场对齐）
        sector_map: {asset_id: primary_sector}
    """
    # BTC 超额基准
    btc_id = None
    btc_rows = conn.execute(
        "SELECT asset_id FROM core.asset WHERE canonical_symbol='BTC' "
        "AND status='active' ORDER BY market_cap DESC NULLS LAST LIMIT 1"
    ).fetchall()
    if btc_rows:
        btc_id = btc_rows[0]["asset_id"]

    # 资产清单（覆盖天数达标 + 有成交量）
    assets = conn.execute("""
        WITH stats AS (
            SELECT asset_id,
                   count(*) AS n_days,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY volume_24h) AS med_vol
            FROM biz.v_asset_market_daily_primary
            WHERE change_24h IS NOT NULL
            GROUP BY asset_id
        )
        SELECT asset_id FROM stats
        WHERE n_days >= %s AND med_vol >= %s
    """, (min_days, MIN_MED_VOL)).fetchall()
    asset_ids = [r["asset_id"] for r in assets]

    # 同实体去重：取根标的，每个根实体只保留市值最大的主资产
    meta = conn.execute("""
        SELECT asset_id, canonical_symbol, canonical_name, market_cap
        FROM core.asset WHERE asset_id = ANY(%s)
    """, (asset_ids,)).fetchall()
    meta_map = {r["asset_id"]: r for r in meta}

    keep_ids: list[int] = []
    seen_root: dict[str, int] = {}
    dropped_dupes: list[tuple[int, int, str, str]] = []  # (dup_id, kept_id, dup_sym, root)
    for aid in asset_ids:
        m = meta_map.get(aid)
        if not m:
            continue
        sym = m["canonical_symbol"] or ""
        name = m["canonical_name"] or sym
        name_low = name.lower()
        # 判定是否为包装/桥接形态：优先用 symbol 根词（CRCLX/CRCLon/CRCLB 前缀一致）
        is_wrapped = any(
            kw in name_low
            for kw in ("tokenized", "xstock", "bstock", "b-stock",
                       "wrapped", "bridged", "prestock", "pre-stock", "derivative")
        )
        if is_wrapped:
            root = re.sub(r"(on|x|b|w|e|z)$", "", sym, flags=re.I)
            if len(root) < 2:
                root = root_entity(name)
        else:
            root = root_entity(name)
        if not root:
            keep_ids.append(aid)
            continue
        cap = m["market_cap"] or 0.0
        if root in seen_root:
            kept_id = seen_root[root]
            if cap > (meta_map[kept_id]["market_cap"] or 0.0):
                # 新资产市值更大，替换
                dropped_dupes.append((kept_id, aid, meta_map[kept_id]["canonical_symbol"], root))
                keep_ids.remove(kept_id)
                seen_root[root] = aid
                keep_ids.append(aid)
            else:
                dropped_dupes.append((aid, kept_id, sym, root))
        else:
            seen_root[root] = aid
            keep_ids.append(aid)

    deduped = len(asset_ids) - len(keep_ids)
    if deduped:
        print(f"[去重] 同实体合并剔除 {deduped} 个包装/桥接形态（保留 {len(keep_ids)} 个主资产）")
        for dup, kept, dsym, root in dropped_dupes[:12]:
            print(f"  - {dsym:<10} → 保留 {meta_map[kept]['canonical_symbol']:<10} (根实体: {root})")
    asset_ids = keep_ids

    # 收益面板（含 BTC 用于算超额）
    ids = asset_ids + ([btc_id] if btc_id and btc_id not in asset_ids else [])
    rows = conn.execute("""
        SELECT asset_id, market_date, change_24h
        FROM biz.v_asset_market_daily_primary
        WHERE asset_id = ANY(%s) AND change_24h IS NOT NULL
        ORDER BY market_date
    """, (ids,)).fetchall()

    panel: dict[int, dict] = {}
    for r in rows:
        panel.setdefault(r["asset_id"], {})[r["market_date"]] = float(r["change_24h"])

    # 扣 BTC 超额
    btc_ret = panel.get(btc_id, {}) if btc_id else {}
    for aid in asset_ids:
        a_ret = panel[aid]
        for d in list(a_ret):
            base = btc_ret.get(d)
            if base is not None:
                a_ret[d] = a_ret[d] - base

    # 全市场对齐日期
    all_dates = sorted(set().union(*[set(panel[a]) for a in asset_ids]))
    if btc_id in panel:
        all_dates = sorted(set(all_dates) | set(btc_ret))

    # 板块映射
    sector_map: dict[int, str] = {}
    sec_rows = conn.execute("""
        SELECT asset_id, sector FROM biz.asset_sector WHERE is_primary = true
    """).fetchall()
    for r in sec_rows:
        sector_map.setdefault(r["asset_id"], r["sector"])
    # 兜底 primary_sector
    for aid in asset_ids:
        if aid not in sector_map:
            sector_map[aid] = "unknown"

    return panel, all_dates, sector_map


def build_matrix(panel: dict, dates: list, asset_ids: list) -> tuple[np.ndarray, dict[int, int]]:
    """构建资产×资产相关矩阵。

    Returns:
        (corr_matrix, idx_map)
        corr_matrix: 对称相关矩阵（NaN 填充不完整配对）
        idx_map: asset_id → 矩阵行号
    """
    n = len(asset_ids)
    idx_map = {aid: i for i, aid in enumerate(asset_ids)}
    corr = np.full((n, n), np.nan)
    for i, aid in enumerate(asset_ids):
        ret = panel[aid]
        series = np.array([ret.get(d, np.nan) for d in dates])
        corr[i, i] = 1.0
        for j in range(i + 1, n):
            other = panel[asset_ids[j]]
            series_j = np.array([other.get(d, np.nan) for d in dates])
            mask = ~(np.isnan(series) | np.isnan(series_j))
            if mask.sum() >= 30:
                r = np.corrcoef(series[mask], series_j[mask])[0, 1]
                if not np.isnan(r):
                    corr[i, j] = corr[j, i] = r
    return corr, idx_map


def main() -> int:
    parser = argparse.ArgumentParser(description="资产截面联动 · 同期相关矩阵（只读）")
    parser.add_argument("--min-days", type=int, default=MIN_DAYS, help="资产最少覆盖天数")
    parser.add_argument("--corr", type=float, default=CORR_THRESHOLD, help="高联动阈值")
    parser.add_argument("--top", type=int, default=TOP_N, help="top 输出条数")
    args = parser.parse_args()

    conn = get_conn()
    try:
        panel, dates, sector_map = load_data(conn, args.min_days)
        asset_ids = list(panel.keys())
        print(f"资产数: {len(asset_ids)} | 对齐日期: {len(dates)} 天")

        corr, idx_map = build_matrix(panel, dates, asset_ids)
        n = len(asset_ids)

        # 提取上三角非 NaN 相关对
        pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                r = corr[i, j]
                if not np.isnan(r):
                    pairs.append((asset_ids[i], asset_ids[j], r))

        print(f"有效相关对: {len(pairs)}")
        if not pairs:
            print("无有效对，检查数据覆盖")
            return 0

        # ① 总体分布
        rs = [p[2] for p in pairs]
        med = float(np.median(rs))
        mean = float(np.mean(rs))
        hi = sum(1 for r in rs if r >= args.corr)
        neg = sum(1 for r in rs if r <= -args.corr)
        print("\n" + "=" * 60)
        print("一、总体分布（扣 BTC 超额后的日收益相关）")
        print("=" * 60)
        print(f"  平均相关 {mean:.3f} | 中位 {med:.3f}")
        print(f"  高联动对 ρ≥{args.corr}: {hi} | 反向对 ρ≤-{args.corr}: {neg}")

        # ② 同板块 vs 跨板块
        same_sector = [p[2] for p in pairs if sector_map[p[0]] == sector_map[p[1]]]
        cross_sector = [p[2] for p in pairs if sector_map[p[0]] != sector_map[p[1]]]
        print("\n" + "=" * 60)
        print("二、同板块 vs 跨板块平均相关（板块标签是否对应联动）")
        print("=" * 60)
        if same_sector:
            print(f"  同板块: n={len(same_sector)} 平均 ρ={np.mean(same_sector):.3f}")
        if cross_sector:
            print(f"  跨板块: n={len(cross_sector)} 平均 ρ={np.mean(cross_sector):.3f}")

        # ③ Top 联动对
        pairs_sorted = sorted(pairs, key=lambda x: -abs(x[2]))
        print("\n" + "=" * 60)
        print(f"三、Top {args.top} 联动对（按 |ρ|）")
        print("=" * 60)
        print(f"  {'资产A':<12} {'资产B':<12} {'板块A':<10} {'板块B':<10} {'ρ':>6}")
        for aid_a, aid_b, r in pairs_sorted[:args.top]:
            sym_a = _symbol(conn, aid_a)
            sym_b = _symbol(conn, aid_b)
            print(f"  {sym_a:<12} {sym_b:<12} {sector_map[aid_a]:<10} "
                  f"{sector_map[aid_b]:<10} {r:>6.2f}")

        # ④ 板块内平均相关排名
        sec_corr = defaultdict(list)
        for aid_a, aid_b, r in pairs:
            if sector_map[aid_a] == sector_map[aid_b]:
                sec_corr[sector_map[aid_a]].append(r)
        print("\n" + "=" * 60)
        print("四、板块内平均相关排名（联动最强的板块）")
        print("=" * 60)
        ranked = sorted(sec_corr.items(), key=lambda kv: -np.mean(kv[1]))
        for sec, rs in ranked[:15]:
            print(f"  {sec:<12} n={len(rs):>5} 平均 ρ={np.mean(rs):.3f}")

        return 0
    finally:
        conn.close()


def _symbol(conn, asset_id: int) -> str:
    rows = conn.execute(
        "SELECT canonical_symbol FROM core.asset WHERE asset_id = %s", (asset_id,)
    ).fetchall()
    return rows[0]["canonical_symbol"] if rows else str(asset_id)


if __name__ == "__main__":
    sys.exit(main())
