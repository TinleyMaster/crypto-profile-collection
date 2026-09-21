#!/usr/bin/env python3
"""催化剂权重校准（P1）：聚合 outcome 实测数据 → 生成各维度校准权重 → catalyst_calibration。

参考《催化剂实测评分与反馈校准方案_2026-09-17.md》v1.1 §4.2/§5。

数据源：catalyst_outcome 中（只取前向口径，勿混 backtest）
  - data_tier='L1'（有 K 线，小时级精确）
  - excess_72h IS NOT NULL（72h 窗口已结算，这是校准主窗口）
  - ret_source='klines+market_daily'（base_time = signal.created_at 的前向追踪样本）
  - base_time + 72h <= NOW()（窗口已走完，剔除「未到期却已结算」行）
  - direction_src IN ('impact','event_type')（有方向判定；neutral/null 不计命中率）

  注：不筛 ret_source 会混入 backtest 行（base_time = published_at 的历史回放口径），
  两套 base_time 基线不可混用做校准取样（见 AGENTS.md）。本脚本产出会写入
  biz.catalyst_calibration 并被快通道 _load_calibration() 注入实时打分，
  混口径样本会直接污染线上权重。

校准维度：
  - event_type     （→ G1 event_weight）
  - source         （→ G1 authority_score）
  - scope          （→ G1 scope_score，用 catalyst_grade.scope_score 分桶）
  - resonance_band （→ 验证 resonance 是否预测 72h 超额，P2 参考）

指标：
  - 幅度（定权用）：aligned_excess_72h = mean(excess_72h × 方向符号)
        即「市场在预测方向上的平均反应幅度」——看空事件跌得越准，值越大
  - 方向可靠性（仅记录，不参与定权）：hit_rate(72h)
  - 其他观测：avg_excess_24h / avg_excess_72h / median_excess_72h / ic_24h

校准分（2026-09-18 修正：强度=幅度，与方向可靠性解耦）：
  rel    = aligned_excess_72h / 1.0 - 1            # 1% 对齐超额 = 基准强度
  adj    = clamp(rel, -1, +1) * 0.30               # 相对先验最大 ±30% 调整
  shrink = n / (n + 30)                            # 小样本向先验收缩
  calibrated_score = prior * (1 + shrink * adj)

  说明：hit_rate 不再进入权重。此前 hit_rate 贡献 ±30 主导，导致 bearish 事件
        （如 tech_upgrade：命中率 0.79、对齐超额仅 +0.1%）被当成「强事件」把
        base_strength 抬高。权重只表达「事件能造成多大价格波动」；方向由
        catalyst_impact.impact_direction 单独决定（technical.py 生成 tp/sl）。
        采用相对调整（乘在 prior 上）以保持先验排序，避免把强弱顺序抹平。

样本门槛：<10 prior；10~30 prior（观察趋势）；≥30 calibrated（实测权重生效）。

用法：
    python calibrate_catalyst_weights.py                 # 本周校准
    python calibrate_catalyst_weights.py --dry-run       # 预览不写入
    python calibrate_catalyst_weights.py --window-end 2026-09-17   # 指定窗口终点
"""
from __future__ import annotations

import argparse
import statistics
import sys
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

MIN_CALIBRATED = 30   # 样本 ≥30 → 实测权重生效
MIN_TREND = 10        # 样本 ≥10 → 记录 trend 观察
ALIGNED_REF = 1.0     # 方向对齐超额基准（%）：1% 视为「基准强度」
MAX_REL_ADJ = 0.30    # 权重相对先验的最大调整幅度（±30%）
DIRECTION_SIGN = {"bullish": 1.0, "bearish": -1.0}
YAML_PATH = Path(__file__).resolve().parent.parent.parent / "workbench" / "catalyst" / "catalyst_rules.yaml"

# scope_score 分桶（对应 catalyst_grade.scope_score 的值）
SCOPE_BUCKETS = {
    90: "single_pair",
    65: "few_pairs",
    45: "many_pairs",
    60: "sector",
    30: "broad",
}


def get_conn():
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        options="-c lock_timeout=30000",
        keepalives=1,
        keepalives_idle=15,
        keepalives_interval=5,
        keepalives_count=3,
    )


def load_prior_scores() -> dict:
    """加载 catalyst_rules.yaml 先验权重（对比用）。"""
    prior = {"event_type": {}, "source": {}, "scope": {}}
    try:
        import yaml
        cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
        prior["event_type"] = cfg.get("event_type_weights", {})
        prior["source"] = cfg.get("authority_scores", {})
        prior["scope"] = cfg.get("scope_score_rules", {})
    except Exception as e:
        print(f"  [warn] 加载 yaml 失败，先验分为空: {e}")
    return prior


