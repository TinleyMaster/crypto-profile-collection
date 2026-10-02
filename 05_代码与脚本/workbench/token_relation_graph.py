#!/usr/bin/env python3
"""代币关系图谱 · 组内相关度分析。

思路（用户定调）：先按**已知关系**把代币归组，再量化「同组是否真的更相关」——
比盲扫全市场币对更可解释，也直接服务「一个起飞 → 另一个随后起飞」的联动判断
（同组高相关是联动的前提条件）。

关系维度（数据落点）：
  1. 赛道     biz.asset_sector（14 类，多标签）✅
  2. 公链     core.asset_contract.chain（多链，按链聚合）✅
  3. 产业链    biz.sector_narrative_asset（20 个叙事，日级快照）✅
  4. 生态/公司 ⚠️ 当前无可用数据（CMC 生态分类成员未摄入、CG 表空），
             实现保留在 load_ecosystem_map，待建 issuer/company 映射表后启用。

方法：
  - top N 币日收益矩阵，扣 BTC 超额（与全项目 excess 口径一致）
  - 排除稳定币（锚定对恒 ~1.0，会污染组内紧密度）
  - 组内同步 Pearson 相关 vs 全市场基线
  - tightness = 组内平均相关 − 全市场平均相关（>0 且越大 → 该组关系越「实」）

运行：
    python token_relation_graph.py              # 全流程（需连库）
    python token_relation_graph.py --selftest
    python token_relation_graph.py --top-n 300 --min-days 40 --min-members 3
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_SCRIPTS_SRC = _HERE.parent / "scripts" / "src"
for p in (_SCRIPTS_SRC, _HERE.parent / "scripts" / "bin"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_lead_lag as sl  # noqa: E402  复用 load_daily_matrix / get_db / symbol_candidates

UTC = timezone.utc

DIM_LABELS = {"sector": "赛道", "chain": "公链", "narrative": "产业链叙事", "ecosystem": "生态/公司"}


# ═══════════════════════════════════════════════════════════════
#  一、纯函数层
# ═══════════════════════════════════════════════════════════════

def sync_corr_matrix(R: np.ndarray) -> np.ndarray:
    """收益矩阵 (T, N) → 同步 Pearson 相关矩阵 (N, N)。"""
    mu = np.nanmean(R, axis=0)
    sd = np.nanstd(R, axis=0)
    Z = np.nan_to_num((R - mu) / (sd + 1e-12))
    return np.clip((Z.T @ Z) / (R.shape[0] - 1), -1.0, 1.0)


def group_corr_stats(corr: np.ndarray, member_idx: list[int], min_members: int = 3) -> dict | None:
    """组内成员（矩阵列下标）→ 组内两两相关统计。成员不足返回 None。"""
    idx = np.asarray(list(member_idx), dtype=int)
    if len(idx) < min_members:
        return None
    sub = corr[np.ix_(idx, idx)]
    iu = np.triu_indices(len(idx), k=1)
    vals = sub[iu]
    return {"n": int(len(idx)), "n_pairs": int(len(vals)),
            "mean": round(float(vals.mean()), 4),
            "median": round(float(np.median(vals)), 4),
            "max": round(float(vals.max()), 4)}


def top_pairs_in_group(corr: np.ndarray, cols: list[str], member_idx: list[int],
                       top_k: int = 5) -> list[dict]:
    """组内 top 高相关对。"""
    idx = list(member_idx)
    out = []
    for a in range(len(idx)):
        for b in range(a + 1, len(idx)):
            out.append((idx[a], idx[b], float(corr[idx[a], idx[b]])))
    out.sort(key=lambda t: -abs(t[2]))
    return [{"a": cols[i], "b": cols[j], "corr": round(c, 4)} for i, j, c in out[:top_k]]


# ═══════════════════════════════════════════════════════════════
#  二、DB 读取层（关系维度）
# ═══════════════════════════════════════════════════════════════

def load_symbol_asset_map(conn) -> dict[str, int]:
    """裸符号 → asset_id（core.asset.canonical_symbol，去重取市值最高）。"""
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT asset_id, canonical_symbol FROM core.asset "
            "WHERE canonical_symbol IS NOT NULL ORDER BY market_cap_rank NULLS LAST")
        out = {}
        for r in cur.fetchall():
            out.setdefault(r["canonical_symbol"], r["asset_id"])
        return out


def _map_by_asset(conn, sql: str, params: tuple) -> dict[int, set[str]]:
    """asset_id → group 集合。"""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        out: dict[int, set[str]] = defaultdict(set)
        for asset_id, g in cur.fetchall():
            if asset_id is not None:
                out[asset_id].add(g)
        return out


def load_sector_map(conn) -> dict[int, set[str]]:
    return _map_by_asset(conn,
                         "SELECT asset_id, sector FROM biz.asset_sector", ())


def load_chain_map(conn) -> dict[int, set[str]]:
    return _map_by_asset(conn,
                         "SELECT asset_id, chain FROM core.asset_contract "
                         "WHERE chain IS NOT NULL AND chain <> ''", ())


def load_narrative_map(conn) -> dict[int, set[str]]:
    return _map_by_asset(conn,
                         """
                         SELECT asset_id, narrative FROM biz.sector_narrative_asset
                         WHERE as_of_date = (SELECT MAX(as_of_date) FROM biz.sector_narrative_asset)
                         """, ())


def load_issuer_map(conn) -> dict[int, set[str]]:
    """biz.asset_issuer（代币→发行方/公司，人工种子）→ asset_id 分组映射。

    第四维「同一家公司」：由 scripts/bin/seed_asset_issuer.py 维护。
    """
    return _map_by_asset(conn,
                         "SELECT asset_id, issuer FROM biz.asset_issuer", ())


def load_ecosystem_map(conn) -> dict[int, set[str]]:
    """CMC '* Ecosystem' 分类 → asset_id（经 cmc_asset_map + asset_source_map 映射）。

    当前**不可用**：cmc_category_member 仅收录 Arc/Long 两个小生态分类，大生态
    （Binance/BNB Chain/Solana …）成员未摄入；src_cg.cg_coin_detail 全空。
    保留实现以便补数后启用。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asm.asset_id, cat.category_name
            FROM src_cmc.cmc_category_member cm
            JOIN src_cmc.cmc_category cat ON cat.category_id = cm.category_id
            JOIN src_cmc.cmc_asset_map cam ON cam.cmc_id = cm.cmc_id
            JOIN core.asset_source_map asm
              ON asm.source_code = 'cmc' AND asm.source_asset_key = cm.cmc_id::text
            WHERE cat.category_name ILIKE '%%Ecosystem%%'
              AND cm.snapshot_date = (SELECT MAX(snapshot_date) FROM src_cmc.cmc_category_member)
            """)
        out: dict[int, set[str]] = defaultdict(set)
        for asset_id, g in cur.fetchall():
            if asset_id is not None:
                out[asset_id].add(g)
        return out


def load_stablecoin_asset_ids(conn) -> set[int]:
    """稳定币 asset_id 集合（相关分析需排除：锚定对恒 ~1.0，会污染组内紧密度）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id FROM biz.asset_sector WHERE sector = 'stablecoin'")
        return {r[0] for r in cur.fetchall()}


