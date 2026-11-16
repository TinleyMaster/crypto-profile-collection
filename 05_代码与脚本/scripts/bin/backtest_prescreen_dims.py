#!/usr/bin/env python3
"""预选池维度回测：信号时刻的 OI/CVD/主动买卖特征，是否能区分后续盈亏。

背景（2026-11-16）
-----------------------------------------------------------------
  已有结论：
    - 信号前 K 线形态对盈亏无区分力（Cliff's d 全 <0.02）
    - 「提前发现」= 提前买入 = 已证伪（前置信号 n=14,406，PF 0.83）
    - ⇒ 若要分层预筛，必须引入形态之外的【正交信息】：衍生品资金面
  本脚本回答：信号时刻可观测的 OI 变化 / CVD 占比 / 主动买卖失衡，
  对后续 24h 收益有没有区分力？有 → 建预选池；无 → 形态与衍生品都挑不出。

数据源
-----------------------------------------------------------------
  - 事件：data/trade_params_events.csv（A/B 档，与实盘扫描器同一缓存）
  - K 线：F:\\代码\\coindata\\coin-binance-swap-candle-csv-1h-2026-11-08（1h）
  - OI/CVD：PostgreSQL biz.oi_cvd_snapshot（5m 桶，WS 实时采集），只读
  - 5m K 线：F:\\代码\\coindata\\coin-binance-swap-candle-5m-csv-2026-11-08
          （含 taker_buy_quote_asset_volume → 主动买卖失衡）

口径（与 backtest_a_entry_timing.py / backtest_pre_signal.py 完全一致）
-----------------------------------------------------------------
  - 事件定义：A = chg24 ≥50%；B = 30%≤chg24<50% 且 vr(1h)≥5.6
  - 出场：simulate(arr, k, entry, 24h, CFG={"sl_pct":0.10}, sig) = EMA7 线破 + SL10% + 到期
  - 成本：COST=0.3%/笔
  - 入场：信号 bar 收盘立即入场（nl 口径，不做限价近似 → 干净对比分层差异）
  - 同币冷却：24h（与既有回测一致）
  - 无前视：特征只取【信号 bar 收盘时刻及之前】的 OI/CVD 序列

特征定义（全部在信号时刻可观测）
-----------------------------------------------------------------
  oi_chg_6h/12h/24h : OI 总价值变化率 = oi(t) / oi(t−Xh) − 1
  cvd_ratio_6h/24h   : CVD 占比 = ΣCVD_5m / Σ成交额（正=主动买净流入）
  taker_imb_6h/24h   : 主动买卖失衡 = (2×taker_buy − total) / total（来自 5m K 线）
  oi_cvd_div_24h     : 背离度 = oi_chg_24h − cvd_ratio_24h（价涨/OI缩/CVD负 → 空头回补虚涨）

用法：
  python bin/backtest_prescreen_dims.py            # 全量
  python bin/backtest_prescreen_dims.py --limit 60 # 小样本自测
"""
from __future__ import annotations

import argparse
import time
import csv
import io
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from backtest_exit_variants import simulate  # noqa: E402
from backtest_pre_signal import load_ab, rich, line  # noqa: E402

DATA_1H = Path(r"F:\代码\coindata\coin-binance-swap-candle-csv-1h-2026-11-08")
DATA_5M = Path(r"F:\代码\coindata\coin-binance-swap-candle-5m-csv-2026-11-08")

CFG = {"sl_pct": 0.10}
N_H = 24
COOL = 24 * 3600
COST = 0.003
HOUR = 3600
BUCKET = 300                      # oi_cvd_snapshot 桶 = 5min

# 窗口（小时）
WINDOWS = (6, 12, 24)


# ----------------------------------------------------------------- 5m K 线读取
# 1h 文件首行是 GBK 中文提示行（"数据导出..."），第 2 行才是表头；
# 5m 文件首行即表头（带 BOM）。两者列集合不同，分别处理。
CSV_COLS_5M = ["candle_begin_time", "open", "high", "low", "close", "volume",
               "quote_volume", "trade_num", "taker_buy_base_asset_volume",
               "taker_buy_quote_asset_volume", "symbol"]
CSV_COLS_1H = ["candle_begin_time", "open", "high", "low", "close", "volume",
               "quote_volume", "trade_num", "taker_buy_base_asset_volume",
               "taker_buy_quote_asset_volume", "Spread", "symbol",
               "avg_price_1m", "avg_price_5m", "fundingRate"]


def _read_csv_fast(path: Path, cols: list[str], skiprows: int,
                   encodings: tuple[str, ...] = ("utf-8-sig", "gb18030")) -> dict | None:
    """向量化读 CSV。pandas 比纯 csv 模块快 ~50x（5m 全量 18GB，纯 Python 不可行）。

    编码：5m = UTF-8(BOM)；1h = GBK（首行是中文提示行「本数据供邢不行…」，第 2 行才是表头）。
    """
    if not path.exists():
        return None
    import pandas as pd
    last_err = None
    for enc in encodings:
        try:
            df = pd.read_csv(path, usecols=cols, skiprows=skiprows, encoding=enc,
                             header=None, names=cols, low_memory=False)
            if df.empty:
                continue
            t = (pd.to_datetime(df["candle_begin_time"], errors="coerce",
                                format="%Y-%m-%d %H:%M:%S")
                 .astype("int64").to_numpy() // 10 ** 9)
            out = {"t": t}
            for c in ("open", "high", "low", "close"):
                out[c[0]] = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=float)
            out["q"] = pd.to_numeric(df["quote_volume"], errors="coerce").to_numpy(dtype=float)
            out["tb"] = pd.to_numeric(df["taker_buy_quote_asset_volume"],
                                      errors="coerce").to_numpy(dtype=float)
            keep = np.isfinite(out["t"]) & (out["c"] > 0) & np.isfinite(out["c"])
            if keep.sum() < 10:
                continue
            return {k: v[keep] for k, v in out.items()}
        except Exception as e:
            last_err = e
            continue
    if last_err:
        pass  # 静默：调用方按 None 处理
    return None


