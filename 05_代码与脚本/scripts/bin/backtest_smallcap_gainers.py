#!/usr/bin/env python3
"""小市值涨幅榜代币 · 冲高回落回测（"涨多少之后大概率下跌"）。

研究问题：
    涨幅榜上的小市值代币，24h 涨幅达到多少时，后期大概率下跌？

口径（2026-10-03 与业务对齐）：
  - 数据源：src_cmc.cmc_asset_quote_snapshot（CMC 全市场快照，2026-08-19 起，
    每日约 3 次快照 / 8000+ 币种）。CMC 官方涨幅榜（biz.asset_trending）因付费
    套餐限制长期为空表，故用快照自行重构「涨幅榜」。
  - 小市值：信号日 market_cap ≤ 50M（可 --mcap-max 调整）。
  - 信号：每个 (cmc_id, 日) 取当日最后一次快照为日收盘口径，24h 涨幅
    percent_change_24h ≥ --min-gain（默认 5%）纳入信号池。同一币同一日只计 1 条，
    避免日内 3 次快照互相重叠计数。
  - 判据：下跌概率 = P(后续 N 日收盘价 < 信号日收盘价)，N ∈ {1,3,7}。
    「大概率下跌」定义：下跌概率 > 50%（默认判据）。
  - 两套输出：
    ① 分桶表（分段口径）：5~10 / 10~20 / 20~30 / 30~50 / 50~100 / 100~200 / 200%+
    ② 阈值扫描（累计口径）：涨幅 ≥ thr 的全部样本，thr 网格 → 找 P(下跌) 跨过
       50% 的临界涨幅（n ≥ MIN_N 才生效）。
  - 附加统计：每桶唯一币数 / 独立信号日数 / forward 价格缺失率（币涨出加载带
    200M、下架、数据缺口；缺失本身是坏结果，此处不记 0 而是单独披露）。

⚠️ 已知局限（结论一律 provisional）：
  - 快照仅 46 天（2026-08-19 ~ 2026-10-03）、单一 regime，不可外推（§12.1-A1/A2）。
  - 同一币连续多日上榜会被重复计数（每个上榜日都是独立「信号」；唯一币数已披露）。
  - 平均收益被右侧极端赢家（少数 10x+）严重扭曲，均值仅供参照，判据以
    下跌概率 + 中位收益为准。

实现说明（2026-10-03 排障定稿）：
  - 全程**服务端 SQL 聚合**，只回传汇总行（几十~几百字节）。远端库对此表全表
    扫描本身只要数秒（实测 DISTINCT ON 日线 CTE 7.3s），但把 ~50 万行原始快照
    传输到本地在慢链路上需 8~25 分钟且偶发被掐断——故不在客户端做逐行回测。
  - 取价语义：daily CTE = 每 (cmc_id, 日) 最后一次快照（日收盘口径）；forward
    收益 = 信号日价格 → d+1/d+3/d+7 日收盘价，LEFT JOIN 缺失记 NULL（= miss）。

用法：
    python backtest_smallcap_gainers.py
    python backtest_smallcap_gainers.py --mcap-max 300000000 --min-gain 10
    python backtest_smallcap_gainers.py --min-n 50
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

MCAP_MAX = 50e6                 # 信号市值上限（USD）
UNIVERSE_CAP = 200e6            # 价格加载带（USD）——覆盖信号币后续 4x 内的价格
MIN_GAIN = 5.0                  # 纳入信号池的最小 24h 涨幅（%）
HORIZONS = (1, 3, 7)            # 后续观察窗口（日）
BUCKETS = ((5, 10), (10, 20), (20, 30), (30, 50),
           (50, 100), (100, 200), (200, 1e9))
THRESH_GRID = (10, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125, 150, 200, 300)
MIN_N = 30                      # 统计桶/阈值最少样本
MIN_DAYS = 5                    # 最少独立信号日
DECLINE_CRIT = 0.50             # 「大概率下跌」判据：P(下跌) > 该值

# ── >50% 热榜专项（--hot）参数 ──
HOT_MIN_GAIN = 50.0             # 热榜涨幅下界（%）
HOT_WINDOW = 14                 # 观察窗口（日）
HOT_BUCKETS = ((50, 75), (75, 100), (100, 150), (150, 200),
               (200, 300), (300, 500), (500, 1e9))
TURNOVER_BANDS = ((0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 1e6))
CHG1H_BANDS = ((0, 5), (5, 10), (10, 20), (20, 40), (40, 1e3))

# >50% 热榜专项 SQL：对每个信号（市值 ≤ mcap_max 且 24h 涨幅 ≥ min_gain、含完整
# HOT_WINDOW 日后续价格）回传**每条信号一行**的聚合指标（~千行级，传输轻量）：
#   各窗口收益 r1/r3/r7/r14、14 日最高点(peak)+到顶日、14 日最低点(trough)+到末日、
#   首次跌破入场价日(first_neg_day)、信号时点快照级 RSI(14)（Cutler 简单平均，
#   用该币全部快照序列 ~8h 粒度）、以及切分用的市值/成交量/1h 涨幅。
#   peak = 进榜后 14 日内最大正收益（"还能冲多高"）；trough = 最低点（"最深回撤"）。
#   ⚠️ RSI 粒度：真 1h RSI 需 K 线，但 >50% 热榜币仅 5.6% 在 Binance 有 1h K 线
#   （选择偏差），故主口径用快照序列（~8h 粒度、全覆盖）的 RSI(14)，语义为
#   「进榜时点的快照级动量过热程度」；真小时级见 --hot 输出的 klines 子样本一节。
HOT_SQL = """
WITH hot AS (
    SELECT DISTINCT ON (cmc_id, DATE(quote_time))
           cmc_id, DATE(quote_time) AS d, quote_time, price_usd AS entry,
           market_cap, volume_24h, percent_change_24h AS chg, percent_change_1h AS chg1h
    FROM src_cmc.cmc_asset_quote_snapshot
    WHERE market_cap > 0 AND market_cap <= %s AND price_usd > 0
      AND percent_change_24h >= %s
      AND DATE(quote_time) <= (SELECT MAX(DATE(quote_time))
                               FROM src_cmc.cmc_asset_quote_snapshot
                               WHERE market_cap > 0 AND market_cap <= %s) - %s
    ORDER BY cmc_id, DATE(quote_time), quote_time DESC
),
px AS (
    SELECT s.cmc_id, s.quote_time, s.price_usd,
           s.price_usd / LAG(s.price_usd) OVER (PARTITION BY s.cmc_id ORDER BY s.quote_time) - 1 AS chg
    FROM src_cmc.cmc_asset_quote_snapshot s
    WHERE s.cmc_id IN (SELECT DISTINCT cmc_id FROM hot) AND s.price_usd > 0
),
rsi AS (
    SELECT cmc_id, quote_time,
           CASE WHEN avg_loss = 0 THEN 100.0
                ELSE 100 - 100 / (1 + avg_gain / avg_loss) END AS rsi14
    FROM (
        SELECT cmc_id, quote_time, chg,
               AVG(GREATEST(chg, 0)) OVER (PARTITION BY cmc_id ORDER BY quote_time
                    ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS avg_gain,
               AVG(GREATEST(-chg, 0)) OVER (PARTITION BY cmc_id ORDER BY quote_time
                    ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS avg_loss
        FROM px
    ) t WHERE chg IS NOT NULL
),
daily AS (
    SELECT DISTINCT ON (cmc_id, DATE(quote_time)) cmc_id, DATE(quote_time) AS d,
           price_usd, market_cap
    FROM src_cmc.cmc_asset_quote_snapshot
    WHERE market_cap > 0 AND market_cap <= %s AND price_usd > 0
    ORDER BY cmc_id, DATE(quote_time), quote_time DESC
),
fwd AS (
    SELECT s.cmc_id, s.d AS sig_d, (fk.price_usd - s.entry) / s.entry AS ret,
           (fk.d - s.d) AS k
    FROM hot s
    JOIN daily fk ON fk.cmc_id = s.cmc_id AND fk.d > s.d AND fk.d <= s.d + %s
),
r AS (
    SELECT cmc_id, sig_d,
           MAX(ret) FILTER (WHERE k = 1) AS r1,
           MAX(ret) FILTER (WHERE k = 3) AS r3,
           MAX(ret) FILTER (WHERE k = 7) AS r7,
           MAX(ret) FILTER (WHERE k = 14) AS r14
    FROM fwd GROUP BY cmc_id, sig_d
),
peak AS (
    SELECT DISTINCT ON (cmc_id, sig_d) cmc_id, sig_d, k AS peak_day, ret AS peak
    FROM fwd ORDER BY cmc_id, sig_d, ret DESC, k
),
trough AS (
    SELECT DISTINCT ON (cmc_id, sig_d) cmc_id, sig_d, k AS trough_day, ret AS trough
    FROM fwd ORDER BY cmc_id, sig_d, ret ASC, k
),
firstneg AS (
    SELECT cmc_id, sig_d, MIN(k) AS first_neg_day
    FROM fwd WHERE ret < 0 GROUP BY cmc_id, sig_d
)
SELECT h.cmc_id, h.d AS sig_d, h.market_cap AS mcap, h.volume_24h AS vol,
       h.chg, h.chg1h, rs.rsi14,
       r.r1, r.r3, r.r7, r.r14,
       p.peak, p.peak_day, t.trough, t.trough_day,
       fn.first_neg_day
FROM hot h
JOIN rsi rs ON rs.cmc_id = h.cmc_id AND rs.quote_time = h.quote_time
JOIN r ON r.cmc_id = h.cmc_id AND r.sig_d = h.d
JOIN peak p ON p.cmc_id = h.cmc_id AND p.sig_d = h.d
JOIN trough t ON t.cmc_id = h.cmc_id AND t.sig_d = h.d
LEFT JOIN firstneg fn ON fn.cmc_id = h.cmc_id AND fn.sig_d = h.d
"""

# 服务端回测主 SQL：只回传两段汇总行（buckets / thresholds）。
#   daily CTE = 每 (cmc_id, 日) 最后一次快照（日收盘口径，市值 ≤ universe_cap）
#   sig       = 信号日（市值 ≤ mcap_max 且 24h 涨幅 ≥ min_gain）LEFT JOIN 后续日收盘价
#   每段每行都是横截面宽表：n / 唯一币 / 独立日 + 每个窗口的 有效样本 / 下跌概率 /
#   中位收益 / 平均收益（NULL 处即 miss，缺失率 = 1 - n_hz/n）。
SQL_BACKTEST = """
WITH daily AS (
    SELECT DISTINCT ON (cmc_id, DATE(quote_time))
           cmc_id, DATE(quote_time) AS d, price_usd, market_cap, percent_change_24h
    FROM src_cmc.cmc_asset_quote_snapshot
    WHERE market_cap > 0 AND market_cap <= %s AND price_usd > 0
    ORDER BY cmc_id, DATE(quote_time), quote_time DESC
),
sig AS (
    SELECT sm.cmc_id, sm.d, sm.percent_change_24h,
           f1.price_usd / sm.price_usd - 1 AS ret1,
           f3.price_usd / sm.price_usd - 1 AS ret3,
           f7.price_usd / sm.price_usd - 1 AS ret7
    FROM (
        SELECT cmc_id, d, price_usd, percent_change_24h,
               LAG(d) OVER (PARTITION BY cmc_id ORDER BY d) AS prev_d
        FROM daily
        WHERE market_cap > 0 AND market_cap <= %s
          AND percent_change_24h >= %s
    ) sm
    LEFT JOIN daily f1 ON f1.cmc_id = sm.cmc_id AND f1.d = sm.d + 1
    LEFT JOIN daily f3 ON f3.cmc_id = sm.cmc_id AND f3.d = sm.d + 3
    LEFT JOIN daily f7 ON f7.cmc_id = sm.cmc_id AND f7.d = sm.d + 7
    WHERE %s = 0
       OR sm.prev_d IS NULL
       OR (sm.d - sm.prev_d) > %s
)
SELECT 'bucket' AS section, bucket AS label,
       COUNT(*) AS n, COUNT(DISTINCT cmc_id) AS coins, COUNT(DISTINCT d) AS days,
       COUNT(ret1) AS n1, AVG((ret1 < 0)::int) AS p1,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret1) AS med1, AVG(ret1) AS mean1,
       COUNT(ret3) AS n3, AVG((ret3 < 0)::int) AS p3,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret3) AS med3, AVG(ret3) AS mean3,
       COUNT(ret7) AS n7, AVG((ret7 < 0)::int) AS p7,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret7) AS med7, AVG(ret7) AS mean7
