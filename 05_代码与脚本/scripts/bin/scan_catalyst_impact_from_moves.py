"""催化剂影响程度模型依据扫描（事件研究法，只读）。

业务问题（审计 2026-10-02）：现有 impact_strength 是 event_type 静态查表，
无法判断催化剂「具体影响程度」。本脚本反向建模：以**历史真实价格异动**为因变量，
回溯窗口内是否存在催化剂及其特征，产出「影响程度判断」的模型依据：

  1. 基线：全样本中「大异动日」占比（无催化剂时的异动频率）
  2. Lift：有催化剂时异动频率 / 基线 —— 催化剂是否真的预测异动
  3. 分组影响幅度：按 event_type / impact_strength / 市值分档 统计
     「窗口内有催化剂时的平均 |异动|」与「方向对齐幅度」

双口径：
- --backward（默认）：异动日回溯 PRE_DAYS 天看有无催化剂（检验「催化剂是异动原因」）
- --forward（推荐）：催化剂发布后 FORWARD_DAYS 天内看是否异动（检验「催化剂能预测异动」，
  与「影响程度判断」的实际用途一致，且规避价格先动→媒体跟发的反向因果）

口径：
- 异动定义：单日 |change_24h| ≥ MOVE_THRESHOLD（默认 15%）
- 对照：无催化剂时段的异动频率作为基线
- 方向对齐：bullish 取 +|异动|、bearish 取 -|异动|、neutral 取 0
  （衡量「催化剂方向是否被市场兑现」）
- 只读：不落库、不写表、不调 API

用法：
    python scan_catalyst_impact_from_moves.py                       # 默认回溯口径
    python scan_catalyst_impact_from_moves.py --forward             # 前瞻口径（推荐）
    python scan_catalyst_impact_from_moves.py --days 60 --move 20 --top 300
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings  # noqa: E402

MOVE_THRESHOLD = 15.0     # 异动定义：单日 |change_24h| ≥ 15%
PRE_DAYS = 2              # 回溯口径：异动前 N 天内有催化剂发布
FORWARD_DAYS = 2          # 前瞻口径：催化剂发布后 N 天内看异动
LOOKBACK_DAYS = 90        # 默认扫描近 90 天
TOP_N = 0                 # 0 = 全资产；>0 = 只扫市值前 N（过滤脏长尾）


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


def load_prices(conn, start: date, top_n: int) -> dict[int, list[dict]]:
    """批量加载 (asset_id, market_date, change_24h)。按资产分组。"""
    rank_filter = ""
    params: list = [start]
    if top_n > 0:
        rank_filter = "AND a.market_cap_rank <= %s"
        params.append(top_n)
    rows = conn.execute(f"""
        SELECT md.asset_id, md.market_date, md.change_24h
        FROM biz.asset_market_daily md
        JOIN core.asset a ON a.asset_id = md.asset_id
        WHERE md.market_date >= %s
          AND md.change_24h IS NOT NULL
          AND md.source_code = 'cmc'
          {rank_filter}
        ORDER BY md.asset_id, md.market_date
    """, params).fetchall()
    data: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        data[r["asset_id"]].append({
            "d": r["market_date"],
            "chg": float(r["change_24h"]),
        })
    return data


def load_catalysts(conn, start: date) -> dict[int, list[dict]]:
    """批量加载催化剂：(asset_id → [{published_at, event_type, sentiment, strength}])。

    绑定口径：优先 catalyst.asset_id；其次 catalyst_asset_link 补足。
    """
    rows = conn.execute("""
        SELECT ac.catalyst_id, ac.published_at,
               COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
               ac.ai_sentiment,
               COALESCE(ac.asset_id, cal.asset_id) AS asset_id,
               MAX(ci.impact_strength) AS impact_strength
        FROM biz.asset_catalyst ac
        LEFT JOIN biz.catalyst_asset_link cal ON cal.catalyst_id = ac.catalyst_id
        LEFT JOIN biz.catalyst_impact ci ON ci.catalyst_id = ac.catalyst_id
        WHERE ac.published_at >= %s
          AND COALESCE(ac.asset_id, cal.asset_id) IS NOT NULL
        GROUP BY 1,2,3,4,5
    """, (start,)).fetchall()
    data: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r["asset_id"] is None:
            continue
        data[r["asset_id"]].append({
            "published_at": r["published_at"].date(),
            "event_type": r["event_type"],
            "sentiment": (r["ai_sentiment"] or "").lower(),
            "strength": r["impact_strength"],
        })
    return data


def window_has_catalyst(cats: list[dict], d: date, pre_days: int) -> bool:
    """异动日 d 回溯 pre_days 天内是否有催化剂发布。"""
    lo = d - timedelta(days=pre_days)
    return any(lo <= c["published_at"] <= d for c in cats)


def aligned_sign(sentiment: str) -> float:
    """催化剂情绪 → 方向对齐符号。"""
    if sentiment == "bullish":
        return 1.0
    if sentiment == "bearish":
        return -1.0
    return 0.0


def scan_backward(prices, cats, args) -> None:
    """回溯口径：异动日之前 PRE_DAYS 天有无催化剂。"""
    with_cat_moves = with_cat_days = wo_cat_moves = wo_cat_days = 0
    grp_move: dict[str, list[float]] = defaultdict(list)
    grp_aligned: dict[str, list[float]] = defaultdict(list)

    for asset_id, seq in prices.items():
        asset_cats = cats.get(asset_id, [])
        for rec in seq:
            d = rec["d"]
            has_cat = window_has_catalyst(asset_cats, d, args.pre)
            is_move = abs(rec["chg"]) >= args.move
            if has_cat:
                with_cat_days += 1
                if is_move:
                    with_cat_moves += 1
            else:
                wo_cat_days += 1
                if is_move:
                    wo_cat_moves += 1

            if has_cat:
                lo = d - timedelta(days=args.pre)
                recent = [c for c in asset_cats if lo <= c["published_at"] <= d]
                if not recent:
                    continue
                c = max(recent, key=lambda x: x["published_at"])
                for key, sent in ((c["event_type"], c["sentiment"]),
                                  (f"strength:{c['strength'] or 'unknown'}", c["sentiment"])):
                    grp_move[key].append(abs(rec["chg"]))
                    grp_aligned[key].append(rec["chg"] * aligned_sign(sent))

    _report_lift("回溯口径（异动日前 %d 天有无催化剂）" % args.pre,
                 with_cat_moves, with_cat_days, wo_cat_moves, wo_cat_days,
                 grp_move, grp_aligned)


def scan_forward(prices, cats, args) -> None:
    """前瞻口径（推荐）：催化剂发布后 FORWARD_DAYS 天内是否异动。

    以每条催化剂为锚点，观察发布后 1..N 天的最大 |异动|；对照为
    「同一资产、无催化剂且无后续催化剂」的普通日窗口。
    """
    # 对照基线：无催化剂日 → 次日异动率（1 日窗口，避免多日窗口稀释）
    wo_days = wo_moves = 0
    grp_move: dict[str, list[float]] = defaultdict(list)
    grp_aligned: dict[str, list[float]] = defaultdict(list)

    for asset_id, seq in prices.items():
        asset_cats = cats.get(asset_id, [])
        by_date = {r["d"]: r["chg"] for r in seq}
        dates = sorted(by_date)
        date_set = set(dates)

        # 基线：无催化剂日（且窗口内均无催化剂）→ 其后 FORWARD_DAYS 天内最大 |异动|
        # （与有催化剂口径同窗口，保证 Lift 可比）
        for i in range(len(dates) - 1):
            d = dates[i]
            if window_has_catalyst(asset_cats, d, args.pre):
                continue
            # 检查后续 FORWARD_DAYS 天窗口内是否落入其他催化剂
            nxt_hits = []
            blocked = False
            for k in range(1, args.forward + 1):
                dk = d + timedelta(days=k)
                if dk in by_date:
                    nxt_hits.append(by_date[dk])
                if window_has_catalyst(asset_cats, dk, args.pre):
                    blocked = True
            if blocked:
                continue
            if not nxt_hits:
                continue
            if max(abs(h) for h in nxt_hits) >= args.move:
                wo_moves += 1
            wo_days += 1

        # 前瞻：每条催化剂发布后 FORWARD_DAYS 内最大 |异动|
        for c in asset_cats:
            pub = c["published_at"]
            # 只看窗口内异动日当天/次日有价格的日子
            hits = []
            for k in range(1, args.forward + 1):
                dk = pub + timedelta(days=k)
                if dk in by_date:
                    hits.append(by_date[dk])
            if not hits:
                continue
            max_abs = max(abs(h) for h in hits)
            # 方向对齐：取窗口内「沿催化剂方向」的最大异动
            s = aligned_sign(c["sentiment"])
            aligned = max((h * s for h in hits), default=0.0)
            grp_move[c["event_type"]].append(max_abs)
            grp_aligned[c["event_type"]].append(aligned)
            skey = f"strength:{c['strength'] or 'unknown'}"
            grp_move[skey].append(max_abs)
            grp_aligned[skey].append(aligned)

    # 有催化剂的"异动率"：每条催化剂 → 发布后 N 天内是否出现 ≥阈值异动
    with_cat = sum(1 for k, v in grp_move.items()
                   if not k.startswith("strength:")
                   for x in v if x >= args.move)
    # 用 event_type 非 strength 组的样本数近似催化剂条数
    cat_total = sum(len(v) for k, v in grp_move.items()
                    if not k.startswith("strength:"))
    _report_lift(f"前瞻口径（催化剂发布后 {args.forward} 天内）",
                 with_cat, cat_total, wo_moves, wo_days,
                 grp_move, grp_aligned)


def _report_lift(title, with_cat_moves, with_cat_days, wo_cat_moves, wo_cat_days,
                 grp_move, grp_aligned):
    def rate(n_moves, n_days):
        return n_moves / n_days if n_days else 0.0

    print("\n" + "=" * 60)
    print(f"一、总体 Lift：{title}")
    print("=" * 60)
    base = rate(wo_cat_moves, wo_cat_days)
    lift = rate(with_cat_moves, with_cat_days) / base if base else float("nan")
    print(f"  无催化剂: 异动 {wo_cat_moves}/{wo_cat_days} 日 = {rate(wo_cat_moves, wo_cat_days)*100:.2f}% (基线)")
    print(f"  有催化剂: 异动 {with_cat_moves}/{with_cat_days} 日 = {rate(with_cat_moves, with_cat_days)*100:.2f}%")
    print(f"  Lift = {lift:.2f}x")

    print("\n" + "=" * 60)
    print("二、按 event_type 的影响幅度")
    print("=" * 60)
    print(f"  {'event_type':<16} {'样本':>5} {'平均|异动|%':>10} {'方向对齐%':>10} {'异动占比%':>8}")
    for k in sorted(grp_move, key=lambda x: -len(grp_move[x])):
        if k.startswith("strength:"):
            continue
        moves = grp_move[k]
        aligned = grp_aligned[k]
        avg_move = sum(moves) / len(moves)
        avg_al = sum(aligned) / len(aligned) if aligned else 0.0
        hit = sum(1 for x in moves if x >= 15.0) / len(moves) * 100
        print(f"  {k:<16} {len(moves):>5} {avg_move:>10.2f} {avg_al:>10.2f} {hit:>8.1f}")

    print("\n" + "=" * 60)
    print("三、按 impact_strength 的影响幅度")
    print("=" * 60)
    print(f"  {'strength':<16} {'样本':>5} {'平均|异动|%':>10} {'方向对齐%':>10} {'异动占比%':>8}")
    for k in sorted(grp_move):
        if not k.startswith("strength:"):
            continue
        moves = grp_move[k]
        aligned = grp_aligned[k]
        avg_move = sum(moves) / len(moves)
        avg_al = sum(aligned) / len(aligned) if aligned else 0.0
        hit = sum(1 for x in moves if x >= 15.0) / len(moves) * 100
        print(f"  {k:<16} {len(moves):>5} {avg_move:>10.2f} {avg_al:>10.2f} {hit:>8.1f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂影响程度模型依据扫描（只读）")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="回溯天数")
    parser.add_argument("--move", type=float, default=MOVE_THRESHOLD, help="异动阈值 %")
    parser.add_argument("--pre", type=int, default=PRE_DAYS, help="回溯口径窗口天数")
    parser.add_argument("--forward", type=int, default=FORWARD_DAYS, help="前瞻口径窗口天数")
    parser.add_argument("--top", type=int, default=TOP_N, help="只扫市值前 N（0=全部）")
    parser.add_argument("--forward-mode", action="store_true", dest="forward_mode",
                        help="使用前瞻口径（推荐，规避反向因果）")
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.days)
    mode = "前瞻" if args.forward_mode else "回溯"
    print(f"扫描范围: {start} ~ {date.today()} | 异动阈值 |chg|≥{args.move}% | "
          f"口径={mode} | top={args.top or 'all'}")

    conn = get_conn()
    try:
        prices = load_prices(conn, start, args.top)
        cats = load_catalysts(conn, start)
        print(f"价格序列资产数: {len(prices)} | 有催化剂资产数: {len(cats)}")

        if args.forward_mode:
            scan_forward(prices, cats, args)
        else:
            scan_backward(prices, cats, args)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

