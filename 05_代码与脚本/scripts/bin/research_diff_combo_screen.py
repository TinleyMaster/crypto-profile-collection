"""每日变化榜「候选→交叉验证」组合筛选器回测（provisional）。

目标：验证早报「📈 每日变化榜」组合玩法是否**叠加增强**——
  候选池：TVL 增幅榜（10-12 投研唯一正预测信号，fwd7 超额 +1.42%）
  交叉验证：巨鲸吸筹（whale_mover up）／交易所提币（exchange_flow_24h up）
  排雷：动量族 Top10（涨幅/市值变动/量价共振/放量/板块龙头 同日 top10）
  追涨警示对照：动量族 Top10（应负）± 巨鲸出货 / 净充值

问题：
  Q1  每个组合的 fwd7/14 超额中位 vs 基准、vs 基线 TVL 信号 → 交叉验证是增强还是仅缩样本？
  Q2  组合漏斗：TVL 候选逐级加过滤后，样本与超额如何变化？
  Q3  动量族 Top10 叠加巨鲸出货/净充值，负超额是否更极端？（警示组合）

口径与 research_daily_diff_forward.py 完全一致（入场=信号日收盘、fwd_k、逐 (日,tier)
全市场基准中位、超额=信号−基准、按日等权）。新榜单历史由 backfill_diff_new_categories.py
回填（2026-10-12）。

用法：
    python research_diff_combo_screen.py
    python research_diff_combo_screen.py --since 2026-09-20
输出：控制台表 + data/diff_combo_screen_summary.csv / diff_combo_screen_signals.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = (1, 3, 7, 14)

# 与 research_daily_diff_forward.py 同源的两段 SQL
SIGNAL_SQL = """
WITH sig AS (
    SELECT d.diff_date, d.asset_id, d.category, d.direction, d.rank,
           d.metric_value,
           COALESCE(d.detail_json->>'mcap_tier', 'other') AS tier,
           a.canonical_symbol, a.market_cap_rank AS cur_mcap_rank
    FROM biz.daily_diff_summary d
    JOIN core.asset a ON a.asset_id = d.asset_id
    WHERE d.diff_date BETWEEN %s AND %s
),
px AS (
    SELECT s.*,
           e.price_usd AS entry_px,
           f1.price_usd AS px1,
           f3.price_usd AS px3,
           f7.price_usd AS px7,
           f14.price_usd AS px14
    FROM sig s
    JOIN biz.asset_market_daily e
         ON e.asset_id = s.asset_id AND e.source_code = 'cmc'
        AND e.market_date = s.diff_date
    LEFT JOIN biz.asset_market_daily f1
         ON f1.asset_id = s.asset_id AND f1.source_code = 'cmc'
        AND f1.market_date = s.diff_date + 1
    LEFT JOIN biz.asset_market_daily f3
         ON f3.asset_id = s.asset_id AND f3.source_code = 'cmc'
        AND f3.market_date = s.diff_date + 3
    LEFT JOIN biz.asset_market_daily f7
         ON f7.asset_id = s.asset_id AND f7.source_code = 'cmc'
        AND f7.market_date = s.diff_date + 7
    LEFT JOIN biz.asset_market_daily f14
         ON f14.asset_id = s.asset_id AND f14.source_code = 'cmc'
        AND f14.market_date = s.diff_date + 14
)
SELECT diff_date, asset_id, category, direction, rank, metric_value, tier,
       canonical_symbol, cur_mcap_rank, entry_px, px1, px3, px7, px14