def read_5m(path: Path) -> dict | None:
    """读 5m CSV → t/o/h/l/c/quote_vol/taker_buy_quote（主动买额）。"""
    d = _read_csv_fast(path, CSV_COLS_5M, skiprows=1)
    if d is None:
        return None
    return {"t": d["t"], "o": d["o"], "h": d["h"], "l": d["l"], "c": d["c"],
            "q": d["q"], "tb": d["tb"]}


def read_1h(path: Path) -> dict | None:
    """读 1h CSV → t/o/h/l/c（跳过首行中文提示行）。"""
    d = _read_csv_fast(path, CSV_COLS_1H, skiprows=1)
    if d is None:
        return None
    return {"t": d["t"], "o": d["o"], "h": d["h"], "l": d["l"], "c": d["c"]}


# ----------------------------------------------------------------- OI/CVD 特征
OI_CACHE_DIR = SCRIPT_DIR.parent / "data" / "oi_cache"


def _load_cache(name: str, cols: list[str]) -> dict[str, dict] | None:
    """从本地导出的 CSV 读 OI/CVD 序列（避免回测过程中反复打 DB）。

    背景：DB 直连在当前网络下不稳定（oi_cvd_snapshot 330 万行，
    单次聚合查询可达 60s+），改为「后台导出一次 → 本地读 CSV 回测」。
    """
    path = OI_CACHE_DIR / f"{name}.csv"
    if not path.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_csv(path)
        if df.empty:
            return None
        out: dict[str, dict] = {}
        for s, g in df.groupby("symbol", sort=False):
            g = g.sort_values("ts")
            out[s] = {
                "ts": g["ts"].to_numpy(dtype=float),
                "oi": g[cols[0]].to_numpy(dtype=float),
                "cvd": g[cols[1]].to_numpy(dtype=float),
                "vol": g[cols[2]].to_numpy(dtype=float),
            }
        print(f"[data] 本地 OI 缓存 {name}: {len(out):,} 币 / {len(df):,} 行", flush=True)
        return out
    except Exception as e:
        print(f"[warn] 读 {name} 缓存失败：{type(e).__name__}: {str(e)[:80]}")
        return None


def bulk_fetch_oi_cvd(syms: list[str], t_lo: int, t_hi: int,
                      allow_db: bool = True) -> dict[str, dict]:
    """加载 OI/CVD 序列（本地缓存优先，缺失时回退 DB）。"""
    cached = _load_cache("oi_cvd_snapshot", ["oi_usd", "cvd_5m_usd", "vol_5m_usd"])
    if cached:
        return {s: cached[s] for s in syms if s in cached}
    if not allow_db:
        print("[data] 跳过 DB（--no-db）：OI 维度将缺失，仅用 5m 派生特征")
        return {}
    return _bulk_fetch_oi_cvd_db(syms, t_lo, t_hi)