def relation_groups(conn, sym2aid: dict[str, int], cols: list[str],
                    min_members: int, top_n_groups: int = 40) -> dict:
    """把各维度的 asset_id 分组折算到「分析宇宙的符号」，返回组规模与成员下标。

    返回 {dim: {"groups": {group: [col_idx]}, "n_groups": int}}。
    issuer 维度放宽到 2 人（同一家公司大多只有 1-2 个非稳定币代币）；
    CMC 生态维度仍不可用（成员表仅 Arc/Long 两个小分类）。
    """
    maps = {
        "sector": load_sector_map(conn),
        "chain": load_chain_map(conn),
        "narrative": load_narrative_map(conn),
        "issuer": load_issuer_map(conn),
    }
    min_members_by_dim = {"issuer": min(2, min_members)}
    # 反查：symbol → col_idx（宇宙内才有下标）
    col_of: dict[str, int] = {s: i for i, s in enumerate(cols)}
    out = {}
    for dim, amap in maps.items():
        mm = min_members_by_dim.get(dim, min_members)
        groups: dict[str, list[int]] = defaultdict(list)
        for sym, aid in sym2aid.items():
            ci = col_of.get(sym)
            if ci is None or aid not in amap:
                continue
            for g in amap[aid]:
                groups[g].append(ci)
        # 去重下标（多标签内部不应重复），并按规模排序保留 top 组
        clean = {g: sorted(set(idx)) for g, idx in groups.items()}
        clean = {g: idx for g, idx in clean.items() if len(idx) >= mm}
        ranked = sorted(clean.items(), key=lambda kv: -len(kv[1]))
        out[dim] = {"groups": dict(ranked[:top_n_groups]),
                    "n_groups_total": len(clean)}
    return out