FROM (
    SELECT *,
           CASE
               WHEN percent_change_24h < 10 THEN '5-10'
               WHEN percent_change_24h < 20 THEN '10-20'
               WHEN percent_change_24h < 30 THEN '20-30'
               WHEN percent_change_24h < 50 THEN '30-50'
               WHEN percent_change_24h < 100 THEN '50-100'
               WHEN percent_change_24h < 200 THEN '100-200'
               ELSE '200+'
           END AS bucket
    FROM sig
) b
GROUP BY bucket
UNION ALL
SELECT 'threshold' AS section, t.thr::text AS label,
       COUNT(*) AS n, COUNT(DISTINCT s.cmc_id) AS coins, COUNT(DISTINCT s.d) AS days,
       COUNT(s.ret1) AS n1, AVG((s.ret1 < 0)::int) AS p1,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.ret1) AS med1, AVG(s.ret1) AS mean1,
       COUNT(s.ret3) AS n3, AVG((s.ret3 < 0)::int) AS p3,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.ret3) AS med3, AVG(s.ret3) AS mean3,
       COUNT(s.ret7) AS n7, AVG((s.ret7 < 0)::int) AS p7,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.ret7) AS med7, AVG(s.ret7) AS mean7
FROM sig s
CROSS JOIN (VALUES (10), (15), (20), (25), (30), (40), (50), (60),
                   (75), (100), (125), (150), (200), (300)) AS t(thr)
