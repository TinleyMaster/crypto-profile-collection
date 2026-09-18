#!/usr/bin/env python3
"""催化剂校准效果评估（P2）：IC 对比 + 高估/低估周报。

参考《催化剂实测评分与反馈校准方案_2026-09-17.md》v1.1 §6/§7-P2。

核心问题：校准权重真的提升了评分的预测力吗？

方法：
1. 取 outcome（L1 + 72h结算 + 有方向）作为样本
2. 对每条样本：
   - 先验评分：signal.composite_score（历史用先验权重生成）
   - 校准评分：用校准权重重算 G1 base_strength → 重算 G6 composite_score
3. IC（信息系数）= composite_score 与 excess_72h 的 Spearman 秩相关
   - IC_prior vs IC_calib：若校准后 IC 更高 → 校准有效
4. 周报：各维度先验分 vs 校准分，标出高估(先验>校准+10)/低估

用法：
    python evaluate_catalyst_calibration.py            # 输出控制台 + Markdown 周报
    python evaluate_catalyst_calibration.py --dry-run  # 仅控制台
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings  # noqa: E402

# G6 composite_score 权重（与 catalyst_rules.yaml / signal.py 一致）
CW = {"kind_strength": 0.25, "resonance": 0.30, "persistence": 0.15,
      "fundamental": 0.15, "technical": 0.15}
PERSISTENCE_SCORES = {"structural": 90, "one_off": 60, "decaying": 30}
FUNDAMENTAL_SCORES = {True: 85, False: 20, None: 50}
TECHNICAL_SCORES = {"up": 85, "range": 55, "down": 25}
SCOPE_BUCKETS = {90: "single_pair", 65: "few_pairs", 45: "many_pairs",
                 60: "sector", 30: "broad"}
MIN_IC_SAMPLES = 10  # IC 最少样本


def get_conn():
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        options="-c lock_timeout=30000",
        keepalives=1, keepalives_idle=15, keepalives_interval=5, keepalives_count=3,
    )


def spearman_ic(scores: list, outcomes: list) -> float | None:
    """Spearman 秩相关（IC）。"""
    pairs = [(s, o) for s, o in zip(scores, outcomes)
             if s is not None and o is not None]
    if len(pairs) < MIN_IC_SAMPLES:
        return None
    pairs.sort(key=lambda p: p[0])
    n = len(pairs)
    # 分数秩
    rank_map = {}
    for i, (s, _) in enumerate(pairs):
        rank_map.setdefault(s, []).append(i + 1)
    rx = [sum(rank_map[s]) / len(rank_map[s]) for s, _ in pairs]
    # 结果秩
    out_sorted = sorted(o for _, o in pairs)
    ry = [out_sorted.index(o) + 1 for _, o in pairs]
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx * vy), 4)


def load_calibration(conn) -> dict:
    """最新一期校准权重（calibrated 模式）。"""
    rows = conn.execute(
        """
        SELECT dim, dim_value, calibrated_score
        FROM biz.catalyst_calibration
        WHERE weight_mode = 'calibrated'
          AND window_end = (SELECT MAX(window_end) FROM biz.catalyst_calibration)
        """
    ).fetchall()
    calib: dict = {}
    for r in rows:
        calib.setdefault(r["dim"], {})[r["dim_value"]] = r["calibrated_score"]
    return calib


def fetch_eval_samples(conn) -> list[dict]:
    """取评估样本：L1 + 72h 超额 + signal 存在。"""
    return conn.execute(
        """
        SELECT
            co.catalyst_id, co.asset_id, co.excess_72h,
            cs.composite_score AS score_prior,
            cs.base_strength AS bs_prior,
            cs.resonance_score, cs.persistence,
            cs.fundamental_pass, cs.technical_state,
            cg.authority_score, cg.event_weight, cg.scope_score,
            COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
            ac.source_code
        FROM biz.catalyst_outcome co
        JOIN biz.catalyst_signal cs
          ON cs.catalyst_id = co.catalyst_id AND cs.asset_id = co.asset_id
        LEFT JOIN biz.catalyst_grade cg ON cg.catalyst_id = co.catalyst_id
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
        WHERE co.data_tier = 'L1'
          AND co.excess_72h IS NOT NULL
          AND cs.composite_score IS NOT NULL
        """
    ).fetchall()


def composite_from(bs: int, s: dict) -> float:
    """用给定 base_strength 重算 G6 composite_score。"""
    res = s["resonance_score"] or 50
    pers = PERSISTENCE_SCORES.get(s["persistence"], 50)
    fund = FUNDAMENTAL_SCORES.get(s["fundamental_pass"], 50)
    tech = TECHNICAL_SCORES.get(s["technical_state"], 50)
    raw = (bs * CW["kind_strength"] + res * CW["resonance"]
           + pers * CW["persistence"] + fund * CW["fundamental"]
           + tech * CW["technical"])
    return round(max(0, min(100, raw)))


def recalc_bs_calib(s: dict, calib: dict) -> int:
    """用校准权重重算 G1 base_strength。

    三因子：
    - authority：校准 source 优先，回退先验 cg.authority_score
    - event_weight：校准 event_type 优先，回退先验 cg.event_weight
    - scope：校准 scope bucket 优先，回退先验 cg.scope_score
    """
    auth = calib.get("source", {}).get(s["source_code"]) or s["authority_score"] or 0
    ev = calib.get("event_type", {}).get(s["event_type"]) or s["event_weight"] or 0
    bucket = SCOPE_BUCKETS.get(s["scope_score"]) if s["scope_score"] is not None else "broad"
    sc = calib.get("scope", {}).get(bucket) or s["scope_score"] or 0
    bs = round(auth * 0.4 + ev * 0.4 + sc * 0.2)
    return max(0, min(100, bs))


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂校准效果评估（P2）")
    parser.add_argument("--dry-run", action="store_true", help="仅控制台不写周报")
    args = parser.parse_args()

    conn = get_conn()
    try:
        calib = load_calibration(conn)
        samples = fetch_eval_samples(conn)
        print(f"评估样本: {len(samples)} 条（L1 + 72h超额 + 有signal）")
        if not samples:
            print("无样本")
            return 0

        # 重算校准后评分
        for s in samples:
            s["bs_calib"] = recalc_bs_calib(s, calib)
            s["score_calib"] = composite_from(s["bs_calib"], s)
        # 重算先验评分（用 signal 里存的 G1 分项重算，保证同口径）
        for s in samples:
            s["score_prior_recalc"] = composite_from(s["bs_prior"], s)

        # ---- IC 对比 ----
        scores_prior = [s["score_prior"] for s in samples]
        scores_prior_rec = [s["score_prior_recalc"] for s in samples]
        scores_calib = [s["score_calib"] for s in samples]
        outcomes = [s["excess_72h"] for s in samples]

        ic_prior = spearman_ic(scores_prior, outcomes)
        ic_prior_rec = spearman_ic(scores_prior_rec, outcomes)
        ic_calib = spearman_ic(scores_calib, outcomes)

        print("\n" + "=" * 60)
        print("IC 对比（composite_score vs 72h 超额收益）")
        print("=" * 60)
        print(f"  先验评分 IC (signal.composite_score) : {ic_prior}")
        print(f"  先验评分 IC (重算同口径)              : {ic_prior_rec}")
        print(f"  校准评分 IC                           : {ic_calib}")
        if ic_prior and ic_calib:
            delta = ic_calib - ic_prior
            verdict = "✅ 校准提升预测力" if delta > 0.01 else (
                "⚠️ 校准基本持平" if abs(delta) <= 0.01 else "❌ 校准反而下降")
            print(f"  变化: {delta:+.4f}  {verdict}")

        # ---- 高估/低估周报 ----
        print("\n" + "=" * 60)
        print("高估/低估周报（先验分 vs 校准分，样本≥30）")
        print("=" * 60)
        rows = conn.execute(
            """
            SELECT dim, dim_value, sample_count, calibrated_score, prior_score, weight_mode
            FROM biz.catalyst_calibration
            WHERE window_end = (SELECT MAX(window_end) FROM biz.catalyst_calibration)
              AND sample_count >= 30
            ORDER BY dim, (prior_score - calibrated_score) DESC
            """
        ).fetchall()
        md_lines = ["# 催化剂校准周报", "",
                    f"> 窗口: {date.today()} ｜ 样本: {len(samples)} 条（L1+72h）",
                    f"> IC: 先验 {ic_prior} → 校准 {ic_calib}（{'提升' if (ic_calib or 0) > (ic_prior or 0) else '持平/下降'}）", ""]
        for r in rows:
            diff = (r["prior_score"] or 0) - (r["calibrated_score"] or 0)
            tag = "🔴 高估" if diff > 10 else ("🟢 低估" if diff < -10 else "⚪ 持平")
            line = (f"| {r['dim']} | {r['dim_value']} | {r['sample_count']} | "
                    f"{r['prior_score']} | {r['calibrated_score']} | {diff:+d} | {tag} |")
            print(f"  {tag}  {r['dim']}/{r['dim_value']:<24} 先验{r['prior_score']:>3} → 校准{r['calibrated_score']:>3} "
                  f"({diff:+d}) 样本{r['sample_count']}")
            md_lines.append(line)
        md_lines.append("")
        md_lines.append("## 解读")
        md_lines.append("- 🔴 高估：先验分比实测高 10+ 分，说明该维度被高估，校准后降权")
        md_lines.append("- 🟢 低估：先验分比实测低 10+ 分，校准后加权")
        md_lines.append("- ⚪ 持平：差值在 ±10 以内，维持先验")

        # ---- 分层收益（可选：按校准前后评分分位） ----
        def band_ret(scores, outcomes, lo, hi):
            vals = [o for s, o in zip(scores, outcomes)
                    if lo <= (s or 0) < hi and o is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        print("\n" + "=" * 60)
        print("分层收益：评分分位 vs 72h 平均超额（%）")
        print("=" * 60)
        print(f"  {'分位':<8} {'先验':>8} {'校准':>8}")
        for name, lo, hi in [("高分 80+", 80, 101), ("中分 60-79", 60, 80),
                             ("低分 <60", 0, 60)]:
            r_prior = band_ret(scores_prior, outcomes, lo, hi)
            r_calib = band_ret(scores_calib, outcomes, lo, hi)
            print(f"  {name:<8} {r_prior if r_prior is not None else '  -':>8} "
                  f"{r_calib if r_calib is not None else '  -':>8}")
            md_lines.append(f"- {name}: 先验 {r_prior}% / 校准 {r_calib}%")

        if not args.dry_run:
            out = SCRIPT_DIR / "data" / f"catalyst_eval_report_{date.today()}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
            print(f"\n周报已导出: {out}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