def calc_ic(composite_scores: list, excesses: list) -> float | None:
    """信息系数：composite_score 与 excess_72h 的 Spearman 秩相关。"""
    if len(composite_scores) < 5:
        return None
    pairs = sorted(zip(composite_scores, excesses))
    n = len(pairs)
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    # 秩
    def rank(vals):
        r = {}
        sorted_vals = sorted(set(vals))
        rank_map = {v: i + 1 for i, v in enumerate(sorted_vals)}
        return [rank_map[v] for v in vals]

    rx = rank(xs)
    ry = rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    varx = sum((a - mx) ** 2 for a in rx)
    vary = sum((b - my) ** 2 for b in ry)
    if varx == 0 or vary == 0:
        return None
    return round(cov / (varx ** 0.5 * vary ** 0.5), 4)


def calibrated_score_for(n: int, prior: int, aligned_excess: float | None) -> int:
    """校准分计算（0-100，与先验分同量纲）。

    语义：本分表示「事件强度 = 市场在预测方向上的反应幅度」，不含命中率。
    方向可靠性（hit_rate）单独记录、不参与定权——此前命中率贡献 ±30 主导，
    导致 bearish 事件（tech_upgrade 命中率 0.79、对齐超额仅 +0.1%）被当成
    「强事件」把 base_strength 抬高。

    公式（相对调整，保持先验强弱排序）：
        rel    = aligned_excess / ALIGNED_REF - 1     # 相对基准强度
        adj    = clamp(rel, -1, +1) * MAX_REL_ADJ     # 最大 ±30% 相对调整
        shrink = n / (n + 30)                         # 小样本向先验收缩
        score  = prior * (1 + shrink * adj)

    Args:
        n: 有效方向样本数
        prior: 先验权重
        aligned_excess: mean(excess_72h × 方向符号)；None 表示无可用样本
    """
    if aligned_excess is None:
        return prior
    rel = float(aligned_excess) / ALIGNED_REF - 1.0
    rel = max(-1.0, min(1.0, rel))
    shrink = n / (n + 30)
    score = prior * (1 + shrink * rel * MAX_REL_ADJ)
    return max(0, min(100, round(score)))


def fetch_samples(conn) -> list[dict]:
    """取校准样本：L1 + 72h 结算 + 有方向判定。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                co.catalyst_id, co.asset_id, co.signal_id,
                co.excess_24h, co.excess_72h,
                co.hit_72h,
                co.impact_direction, co.direction_src,
                COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
                ac.source_code,
                cg.scope_score,
                cr.resonance_score,
                cs.composite_score
            FROM biz.catalyst_outcome co
            JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
            LEFT JOIN biz.catalyst_grade cg ON cg.catalyst_id = co.catalyst_id
            LEFT JOIN biz.catalyst_resonance cr
                   ON cr.catalyst_id = co.catalyst_id AND cr.asset_id = co.asset_id
            LEFT JOIN biz.catalyst_signal cs
                   ON cs.catalyst_id = co.catalyst_id AND cs.asset_id = co.asset_id
            WHERE co.data_tier = 'L1'
              AND co.excess_72h IS NOT NULL
              AND co.ret_source = 'klines+market_daily'
              AND co.base_time + INTERVAL '72 hours' <= NOW()
              AND co.direction_src IN ('impact', 'event_type')
            """
        )
        return cur.fetchall()