def fetch_oi_features_cg(events: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """用多所聚合 OI（biz.cg_oi_hist）算特征 —— 长窗口验证源。

    为什么需要：`biz.oi_cvd_snapshot` 只有 2026-08 起（68 天窗口），三段检验
    切成同一段行情，不构成跨环境泛化验证。cg_oi_hist 的 12h 粒度覆盖
    2025-10-11 ~ 2026-10-07（约 360 天），可把验证窗口拉长约 5 倍。

    口径差异（必须知悉）：
      - oi_cvd_snapshot = Binance 单所 OI × markPrice，5m 桶
      - cg_oi_hist     = 多所聚合 OI（CoinGlass），12h 桶
      → 数值不可直接对比，本脚本只做「各自口径内的分层检验」，比较的是
        「同一源下不同档位的相对差异」，跨源只比方向/显著性，不比绝对值。

    无前视：所有 CASE 条件限定 cg.ts <= ev.t_sig。
    """
    out: dict[tuple[str, int], dict] = {}
    if not events:
        return out
    syms = sorted({s for s, _ in events})
    all_syms = [str(s) for s, _ in events]
    all_ts = [int(t) for _, t in events]
    # 12h 粒度：24h = 2 根，48h = 4 根，72h = 6 根
    SQL = """
    WITH ev AS (
      SELECT * FROM unnest(%s::text[], %s::bigint[]) AS t(symbol, t_sig)
    ), oi AS (
      SELECT symbol, EXTRACT(EPOCH FROM ts) AS ts, oi_close
      FROM biz.cg_oi_hist
      WHERE interval = '12h' AND symbol = ANY(%s) AND ts >= to_timestamp(%s)
    ), j AS (
      SELECT ev.symbol, ev.t_sig, oi.ts, oi.oi_close
      FROM ev LEFT JOIN oi ON oi.symbol = ev.symbol
        AND oi.ts >= ev.t_sig - 259200 AND oi.ts <= ev.t_sig
    )
    SELECT j.symbol, j.t_sig,
           count(j.ts) AS n_b,
           (array_agg(j.oi_close ORDER BY abs(j.ts - (j.t_sig - 86400))
                       ) FILTER (WHERE j.ts <= j.t_sig - 86400))[1] AS oi_24h_ago,
           (array_agg(j.oi_close ORDER BY abs(j.ts - (j.t_sig - 172800))
                       ) FILTER (WHERE j.ts <= j.t_sig - 172800))[1] AS oi_48h_ago,
           (array_agg(j.oi_close ORDER BY abs(j.ts - (j.t_sig - 259200))
                       ) FILTER (WHERE j.ts <= j.t_sig - 259200))[1] AS oi_72h_ago,
           (array_agg(j.oi_close ORDER BY j.ts DESC))[1] AS oi_now
    FROM j GROUP BY j.symbol, j.t_sig
    """
    try:
        import psycopg
        from crypto_research.config import get_settings
        settings = get_settings(require_database=True)
        with psycopg.connect(settings.database_url, connect_timeout=20) as conn:
            conn.execute("SET default_transaction_read_only=on")
            conn.execute("SET statement_timeout='300s'")
            with conn.cursor() as cur:
                cur.execute(SQL, (all_syms, all_ts, syms, 1759276800.0))
                rows = cur.fetchall()
    except Exception as e:
        import traceback
        print(f"[warn] cg OI 特征聚合失败：{type(e).__name__}: {str(e)[:200]}")
        traceback.print_exc()
        return out

    for r in rows:
        sym, t_sig, n_b = r[0], int(r[1]), r[2]
        f: dict = {"n_buckets": int(n_b or 0)}
        now, ago24, ago48, ago72 = r[6], r[3], r[4], r[5]
        if now is not None and ago24 is not None and float(ago24) > 0:
            f["oi_chg_24h"] = float(now) / float(ago24) - 1
        if now is not None and ago48 is not None and float(ago48) > 0:
            f["oi_chg_48h"] = float(now) / float(ago48) - 1
        if now is not None and ago72 is not None and float(ago72) > 0:
            f["oi_chg_72h"] = float(now) / float(ago72) - 1
        out[(sym, t_sig)] = f
    print(f"[data] CG 多所 OI 特征（SQL 聚合）：{len(out):,} 个事件点，含 oi_chg_24h 的 "
          f"{sum(1 for v in out.values() if 'oi_chg_24h' in v):,}", flush=True)
    return out


def fetch_oi_features_sql(events: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """SQL 侧聚合：一次性算出每个 (symbol, 信号时刻) 的 OI/CVD 特征。

    为什么在 SQL 里算：单符号全量拉 16s（网络传输瓶颈），439 币需数小时；
    改成 SQL 内做窗口聚合，只回传 5,272 行特征 → 实测 24s。
    无前视：所有 CASE 条件都限定 oi.ts <= ev.t_sig。
    """
    out: dict[tuple[str, int], dict] = {}
    if not events:
        return out
    syms = sorted({s for s, _ in events})
    all_syms = [str(s) for s, _ in events]
    all_ts = [int(t) for _, t in events]
    SQL = """
    WITH ev AS (
      SELECT * FROM unnest(%s::text[], %s::bigint[]) AS t(symbol, t_sig)
    ), oi AS (
      SELECT symbol, EXTRACT(EPOCH FROM ts) AS ts, oi_usd, cvd_5m_usd, vol_5m_usd
      FROM biz.oi_cvd_snapshot
      WHERE symbol = ANY(%s) AND ts >= to_timestamp(%s)
    ), j AS (
      SELECT ev.symbol, ev.t_sig, oi.ts, oi.oi_usd, oi.cvd_5m_usd, oi.vol_5m_usd
      FROM ev LEFT JOIN oi ON oi.symbol = ev.symbol
        AND oi.ts >= ev.t_sig - 86400 AND oi.ts <= ev.t_sig
    )
    SELECT j.symbol, j.t_sig,
           count(j.ts) AS n_b,
           -- 窗口起点的桶（≤ 边界 且 最接近边界）：用 array_agg 按 |ts−边界| 排序
           (array_agg(j.oi_usd ORDER BY abs(j.ts - (j.t_sig - 21600))
                       ) FILTER (WHERE j.ts <= j.t_sig - 21600))[1] AS oi_6h_ago,
           (array_agg(j.oi_usd ORDER BY abs(j.ts - (j.t_sig - 43200))
                       ) FILTER (WHERE j.ts <= j.t_sig - 43200))[1] AS oi_12h_ago,
           (array_agg(j.oi_usd ORDER BY abs(j.ts - (j.t_sig - 86400))
                       ) FILTER (WHERE j.ts <= j.t_sig - 86400))[1] AS oi_24h_ago,
           -- 近端：窗口内最后一个桶（ts 最大）
           (array_agg(j.oi_usd ORDER BY j.ts DESC))[1] AS oi_now,
           (array_agg(j.oi_usd ORDER BY j.ts DESC) FILTER (WHERE j.ts > j.t_sig - 21600))[1] AS oi_6h,
           (array_agg(j.oi_usd ORDER BY j.ts DESC) FILTER (WHERE j.ts > j.t_sig - 43200))[1] AS oi_12h,
           sum(CASE WHEN j.ts > j.t_sig - 21600 THEN j.cvd_5m_usd END) AS cvd_6h,
           sum(CASE WHEN j.ts > j.t_sig - 21600 THEN j.vol_5m_usd END) AS vol_6h,
           sum(CASE WHEN j.ts > j.t_sig - 86400 THEN j.cvd_5m_usd END) AS cvd_24h,
           sum(CASE WHEN j.ts > j.t_sig - 86400 THEN j.vol_5m_usd END) AS vol_24h
    FROM j GROUP BY j.symbol, j.t_sig
    """
    try:
        import psycopg
        from crypto_research.config import get_settings
        settings = get_settings(require_database=True)
        with psycopg.connect(settings.database_url, connect_timeout=20) as conn:
            conn.execute("SET default_transaction_read_only=on")
            conn.execute("SET statement_timeout='300s'")
            with conn.cursor() as cur:
                cur.execute(SQL, (all_syms, all_ts, syms, 1759276800.0))
                rows = cur.fetchall()
    except Exception as e:
        import traceback
        print(f"[warn] OI 特征 SQL 聚合失败：{type(e).__name__}: {str(e)[:200]}")
        traceback.print_exc()
        return out

    for r in rows:
        sym, t_sig, n_b = r[0], int(r[1]), r[2]
        f: dict = {"n_buckets": int(n_b or 0)}
        # 列序：symbol, t_sig, n_b, oi_6h_ago, oi_12h_ago, oi_24h_ago,
        #        oi_now, oi_6h, oi_12h, cvd_6h, vol_6h, cvd_24h, vol_24h
        oi_6h_ago, oi_12h_ago, oi_24h_ago = r[3], r[4], r[5]
        oi_now, oi_6h, oi_12h = r[6], r[7], r[8]
        cvd_6h, vol_6h, cvd_24h, vol_24h = r[9], r[10], r[11], r[12]
        # OI 变化率：近端值 / 远端值 − 1
        if oi_now is not None and oi_6h_ago is not None and float(oi_6h_ago) > 0:
            f["oi_chg_6h"] = float(oi_now) / float(oi_6h_ago) - 1
        if oi_now is not None and oi_12h_ago is not None and float(oi_12h_ago) > 0:
            f["oi_chg_12h"] = float(oi_now) / float(oi_12h_ago) - 1
        if oi_now is not None and oi_24h_ago is not None and float(oi_24h_ago) > 0:
            f["oi_chg_24h"] = float(oi_now) / float(oi_24h_ago) - 1
        # CVD 占比
        if cvd_6h is not None and vol_6h is not None and float(vol_6h) > 0:
            f["cvd_ratio_6h"] = float(cvd_6h) / float(vol_6h)
        if cvd_24h is not None and vol_24h is not None and float(vol_24h) > 0:
            f["cvd_ratio_24h"] = float(cvd_24h) / float(vol_24h)
        if "oi_chg_24h" in f and "cvd_ratio_24h" in f:
            f["oi_cvd_div_24h"] = f["oi_chg_24h"] - f["cvd_ratio_24h"]
        out[(sym, t_sig)] = f
    print(f"[data] OI 特征（SQL 聚合）：{len(out):,} 个事件点，其中含 OI 变化的 "
          f"{sum(1 for v in out.values() if 'oi_chg_24h' in v):,}", flush=True)
    return out


def _bulk_fetch_oi_cvd_db(syms: list[str], t_lo: int, t_hi: int) -> dict[str, dict]:
    """DB 回退路径：逐事件拉桶（慢，仅在缓存缺失时用）。

    ⚠ 单符号全量查询约 16s（网络传输是瓶颈，非 DB 计算），439 币需数小时。
    首选 _fetch_oi_features_sql()（SQL 侧聚合，24s 拿全量）。
    """
    out: dict[str, dict] = {}
    if not syms:
        return out
    OI_DATA_LO = 1787000000      # oi_cvd_snapshot 约 2026-08 起
    t_lo = max(t_lo, OI_DATA_LO)
    if t_hi <= t_lo:
        print("[warn] 事件区间与 OI 数据无交集")
        return out
    print(f"[data] DB 拉取窗口 "
          f"{datetime.fromtimestamp(t_lo, timezone.utc):%Y-%m-%d} ~ "
          f"{datetime.fromtimestamp(t_hi, timezone.utc):%Y-%m-%d}", flush=True)
    try:
        import psycopg
        from crypto_research.config import get_settings
        settings = get_settings(require_database=True)
        with psycopg.connect(settings.database_url, connect_timeout=20) as conn:
            conn.execute("SET default_transaction_read_only=on")
            conn.execute("SET statement_timeout='600s'")
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT symbol, EXTRACT(EPOCH FROM ts), oi_usd, cvd_5m_usd, vol_5m_usd
                    FROM biz.oi_cvd_snapshot
                    WHERE symbol = ANY(%s)
                      AND ts >= to_timestamp(%s) AND ts <= to_timestamp(%s)
                """, (syms, float(t_lo), float(t_hi)))
                rows = cur.fetchall()
    except Exception as e:
        print(f"[warn] OI 批量拉取失败：{type(e).__name__}: {str(e)[:120]}")
        return out

    print(f"[data] OI/CVD 批量拉取：{len(rows):,} 行", flush=True)

    by_sym: dict[str, list] = defaultdict(list)
    for s, ts, oi, cvd, vol in rows:
        by_sym[s].append((float(ts), oi, cvd, vol))
    print(f"[data] 覆盖 {len(by_sym):,}/{len(syms):,} 币", flush=True)

    for s, lst in by_sym.items():
        out[s] = {
            "ts": np.array([x[0] for x in lst]),
            "oi": np.array([float(x[1]) if x[1] is not None else np.nan for x in lst]),
            "cvd": np.array([float(x[2]) if x[2] is not None else np.nan for x in lst]),
            "vol": np.array([float(x[3]) if x[3] is not None else np.nan for x in lst]),
        }
    return out


def oi_features(d: dict | None, t_signal: int, lookback_h: int = 30) -> dict | None:
    """从预取的 OI/CVD 序列算特征（无前视：只取 ≤ t_signal 的桶）。"""
    if d is None:
        return None
    ts, oi, cvd, vol = d["ts"], d["oi"], d["cvd"], d["vol"]
    m = (ts >= t_signal - lookback_h * HOUR) & (ts <= t_signal)
    if m.sum() < 12:
        return None
    ts_w, oi_w, cvd_w, vol_w = ts[m], oi[m], cvd[m], vol[m]
    ok = np.isfinite(oi_w) & (oi_w > 0)
    if ok.sum() < 6:
        return None
    out = {"n_buckets": int(m.sum())}
    for w in WINDOWS:
        cut = t_signal - w * HOUR
        mw = ts_w >= cut
        if mw.sum() < 3:
            continue
        oi_x = oi_w[mw]
        oi_x = oi_x[np.isfinite(oi_x) & (oi_x > 0)]
        if len(oi_x) < 2:
            continue
        out[f"oi_chg_{w}h"] = float(oi_x[-1] / oi_x[0] - 1)
        tot_vol = float(np.nansum(vol_w[mw]))
        out[f"cvd_ratio_{w}h"] = (float(np.nansum(cvd_w[mw]) / tot_vol)
                                  if tot_vol > 0 else 0.0)
    if "oi_chg_24h" in out and "cvd_ratio_24h" in out:
        out["oi_cvd_div_24h"] = out["oi_chg_24h"] - out["cvd_ratio_24h"]
    return out


def taker_imb_5m(arr5: dict | None, t_signal: int, w: int) -> float | None:
    """5m K 线主动买卖失衡 = (2×taker_buy − total)/total，窗口 w 小时。

    完全离线（K 线自带 taker_buy_quote_asset_volume），不依赖 DB。
    正 = 主动买盘占优；负 = 主动卖盘占优。
    """
    if arr5 is None:
        return None
    t = arr5["t"]
    m = (t > t_signal - w * HOUR) & (t <= t_signal)
    if m.sum() < 6:
        return None
    q = arr5["q"][m]
    tb = arr5["tb"][m]
    tot = float(q.sum())
    if tot <= 0:
        return None
    return float((2.0 * tb.sum() - tot) / tot)


def cvd_proxy_5m(arr5: dict | None, t_signal: int, w: int) -> float | None:
    """5m K 线 CVD 代理 = 主动买额 − 主动卖额，再除以总成交额。

    = 主动买卖失衡的另一种表达（等价于 taker_imb/2），此处保留独立命名，
    便于与 DB 侧 oi_cvd_snapshot 的 cvd_ratio 对照。
    """
    imb = taker_imb_5m(arr5, t_signal, w)
    return None if imb is None else imb / 2.0


# ----------------------------------------------------------------- 主流程
def seg3_mean(sub: list[dict]) -> list[float]:
    """按事件时间等分三段的均净（无后视一致性检验）。"""
    srt = sorted(sub, key=lambda r: r["eo"])
    kk = max(1, len(srt) // 3)
    return [float(np.mean([r["ret"] - COST for r in sg]))
            for sg in (srt[:kk], srt[kk:2 * kk], srt[2 * kk:]) if sg]


def seg3_detail(sub: list[dict]) -> list[dict]:
    """三段明细：n / 时间起止 / 跨度天数 / 均净 —— 必看跨度，否则会误读强度。

    ⚠ 关键陷阱：若样本整体只覆盖一个短窗口，三段是「同一段行情切三刀」，
    不构成跨环境泛化验证。跨度天数必须与结论一起陈述。
    """
    srt = sorted(sub, key=lambda r: r["eo"])
    kk = max(1, len(srt) // 3)
    out = []
    for i, sg in enumerate([srt[:kk], srt[kk:2 * kk], srt[2 * kk:]], 1):
        if not sg:
            continue
        span = (int(sg[-1]["eo"]) - int(sg[0]["eo"])) / 86400.0
        net = np.array([r["ret"] - COST for r in sg])
        out.append({
            "seg": i, "n": len(sg),
            "from": datetime.fromtimestamp(int(sg[0]["eo"]), timezone.utc).strftime("%Y-%m-%d"),
            "to": datetime.fromtimestamp(int(sg[-1]["eo"]), timezone.utc).strftime("%Y-%m-%d"),
            "span_days": span, "mean": float(net.mean()),
            "win": float((net > 0).mean()),
        })
    return out


def print_seg3(sub: list[dict], tag: str) -> None:
    """打印三段明细 + 强度警示。"""
    det = seg3_detail(sub)
    if len(det) < 3:
        print(f"    {tag}: 样本不足（{len(det)} 段）")
        return
    means = [d["mean"] for d in det]
    same = "✅ 三段同号" if (all(m > 0 for m in means) or all(m < 0 for m in means)) \
        else "⚠ 三段不同号"
    spans = [d["span_days"] for d in det]
    tot_span = (int(max(sub, key=lambda r: r["eo"])["eo"])
                - int(min(sub, key=lambda r: r["eo"])["eo"])) / 86400.0
    for d in det:
        print(f"    第{d['seg']}段 n={d['n']:4d}  {d['from']}~{d['to']}  "
              f"跨度{d['span_days']:6.1f}天  均净{d['mean']:+7.2f}%  胜率{d['win']*100:5.1f}%")
    warn = ""
    if tot_span < 120:
        warn = (f"  ⚠⚠ 整体窗口仅 {tot_span:.0f} 天（每段 {min(spans):.0f}~{max(spans):.0f} 天）"
                f" → 三段是同一段行情切三刀，不构成跨环境泛化验证")
    print(f"    → {same}   整体窗口 {tot_span:.0f} 天{warn}")


def tstat(vals: np.ndarray) -> float:
    if len(vals) < 3 or vals.std(ddof=1) == 0:
        return 0.0
    return float(vals.mean() / (vals.std(ddof=1) / np.sqrt(len(vals))))


def limit_entry_price_5m(arr5: dict | None, entry: float, t_sig: int,
                         discount: float = 0.005, wait_h: int = 1) -> tuple[float, int] | None:
    """实盘限价入场判定：信号后 wait_h 小时内回撤 discount → 按限价成交。

    与实盘执行器一致：挂 信号价×(1−0.5%) GTC 限价单等回撤，60min 未成交 → 放弃。
    成交价取限价（保守；跳空穿越按更差的开盘价处理见调用方）。
    用 5m K 线精确判定（实盘同粒度）。
    返回 (成交价, 成交时刻 ts) 或 None（放弃）。
    """
    if arr5 is None:
        return None
    t = arr5["t"]
    lo = np.searchsorted(t, t_sig, side="left")
    hi = np.searchsorted(t, t_sig + wait_h * HOUR, side="left")
    if hi <= lo:
        return None
    lim_px = entry * (1 - discount)
    seg_l = arr5["l"][lo:hi]
    hit = np.nonzero(seg_l <= lim_px)[0]
    if not len(hit):
        return None
    k = int(hit[0])
    # 跳空：bar 开盘已低于限价 → 按开盘成交（更差价，realistic）
    px = float(arr5["o"][lo + k]) if arr5["o"][lo + k] <= lim_px else lim_px
    return px, int(t[lo + k])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="限制币数（自测用）")
    ap.add_argument("--no-db", action="store_true", help="不连 DB（只用 5m 派生特征）")
    ap.add_argument("--no-5m", action="store_true", help="跳过 5m 读取（只用 DB OI 特征）")
    ap.add_argument("--entry", choices=["nl", "limit"], default="nl",
                    help="入场口径：nl=信号收盘立即入场 / limit=实盘限价-0.5%+60min放弃")
    ap.add_argument("--limit-discount", type=float, default=0.005,
                    help="限价深度（默认 0.5%，与实盘一致）")
    ap.add_argument("--limit-wait-h", type=int, default=1,
                    help="限价等待小时（默认 1h，与实盘一致）")
    ap.add_argument("--oi-source", choices=["binance", "cg"], default="binance",
                    help="OI 特征源：binance=oi_cvd_snapshot(5m,2026-08起) / "
                         "cg=cg_oi_hist(12h,2025-10起，长窗口验证)")
    args = ap.parse_args()

    ab = load_ab()
    files = sorted(DATA_1H.glob("*.csv"))
    if args.limit:
        files = files[:args.limit]
    print(f"[data] 1h 文件 {len(files):,}；A/B 事件币 {len(ab):,}")
    print("[data] OI/CVD 源 = biz.oi_cvd_snapshot（DB 只读，批量拉取）")
    print("[data] 主动买卖源 = 5m K 线 taker_buy_quote_asset_volume\n")

    # 预取 OI 特征：SQL 侧聚合（24s 拿全量 5,272 事件点）
    oi_feats: dict[tuple[str, int], dict] = {}
    if not args.no_db:
        ev_pairs = []
        for path in files:
            sym = path.stem.replace("-", "")
            for e in ab.get(sym, []):
                if e["sig"] in ("A", "B"):
                    ev_pairs.append((sym, e["eo"] + HOUR))   # 事件 bar 收盘时刻
        if args.oi_source == "cg":
            oi_feats = fetch_oi_features_cg(ev_pairs)
        else:
            oi_feats = fetch_oi_features_sql(ev_pairs)

    rows: list[dict] = []
    n_nool = 0
    n_nofill = 0
    n_files = 0
    t_start = time.time()
    for fi, path in enumerate(files):
        sym = path.stem.replace("-", "")
        evs = [e for e in ab.get(sym, []) if e["sig"] in ("A", "B")]
        if not evs:
            continue
        arr = read_1h(path)
        if arr is None:
            continue
        t = arr["t"]
        if len(t) < 60:
            continue
        n_files += 1
        # 5m 用于主动买卖失衡（懒加载）
        arr5 = None
        if not args.no_5m:
            p5 = DATA_5M / f"{path.stem}.csv"
            arr5 = read_5m(p5) if p5.exists() else None

        cand = []
        for ev in evs:
            k = int(np.searchsorted(t, ev["eo"], side="left"))
            if k >= len(t) or t[k] != ev["eo"]:
                continue
            cand.append((k, ev))
        keep, last = [], None
        for k, ev in sorted(cand):
            if last is None or t[k] - last >= COOL:
                keep.append((k, ev))
                last = t[k]
        if not keep:
            continue

        for k, ev in keep:
            t_sig = int(t[k]) + HOUR          # 事件 bar 收盘时刻
            feats = dict(oi_feats.get((sym, t_sig), {}))
            # 5m 派生的主动买卖特征（离线可得，全历史覆盖）
            ti6 = taker_imb_5m(arr5, t_sig, 6)
            ti24 = taker_imb_5m(arr5, t_sig, 24)
            if ti6 is not None:
                feats["taker_imb_6h"] = ti6
            if ti24 is not None:
                feats["taker_imb_24h"] = ti24
            # 无 OI 数据时，用 K 线派生的 CVD 代理补齐 cvd_ratio 维度
            if "cvd_ratio_24h" not in feats and ti24 is not None:
                feats["cvd_ratio_24h"] = ti24 / 2.0
            if "cvd_ratio_6h" not in feats and ti6 is not None:
                feats["cvd_ratio_6h"] = ti6 / 2.0
            if not feats:
                n_nool += 1
                continue

            # ── 入场：nl（立即入场）或 lim（实盘限价 −0.5% / 60min 未成交放弃）──
            if args.entry == "limit":
                got = limit_entry_price_5m(arr5, ev["entry"], t_sig,
                                            discount=args.limit_discount,
                                            wait_h=args.limit_wait_h)
                if got is None:
                    n_nofill += 1
                    continue
                entry_px, entry_ts = got
                # 出场用实盘口径 simulate，但入场价换成限价成交价
                # （simulate 内部以 i_eo 定位 K 线，此处仍用事件 bar 索引 k，
                #   因为出场规则基于信号 bar 起的 EMA7 线与 SL，首小时保护一致）
                nl = simulate(arr, k, entry_px, N_H, CFG, ev["sig"])
                if nl is None:
                    continue
                ret, reason, mfe = nl[0], nl[1], nl[3]
                # 配对改善：限价成交价 vs 信号价
                entry_gap_bp = (entry_px / ev["entry"] - 1) * 1e4
            else:
                nl = simulate(arr, k, ev["entry"], N_H, CFG, ev["sig"])
                if nl is None:
                    continue
                ret, reason, mfe = nl[0], nl[1], nl[3]
                entry_px, entry_ts = ev["entry"], t_sig
                entry_gap_bp = 0.0

            rec = {"sym": sym, "eo": int(t[k]), "sig": ev["sig"], "chg24": ev["chg24"],
                   "ret": ret, "reason": reason, "mfe": mfe,
                   "entry_px": entry_px, "entry_ts": entry_ts,
                   "entry_gap_bp": entry_gap_bp,
                   "dt": datetime.fromtimestamp(t_sig, timezone.utc), **feats}
            if ti6 is not None:
                rec["taker_imb_6h"] = ti6
            if ti24 is not None:
                rec["taker_imb_24h"] = ti24
            rows.append(rec)

        if (fi + 1) % 50 == 0:
            print(f"  .. {fi+1}/{len(files)} 币  样本 {len(rows):,}  "
                  f"用时 {time.time()-t_start:.0f}s", flush=True)

    print(f"[data] 有效样本（带 OI 特征）{len(rows):,}；无 OI 数据跳过 {n_nool:,}")
    if args.entry == "limit":
        print(f"[data] 限价未成交放弃：{n_nofill:,}"
              f"（成交率 {100*len(rows)/max(1,len(rows)+n_nofill):.1f}%）")
    print(f"[data] 实际处理币数 {n_files:,}（有 1h+5m 文件且含 A/B 事件）")
    if len(rows) < 50:
        print("[warn] 样本不足")
        return 1


    base = rich(rows)
    print()
    print("=" * 132)
    print("Q0 基准（全样本，nl 入场 / EMA7+SL10%+24h / 成本0.3%）")
    print("=" * 132)
    print(line("全样本", base))

    if args.entry == "limit":
        gaps = np.array([r["entry_gap_bp"] for r in rows if "entry_gap_bp" in r])
        if len(gaps):
            print(f"\n  限价配对改善：入场价差 中位 {np.median(gaps):+.1f}bp "
                  f"均值 {gaps.mean():+.1f}bp（vs 信号价）")

    # ---- 样本期分层：OI 数据仅 2026-08 起 → 早期事件无 OI 特征 ----
    OI_START = 1787000000      # oi_cvd_snapshot 约 2026-08 起
    late = [r for r in rows if r["eo"] >= OI_START]
    early = [r for r in rows if r["eo"] < OI_START]
    print()
    print("=" * 132)
    print("Q0b 样本期分层（OI/CVD 数据仅 2026-08 起 → 早期事件无 OI 特征）")
    print("=" * 132)
    print(f"  早期（<2026-08，无 OI） n={len(early):5d}  "
          + (line("", rich(early), 2).strip() if len(early) >= 20 else "样本不足"))
    print(f"  近期（≥2026-08，有 OI） n={len(late):5d}  "
          + (line("", rich(late), 2).strip() if len(late) >= 20 else "样本不足"))
    n_oi_feat = sum(1 for r in rows if "oi_chg_24h" in r)
    print(f"  含 OI 变化率特征的事件：{n_oi_feat:,} / {len(rows):,}"
          f"（占 {100*n_oi_feat/max(1,len(rows)):.1f}%）")

    net = np.array([r["ret"] - COST for r in rows])
    print(f"\n  特征覆盖：")
    if args.oi_source == "cg":
        OI_KEYS = ["oi_chg_24h", "oi_chg_48h", "oi_chg_72h"]
    else:
        OI_KEYS = ["oi_chg_6h", "oi_chg_12h", "oi_chg_24h"]
    for key in OI_KEYS + ["cvd_ratio_6h", "cvd_ratio_24h",
                          "oi_cvd_div_24h", "taker_imb_6h", "taker_imb_24h"]:
        vals = [r[key] for r in rows if key in r]
        if vals:
            v = np.array(vals, dtype=float)
            print(f"    {key:18s} n={len(v):5d}  p5={np.percentile(v,5)*100:+7.3f} "
                  f"p25={np.percentile(v,25)*100:+7.3f} p50={np.percentile(v,50)*100:+7.3f} "
                  f"p75={np.percentile(v,75)*100:+7.3f} p95={np.percentile(v,95)*100:+7.3f}")

    # ---------------- 分层检验 ----------------
    def strat(key: str, nb: int = 4) -> None:
        vals = [(r[key], r) for r in rows if key in r and np.isfinite(r[key])]
        if len(vals) < 100:
            print(f"\n  [skip] {key} 样本不足")
            return
        arr_v = np.array([v[0] for v in vals])
        qs = np.percentile(arr_v, [100 * i / nb for i in range(1, nb)])
        edges = [-np.inf] + list(qs) + [np.inf]
        print()
        print("-" * 132)
        print(f"分层：{key}（{nb} 档，按分位切分）")
        print("-" * 132)
        print(f"  {'档':10s} {'n':>6s} {'胜%':>7s} {'均净%':>8s} {'中位%':>8s} {'PF':>6s} "
              f"{'右尾%':>7s} {'止损%':>7s} {'t值':>7s}  OOS三段均净%")
        for i in range(nb):
            lo, hi = edges[i], edges[i + 1]
            m = (arr_v > lo) & (arr_v <= hi) if i > 0 else (arr_v <= hi)
            sub = [r for (v, r), keep in zip(vals, m) if keep]
            lbl = f"Q{i+1}"
            if len(sub) < 15:
                print(f"  {lbl:<9s} n={len(sub):6d}  样本不足")
                continue
            s = rich(sub)
            segs = seg3_mean(sub)
            tt = tstat(np.array([r["ret"] - COST for r in sub]))
            print(f"  {lbl:<9s} {s['n']:6d} {s['win']:6.1f}% {s['mean']:+7.2f}% "
                  f"{s['med']:+7.2f}% {s['PF']:6.2f} {s['r20']:6.1f}% {s['sl']:6.1f}% "
                  f"{tt:+7.2f}  {segs[0]:+6.2f}/{segs[1]:+6.2f}/{segs[2]:+6.2f}")
        # 三段明细（必看跨度 —— 短窗口下三段同号 ≠ 跨环境泛化）
        if len(vals) >= 60:
            print(f"  ── {key} 整体三段明细 ──")
            print_seg3([r for _, r in vals], "全样本")

    print()
    print("=" * 132)
    print("Q1 单因子分层：各维度四分位 → 后续 24h 收益是否单调/有区分度")
    print("=" * 132)
    for key in OI_KEYS + ["cvd_ratio_6h", "cvd_ratio_24h",
                          "oi_cvd_div_24h", "taker_imb_6h", "taker_imb_24h"]:
        strat(key)

    # ---------------- 二维交叉：OI 变化 × CVD 占比 ----------------
    print()
    print("=" * 132)
    print("Q2 二维交叉：OI 变化率(24h) × CVD 占比(24h) —— 「价涨+OI涨」vs「价涨+OI缩」")
    print("=" * 132)
    both = [r for r in rows if "oi_chg_24h" in r and "cvd_ratio_24h" in r]
    if len(both) >= 100:
        oi_v = np.array([r["oi_chg_24h"] for r in both])
        cv_v = np.array([r["cvd_ratio_24h"] for r in both])
        oi_med = float(np.median(oi_v))
        cv_med = float(np.median(cv_v))
        print(f"  中位切点：OI 变化率 {oi_med*100:+.2f}%   CVD 占比 {cv_med*100:+.2f}%")
        quadrants = {
            "OI↑ + CVD+ (健康增仓)": (oi_v > oi_med) & (cv_v > cv_med),
            "OI↑ + CVD- (拉高派发)": (oi_v > oi_med) & (cv_v <= cv_med),
            "OI↓ + CVD+ (空头回补)": (oi_v <= oi_med) & (cv_v > cv_med),
            "OI↓ + CVD- (全面撤退)": (oi_v <= oi_med) & (cv_v <= cv_med),
        }
        print(f"\n  {'象限':26s} {'n':>6s} {'胜%':>7s} {'均净%':>8s} {'中位%':>8s} {'PF':>6s} "
              f"{'右尾%':>7s} {'止损%':>7s} {'t值':>7s}  OOS三段均净%")
        for name, m in quadrants.items():
            sub = [r for r, keep in zip(both, m) if keep]
            if len(sub) < 15:
                print(f"  {name:26s} n={len(sub):6d}  样本不足")
                continue
            s = rich(sub)
            segs = seg3_mean(sub)
            tt = tstat(np.array([r["ret"] - COST for r in sub]))
            print(f"  {name:26s} {s['n']:6d} {s['win']:6.1f}% {s['mean']:+7.2f}% "
                  f"{s['med']:+7.2f}% {s['PF']:6.2f} {s['r20']:6.1f}% {s['sl']:6.1f}% "
                  f"{tt:+7.2f}  {segs[0]:+6.2f}/{segs[1]:+6.2f}/{segs[2]:+6.2f}")

    # ---------------- 区分力度检验（Cliff's d） ----------------
    print()
    print("=" * 132)
    print("Q3 区分力度：盈利组 vs 亏损组，Cliff's d（|d|<0.02 = 无区分力）")
    print("=" * 132)
    wins = [r for r in rows if r["ret"] - COST > 0]
    loss = [r for r in rows if r["ret"] - COST <= 0]
    print(f"  盈利 {len(wins)} / 亏损 {len(loss)}")
    try:
        from scipy import stats as sstats
    except ImportError:
        sstats = None
    print(f"\n  {'特征':18s} {'W中位%':>10s} {'L中位%':>10s} {'Cliff d':>9s} {'MW p':>10s}")
    for key in OI_KEYS + ["cvd_ratio_6h", "cvd_ratio_24h",
                          "oi_cvd_div_24h", "taker_imb_6h", "taker_imb_24h"]:
        wv = np.array([r[key] for r in wins if key in r], dtype=float)
        lv = np.array([r[key] for r in loss if key in r], dtype=float)
        if len(wv) < 30 or len(lv) < 30:
            print(f"  {key:18s} 样本不足")
            continue
        ranks = sstats.rankdata(np.concatenate([wv, lv]))
        r1 = ranks[:len(wv)].sum()
        r2 = ranks[len(wv):].sum()
        aw = (r1 - len(wv) * (len(wv) + 1) / 2) / (len(wv) * len(lv))
        bw = (r2 - len(lv) * (len(lv) + 1) / 2) / (len(wv) * len(lv))
        d = aw - bw
        p = (sstats.mannwhitneyu(wv, lv, alternative="two-sided").pvalue
             if sstats else float("nan"))
        flag = "★有区分度" if abs(d) >= 0.10 else ("弱" if abs(d) >= 0.05 else "无区分力")
        print(f"  {key:18s} {np.median(wv)*100:+10.3f} {np.median(lv)*100:+10.3f} "
              f"{d:+9.3f} {p:10.3g}  {flag}")

    # ---------------- A / B 分档 × 最佳象限 ----------------
    print()
    print("=" * 132)
    print("Q4 A/B 分档内检验（信号形态差异：A 前面一直强、B 最后一根爆拉）")
    print("=" * 132)
    for sig in ("A", "B"):
        sub_sig = [r for r in rows if r["sig"] == sig]
        if len(sub_sig) < 50:
            print(f"\n  {sig} 档样本不足（n={len(sub_sig)}）")
            continue
        print(f"\n  ── {sig} 档（n={len(sub_sig)}）")
        s = rich(sub_sig)
        print(f"    {line('', s, 4).strip()}")
        for key in ["oi_chg_24h", "cvd_ratio_24h", "oi_cvd_div_24h"]:
            vals = [(r[key], r) for r in sub_sig if key in r]
            if len(vals) < 60:
                continue
            arr_v = np.array([v[0] for v in vals])
            qs = np.percentile(arr_v, [25, 50, 75])
            edges = [-np.inf] + list(qs) + [np.inf]
            print(f"    分层 {key}:")
            for i in range(4):
                lo, hi = edges[i], edges[i + 1]
                m = (arr_v > lo) & (arr_v <= hi) if i > 0 else (arr_v <= hi)
                sub = [r for (v, r), keep in zip(vals, m) if keep]
                if len(sub) < 15:
                    continue
                ss = rich(sub)
                segs = seg3_mean(sub)
                tt = tstat(np.array([r["ret"] - COST for r in sub]))
                print(f"      Q{i+1} n={ss['n']:5d} 胜={ss['win']:5.1f}% 均={ss['mean']:+6.2f}% "
                      f"PF={ss['PF']:5.2f} 右尾={ss['r20']:4.1f}% t={tt:+6.2f} "
                      f"OOS {segs[0]:+5.2f}/{segs[1]:+5.2f}/{segs[2]:+5.2f}")

    # 落盘
    out = SCRIPT_DIR.parent / "data" / "_prescreen_dim_events.csv"
    keys = sorted({k for r in rows for k in r})
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            row = {k: (v.isoformat() if isinstance(v, datetime) else v)
                   for k, v in r.items()}
            w.writerow(row)
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