# ═══════════════════════════════════════════════════════════════
#  三、分析
# ═══════════════════════════════════════════════════════════════

def prepare(conn, top_n: int, min_days: int, min_members: int,
            winsorize_pct: float = 50.0) -> dict:
    """公共准备（供本脚本与回测脚本复用）：宇宙收益矩阵 + 关系分组。

    返回 {cols, ret, rel, window, n_excluded_stable}。
    ret 为已扣 BTC、已排稳定币的日超额收益矩阵（小数制，默认 ±50% winsorize，A6）。
    """
    d_idx, d_ret, d_cols = sl.load_daily_matrix(conn, top_n, min_days, winsorize_pct)
    if "BTC" in d_cols:
        btc_col = d_cols.index("BTC")
        d_cols = [s for s in d_cols if s != "BTC"]
        d_ret = np.delete(d_ret, btc_col, axis=1)
    # 裁掉头部稀疏行（新上市币）
    d_ret, cut = sl.trim_sparse_head(d_ret)
    window = f"{str(d_idx[cut])[:10]} ~ {str(d_idx[-1])[:10]}"

    # 排除稳定币：锚定对恒 ~1.0，会把所有维度的组内紧密度带偏
    sym2aid = load_symbol_asset_map(conn)
    stable_aids = load_stablecoin_asset_ids(conn)
    keep = [i for i, s in enumerate(d_cols)
            if sym2aid.get(s) not in stable_aids]
    n_excluded_stable = len(d_cols) - len(keep)
    d_cols = [d_cols[i] for i in keep]
    d_ret = d_ret[:, keep]
    sym2aid = {s: aid for s, aid in sym2aid.items() if s in set(d_cols)}
    del keep

    # 关系分组（折算到宇宙符号）
    rel = relation_groups(conn, sym2aid, d_cols, min_members)
    del sym2aid

    return {"cols": d_cols, "ret": d_ret, "rel": rel, "window": window,
            "n_excluded_stable": n_excluded_stable}


def analyze(conn, args) -> dict:
    p = prepare(conn, args.top_n, args.min_days, args.min_members, args.winsorize_pct)
    d_cols, d_ret, rel = p["cols"], p["ret"], p["rel"]

    corr = sync_corr_matrix(d_ret)
    # 全市场基线 = 非对角同步相关的均值
    iu = np.triu_indices(len(d_cols), k=1)
    global_vals = corr[iu]
    global_mean = float(global_vals.mean())
    global_median = float(np.median(global_vals))

    dims = {}
    for dim, blk in rel.items():
        rows = []
        for group, idx in blk["groups"].items():
            st = group_corr_stats(corr, idx, args.min_members)
            if not st:
                continue
            pairs = top_pairs_in_group(corr, d_cols, idx)
            rows.append({"group": group, **st,
                         "tightness": round(st["mean"] - global_mean, 4),
                         "top_pairs": pairs})
        rows.sort(key=lambda r: -r["tightness"])
        dims[dim] = {"n_groups_analyzed": len(rows),
                     "n_groups_total": blk["n_groups_total"], "groups": rows}

    return {"generated_at": datetime.now(UTC).isoformat(),
            "args": vars(args), "window": p["window"],
            "universe": {"n": len(d_cols), "n_days": d_ret.shape[0],
                         "n_excluded_stable": p["n_excluded_stable"]},
            "baseline": {"mean": round(global_mean, 4), "median": round(global_median, 4)},
            "dims": dims}


# ═══════════════════════════════════════════════════════════════
#  四、输出
# ═══════════════════════════════════════════════════════════════