def build_dim_stats(samples: list[dict]) -> dict:
    """按四维度聚合统计。返回 {dim: {value: stats_dict}}。"""
    dims = {"event_type": {}, "source": {}, "scope": {}, "resonance_band": {}}

    def add(dim, value, s):
        if value is None:
            return
        d = dims[dim].setdefault(str(value), {"excess_24h": [], "excess_72h": [],
                                              "hits": 0, "hit_total": 0,
                                              "aligned_72h": [], "composite": [],
                                              "excess_72h_for_ic": []})
        if s["excess_24h"] is not None:
            d["excess_24h"].append(s["excess_24h"])
        if s["excess_72h"] is not None:
            d["excess_72h"].append(s["excess_72h"])
        if s["hit_72h"] is not None:
            d["hit_total"] += 1
            if s["hit_72h"]:
                d["hits"] += 1
        # 方向对齐超额：看空事件以 -excess 计，衡量「按预测方向的反应幅度」
        sign = DIRECTION_SIGN.get((s["impact_direction"] or "").strip().lower())
        if sign is not None and s["excess_72h"] is not None:
            d["aligned_72h"].append(float(s["excess_72h"]) * sign)
        if s["composite_score"] is not None and s["excess_72h"] is not None:
            d["composite"].append(s["composite_score"])
            d["excess_72h_for_ic"].append(s["excess_72h"])

    for s in samples:
        add("event_type", s["event_type"], s)
        add("source", s["source_code"], s)
        scope_bucket = SCOPE_BUCKETS.get(s["scope_score"]) if s["scope_score"] is not None else None
        add("scope", scope_bucket, s)
        rs = s["resonance_score"]
        if rs is not None:
            band = "80-100" if rs >= 80 else ("60-79" if rs >= 60 else ("40-59" if rs >= 40 else "0-39"))
            add("resonance_band", band, s)

    return dims


def make_stats(d: dict) -> dict:
    """从原始样本聚合出统计指标。

    sample_count = 参与命中判定的样本数（hit_total，即非 neutral 方向）
    校准门槛基于此数，避免中性事件占比高的类型被小样本命中率误导。
    """
    n = len(d["excess_72h"])
    hit_total = d["hit_total"]
    hit_rate = round(d["hits"] / hit_total, 4) if hit_total else None
    avg24 = round(sum(d["excess_24h"]) / len(d["excess_24h"]), 4) if d["excess_24h"] else None
    avg72 = round(sum(d["excess_72h"]) / n, 4) if n else None
    med72 = round(statistics.median(d["excess_72h"]), 4) if n else None
    # 幅度（定权基准）：median(|excess_72h|)
    med_abs72 = round(statistics.median(abs(float(x)) for x in d["excess_72h"]), 4) if n else None
    # 方向对齐幅度（定权用）：mean(excess × 方向符号)
    aligned = round(sum(d["aligned_72h"]) / len(d["aligned_72h"]), 4) if d["aligned_72h"] else None
    ic = calc_ic(d["composite"], d["excess_72h_for_ic"])
    return {
        "sample_count": hit_total,     # 校准门槛：有效方向样本数
        "excess_samples": n,           # 有 72h 超额的样本数（超额统计口径）
        "hit_rate": hit_rate,          # 方向可靠性（仅记录，不参与定权）
        "avg_excess_24h": avg24,
        "avg_excess_72h": avg72,
        "median_excess_72h": med72,
        "median_abs_excess_72h": med_abs72,
        "aligned_excess_72h": aligned,  # 定权依据：方向对齐的反应幅度
        "ic_24h": ic,
    }