WHERE s.percent_change_24h >= t.thr
GROUP BY t.thr
"""


def fmt_bucket(label: str) -> str:
    """'5-10' → '+5~10%'；'200+' → '+200%+'（SQL 内避免 % 字符，展示时补回）。"""
    head, sep, tail = label.partition("-")
    head = head.rstrip("+")
    if not sep or tail == "+":
        return f"+{head}%+"
    return f"+{head}~{tail}%"


def bucket_order(label: str) -> int:
    head = label.split("-")[0].rstrip("+")
    return next((i for i, b in enumerate(BUCKETS) if b[0] == int(head)), 99)


def print_rows(rows: list[dict], fields: list[tuple[str, str, int]], title: str) -> None:
    print(f"\n=== {title} ===")
    headers = "".join(f"{name:>{width}}" for name, _, width in fields)
    print(headers)
    print("-" * len(headers))
    for r in rows:
        line = ""
        for key, fmt, width in fields:
            v = r.get(key)
            if v is None:
                line += f"{'--':>{width}}"
            elif fmt == "s":
                line += f"{v:>{width}}"
            elif fmt == "pct":
                line += f"{v * 100:>{width}.1f}"
            elif fmt == "int":
                line += f"{int(v):>{width}}"
            else:
                line += f"{v * 100:>{width}.2f}"
        print(line)


def run_backtest(conn, universe_cap: float, mcap_max: float, min_gain: float,
                 episode_gap: int, min_n: int) -> tuple[list[dict], list[dict]]:
    """执行服务端 SQL 回测，返回 (分桶行, 阈值扫描行)。"""
    with conn.cursor() as cur:
        cur.execute(SQL_BACKTEST,
                    (universe_cap, mcap_max, min_gain, episode_gap, episode_gap))
        raw = cur.fetchall()
    rows = [
        {
            "section": r[0], "label": r[1], "n": r[2], "coins": r[3], "days": r[4],
            **{f"n{hz}": r[5 + (i * 4)] for i, hz in enumerate(HORIZONS)},
            **{f"p{hz}": r[6 + (i * 4)] for i, hz in enumerate(HORIZONS)},
            **{f"med{hz}": r[7 + (i * 4)] for i, hz in enumerate(HORIZONS)},
            **{f"mean{hz}": r[8 + (i * 4)] for i, hz in enumerate(HORIZONS)},
        }
        for r in raw
    ]
    bucket_rows = [r for r in rows if r["section"] == "bucket"]
    sweep_rows = [r for r in rows if r["section"] == "threshold"]
    bucket_rows.sort(key=lambda r: bucket_order(r["label"]))
    sweep_rows.sort(key=lambda r: float(r["label"]))
    bucket_rows = [r for r in bucket_rows if r["n"] >= min_n and r["days"] >= MIN_DAYS]
    sweep_rows = [r for r in sweep_rows if r["n"] >= min_n]
    return bucket_rows, sweep_rows


def crit_threshold(sweep_rows: list[dict], hz: int) -> tuple[str, dict] | None:
    """首个 P(下跌) > 判据线的阈值档（累计口径）。"""
    for r in sweep_rows:
        p = r.get(f"p{hz}")
        if p is not None and p > DECLINE_CRIT:
            return r["label"], r
    return None


def _med(vals: list) -> float | None:
    """中位数（None 忽略）。"""
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _pct(vals: list, p: float) -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    vals.sort()
    return vals[min(len(vals) - 1, int(len(vals) * p))]


def _hot_klines_subsample(conn, sigs: list[dict]) -> None:
    """Binance 1h K 线真小时级子样本（仅覆盖有 K 线的币，披露选择偏差）。

    对每条 >50% 热榜信号（该币在 biz.asset_klines 有 1h K 线）：
      - 入场 = 信号日最后一根 1h bar 收盘价（≈ 日收盘口径对齐）
      - 真 1h RSI(14)（Cutler 简单平均，基于 1h 收盘序列）
      - 后续 +1/+4/+24/+72h 收益、72h 内峰值与到顶小时数
    ⚠️ 覆盖仅 ~5.6%（小市值涨幅榜币多数不在 Binance），结果不可外推到全样本。
    """
    hot_ids = {s["cmc_id"] for s in sigs}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cmc_id, symbol FROM src_cmc.cmc_asset_map WHERE cmc_id = ANY(%s)",
            (list(hot_ids),),
        )
        # symbol 归一化：去掉 USDT 后缀、统一大写，以对齐 biz.asset_klines 的 symbol
        # （klines symbol 如 'PEPEUSDT'，cmc_asset_map.symbol 通常为 'PEPE'）。
        sym_of = {cid: (sym or "").upper().rstrip("USDT")
                  for cid, sym in cur.fetchall()}
        base_symbols = [sym for sym in set(sym_of.values()) if sym]
        # klines 表 symbol 带 USDT 后缀，查询时补回（匹配到的都是 USDT 交易对）
        symbols_q = [f"{s}USDT" for s in base_symbols]
        if not symbols_q:
            print("\n[hot] klines 子样本：无币匹配到 1h K 线，跳过")
            return
        cur.execute(
            "SELECT symbol, open_time, close_px FROM biz.asset_klines "
            "WHERE interval = '1h' AND symbol = ANY(%s) "
            "AND open_time >= '2026-08-15' AND open_time <= '2026-10-04' "
            "ORDER BY symbol, open_time",
            (symbols_q,),
        )
        rows = cur.fetchall()
    closes: dict[str, list[tuple]] = {}
    for sym, t, px in rows:
        if px is None:
            continue
        closes.setdefault(sym.upper().rstrip("USDT"), []).append((t, float(px)))
    n_sym = len({k for k in closes if any(v for v in closes[k])})
    print(f"[hot] klines 子样本：{n_sym} 个 symbol 命中 1h K 线 "
          f"（覆盖信号 {sum(1 for s in sigs if s['cmc_id'] in sym_of)} 条）")

    def _rsi(closes_: list[float]) -> float | None:
        if len(closes_) < 15:
            return None
        gains = losses = 0.0
        for i in range(len(closes_) - 14, len(closes_)):
            d = closes_[i] / closes_[i - 1] - 1
            if d >= 0:
                gains += d
            else:
                losses -= d
        if losses == 0:
            return 100.0
        rs = (gains / 14) / (losses / 14)
        return 100 - 100 / (1 + rs)

    recs = []
    for s in sigs:
        sym = sym_of.get(s["cmc_id"])
        series = closes.get(sym)
        if not series:
            continue
        day_start = s["sig_d"]
        idxs = [i for i, (t, _) in enumerate(series)
                if day_start <= t.date() < day_start + timedelta(days=1)]
        if not idxs:
            continue
        entry_idx = idxs[-1]
        if entry_idx < 14:
            continue
        entry = series[entry_idx][1]
        rsi = _rsi([px for _, px in series[: entry_idx + 1]])
        if rsi is None:
            continue
        n = len(series)
        def _ret(h: int):
            j = entry_idx + h
            return series[j][1] / entry - 1 if j < n else None
        rets = {h: _ret(h) for h in (1, 4, 24, 72)}
        fwd72 = [series[j][1] / entry - 1
                 for j in range(entry_idx + 1, min(entry_idx + 73, n))]
        peak = max(fwd72) if fwd72 else None
        peak_h = fwd72.index(peak) + 1 if peak is not None else None
        recs.append({"rsi": rsi, "rets": rets, "peak": peak, "peak_h": peak_h})
    if len(recs) < 10:
        print(f"[hot] klines 子样本样本过少（{len(recs)}），跳过统计")
        return

    print("\n=== klines 真小时级子样本（⚠️ 仅 Binance 有 K 线的币，n={}，选择偏差不可外推）===".format(len(recs)))
    bands = ((0, 60, "RSI<60"), (60, 70, "60~70"), (70, 80, "70~80"),
             (80, 90, "80~90"), (90, 1000, "RSI>=90"))
    print(f"{'RSI分档':>10}{'n':>5}{'跌+1h%':>8}{'跌+24h%':>9}{'跌+72h%':>9}"
          f"{'中位+24h%':>10}{'中位+72h%':>10}{'中位峰值%':>10}{'中位到顶h':>10}")
    print("-" * 76)
    for lo, hi, lb in bands:
        sub = [r for r in recs if lo <= r["rsi"] < hi]
        if len(sub) < 10:
            continue
        def _f(h):
            vals = [r["rets"][h] for r in sub if r["rets"][h] is not None]
            return sum(1 for x in vals if x < 0) / len(vals) if vals else None, _med(vals)
        p1, _ = _f(1); p24, m24 = _f(24); p72, m72 = _f(72)
        pk = [r["peak"] for r in sub if r["peak"] is not None]
        ph = [r["peak_h"] for r in sub if r["peak_h"] is not None]
        print(f"{lb:>10}{len(sub):>5}"
              f"{p1 * 100 if p1 else float('nan'):>8.1f}"
              f"{p24 * 100 if p24 else float('nan'):>9.1f}"
              f"{p72 * 100 if p72 else float('nan'):>9.1f}"
              f"{m24 * 100 if m24 is not None else float('nan'):>10.1f}"
              f"{m72 * 100 if m72 is not None else float('nan'):>10.1f}"
              f"{_med(pk) * 100 if pk else float('nan'):>10.1f}"
              f"{_med(ph) if ph else float('nan'):>10.1f}")
    print("\n[hot] ⚠️ klines 子样本为 Binance 流动性较好的币，跌概率通常低于全样本；"
          "真小时级结论仅限该子样本，不得外推到小市值涨幅榜整体")


def run_hot_analysis(conn, args) -> int:
    """>50% 涨幅榜专项：细桶 + 收益分布 + 冲高空间 + 快照 RSI + 换手/1h 动量交叉。"""
    with conn.cursor() as cur:
        cur.execute(HOT_SQL, (args.mcap_max, HOT_MIN_GAIN, args.universe_cap,
                              HOT_WINDOW, args.universe_cap, HOT_WINDOW))
        raw = cur.fetchall()

    # 每条信号一行：(cmc_id, sig_d, mcap, vol, chg, chg1h, rsi14,
    #                r1, r3, r7, r14, peak, peak_day, trough, trough_day, first_neg_day)
    sigs = []
    for r in raw:
        sigs.append({
            "cmc_id": r[0], "sig_d": r[1],
            "mcap": float(r[2] or 0), "vol": float(r[3] or 0),
            "chg": float(r[4] or 0), "chg1h": float(r[5] or 0),
            "rsi14": float(r[6]) if r[6] is not None else None,
            "r1": float(r[7]) if r[7] is not None else None,
            "r3": float(r[8]) if r[8] is not None else None,
            "r7": float(r[9]) if r[9] is not None else None,
            "r14": float(r[10]) if r[10] is not None else None,
            "peak": float(r[11]) if r[11] is not None else None,
            "peak_day": int(r[12]) if r[12] is not None else None,
            "trough": float(r[13]) if r[13] is not None else None,
            "trough_day": int(r[14]) if r[14] is not None else None,
            "first_neg_day": int(r[15]) if r[15] is not None else None,
            "turnover": (float(r[3] or 0) / float(r[2])) if r[2] else None,
        })
    n_coins = len({s["cmc_id"] for s in sigs})
    print(f"[hot] >{HOT_MIN_GAIN:.0f}% 涨幅榜信号 {len(sigs)} 条 / 唯一币 {n_coins} 个")
    if not sigs:
        print("[hot] 无信号，检查 --mcap-max / 数据")
        return 1

    def _bucket_label(b: tuple) -> str:
        return f"+{b[0]:.0f}~{int(b[1]) if b[1] < 1e9 else 999}%"

    def _slice_stats(sub: list, label: str, lo: float = 0, hi: float = 1e9) -> dict:
        n = len(sub)
        out = {"label": label, "lo": lo, "hi": hi, "n": n}
        for hz, key in ((1, "r1"), (3, "r3"), (7, "r7"), (14, "r14")):
            rets = [s[key] for s in sub if s[key] is not None]
            if len(rets) >= args.min_n:
                out[f"pdn{hz}"] = sum(1 for x in rets if x < 0) / len(rets)
                out[f"med{hz}"] = _med(rets)
            else:
                out[f"pdn{hz}"] = out[f"med{hz}"] = None
        pe = [s["peak"] for s in sub if s["peak"] is not None]
        tr = [s["trough"] for s in sub if s["trough"] is not None]
        pd_ = [s["peak_day"] for s in sub if s["peak_day"] is not None]
        nv = [s["first_neg_day"] for s in sub if s["first_neg_day"] is not None]
        out["med_peak"] = _med(pe)
        out["med_peak_day"] = _med(pd_)
        out["med_trough"] = _med(tr)
        out["never_neg"] = 1 - len(nv) / n if n else None
        out["p75_peak"] = _pct(pe, 0.75)
        out["p25_peak"] = _pct(pe, 0.25)
        out["p25_peak_day"] = _pct(pd_, 0.25)
        out["p75_peak_day"] = _pct(pd_, 0.75)
        out["peak_ge10"] = sum(1 for x in pe if x >= 0.10) / len(pe) if pe else None
        out["peak_ge30"] = sum(1 for x in pe if x >= 0.30) / len(pe) if pe else None
        out["med_rsi"] = _med([s["rsi14"] for s in sub if s["rsi14"] is not None])
        return out

    def _print_slice(rows: list[dict], title: str, cols: list[str],
                     col_head: list[str], raw_cols: tuple = ()) -> None:
        """打印分档表；cols 默认按百分比×100 显示，raw_cols 中的列原样显示（如天数）。"""
        print(f"\n=== {title} ===")
        print(f"{'分组':>12}" + "".join(f"{h:>10}" for h in col_head))
        print("-" * (12 + 10 * len(col_head)))
        for r in rows:
            line = f"{r['label']:>12}"
            for c in cols:
                v = r.get(c)
                if v is None:
                    line += f"{float('nan'):>10.1f}"
                elif c in raw_cols:
                    line += f"{v:>10.1f}"
                else:
                    line += f"{v * 100:>10.1f}"
            print(line)

    # ── ① 细涨幅桶主表 ──
    rows = []
    for b in HOT_BUCKETS:
        sub = [s for s in sigs if b[0] <= s["chg"] < b[1]]
        if len(sub) >= args.min_n:
            rows.append(_slice_stats(sub, _bucket_label(b), b[0], b[1]))
    _print_slice(rows, f"细涨幅桶（市值 ≤${args.mcap_max / 1e6:.0f}M，>50% 热榜；"
                        f"跌概率% / 中位收益%，窗口 1/3/7/14 日）",
                 ["pdn1", "pdn3", "pdn7", "pdn14", "med1", "med7", "med14"],
                 ["跌1d", "跌3d", "跌7d", "跌14d", "中位1d", "中位7d", "中位14d"])
    _print_slice(rows, "冲高空间（进榜后 14 日内还能冲多高 / 多久到顶 / 回撤多深）",
                 ["med_peak", "p75_peak", "med_peak_day", "med_trough", "never_neg"],
                 ["中位峰值%", "P75峰值%", "中位到顶日", "中位最低点%", "14日未跌率%"],
                 raw_cols=("med_peak_day",))

    # ── ② 收益分布（D+7 / D+14 分位数）──
    for hz, col_key in ((7, "r7"), (14, "r14")):
        print(f"\n=== 收益分布（D+{hz} 收益，P10/P25/P50/P75/P90 %）===")
        print(f"{'分组':>12}" + "".join(f"{h:>10}" for h in ["P10", "P25", "P50", "P75", "P90"]))
        print("-" * 62)
        for r in rows:
            sub = [s for s in sigs if r["lo"] <= s["chg"] < r["hi"]]
            rets = sorted(s[col_key] for s in sub if s[col_key] is not None)
            if len(rets) < args.min_n:
                continue
            q = [rets[min(len(rets) - 1, int(len(rets) * p))] for p in (0.10, 0.25, 0.50, 0.75, 0.90)]
            print(f"{r['label']:>12}" + "".join(f"{v * 100:>10.1f}" for v in q))

    # ── ③ 峰值分布细化（极值桶：入场后多久到顶 / 峰值多远 / 还能冲≥10%/30% 的比例）──
    _print_slice(rows, "峰值分布（P25/P50/P75 峰值 %、到顶日、还能冲 ≥10%/≥30% 的比例）",
                 ["p25_peak", "med_peak", "p75_peak", "p25_peak_day", "med_peak_day",
                  "p75_peak_day", "peak_ge10", "peak_ge30"],
                 ["P25峰值", "中位峰值", "P75峰值", "P25到顶日", "中位到顶日",
                  "P75到顶日", "峰值≥10%", "峰值≥30%"],
                 raw_cols=("p25_peak_day", "med_peak_day", "p75_peak_day"))

    # ── ④ 快照级 RSI(14) 分档（全覆盖；~8h 粒度）──
    rsi_bands = ((0, 60, "RSI<60"), (60, 70, "60~70"), (70, 80, "70~80"),
                 (80, 90, "80~90"), (90, 1000, "RSI>=90"))
    rsi_rows = []
    for lo, hi, lb in rsi_bands:
        sub = [s for s in sigs if s["rsi14"] is not None and lo <= s["rsi14"] < hi]
        if len(sub) >= args.min_n:
            st = _slice_stats(sub, lb)
            st["med_rsi"] = _med([s["rsi14"] for s in sub])
            rsi_rows.append(st)
    _print_slice(rsi_rows, "快照级 RSI(14) 分档（进榜时点动量过热程度 × 后续表现；"
                           "⚠️ 全覆盖但 ~8h 粒度，见 klines 子样本）",
                 ["med_rsi", "pdn1", "pdn7", "med1", "med7", "med_peak", "med_peak_day"],
                 ["中位RSI", "跌1d", "跌7d", "中位1d", "中位7d", "中位峰值", "中位到顶日"],
                 raw_cols=("med_rsi", "med_peak_day"))
    if rsi_rows:
        med_all = _med([s["rsi14"] for s in sigs if s["rsi14"] is not None])
        print(f"\n[hot] 全样本中位 RSI(14) = {med_all:.0f}（>50% 涨幅榜动量已普遍过热，"
              f"RSI 在样本内钝化，区分度弱）")

    # ── ⑤ 1h 急拉专项（含 40%+ 一根巨阳）──
    mo_rows = []
    for b in CHG1H_BANDS:
        sub = [s for s in sigs if b[0] <= s["chg1h"] < b[1]]
        if len(sub) >= args.min_n:
            st = _slice_stats(sub, f"1h涨{int(b[0])}~{int(b[1]) if b[1] < 1e3 else 999}%")
            st["med_peak_day"] = _med([s["peak_day"] for s in sub if s["peak_day"] is not None])
            mo_rows.append(st)
    _print_slice(mo_rows, "1h 急拉分档（当日拉升急缓 × 峰值/到顶；40%+ = 一根巨阳）",
                 ["pdn1", "pdn7", "med7", "med_peak", "med_peak_day", "med_trough"],
                 ["跌1d", "跌7d", "中位7d", "中位峰值", "中位到顶日", "中位最低点"],
                 raw_cols=("med_peak_day",))

    # ── ⑥ klines 真小时级子样本（仅 Binance 有 1h K 线的币；披露选择偏差）──
    _hot_klines_subsample(conn, sigs)

    # ── ⑦ 交叉特征 ──
    # 换手率（volume_24h / market_cap）
    to_rows = []
    for b in TURNOVER_BANDS:
        sub = [s for s in sigs if s["turnover"] is not None and b[0] <= s["turnover"] < b[1]]
        if len(sub) >= args.min_n:
            st = _slice_stats(sub, f"换手{int(b[0] * 100)}~{int(b[1] * 100) if b[1] < 1e6 else 999}%")
            to_rows.append(st)
    _print_slice(to_rows, "换手率分档（volume_24h / 市值）",
                 ["pdn1", "pdn7", "med7", "med_peak"],
                 ["跌1d", "跌7d", "中位7d", "中位峰值"])
    # 1h 动量
    mo_rows = []
    for b in CHG1H_BANDS:
        sub = [s for s in sigs if b[0] <= s["chg1h"] < b[1]]
        if len(sub) >= args.min_n:
            mo_rows.append(_slice_stats(sub, f"1h涨{int(b[0])}~{int(b[1]) if b[1] < 1e3 else 999}%"))
    _print_slice(mo_rows, "1h 动量分档（percent_change_1h，当日拉升急缓）",
                 ["pdn1", "pdn7", "med7", "med_peak"],
                 ["跌1d", "跌7d", "中位7d", "中位峰值"])

    # ── CSV ──
    out_path = Path(args.out) if args.out else \
        SCRIPT_DIR.parent / "data" / "backtest_smallcap_hot50.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label", "n", "pdn1", "pdn3", "pdn7", "pdn14",
                    "med1", "med3", "med7", "med14",
                    "med_peak", "p75_peak", "med_peak_day",
                    "med_trough", "never_neg"])
        for r in rows:
            w.writerow([r["label"], r["n"], r.get("pdn1"), r.get("pdn3"),
                        r.get("pdn7"), r.get("pdn14"), r.get("med1"), r.get("med3"),
                        r.get("med7"), r.get("med14"), r.get("med_peak"),
                        r.get("p75_peak"), r.get("med_peak_day"),
                        r.get("med_trough"), r.get("never_neg")])
    print(f"\n[hot] 结果已存 {out_path}")
    return 0


def run_sensitivity(conn, args) -> int:
    """敏感性对比：市值档 × 事件口径（全信号 vs 首日事件）网格。"""
    mcap_grid = (30e6, 50e6, 100e6)
    epi_grid = (0, 1)
    results: dict[tuple, tuple] = {}
    for mc in mcap_grid:
        for gap in epi_grid:
            results[(mc, gap)] = run_backtest(conn, args.universe_cap, mc,
                                              args.min_gain, gap, args.min_n)
            b, s = results[(mc, gap)]
            print(f"[sensitivity] 市值 ≤${int(mc // 1e6)}M × {'首日' if gap else '全信号'}"
                  f"：桶 {len(b)} / 档 {len(s)}")

    combos = [(mc, gap) for mc in mcap_grid for gap in epi_grid]
    col_head = [f"{int(mc // 1e6)}M{'首' if gap else '全'}" for mc, gap in combos]

    def _matrix(metric: str, title: str) -> None:
        print(f"\n=== {title}（分桶口径）===")
        print(f"{'涨幅桶':>10}" + "".join(f"{h:>10}" for h in col_head))
        print("-" * (10 + 10 * len(col_head)))
        labels = [fmt_bucket(r["label"]) for r in results[combos[0]][0]]
        for i, lb in enumerate(labels):
            line = f"{lb:>10}"
            for mc, gap in combos:
                b = results[(mc, gap)][0]
                v = b[i].get(metric) if i < len(b) else None
                line += f"{v * 100 if v is not None else float('nan'):>10.1f}"
            print(line)

    _matrix("p7", "7 日后下跌概率 %")
    _matrix("med7", "7 日中位收益 %")
    _matrix("p1", "1 日后下跌概率 %")

    print("\n=== 临界涨幅（首个 P(下跌) > 50% 的阈值档；n ≥ {}）===".format(args.min_n))
    print(f"{'口径':>12}{'1日后':>10}{'3日后':>10}{'7日后':>10}")
    print("-" * 42)
    for mc, gap in combos:
        s = results[(mc, gap)][1]
        label = f"{int(mc // 1e6)}M{'首日' if gap else '全信号'}"
        cells = []
        for hz in HORIZONS:
            crit = crit_threshold(s, hz)
            cells.append(f">={crit[0]}%" if crit else "无")
        print(f"{label:>12}{cells[0]:>10}{cells[1]:>10}{cells[2]:>10}")

    # CSV
    out_path = Path(args.out) if args.out else \
        SCRIPT_DIR.parent / "data" / "backtest_smallcap_gainers_sensitivity.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mcap_m", "episode", "section", "label", "n", "unique_coins",
                    "signal_days"] +
                   sum(([f"n_{hz}d", f"p_decline_{hz}d", f"median_{hz}d", f"mean_{hz}d"]
                        for hz in HORIZONS), []))
        for mc, gap in combos:
            b, s = results[(mc, gap)]
            for r in b + s:
                label = (fmt_bucket(r["label"]) if r["section"] == "bucket"
                         else f">={r['label']}%")
                row = [int(mc // 1e6), "first_day" if gap else "all",
                       r["section"], label, r["n"], r["coins"], r["days"]]
                for hz in HORIZONS:
                    row += [r.get(f"n{hz}"), r.get(f"p{hz}"),
                            r.get(f"med{hz}"), r.get(f"mean{hz}")]
                w.writerow(row)
    print(f"\n[sensitivity] 结果已存 {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="小市值涨幅榜冲高回落回测（服务端 SQL 聚合）")
    parser.add_argument("--mcap-max", type=float, default=MCAP_MAX, help="信号市值上限（USD）")
    parser.add_argument("--universe-cap", type=float, default=UNIVERSE_CAP,
                        help="价格加载带（USD），需 ≥ mcap-max 以覆盖后续价格")
    parser.add_argument("--min-gain", type=float, default=MIN_GAIN, help="信号最小 24h 涨幅（%）")
    parser.add_argument("--min-n", type=int, default=MIN_N, help="统计最少样本")
    parser.add_argument("--episode-gap", type=int, default=0,
                        help=">0 时同一币距上一信号 ≤N 天的重复上榜只保留首日（首日事件口径）")
    parser.add_argument("--sensitivity", action="store_true",
                        help="敏感性对比模式：市值 {30/50/100M} × 事件口径 {全信号/首日}")
    parser.add_argument("--hot", action="store_true",
                        help=f">{HOT_MIN_GAIN:.0f}% 涨幅榜专项：细桶 + 收益分布 + 冲高空间 + 换手/1h动量交叉")
    parser.add_argument("--out", type=str, default="", help="CSV 输出路径")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        if args.sensitivity:
            return run_sensitivity(conn, args)
        if args.hot:
            return run_hot_analysis(conn, args)
        bucket_rows, sweep_rows = run_backtest(
            conn, args.universe_cap, args.mcap_max, args.min_gain,
            args.episode_gap, args.min_n)

    print(f"[gainer-backtest] 小市值上限 ${args.mcap_max / 1e6:.0f}M，"
          f"信号涨幅下界 {args.min_gain}%，窗口 {HORIZONS} 日，判据 P(下跌)>{DECLINE_CRIT:.0%}"
          + ("，首日事件口径" if args.episode_gap > 0 else ""))
    print(f"[gainer-backtest] 服务端聚合完成，信号段 {len(bucket_rows)} 桶 / 阈值段 {len(sweep_rows)} 档")

    fields = [("label", "s", 10), ("n", "int", 6), ("coins", "int", 6), ("days", "int", 5)]
    for hz in HORIZONS:
        fields += [(f"p{hz}", "pct", 8), (f"med{hz}", "f", 9)]
    print_rows(
        [{**r, "label": fmt_bucket(r["label"])} for r in bucket_rows], fields,
        f"分桶表（下跌概率 / 中位收益%，分段口径；判据线 {DECLINE_CRIT:.0%}）",
    )
    print_rows(
        [{"label": f">={r['label']}%", **r} for r in sweep_rows],
        [("label", "s", 10), ("n", "int", 6), ("coins", "int", 6), ("days", "int", 5)]
        + sum(([(f"p{hz}", "pct", 8), (f"med{hz}", "f", 9)] for hz in HORIZONS), []),
        "阈值扫描（涨幅 ≥ thr 的累计样本；下跌概率 > 50% 即进入『大概率下跌』区）",
    )

    # 临界涨幅结论
    print("\n=== 临界涨幅（下跌概率首次 > 50%，且 n ≥ {}）===".format(args.min_n))
    for hz in HORIZONS:
        crit = crit_threshold(sweep_rows, hz)
        if crit is not None:
            thr, r = crit
            print(f"  {hz} 日后：24h 涨幅 ≥ {thr}% 时下跌概率 {r[f'p{hz}']:.1%}（n={r['n']}）"
                  f"→ 大概率下跌")
        else:
            print(f"  {hz} 日后：全部阈值档 P(下跌) ≤ 50%（样本不足或规律不成立）")
    print("\n[gainer-backtest] ⚠️ 快照仅 46 天、单一 regime，结论 provisional；"
          "同币连续上榜会重复计数（唯一币数见上表；--episode-gap 可做首日事件口径）")

    # CSV 输出
    if not args.out:
        args.out = str(SCRIPT_DIR.parent / "data" / "backtest_smallcap_gainers.csv")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "bucket_or_thr", "n", "unique_coins", "signal_days"] +
                   sum(([f"n_{hz}d", f"p_decline_{hz}d", f"median_{hz}d", f"mean_{hz}d"]
                        for hz in HORIZONS), []))
        for r in bucket_rows + sweep_rows:
            label = (fmt_bucket(r["label"]) if r["section"] == "bucket"
                     else f">={r['label']}%")
            row = [r["section"], label, r["n"], r["coins"], r["days"]]
            for hz in HORIZONS:
                row += [r.get(f"n{hz}"), r.get(f"p{hz}"), r.get(f"med{hz}"), r.get(f"mean{hz}")]
            w.writerow(row)
    print(f"\n[gainer-backtest] 结果已存 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
