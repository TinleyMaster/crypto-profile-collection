#!/usr/bin/env python
"""主池告警「有效样本数」分析（只读）：横截面去相关后的胜率 / 赔率 / 期望。

为什么需要（2026-09-24 第 2 天 soak 验收发现）：
  `2026-09-23 14:04~14:50` 一小时内主池落了 **192 条**信号，其中 **189 条 down**、
  187 条 S4、成员均跌幅 -3.96%、均量比 5.15 —— 这是**一次大盘同步下跌**，不是
  192 个独立观测（`15:00` 蓄势池 103 条同理）。按条数计胜率/赔率会把 1 个宏观
  事件当成上百个样本，统计显著性被系统性高估。

两个去相关层级（同一 bet 内成员**等权平均**成一个观测）：
  - L1 事件级：同一小时 + 同方向 → 1 个 bet（捕捉「同一波行情」）
  - L2 日级：  同一日   + 同方向 → 1 个 bet（捕捉「同一 regime」）

⚠️ 去相关**几乎不改变点估计**（只是等权重加权，均值基本不动），它改变的是
**有效样本数 n_eff** —— 也就是那些数字值不值得信。判据：L2 的 n_eff ≥ 10 才谈
「有统计意义」，而 L2 的 n_eff ≈ 独立交易日数 × 方向数。故「攒够 10 个日历日」
必须按 L2 的 n_eff 核验，不能按条数。

口径：`aligned_ret_*` 为**方向对齐后收益（%）**（与 `scan_edge_daily.avg_*` 同源），
`base_time` 为告警时点。**不改动任何表**，与 `build_scan_edge_report.py` 同源不同工具
（后者负责日报落库；本工具负责回答「这些读数有多少独立样本」）。

用法：
    python scripts/bin/scan_effective_sample.py --days 7
    python scripts/bin/scan_effective_sample.py --days 3 --pool main --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

WINDOWS = (1, 4, 12, 24)
DEFAULT_MIN_EFF = 10  # L2（日级）有效样本门槛：不足即判「样本不可用」


def stat(rets: list[float]) -> dict | None:
    """胜率 / 期望 / 赔率 / PF。`rets` 为方向对齐后收益（%）。

    赔率/PF 在**任一侧为空时返回 None**（不记 ∞）—— 与
    `build_scan_edge_report.agg` 口径一致，且 `Infinity` 不是合法 JSON
    （`jq` / `JSON.parse` 会直接报错），故 JSON 出口必须可空。
    """
    n = len(rets)
    if n == 0:
        return None
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]   # 平盘计负
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    avg_w = (gross_w / len(wins)) if wins else None
    avg_l = (sum(losses) / len(losses)) if losses else None
    return {
        "n": n,
        "win": len(wins) / n,
        "avg": sum(rets) / n,
        "odds": (avg_w / abs(avg_l)) if (avg_w is not None and avg_l) else None,
        "pf": (gross_w / gross_l) if gross_l > 0 else None,
    }


def fmt(label: str, s: dict | None, width: int = 18) -> str:
    if not s:
        return f"  {label:<{width}} （无样本）"
    odds = "   -  " if s["odds"] is None else f"{s['odds']:5.2f}"
    pf = "   -  " if s["pf"] is None else f"{s['pf']:5.2f}"
    return (f"  {label:<{width}} n={s['n']:4d} 胜率={s['win'] * 100:5.1f}% "
            f"期望={s['avg']:+6.2f}% 赔率={odds} PF={pf}")


def cluster_means(rows: list[dict], h: int, key_fn) -> tuple[list[float], dict]:
    """按 `key_fn` 聚类，簇内等权平均 → 每个簇 1 个观测。该窗口无值的成员跳过。"""
    groups: dict = {}
    for r in rows:
        v = r[f"aligned_ret_{h}h"]
        if v is None:
            continue
        groups.setdefault(key_fn(r), []).append(float(v))
    return [sum(v) / len(v) for v in groups.values()], groups


def load_rows(conn, days: int, pool: str) -> list[dict]:
    sql = (
        "SELECT signal_id, symbol, pool, scenario, timeframe, p_dir, base_time, "
        "       aligned_ret_1h, aligned_ret_4h, aligned_ret_12h, aligned_ret_24h "
        "FROM biz.scan_signal_outcome "
        "WHERE base_time >= NOW() - make_interval(days => (%s)::int) "
        "  AND base_time IS NOT NULL "
    )
    params: list = [days]
    if pool:
        sql += " AND pool = %s "
        params.append(pool)
    sql += " ORDER BY base_time"
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="告警有效样本数（横截面去相关）分析")
    ap.add_argument("--days", type=int, default=7, help="回溯天数（按 base_time，默认 7）")
    ap.add_argument("--pool", type=str, default="", help="限定池（如 main）；默认全部")
    ap.add_argument("--min-eff", type=int, default=DEFAULT_MIN_EFF,
                    help=f"L2 日级有效样本门槛（默认 {DEFAULT_MIN_EFF}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    settings = get_settings()
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT NOW() AS now")
            now = cur.fetchone()["now"]
        rows = load_rows(conn, args.days, args.pool)

    out: dict = {
        "as_of": now.isoformat(),
        "days": args.days,
        "pool": args.pool or "all",
        "n_raw": len(rows),
        "min_eff": args.min_eff,
        "windows": {},
    }

    if rows:
        t0, t1 = rows[0]["base_time"], rows[-1]["base_time"]
        out["base_time_range"] = [t0.isoformat(), t1.isoformat()]
        by_pool: dict[str, int] = {}
        for r in rows:
            by_pool[r["pool"]] = by_pool.get(r["pool"], 0) + 1
        out["pools"] = by_pool

        # L1 = 同小时同向；L2 = 同日同向
        k_l1 = lambda r: (r["base_time"].replace(minute=0, second=0, microsecond=0), r["p_dir"])
        k_l2 = lambda r: (r["base_time"].date(), r["p_dir"])

        if not args.json:
            print(f"时点(UTC) = {now}")
            print(f"样本: {len(rows)} 条  base_time {t0} ~ {t1}")
            print(f"池分布: {by_pool}")
            print("\n════ 横截面去相关读数（同向同小时 / 同向同日 各记为 1 个 bet，簇内等权）")
            print(f"  {'窗口':<5}{'口径':<20}统计")
        # ⚠️ 统计与 JSON 落 dict 必须在 `if not args.json` 之外：
        #    否则 --json 模式下 windows 恒为空 dict（历史 bug）。
        for h in WINDOWS:
            raw, _ = cluster_means(rows, h, lambda r: r["signal_id"])
            l1, _ = cluster_means(rows, h, k_l1)
            l2, _ = cluster_means(rows, h, k_l2)
            s_raw, s_l1, s_l2 = stat(raw), stat(l1), stat(l2)
            out["windows"][f"{h}h"] = {
                "raw": s_raw,
                "l1_event": s_l1,
                "l2_day": s_l2,
                "n_clusters": {"l1": len(l1), "l2": len(l2)},
            }
            if not args.json:
                print(f"  {h}h")
                print(fmt(f"原始(按条数)", s_raw, 20))
                print(fmt(f"L1 事件级(时×向)", s_l1, 20))
                print(fmt(f"L2 日级(日×向)", s_l2, 20))

        # 最大簇画像（用 24h 可结算条数）—— 让「1 个事件 = N 条」显形
        _, g24 = cluster_means(rows, 24, k_l1)
        biggest = sorted(g24.items(), key=lambda kv: -len(kv[1]))[:5]
        out["top_clusters_24h"] = [
            {"hour": k[0].isoformat(), "dir": k[1], "n": len(v), "avg_24h": sum(v) / len(v)}
            for k, v in biggest
        ]
        if not args.json:
            print("\n════ 最大簇（同小时同向，按 24h 可结算条数）")
            for k, v in biggest:
                print(f"  {k[0]} {k[1]:<5} n={len(v):4d} 簇均 24h={sum(v) / len(v):+6.2f}%")

        n_l2 = len(cluster_means(rows, 24, k_l2)[0])
        out["n_eff_l2"] = n_l2
        out["sample_ok"] = n_l2 >= args.min_eff
        if not args.json:
            n_l1 = len(cluster_means(rows, 24, k_l1)[0])
            print(f"\n集中度: {len(rows)} 条 → L1 {n_l1} 个事件 → L2 {n_l2} 个日级观测"
                  f"（{len(rows) / n_l1:.1f} 条/事件）" if n_l1 else "")
            print(f"样本可用性: L2 n_eff={n_l2} {'≥' if out['sample_ok'] else '<'} "
                  f"{args.min_eff} ⇒ {'可用' if out['sample_ok'] else '不可用（只有点估计、无统计显著性）'}")
    else:
        out["sample_ok"] = False
        out["n_eff_l2"] = 0
        if not args.json:
            print(f"时点(UTC) = {now}\n样本: 0 条（--days {args.days}"
                  f"{' --pool ' + args.pool if args.pool else ''} 无已结算记录）")

    if args.json:
        # allow_nan=False：任何非有限值（inf/nan）都在这里**显式报错**，而不是
        # 静默写出 `Infinity` / `NaN`（二者不是合法 JSON，会把下游解析器打炸）。
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str, allow_nan=False))
    return 0 if out["sample_ok"] else 3


if __name__ == "__main__":
    sys.exit(main())