FROM px
WHERE entry_px IS NOT NULL AND entry_px > 0
"""

BENCH_SQL = """
WITH base AS (
    SELECT amd.market_date, amd.asset_id, amd.price_usd,
           CASE WHEN a.market_cap_rank <= 10 THEN 'top10'
                WHEN a.market_cap_rank <= 100 THEN 'top100'
                WHEN a.market_cap_rank <= 500 THEN 'top500'
                WHEN a.market_cap_rank <= 1000 THEN 'top1000'
                WHEN a.market_cap_rank <= 3000 THEN 'top3000'
                ELSE 'other' END AS tier
    FROM biz.asset_market_daily amd
    JOIN core.asset a ON a.asset_id = amd.asset_id
    WHERE amd.source_code = 'cmc'
      AND amd.market_date BETWEEN %s AND %s
),
win AS (
    SELECT market_date, asset_id, tier, price_usd,
           (LEAD(price_usd, 1)  OVER w / price_usd - 1) AS f1,
           (LEAD(price_usd, 3)  OVER w / price_usd - 1) AS f3,
           (LEAD(price_usd, 7)  OVER w / price_usd - 1) AS f7,
           (LEAD(price_usd, 14) OVER w / price_usd - 1) AS f14
    FROM base
    WINDOW w AS (PARTITION BY asset_id ORDER BY market_date)
)
SELECT market_date, tier,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f1)  AS med1,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f3)  AS med3,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f7)  AS med7,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f14) AS med14
FROM win
WHERE f1 IS NOT NULL
GROUP BY market_date, tier
ORDER BY market_date, tier
"""

# 动量族五榜（负预测族）
MOMENTUM_BOARDS = (
    "price_change_24h",
    "price_volume_surge",
    "volume_surge_24h",
    "market_cap_mover",
    "sector_rotation",
)


def _median(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _pdown(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return 100.0 * sum(1 for v in vals if v < 0) / len(vals)


def _fmt(v, digits=2, sign=True):
    if v is None:
        return "    -"
    return f"{v:+{digits + 3}.{digits}f}" if sign else f"{v:>{digits + 3}.{digits}f}"


def _day_ew(series):
    """按日等权：先按日取中位，再对日序列取中位。返回 (ew_median, 日序列)。"""
    by_day = {}
    for v in series:
        if v is None:
            continue
        by_day.setdefault(v[0], []).append(v[1])
    day_meds = [m for lst in by_day.values() if lst for m in [_median(lst)] if m is not None]
    if not day_meds:
        return None, []
    return _median(day_meds), sorted(day_meds)


def _wilcoxon_p(day_ex_meds):
    """按日等权超额中位序列 vs 0 的 Wilcoxon 配对符号秩 p（样本不足返回 None）。"""
    if len(day_ex_meds) < 5:
        return None
    from scipy import stats as sstats
    vals = [v for v in day_ex_meds if v != 0]
    if len(vals) < 5:
        return None
    try:
        _, p = sstats.wilcoxon(vals)
        return float(p)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="每日变化榜组合筛选器回测")
    parser.add_argument("--since", type=str, default="2026-08-19", help="起始日期")
    parser.add_argument("--until", type=str, default=None, help="结束日期（默认今天）")
    parser.add_argument("--live", action="store_true", help="追加输出最新一天的实际候选清单")
    args = parser.parse_args()

    since = args.since
    until = args.until or str(date.today())
    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SIGNAL_SQL, (since, until))
            cols = [d[0] for d in cur.description]
            sig_rows = cur.fetchall()
            sigs = [dict(zip(cols, r)) for r in sig_rows]

            cur.execute(BENCH_SQL, (since, until))
            cols = [d[0] for d in cur.description]
            bench_rows = cur.fetchall()
            bench = {}
            for r in bench_rows:
                d = dict(zip(cols, r))
                for h in HORIZONS:
                    if d.get(f"med{h}") is not None:
                        d[f"med{h}"] = float(d[f"med{h}"])
                bench[(str(d["market_date"]), d["tier"])] = d

    # 组装每信号 fwd / 基准 / 超额
    for s in sigs:
        entry = s["entry_px"]
        s["fwd"] = {}
        for h in HORIZONS:
            px = s.get(f"px{h}")
            s["fwd"][h] = float(px / entry - 1) if (px and entry) else None
        b = bench.get((str(s["diff_date"]), s["tier"]))
        s["bench"] = {h: (b.get(f"med{h}") if b else None) for h in HORIZONS}
        s["excess"] = {
            h: (s["fwd"][h] - s["bench"][h])
            if (s["fwd"][h] is not None and s["bench"][h] is not None) else None
            for h in HORIZONS
        }

    # ── 建索引：同日同币的榜单成员关系 ──
    # on_board[(diff_date, asset_id)] = {(category): rank, ...}
    on_board: dict[tuple, dict] = {}
    for s in sigs:
        key = (s["diff_date"], s["asset_id"])
        on_board.setdefault(key, {})[s["category"]] = s["rank"]

    # 每 (日,币) 取一个代表信号（优先 TVL 榜 up，否则任一条）—— screen_stats 直接查表
    tvl_by_key: dict[tuple, dict] = {}
    any_by_key: dict[tuple, dict] = {}
    for s in sigs:
        key = (s["diff_date"], s["asset_id"])
        any_by_key.setdefault(key, s)
        if s["category"] == "tvl_surge_24h" and s["direction"] == "up":
            tvl_by_key.setdefault(key, s)

    def board_members(cat, direction="up", top_n=None):
        """从 sigs 里选 (cat, direction) 的 (diff_date, asset_id) 集合，可限定 rank<=top_n。"""
        out = set()
        for s in sigs:
            if s["category"] == cat and s["direction"] == direction:
                if top_n is None or s["rank"] <= top_n:
                    out.add((s["diff_date"], s["asset_id"]))
        return out

    tvl_set = board_members("tvl_surge_24h", "up")
    whale_up = board_members("whale_mover", "up")
    whale_down = board_members("whale_mover", "down")
    ex_up = board_members("exchange_flow_24h", "up")
    ex_down = board_members("exchange_flow_24h", "down")
    mom_top10 = set()
    for cat in MOMENTUM_BOARDS:
        mom_top10 |= board_members(cat, "up", top_n=10)
    mom_any = set()
    for cat in MOMENTUM_BOARDS:
        mom_any |= board_members(cat, "up")

    # ── 定义组合筛选器：入参为 (diff_date, asset_id) 集合 ──
    def screen_stats(keys, label):
        """对一组 (diff_date, asset_id) 计算前向收益/超额统计。"""
        uniq = []
        seen = set()
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            s = tvl_by_key.get(k) or any_by_key.get(k)
            if s is not None:
                uniq.append(s)
        n = len(uniq)
        coins = len({s["asset_id"] for s in uniq})
        days = len({s["diff_date"] for s in uniq})
        row = {"label": label, "n": n, "coins": coins, "days": days}
        for h in HORIZONS:
            vals = [s["fwd"][h] for s in uniq]
            ex = [s["excess"][h] for s in uniq]
            row[f"med{h}"] = _median(vals)
            row[f"pdown{h}"] = _pdown(vals)
            row[f"excess{h}"] = _median(ex)
        ew, day_ex = _day_ew([(s["diff_date"], s["excess"][7]) for s in uniq])
        row["ew_ex7"] = ew
        row["wp7"] = _wilcoxon_p(day_ex)
        return row, uniq

    SCREENS = [
        # (标签, keys 集合, 是否对比基线 TVL)
        ("① TVL 榜（基线）", tvl_set, None),
        ("② TVL + 巨鲸吸筹", tvl_set & whale_up, "① TVL 榜（基线）"),
        ("③ TVL + 交易所提币", tvl_set & ex_up, "① TVL 榜（基线）"),
        ("④ TVL + 巨鲸吸筹 + 提币", tvl_set & whale_up & ex_up, "② TVL + 巨鲸吸筹"),
        ("⑤ TVL − 动量Top10（排雷）", tvl_set - mom_top10, "① TVL 榜（基线）"),
        ("⑥ 完整玩法：TVL+鲸+提−动量Top10",
         (tvl_set & whale_up & ex_up) - mom_top10, "④ TVL + 巨鲸吸筹 + 提币"),
        ("⑦ 追涨警示：动量Top10（对照）", mom_top10, None),
        ("⑧ 警示+巨鲸出货", mom_top10 & whale_down, "⑦ 追涨警示：动量Top10（对照）"),
        ("⑨ 警示+净充值", mom_top10 & ex_down, "⑦ 追涨警示：动量Top10（对照）"),
        # 新榜单单榜（短历史，观察自身信号）
        ("⑩ 巨鲸吸筹（单榜）", whale_up, None),
        ("⑪ 巨鲸出货（单榜）", whale_down, None),
        ("⑫ 交易所提币（单榜）", ex_up, None),
        ("⑬ 交易所净充值（单榜）", ex_down, None),
        ("⑭ 巨鲸吸筹 ∩ 提币", whale_up & ex_up, None),
    ]

    print("=" * 128)
    print(f"每日变化榜组合筛选器回测  [{since} ~ {until}]  （新榜单历史为回填样本，provisional）")
    print("=" * 128)
    print(
        f"{'组合':<34s}{'n':>6s}{'币数':>6s}{'天数':>5s}"
        f"{'fwd7中位':>10s}{'跌7d%':>8s}{'超额7d':>10s}"
        f"{'fwd14中位':>10s}{'跌14d%':>8s}{'超额14d':>10s}"
        f"{'EW超额7d':>10s}{'Wilcoxon-p':>11s}{'vs基线':>10s}"
    )
    rows = []
    base_ex7 = None
    for label, keys, vs_label in SCREENS:
        row, uniq = screen_stats(keys, label)
        rows.append(row)
        vs = ""
        if vs_label:
            bl = next((r for r in rows if r["label"] == vs_label), None)
            if bl and bl["excess7"] is not None and row["excess7"] is not None:
                vs = f"{(row['excess7'] - bl['excess7']) * 100:+.2f}pp"
        if label.startswith("①"):
            base_ex7 = row["excess7"]
        print(
            f"{label:<34s}{row['n']:>6d}{row['coins']:>6d}{row['days']:>5d}"
            f"{_fmt(row['med7'] * 100, 2) if row['med7'] is not None else '    -':>10s}"
            f"{_fmt(row['pdown7'], 1, False) if row['pdown7'] is not None else '    -':>8s}"
            f"{_fmt(row['excess7'] * 100, 2) if row['excess7'] is not None else '    -':>10s}"
            f"{_fmt(row['med14'] * 100, 2) if row['med14'] is not None else '    -':>10s}"
            f"{_fmt(row['pdown14'], 1, False) if row['pdown14'] is not None else '    -':>8s}"
            f"{_fmt(row['excess14'] * 100, 2) if row['excess14'] is not None else '    -':>10s}"
            f"{_fmt(row['ew_ex7'] * 100, 2) if row['ew_ex7'] is not None else '    -':>10s}"
            f"{row['wp7'] if row['wp7'] is not None else float('nan'):>11.4f}"
            f"{vs:>10s}"
        )

    # ── 小样本提示：n<20 的组合列出贡献币（避免被一两个币主导误读） ──
    print("\n" + "=" * 110)
    print("小样本组合（n<20）贡献币明细：")
    print("=" * 110)
    for label, keys, _vs in SCREENS:
        row, uniq = screen_stats(keys, label)
        if row["n"] >= 20:
            continue
        by_coin: dict[str, list] = {}
        for s in uniq:
            sym = s["canonical_symbol"]
            by_coin.setdefault(sym, []).append(s["fwd"][7] if s["fwd"][7] is not None else None)
        parts = []
        for sym, vals in sorted(by_coin.items(), key=lambda kv: -len(kv[1]))[:6]:
            m = _median(vals)
            parts.append(f"{sym}(n={len(vals)},fwd7={_fmt(m * 100, 2) if m is not None else '-'})")
        print(f"  {label:<34s} n={row['n']:<4d} 币数={row['coins']:<4d} → " + " ".join(parts))

    # ── 组合漏斗：TVL 候选逐级过滤 ──
    print("\n" + "=" * 110)
    print("组合漏斗：TVL 候选逐级加过滤（n 与 fwd7 超额变化）")
    print("=" * 110)
    print(f"{'过滤阶段':<44s}{'n':>6s}{'币数':>6s}{'天数':>5s}{'超额7d':>10s}{'超额14d':>10s}{'相对上一步':>12s}")
    stages = [
        ("TVL 榜全部候选", tvl_set),
        ("∩ 巨鲸吸筹", tvl_set & whale_up),
        ("∩ 巨鲸吸筹 ∩ 提币", tvl_set & whale_up & ex_up),
        ("∩ 鲸 ∩ 提 − 动量Top10", (tvl_set & whale_up & ex_up) - mom_top10),
        ("∩ 鲸 ∩ 提 − 任意动量榜", (tvl_set & whale_up & ex_up) - mom_any),
    ]
    prev_ex7 = None
    for label, keys in stages:
        row, _ = screen_stats(keys, label)
        delta = ""
        if prev_ex7 is not None and row["excess7"] is not None:
            delta = f"{(row['excess7'] - prev_ex7) * 100:+.2f}pp"
        prev_ex7 = row["excess7"]
        print(
            f"{label:<44s}{row['n']:>6d}{row['coins']:>6d}{row['days']:>5d}"
            f"{_fmt(row['excess7'] * 100, 2) if row['excess7'] is not None else '    -':>10s}"
            f"{_fmt(row['excess14'] * 100, 2) if row['excess14'] is not None else '    -':>10s}"
            f"{delta:>12s}"
        )

    # ── CSV ──
    out_dir = SCRIPT_DIR.parent / "data"
    out_dir.mkdir(exist_ok=True)
    f1 = out_dir / "diff_combo_screen_summary.csv"
    with open(f1, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["screen", "n", "coins", "days", "med7", "pdown7", "excess7",
                    "med14", "pdown14", "excess14", "ew_ex7", "wilcoxon_p7"])
        for r in rows:
            w.writerow([r["label"], r["n"], r["coins"], r["days"],
                        *(f"{r.get(k)}" if r.get(k) is not None else "" for k in
                          ("med7", "pdown7", "excess7", "med14", "pdown14", "excess14",
                           "ew_ex7", "wp7"))])
    print(f"\n[CSV] {f1}")

    f2 = out_dir / "diff_combo_screen_signals.csv"
    with open(f2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["screen", "diff_date", "asset_id", "symbol", "tier", "entry_px",
                    "fwd1", "fwd3", "fwd7", "fwd14", "excess7"])
        for label, keys, _vs in SCREENS:
            _row, uniq = screen_stats(keys, label)
            for s in sorted(uniq, key=lambda x: (x["diff_date"], x["asset_id"])):
                w.writerow([label, s["diff_date"], s["asset_id"], s["canonical_symbol"],
                            s["tier"], s["entry_px"],
                            *(f"{s['fwd'][h]}" if s["fwd"][h] is not None else "" for h in HORIZONS),
                            s["excess"][7] if s["excess"][7] is not None else ""])
    print(f"[CSV] {f2}")

    # ── 实时筛选：最新一天通过各阶段的实际候选 ──
    if args.live:
        latest = max(s["diff_date"] for s in sigs)
        print("\n" + "=" * 110)
        print(f"最新一天（{latest}）筛选结果")
        print("=" * 110)

        def _on_day(keys, day):
            return {k for k in keys if k[0] == day}

        d_tvl = _on_day(tvl_set, latest)
        d_whale_up = _on_day(whale_up, latest)
        d_whale_down = _on_day(whale_down, latest)
        d_ex_up = _on_day(ex_up, latest)
        d_ex_down = _on_day(ex_down, latest)
        d_mom = _on_day(mom_top10, latest)

        def _detail(k):
            s = tvl_by_key.get(k) or any_by_key.get(k)
            sym = s["canonical_symbol"] if s else "?"
            return s["cur_mcap_rank"] if s and s.get("cur_mcap_rank") else None

        base = sorted(d_tvl, key=lambda k: -int(any_by_key[k]["metric_value"] or 0))
        print(f"\n① 候选池（TVL 增幅榜 up，n={len(d_tvl)}）：")
        for k in base[:20]:
            print(f"    {any_by_key[k]['canonical_symbol']:<12s} mcap_rank={_detail(k)}")
        print("\n② 交叉验证（TVL 候选 × 巨鲸吸筹 / 提币）：")
        crossed = sorted((d_tvl & (d_whale_up | d_ex_up)), key=lambda k: k[1])
        for k in crossed:
            s = tvl_by_key[k]
            flags = []
            if k in d_whale_up:
                flags.append("鲸吸筹")
            if k in d_ex_up:
                flags.append("提币")
            if k in d_whale_down:
                flags.append("⚠鲸出货")
            if k in d_ex_down:
                flags.append("⚠净充值")
            if k in d_mom:
                flags.append("⚠动量Top10")
            print(f"    {s['canonical_symbol']:<12s} {' '.join(flags) or '（无额外命中）'}")
        if not crossed:
            print("    （无交集：TVL 候选未出现在巨鲸吸筹/提币榜）")
        print("\n③ 排雷（TVL 候选 − 动量Top10，即不受追涨警示影响的候选）：")
        clean = sorted(d_tvl - d_mom, key=lambda k: k[1])
        for k in clean[:20]:
            s = tvl_by_key[k]
            print(f"    {s['canonical_symbol']:<12s} mcap_rank={_detail(k)}")
        print("\n④ 追涨警示（动量Top10，建议规避/反向）：")
        for k in sorted(d_mom, key=lambda k: k[1])[:15]:
            s = any_by_key[k]
            flags = []
            if k in d_whale_down:
                flags.append("鲸出货")
            if k in d_ex_down:
                flags.append("净充值")
            print(f"    {s['canonical_symbol']:<12s} " + (" ".join(flags) if flags else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main())