def print_report(r: dict) -> None:
    w = "\u2500" * 74
    print(f"\n{w}\n\u4ee3\u5e01\u5173\u7cfb\u56fe\u8c31 \u00b7 \u7ec4\u5185\u76f8\u5173\u5ea6  {r['generated_at'][:10]}\n{w}")
    print(f"\u5b87\u5b99\uff1a{r['universe']['n']} \u4e2a\u5e01 \u00d7 {r['universe']['n_days']} \u5929"
          f"\uff08{r['window']} UTC\uff09\u00b7 \u5df2\u6263 BTC \u8d85\u989d")
    print(f"\u5168\u5e02\u573a\u57fa\u7ebf\uff1a\u5e73\u5747\u540c\u6b65\u76f8\u5173 {r['baseline']['mean']}"
          f"\uff08\u4e2d\u4f4d\u6570 {r['baseline']['median']}\uff09 \u2014 \u7ec4\u5185\u76f8\u5173\u8d85\u8fc7\u5b83\u8d8a\u591a"
          f"\uff0c\u8be5\u7ec4\u5173\u7cfb\u8d8a\u201c\u5b9e\u201d")
    for dim, blk in r["dims"].items():
        label = DIM_LABELS.get(dim, dim)
        print(f"\n\u3010{label}\u3011\u5206\u6790 {blk['n_groups_analyzed']}/{blk['n_groups_total']} \u7ec4"
              f" \u00b7 Top10\uff08\u6309\u7d27\u5bc6\u5ea6 tightness \u964d\u5e8f\uff09")
        for g in blk["groups"][:10]:
            pairs = "\u3001".join(f"{p['a']}-{p['b']}({p['corr']:.2f})"
                                  for p in g["top_pairs"][:3])
            print(f"  {g['group']:<24} n={g['n']:<3} \u7ec4\u5185\u76f8\u5173={g['mean']:.3f}"
                  f" \u7d27\u5bc6\u5ea6\u00b1{g['tightness']:+.3f}  \u6700\u4f73\u5bf9 {pairs}")


def main() -> int:
    ap = argparse.ArgumentParser(description="代币关系图谱 · 组内相关度分析")
    ap.add_argument("--top-n", type=int, default=400, help="分析宇宙 top N（按最新市值）")
    ap.add_argument("--min-days", type=int, default=40, help="宇宙最少日频覆盖天数")
    ap.add_argument("--min-members", type=int, default=3, help="组内最少成员数（低于不分析）")
    ap.add_argument("--winsorize-pct", type=float, default=50.0,
                    help="收益 winsorize 阈值 %%（±限幅防单币离群，A6；<=0 关闭）")
    ap.add_argument("--output", type=str, default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    with sl.get_db() as conn:
        rep = analyze(conn, args)
    print_report(rep)
    out = args.output or str(_HERE / "output" / f"relation_graph_{datetime.now(UTC):%Y-%m-%d}.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON \u62a5\u544a\uff1a{out}")
    return 0


def selftest() -> int:
    passed = failed = 0

    def check(cond, name, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  \u2713 {name}")
        else:
            failed += 1
            print(f"  \u2717 {name}  {detail}")

    print("\n\u3010\u81ea\u68c0\u3011\u540c\u6b65\u76f8\u5173\u77e9\u9635")
    rng = np.random.default_rng(5)
    T, N = 500, 6
    common = rng.normal(size=T) * 0.5          # 共同因子
    R = rng.normal(size=(T, N)) * 0.3
    R[:, 0] += common                          # col0/col1 强相关
    R[:, 1] += common
    C = sync_corr_matrix(R)
    check(abs(C[0, 1]) > abs(C[0, 2]), "共同因子 → col0-col1 相关 > col0-col2", f"got {C[0,1]:.3f} vs {C[0,2]:.3f}")
    # 对角 ≈1（容差放宽到 0.01：标准化含 1e-12 epsilon，且除的是 T-1 非 T）
    check(abs(C[0, 0] - 1.0) < 0.01, "对角 ≈ 1")

    print("\n\u3010\u81ea\u68c0\u3011\u7ec4\u5185\u7edf\u8ba1")
    st = group_corr_stats(C, [0, 1], min_members=2)
    check(st and st["n"] == 2 and st["n_pairs"] == 1 and st["mean"] > 0.5,
          "组内 2 成员 → 1 对相关取到强正值", f"got={st}")
    check(group_corr_stats(C, [0], min_members=2) is None, "成员不足 → None")
    tp = top_pairs_in_group(C, [f"S{i}" for i in range(N)], [0, 1, 2], top_k=2)
    check(tp and tp[0]["a"] == "S0" and tp[0]["b"] == "S1", "组内 top 对首位 = S0-S1", f"got={tp}")

    print(f"\n\u81ea\u68c0\u7ed3\u679c\uff1a{passed} \u901a\u8fc7 / {failed} \u5931\u8d25")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