def upsert_calibration(conn, dim, value, stats, prior, window_end) -> int:
    """写入 catalyst_calibration + calibration_log。返回 calibrated_score。"""
    n = stats["sample_count"]
    if n >= MIN_CALIBRATED:
        mode = "calibrated"
        score = calibrated_score_for(n, prior, stats["aligned_excess_72h"])
    elif n >= MIN_TREND:
        mode = "prior"  # 样本不足但可观察趋势
        score = prior
    else:
        mode = "prior"
        score = prior

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biz.catalyst_calibration (
                dim, dim_value, sample_count, hit_rate,
                avg_excess_24h, avg_excess_72h, median_excess_72h,
                ic_24h, calibrated_score, prior_score, weight_mode,
                window_start, window_end
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (dim, dim_value, window_end) DO UPDATE SET
                sample_count = EXCLUDED.sample_count,
                hit_rate = EXCLUDED.hit_rate,
                avg_excess_24h = EXCLUDED.avg_excess_24h,
                avg_excess_72h = EXCLUDED.avg_excess_72h,
                median_excess_72h = EXCLUDED.median_excess_72h,
                ic_24h = EXCLUDED.ic_24h,
                calibrated_score = EXCLUDED.calibrated_score,
                prior_score = EXCLUDED.prior_score,
                weight_mode = EXCLUDED.weight_mode,
                window_start = EXCLUDED.window_start,
                created_at = NOW()
            """,
            (dim, value, n, stats["hit_rate"], stats["avg_excess_24h"],
             stats["avg_excess_72h"], stats["median_excess_72h"], stats["ic_24h"],
             score, prior, mode, window_end - timedelta(days=7), window_end),
        )
        # 变更审计：仅当 calibrated 且分值与先验不同
        if mode == "calibrated" and score != prior:
            cur.execute(
                """
                INSERT INTO biz.calibration_log (calib_id, dim, dim_value, prior_score, new_score, reason)
                VALUES (
                    (SELECT calib_id FROM biz.catalyst_calibration
                     WHERE dim=%s AND dim_value=%s AND window_end=%s),
                    %s, %s, %s, %s, 'auto: 实测校准'
                )
                """,
                (dim, value, window_end, dim, value, prior, score),
            )
    return score


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂权重校准（P1）")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--window-end", type=str, default=None,
                        help="校准窗口终点 YYYY-MM-DD，默认今天")
    args = parser.parse_args()

    window_end = date.fromisoformat(args.window_end) if args.window_end else date.today()
    prior = load_prior_scores()
    # 将先验 scope 值转成 scope bucket 名（与 SCOPE_BUCKETS 一致）
    prior_scope = {v: k for k, v in SCOPE_BUCKETS.items()}

    conn = get_conn()
    try:
        samples = fetch_samples(conn)
        print(f"校准样本: {len(samples)} 条（L1 + 72h结算 + 有方向）")
        if not samples:
            print("无样本可校准")
            return 0

        dims = build_dim_stats(samples)
        report_lines = []
        for dim in ["event_type", "source", "scope", "resonance_band"]:
            print(f"\n{'=' * 70}\n维度: {dim}\n{'=' * 70}")
            header = (f"  {'值':<24} {'方向样本':>6} {'命中率':>7} {'24h超额':>9} "
                      f"{'72h超额':>9} {'中位72h':>9} {'对齐72h':>9} {'先验':>5} {'校准分':>6} {'模式':<11}")
            print(header)
            for value, d in sorted(dims[dim].items(),
                                   key=lambda kv: -kv[1]["hit_total"]):
                stats = make_stats(d)
                if dim == "event_type":
                    p = prior["event_type"].get(value, 15)
                elif dim == "source":
                    # 先验 authority：精确匹配或 kol 前缀
                    p = prior["source"].get(value, 85 if value.startswith("kol_catalyst_binance_square") else 40)
                elif dim == "scope":
                    p = prior["scope"].get(SCOPE_BUCKETS.get(prior_scope.get(value, 0), value), 30)
                else:
                    p = 50  # resonance_band 无先验，仅观测
                score = upsert_calibration(conn, dim, value, stats, p, window_end) \
                    if not args.dry_run else (
                        calibrated_score_for(stats["sample_count"], p,
                                             stats["aligned_excess_72h"])
                        if stats["sample_count"] >= MIN_CALIBRATED else p)
                mode = "calibrated" if stats["sample_count"] >= MIN_CALIBRATED else "prior"
                hr = f"{stats['hit_rate']:.2f}" if stats["hit_rate"] is not None else "  -  "
                a24 = f"{stats['avg_excess_24h']:+.1f}" if stats["avg_excess_24h"] is not None else "  -  "
                a72 = f"{stats['avg_excess_72h']:+.1f}" if stats["avg_excess_72h"] is not None else "  -  "
                m72 = f"{stats['median_excess_72h']:+.1f}" if stats["median_excess_72h"] is not None else "  -  "
                al = f"{stats['aligned_excess_72h']:+.1f}" if stats["aligned_excess_72h"] is not None else "  -  "
                print(f"  {value:<24} {stats['sample_count']:>6} {hr:>7} {a24:>9} {a72:>9} {m72:>9} {al:>9} "
                      f"{p:>5} {score:>6} {mode:<11}")
                report_lines.append((dim, value, stats, p, score, mode))
        if not args.dry_run:
            conn.commit()
            print(f"\n已写入 catalyst_calibration + calibration_log（窗口 {window_end}）")
        else:
            print("\n[dry-run] 未写入")

        # 汇总 CSV
        csv_path = SCRIPT_DIR / "data" / f"calibration_{window_end}.csv"
        try:
            import csv
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["dim", "value", "samples", "hit_rate", "avg_excess_24h",
                            "avg_excess_72h", "median_excess_72h", "aligned_excess_72h",
                            "ic_24h", "prior", "calibrated", "mode"])
                for dim, value, st, p, score, mode in report_lines:
                    w.writerow([dim, value, st["sample_count"], st["hit_rate"],
                                st["avg_excess_24h"], st["avg_excess_72h"],
                                st["median_excess_72h"], st["aligned_excess_72h"],
                                st["ic_24h"], p, score, mode])
            print(f"报告已导出: {csv_path}")
        except Exception as e:
            print(f"  [warn] CSV 导出失败: {e}")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
