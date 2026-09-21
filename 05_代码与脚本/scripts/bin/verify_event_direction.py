#!/usr/bin/env python3
"""方向映射校验（评审遗留第 2 项）：event_type → 方向标注是否经得起实测。

用 catalyst_outcome 的 72h 超额收益（扣 BTC beta）实测每个 event_type 的价格方向，
对比 collect_catalyst_outcome.EVENT_TYPE_DIRECTION 的默认方向映射，找出：
  1. 方向标注错误、需要翻转的 event_type
  2. 原本标 neutral 但实际有明确方向、应补充的 event_type
  3. 原本标方向但实际无方向、应降级 neutral 的 event_type

判断口径与 hit 判定一致（excess>0 视为涨），主看中位数 + 涨占比，样本 >=10 才下结论。

取样口径（只取前向样本）：
  - ret_source = 'klines+market_daily'（base_time = signal.created_at，交易决策起点）
  - base_time + 72h <= NOW()（窗口已走完，排除「未到期却已结算」行）
  不这样筛会混入 backtest 行（base_time = published_at，历史回放口径）——
  两套 base_time 基线不可混用做校准取样（见 AGENTS.md）。

用法：
    python verify_event_direction.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from collect_catalyst_outcome import EVENT_TYPE_DIRECTION, get_conn  # noqa: E402


def classify_direction(median: float, up_ratio: float, n: int) -> str:
    """据实测判实际方向；样本不足或无方向归 neutral。"""
    if n < 10:
        return "neutral"
    if median > 1.0 and up_ratio >= 0.55:
        return "bullish"
    if median < -1.0 and up_ratio <= 0.45:
        return "bearish"
    return "neutral"


def main():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
               COUNT(*) AS n,
               ROUND(AVG(co.excess_72h)::numeric, 3) AS avg_excess_72h,
               ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY co.excess_72h)::numeric, 3) AS median_excess_72h,
               ROUND(AVG(CASE WHEN co.excess_72h > 0 THEN 1.0 ELSE 0.0 END)::numeric, 3) AS up_ratio,
               ROUND(AVG(co.ret_72h)::numeric, 3) AS avg_ret_72h,
               ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY co.ret_72h)::numeric, 3) AS median_ret_72h
        FROM biz.catalyst_outcome co
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
        WHERE co.excess_72h IS NOT NULL
          AND co.ret_source = 'klines+market_daily'
          AND co.base_time + INTERVAL '72 hours' <= NOW()
        GROUP BY 1
        ORDER BY n DESC
        """
    ).fetchall()
    conn.close()

    print(f"{'event_type':<20} {'n':>5} {'中位超额72h':>12} {'均值超额72h':>12} "
          f"{'涨占比':>7} {'中位收益72h':>12} {'当前映射':<10} {'实际方向':<10} 判定")
    print("-" * 120)

    flips = []      # (event_type, old, new) 需要翻转
    additions = []  # (event_type, new) 建议补充方向
    for r in rows:
        et = r["event_type"]
        n = r["n"]
        median = float(r["median_excess_72h"] or 0)
        avg = float(r["avg_excess_72h"] or 0)
        up = float(r["up_ratio"] or 0)
        med_ret = float(r["median_ret_72h"] or 0)
        cur = EVENT_TYPE_DIRECTION.get(et)
        cur_s = cur if cur else "None"
        actual = classify_direction(median, up, n)

        verdict = "OK"
        if n >= 10:
            if cur == "bullish" and actual == "bearish":
                verdict = "★翻转→bearish"
                flips.append((et, "bullish", "bearish"))
            elif cur == "bearish" and actual == "bullish":
                verdict = "★翻转→bullish"
                flips.append((et, "bearish", "bullish"))
            elif cur in (None, "neutral") and actual != "neutral":
                verdict = f"建议补{actual}"
                additions.append((et, actual))
            elif cur in ("bullish", "bearish") and actual == "neutral":
                verdict = "方向弱→考虑neutral"
            else:
                verdict = "OK"
        else:
            verdict = "样本不足"

        print(f"{et:<20} {n:>5} {median:>12.2f} {avg:>12.2f} {up:>7.2f} "
              f"{med_ret:>12.2f} {cur_s:<10} {actual:<10} {verdict}")

    print()
    print("=" * 60)
    if flips:
        print("需要翻转的方向映射：")
        for et, old, new in flips:
            print(f"  {et}: {old} -> {new}")
    else:
        print("无需要翻转的方向映射")
    if additions:
        print("建议补充方向：")
        for et, new in additions:
            print(f"  {et}: None -> {new}")


if __name__ == "__main__":
    main()
