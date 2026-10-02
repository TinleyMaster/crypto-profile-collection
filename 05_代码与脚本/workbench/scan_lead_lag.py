#!/usr/bin/env python3
"""代币联动 · 领先-滞后（lead-lag）关系研究脚本。

业务问题
--------
「一个代币起飞之后，另一个代币大概率随后也起飞」——即 A 币异动后 N 小时/日内
B 币跟涨。这是同步相关性（现有 compute_correlation_matrix 只算 lag=0）的
时间偏移版：领先-滞后（lead-lag）关系。

方法（两步验证，防伪相关）
--------------------------
1. 互相关扫描：对全宇宙币对算「A 收益(t) 与 B 收益(t+k)」的 Pearson 相关
   （k=1..max_lag），找最优滞后 k*。收益一律取**对 BTC 的超额收益**
   （ret − ret_btc），扣除大盘 beta，避免「一起涨只是都在涨」的伪联动。
2. 事件研究验证：以 scan_signal 告警（或自定义「24h 涨超 X%」K 线事件）为
   「起飞事件」，统计候选跟随币 B 在事件后 1h/4h/12h/24h 的：
   跟涨概率 P(excess>0)、平均超额、对照基线概率的提升度（lift）。

数据源
------
- 小时级：biz.asset_klines（Binance USDT 永续 1h），已扣 BTC 超额
- 日频：  biz.v_asset_market_daily_primary（cmc > cmc_historical 单源视图）
- 事件：  biz.scan_signal.alerted_at（p_dir='up' 的告警时刻）
- 赛道：  biz.asset_sector + core.asset（canonical_symbol → sector）

运行
----
    python scan_lead_lag.py                  # 全流程（需连库）
    python scan_lead_lag.py --selftest       # 离线纯函数自检（不连库）
    python scan_lead_lag.py --hourly-top 100 --days-lag-max 10
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_SCRIPTS_SRC = _HERE.parent / "scripts" / "src"
for p in (_SCRIPTS_SRC, _HERE.parent / "scripts" / "bin"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

UTC = timezone.utc
HORIZONS_H = (1, 4, 12, 24)  # 事件研究跟涨窗口（小时）


def _to_utc64(dt) -> np.datetime64:
    """TIMESTAMPTZ → UTC 无时区 datetime64[s]（np.datetime64 不支持 tz-aware，须先归一到 UTC）。"""
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return np.datetime64(dt).astype("datetime64[s]")


# ═══════════════════════════════════════════════════════════════
#  一、纯函数层（离线可测）
# ═══════════════════════════════════════════════════════════════

def symbol_candidates(symbol: str) -> list[str]:
    """合约符号 → 候选裸符号序列（复用 scan_daemon._symbol_candidates 口径）。

    'B2USDT' → ['B2USDT','B2']；'1000FLOKIUSDT' → ['1000FLOKIUSDT','1000FLOKI','FLOKI']。
    """
    s = (symbol or "").upper()
    base = s[:-4] if s.endswith("USDT") and len(s) > 4 else s
    out = [s, base]
    for pre in ("1000000", "1000"):
        if base.startswith(pre) and len(base) > len(pre):
            out.append(base[len(pre):])
            break
    return list(dict.fromkeys(out))


def simple_returns(closes: np.ndarray) -> np.ndarray:
    """收盘价序列 → 简单收益率（相邻两期），首期为 NaN。"""
    if len(closes) < 2:
        return np.full(len(closes), np.nan)
    out = np.full(len(closes), np.nan)
    out[1:] = closes[1:] / closes[:-1] - 1.0
    return out


def excess_returns(ret: np.ndarray, btc_ret: np.ndarray) -> np.ndarray:
    """超额收益 = 币收益 − BTC 同周期收益（与 scan_signal_outcome.excess 同口径）。"""
    return ret - btc_ret


def winsorize(R: np.ndarray, limit_pct: float = 50.0) -> np.ndarray:
    """收益 winsorize（审计 A6）：把 |值| 超过 limit% 的裁到 ±limit%。

    实测 meme/ethereum 组内出现 110%/57% 的异常均值——单币 peg 破裂/数据异常
    的 +300% 日把 peer 均值拉飞，同时污染相关矩阵。winsorize 后这些离群日的
    贡献被限制在合理范围，正常大波动（<50%）不受影响。limit_pct<=0 关闭。
    """
    if not limit_pct or limit_pct <= 0:
        return R
    lim = limit_pct / 100.0
    return np.clip(R, -lim, lim)


def _pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 3:
        return None
    xv, yv = x[mask], y[mask]
    xd, yd = xv - xv.mean(), yv - yv.mean()
    denom = np.sqrt(np.dot(xd, xd) * np.dot(yd, yd))
    if denom == 0:
        return None
    return float(np.dot(xd, yd) / denom)


def lead_lag_corr(ret_a: np.ndarray, ret_b: np.ndarray, max_lag: int,
                  min_obs: int = 60) -> dict[int, tuple[float | None, int]]:
    """两序列全部滞后的互相关。

    返回 {lag: (corr, n)}，corr(lag=k) = corr(ret_a[t], ret_b[t+k])，
    k>0 表示 A 领先 B k 个周期；k<0 表示 B 领先 A。
    """
    n = len(ret_a)
    out: dict[int, tuple[float | None, int]] = {}
    for k in range(-max_lag, max_lag + 1):
        if k >= 0:
            x, y = ret_a[: n - k], ret_b[k:]
        else:
            x, y = ret_a[-k:], ret_b[: n + k]
        mask = np.isfinite(x) & np.isfinite(y)
        nn = int(mask.sum())
        corr = None
        if nn >= min_obs and nn >= 3:
            corr = _pearson(x, y)
        out[k] = (corr, nn)
    return out


def pairwise_lead_lag_matrix(R: np.ndarray, max_lag: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """对收益矩阵 R (T, N) 向量化扫描全部币对的正滞后互相关。

    返回 (best_lag, best_corr)，均为 (N, N)：
      best_lag[i, j]  = argmax_{k=1..max_lag} corr(R_i[t], R_j[t+k])  → i 领先 j
      best_corr[i, j] = 对应的相关值
    lag=0（同步相关）不参与最优选择，另行单独算。
    """
    T, N = R.shape
    mu = np.nanmean(R, axis=0)
    sd = np.nanstd(R, axis=0)
    Z = np.nan_to_num((R - mu) / (sd + 1e-12))
    best_lag = np.zeros((N, N), dtype=int)
    best_corr = np.zeros((N, N))  # 0 初始化：|C| > |0| 首轮即可更新（-inf 会让任何 |C| 都无法大于它）
    for k in range(1, max_lag + 1):
        Tk = T - k
        A, B = Z[:Tk], Z[k:]
        C = (A.T @ B) / (Tk - 1)  # (N,N) 相关近似（NaN 已零填充，缺值行贡献≈0）
        update = np.abs(C) > np.abs(best_corr)
        best_corr[update] = C[update]
        best_lag[update] = k
    # 同步相关（lag=0，对照组）
    sync = (Z.T @ Z) / (T - 1)
    return best_lag, best_corr, sync


def align_matrix(index: np.ndarray, series_map: dict[str, np.ndarray]) -> np.ndarray:
    """把 dict[symbol -> (ts, val)] 对齐成 (T, N) 矩阵。

    index 为统一时间轴（升序），缺失时点填 NaN；行数 = len(index)。
    """
    idx_pos = {np.datetime64(ts): p for p, ts in enumerate(index)}
    n = len(index)
    cols: list[str] = []
    rows: list[np.ndarray] = []
    for sym, (ts, val) in series_map.items():
        m = np.full(n, np.nan)
        for t, v in zip(ts, val):
            p = idx_pos.get(np.datetime64(t))
            if p is not None:
                m[p] = v
        cols.append(sym)
        rows.append(m)
    return np.column_stack(rows), cols


def trim_sparse_head(matrix: np.ndarray, min_ratio: float = 0.5) -> tuple[np.ndarray, int]:
    """裁掉矩阵头部「多数币还没上线」的稀疏行。

    返回 (裁后矩阵, 裁掉行数)。探索性分析里新上市币的头部 NaN 会污染互相关。
    """
    if matrix.shape[0] == 0:
        return matrix, 0
    present = np.isfinite(matrix).sum(axis=1) / matrix.shape[1]
    cut = 0
    while cut < matrix.shape[0] and present[cut] < min_ratio:
        cut += 1
    return matrix[cut:], cut


def forward_excess_at(ts_arr: np.ndarray, close_arr: np.ndarray,
                      btc_close_arr: np.ndarray, event_ts, horizons=HORIZONS_H,
                      start_offset: int = 0):
    """事件时刻 → 跟随币各窗口的 BTC 超额收益（%）。

    base = 事件时刻所在 1h K 线收盘；fwd = base 后 (start_offset + h) 根 K 线收盘。
    start_offset 用于「滞后对齐」：若 A 领先 B 的滞后为 L，则从 +L 起量 B 的跟涨，
    与榜单 lag 口径一致。基线用同口径（同用 bar 偏移），保证对比公平。
    """
    base_i = int(np.searchsorted(ts_arr, _to_utc64(event_ts), side="right")) - 1
    if base_i < 0 or base_i >= len(close_arr):
        return {}
    base, base_btc = close_arr[base_i], btc_close_arr[base_i]
    if not (np.isfinite(base) and np.isfinite(base_btc)) or base <= 0 or base_btc <= 0:
        return {}
    out = {}
    for h in horizons:
        fwd_i = base_i + start_offset + h
        if fwd_i >= len(close_arr):
            out[h] = None
            continue
        fwd, fwd_btc = close_arr[fwd_i], btc_close_arr[fwd_i]
        if not (np.isfinite(fwd) and np.isfinite(fwd_btc)) or fwd <= 0 or fwd_btc <= 0:
            out[h] = None
            continue
        out[h] = ((fwd / base - 1.0) - (fwd_btc / base_btc - 1.0)) * 100.0
    return out


def baseline_forward(close_arr: np.ndarray, btc_close_arr: np.ndarray,
                     horizons=HORIZONS_H, start_offset: int = 0) -> dict[int, np.ndarray]:
    """基线：全部 bar 起点的各窗口超额收益分布（%），与事件口径对齐（含滞后对齐）。"""
    out: dict[int, np.ndarray] = {}
    n = len(close_arr)
    for h in horizons:
        base, base_btc = close_arr[start_offset: n - h], btc_close_arr[start_offset: n - h]
        fwd, fwd_btc = close_arr[start_offset + h:], btc_close_arr[start_offset + h:]
        ok = (np.isfinite(base) & (base > 0) & np.isfinite(fwd) & (fwd > 0)
              & np.isfinite(base_btc) & (base_btc > 0) & np.isfinite(fwd_btc) & (fwd_btc > 0))
        ex = (fwd[ok] / base[ok] - 1.0) - (fwd_btc[ok] / base_btc[ok] - 1.0)
        out[h] = ex * 100.0
    return out


def summarize_excess(values: list[float | None]) -> tuple[int, float | None, float | None]:
    """事件样本 → (n, 跟涨概率, 平均超额%)。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return 0, None, None
    arr = np.asarray(vals, dtype=float)
    return len(arr), float((arr > 0).mean()), float(arr.mean())


# ═══════════════════════════════════════════════════════════════
#  二、DB 读取层
# ═══════════════════════════════════════════════════════════════

def get_db():
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    return get_connection(get_settings(require_database=True).database_url)


def load_kline_universe(conn, min_bars: int, top_n: int, days: int) -> list[str]:
    """1h K 线宇宙：近 N 天 bar 数达标 → 按近 24h 成交额降序取 top N（不含 BTCUSDT）。"""
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT k.symbol, COUNT(*) AS bars
            FROM biz.asset_klines k
            WHERE k.interval = '1h' AND k.open_time >= NOW() - make_interval(days => %s)
            GROUP BY k.symbol
            HAVING COUNT(*) >= %s
            """,
            (days, min_bars),
        )
        bars = {r["symbol"]: r["bars"] for r in cur.fetchall()}
        cur.execute(
            """
            SELECT symbol, SUM(quote_vol) AS vol24 FROM (
                SELECT symbol, quote_vol,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY open_time DESC) AS rn
                FROM biz.asset_klines
                WHERE interval = '1h' AND open_time >= NOW() - INTERVAL '48 hours'
            ) t WHERE rn <= 24 GROUP BY symbol
            """,
        )
        vol24 = {r["symbol"]: float(r["vol24"] or 0) for r in cur.fetchall()}
    ranked = sorted(bars, key=lambda s: (-vol24.get(s, 0), -bars[s]))
    return [s for s in ranked if s != "BTCUSDT"][:top_n]


def load_kline_closes(conn, symbols: list[str], days: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """symbol → (epoch秒 datetime64[s], close_px float64)，仅 1h、近 N 天。

    取数走紧凑列（epoch 整数 + float8），避免 NUMERIC→Decimal 与 tz-aware 转换——
    实测远程库全窗 12 万行需 ~150s，限窗 + 紧凑列 5 万行仅 ~14s。
    """
    import psycopg.rows

    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    if not symbols:
        return out
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT symbol, extract(epoch FROM open_time)::bigint AS ot, close_px::float8 AS px "
            "FROM biz.asset_klines WHERE interval = '1h' AND symbol = ANY(%s) "
            "  AND close_px IS NOT NULL AND open_time >= NOW() - make_interval(days => %s) "
            "ORDER BY symbol, open_time",
            (symbols, days),
        )
        buf: dict[str, list] = defaultdict(list)
        for r in cur.fetchall():
            buf[r["symbol"]].append((r["ot"], r["px"]))
    for sym, rows in buf.items():
        rows.sort(key=lambda t: t[0])
        epochs = np.asarray([r[0] for r in rows], dtype="int64")
        out[sym] = (epochs.astype("datetime64[s]"), np.asarray([r[1] for r in rows]))
    return out


def load_btc_kline(conn, days: int) -> tuple[np.ndarray, np.ndarray] | None:
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT extract(epoch FROM open_time)::bigint AS ot, close_px::float8 AS px "
            "FROM biz.asset_klines WHERE interval = '1h' AND symbol = 'BTCUSDT' "
            "  AND close_px IS NOT NULL AND open_time >= NOW() - make_interval(days => %s) "
            "ORDER BY open_time",
            (days,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    epochs = np.asarray([r["ot"] for r in rows], dtype="int64")
    return epochs.astype("datetime64[s]"), np.asarray([r["px"] for r in rows])


def load_daily_matrix(conn, top_n: int, min_days: int,
                      winsorize_pct: float = 50.0) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """日频收益矩阵（已扣 BTC 超额，默认 ±50% winsorize 防单币离群，A6）。

    返回 (index, R, symbols)：index 为 market_date 升序；symbols 为裸符号。
    winsorize_pct<=0 关闭裁剪。
    """
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 第一步：按最新市值取 top N 资产（asset_market_daily 有 8061 个币，必须先行裁剪）
        cur.execute(
            """
            SELECT asset_id FROM (
                SELECT asset_id, market_cap,
                       ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS rk
                FROM biz.asset_market_daily
                WHERE market_date = (SELECT MAX(market_date) FROM biz.asset_market_daily)
            ) t WHERE rk <= %s
            """,
            (top_n,),
        )
        top_ids = [r["asset_id"] for r in cur.fetchall()]
        # 第二步：只拉这些币的历史（内联三源去重）
        cur.execute(
            """
            SELECT symbol, market_date, price_usd, market_cap FROM (
                SELECT a.canonical_symbol AS symbol, v.market_date,
                       v.price_usd::float8 AS price_usd, v.market_cap::float8 AS market_cap,
                       ROW_NUMBER() OVER (PARTITION BY v.asset_id, v.market_date
                                          ORDER BY CASE v.source_code
                                              WHEN 'cmc' THEN 1
                                              WHEN 'cmc_historical' THEN 2
                                              ELSE 9 END) AS rn
                FROM biz.asset_market_daily v
                JOIN biz.coin_basic cb ON cb.asset_id = v.asset_id
                JOIN core.asset a ON a.asset_id = v.asset_id
                WHERE v.asset_id = ANY(%s) AND v.price_usd > 0
                  AND v.market_date >= CURRENT_DATE - %s
            ) t WHERE rn = 1
            """,
            (top_ids, min_days + 14),
        )
        raw = cur.fetchall()
    by_sym: dict[str, list] = defaultdict(list)
    cap_last: dict[str, float] = {}
    for r in raw:
        by_sym[r["symbol"]].append((r["market_date"], float(r["price_usd"])))
        cap_last[r["symbol"]] = float(r["market_cap"] or 0)
    # 只留覆盖天数达标且市值最高的 top N（去掉 STABLE/极端重复符号）
    eligible = [s for s, rows in by_sym.items() if len(rows) >= min_days and s not in ("BTC",)]
    eligible.sort(key=lambda s: -cap_last.get(s, 0))
    if "BTC" not in by_sym:
        raise RuntimeError("日频缺 BTC 序列，无法做超额收益")
    picked = eligible[:top_n] + ["BTC"]
    dates = sorted({d for s in picked for d, _ in by_sym[s]})
    index = np.asarray(dates, dtype="datetime64[D]")
    n = len(index)
    pos = {np.datetime64(d): p for p, d in enumerate(index)}
    cols: list[str] = []
    mat: list[np.ndarray] = []
    for s in picked:
        m = np.full(n, np.nan)
        for d, px in by_sym[s]:
            p = pos.get(np.datetime64(d))
            if p is not None:
                m[p] = px
        cols.append(s)
        mat.append(m)
    closes = np.column_stack(mat)
    rets = np.full_like(closes, np.nan)
    rets[1:] = closes[1:] / closes[:-1] - 1.0
    btc_idx = cols.index("BTC")
    excess = rets - rets[:, [btc_idx]]
    return index, winsorize(excess, winsorize_pct), cols


def load_sector_map(conn) -> dict[str, str]:
    """裸符号 → 主赛道（is_primary 优先，无则取第一条）。"""
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT a.canonical_symbol AS symbol, s.sector,
                   ROW_NUMBER() OVER (PARTITION BY a.canonical_symbol
                                      ORDER BY (s.is_primary) DESC, s.sector) AS rn
            FROM biz.asset_sector s
            JOIN core.asset a ON a.asset_id = s.asset_id
            """
        )
        out: dict[str, str] = {}
        for r in cur.fetchall():
            if r["rn"] == 1:
                out[r["symbol"]] = r["sector"]
    return out


def symbol_sector(sector_map: dict[str, str], symbol: str) -> str | None:
    """合约符号（含 USDT/1000X）→ 赛道（按候选序列查裸符号）。"""
    for cand in symbol_candidates(symbol):
        if cand in sector_map:
            return sector_map[cand]
    return None


def load_scan_events(conn, days: int, min_confidence: tuple[str, ...]) -> list[tuple[datetime, str]]:
    """scan_signal 起飞事件：已告警 + 方向 up + 置信度达标。"""
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT alerted_at, symbol FROM biz.scan_signal "
            "WHERE alerted_at IS NOT NULL AND p_dir = 'up' "
            "  AND confidence = ANY(%s) AND alerted_at >= NOW() - make_interval(days => %s) "
            "ORDER BY alerted_at",
            (list(min_confidence), days),
        )
        return [(r["alerted_at"], r["symbol"]) for r in cur.fetchall()]


def kline_surge_events(ts_arr: np.ndarray, close_arr: np.ndarray,
                       surge_pct: float, cooldown_h: float) -> list[datetime]:
    """K 线起飞事件：24h 涨幅首次突破阈值（连续超阈只记起始点）+ 冷却。"""
    if len(close_arr) < 25:
        return []
    ret24 = np.full(len(close_arr), np.nan)
    ret24[24:] = close_arr[24:] / close_arr[:-24] - 1.0
    hit = np.isfinite(ret24) & (ret24 >= surge_pct / 100.0)
    starts = []
    last_ts = None
    for i in range(24, len(hit)):
        if hit[i] and (not hit[i - 1]):
            t = datetime.fromtimestamp(ts_arr[i].astype("datetime64[s]").astype(int), tz=UTC)
            if last_ts is None or (t - last_ts) >= timedelta(hours=cooldown_h):
                starts.append(t)
                last_ts = t
    return starts


# ═══════════════════════════════════════════════════════════════
#  三、报告构建
# ═══════════════════════════════════════════════════════════════

def fmt_pct(v: float | None, digits: int = 1) -> str:
    return "-" if v is None else f"{v * 100:.{digits}f}%"


def fmt_num(v: float | None, digits: int = 2) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def _corr_pvalue(r: float, n: int) -> float:
    """相关 r（有效样本 n）的 H0:r=0 p 值（正态近似 z = r·√(n−1)）。

    仅用 math.erfc，不依赖 scipy。lead-lag 收益有自相关，p 会略偏乐观，
    作筛选用可接受（FDR 阈值保守方向仍成立）。
    """
    import math

    if n < 3 or not np.isfinite(r):
        return 1.0
    z = abs(r) * math.sqrt(max(n - 1, 1))
    return min(1.0, math.erfc(z / math.sqrt(2.0)))


def bh_fdr(pvals: list[float], q: float, m: int | None = None) -> set[int]:
    """Benjamini-Hochberg FDR 校正：返回通过 q 阈值的下标集合（审计 A2）。

    m = 总检验数（默认 = len(pvals)）。对全市场 N×(N−1)×max_lag 次滞后检验做
    多重比较校正——只喂门槛之上的 p 值但用全市场 m 作分母，是保守方向
    （未保留检验的 p 必然 ≥ 已保留者，不会漏报）。
    """
    if not pvals or q <= 0:
        return set()
    m = m if m and m >= len(pvals) else len(pvals)
    order = sorted(range(len(pvals)), key=lambda k: pvals[k])
    # 最大 k 使 p_(k) <= (k/m)·q；其前 k 个（最小的 k 个 p）全部通过
    cutoff = 0
    for rank, k in enumerate(order, start=1):
        if pvals[k] <= (rank / m) * q:
            cutoff = rank
    return set(order[:cutoff])


def build_hourly_report(R: np.ndarray, symbols: list[str], max_lag: int,
                        min_corr: float, top_pairs: int, sector_map: dict[str, str],
                        fdr_q: float = 0.05):
    best_lag, best_corr, sync = pairwise_lead_lag_matrix(R, max_lag)
    n = len(symbols)
    m_tests = n * (n - 1) * max_lag  # 全市场有序对 × 滞后 的总检验数（BH 分母）
    rows = []
    pvals = []
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            c = best_corr[i, j]
            if not np.isfinite(c) or abs(c) < min_corr:
                continue
            lag = int(best_lag[i, j])
            n_eff = R.shape[0] - lag  # 该滞后的有效样本
            p = _corr_pvalue(c, n_eff)
            rows.append((i, j, lag, float(c), float(sync[i, j]), p))
            pvals.append(p)
    fdr_pass = bh_fdr(pvals, fdr_q, m_tests) if fdr_q > 0 else set(range(len(pvals)))
    # 只在 FDR 通过的候选中取 top（A2：不过 FDR 的视为噪音，不进入榜单）
    rows = [r for k, r in enumerate(rows) if k in fdr_pass]
    rows.sort(key=lambda r: -abs(r[3]))
    top = rows[:top_pairs]
    sync_rows = sorted(
        ((i, j, float(sync[i, j])) for i in range(n) for j in range(n) if i != j),
        key=lambda r: -abs(r[2]),
    )[:10]
    return {
        "n_symbols": n,
        "n_bars": R.shape[0],
        "pairs_scanned": n * (n - 1),
        "pairs_above_threshold": len(pvals),
        "pairs_fdr_pass": len(rows),
        "fdr_q": fdr_q,
        "top_lead_lag": [
            {"leader": symbols[i], "follower": symbols[j], "lag": lag, "corr": round(c, 4),
             "sync_corr": round(sc, 4), "pvalue": round(p, 6),
             "leader_sector": symbol_sector(sector_map, symbols[i]),
             "follower_sector": symbol_sector(sector_map, symbols[j])}
            for i, j, lag, c, sc, p in top
        ],
        "top_sync": [{"a": symbols[i], "b": symbols[j], "corr": round(c, 4)}
                     for i, j, c in sync_rows],
    }


def build_sector_rotation(R, cols, sector_map: dict[str, str], max_lag: int):
    """日频赛道指数（等权平均超额）间的 lead-lag。"""
    groups: dict[str, list[int]] = defaultdict(list)
    for c, sym in enumerate(cols):
        sec = symbol_sector(sector_map, sym)
        if sec and sec != "other":
            groups[sec].append(c)
    res = {}
    for sec, idxs in groups.items():
        if len(idxs) < 2:
            continue
        sec_ret = np.nanmean(R[:, idxs], axis=1)
        res[sec] = sec_ret
    secs = list(res)
    if len(secs) < 2:
        return {"n_sectors": len(secs), "pairs": []}
    m = np.column_stack([res[s] for s in secs])
    bl, bc, _sync = pairwise_lead_lag_matrix(m, max_lag)
    pairs = []
    for i, a in enumerate(secs):
        for j, b in enumerate(secs):
            if i == j or not np.isfinite(bc[i, j]) or abs(bc[i, j]) < 0.15:
                continue
            pairs.append({"leader_sector": a, "follower_sector": b,
                          "lag_days": int(bl[i, j]), "corr": round(float(bc[i, j]), 4)})
    pairs.sort(key=lambda r: -abs(r["corr"]))
    return {"n_sectors": len(secs), "pairs": pairs[:20]}


def build_event_study(kline_data: dict[str, tuple[np.ndarray, np.ndarray]],
                      btc: tuple[np.ndarray, np.ndarray],
                      events: list[tuple[datetime, str]],
                      follow_candidates: dict[str, list[tuple[str, int]]],
                      min_events: int):
    """事件研究：A 起飞事件 → B 各窗口跟涨统计（超额、基线、lift）。"""
    btc_ts, btc_close = btc
    out_rows = []
    # 事件按 leader 分组
    by_leader: dict[str, list[datetime]] = defaultdict(list)
    for ts, sym in events:
        by_leader[sym].append(ts)
    for leader, ev_times in by_leader.items():
        for follower, lag in follow_candidates.get(leader, []):
            if follower not in kline_data:
                continue
            f_ts, f_close = kline_data[follower]
            # 对齐跟随币与 BTC 的时间轴（交集）
            common = np.intersect1d(f_ts, btc_ts)
            if len(common) < 100:
                continue
            pos_f = {np.datetime64(t): p for p, t in enumerate(f_ts)}
            pos_b = {np.datetime64(t): p for p, t in enumerate(btc_ts)}
            fc = np.asarray([f_close[pos_f[t]] for t in common])
            bc = np.asarray([btc_close[pos_b[t]] for t in common])
            base = baseline_forward(fc, bc, start_offset=lag)
            pair_ev: dict[int, list] = defaultdict(list)
            for ev_ts in ev_times:
                fx = forward_excess_at(common, fc, bc, ev_ts, start_offset=lag)
                for h in HORIZONS_H:
                    pair_ev[h].append(fx.get(h))
            row = {"leader": leader, "follower": follower, "lag": lag,
                   "n_events": len(ev_times)}
            for h in HORIZONS_H:
                n, prob, mean = summarize_excess(pair_ev[h])
                base_arr = base[h]
                base_prob = float((base_arr > 0).mean()) if len(base_arr) else None
                base_mean = float(base_arr.mean()) if len(base_arr) else None
                lift = (prob / base_prob) if (prob is not None and base_prob) else None
                row[f"h{h}"] = {"n": n, "follow_prob": prob, "mean_excess": mean,
                                "base_prob": base_prob, "base_mean": base_mean, "lift": lift}
            if row["h1"]["n"] >= min_events:
                out_rows.append(row)
    out_rows.sort(key=lambda r: -(r["h1"]["lift"] or 0), reverse=True)
    return out_rows


def build_report(conn, args) -> dict:
    report: dict = {"generated_at": datetime.now(UTC).isoformat(), "args": vars(args)}
    import sys as _sys

    def step(msg: str) -> None:
        print(f"[progress] {msg}", file=_sys.stderr, flush=True)

    sector_map = load_sector_map(conn)
    step("sector_map 就绪")

    # ── 一、小时级 lead-lag ──
    universe = load_kline_universe(conn, args.hourly_min_bars, args.hourly_top, args.hours_days)
    step(f"小时级宇宙 {len(universe)} 币")
    btc = load_btc_kline(conn, args.hours_days)
    kline_all = load_kline_closes(conn, universe + (["BTCUSDT"] if btc is None else []),
                                  args.hours_days)
    if btc is None and "BTCUSDT" in kline_all:
        btc = kline_all["BTCUSDT"]
    if btc is None:
        raise RuntimeError("缺 BTCUSDT 1h K 线，无法做超额收益")
    step(f"K 线载入完成（含 BTC {len(btc[0])} 根）")
    # 对齐：公共时间轴 = BTC 时间轴 ∩ 宇宙币时间轴
    universe_ts = set()
    for s in universe:
        if s in kline_all:
            universe_ts.update(np.datetime64(t) for t in kline_all[s][0])
    common_ts = np.asarray(sorted(set(np.datetime64(t) for t in btc[0]) & universe_ts),
                           dtype="datetime64[s]")
    series_map = {}
    for s in universe:
        if s not in kline_all:
            continue
        ts, close = kline_all[s]
        pos = {np.datetime64(t): p for p, t in enumerate(ts)}
        keep_ts = np.asarray([t for t in common_ts if t in pos])
        if len(keep_ts) < args.hourly_min_bars:
            continue
        vals = np.asarray([close[pos[t]] for t in keep_ts])
        rets = simple_returns(vals)
        bpos = {np.datetime64(t): p for p, t in enumerate(btc[0])}
        bvals = np.asarray([btc[1][bpos[t]] for t in keep_ts])
        bret = simple_returns(bvals)
        series_map[s] = (keep_ts, winsorize(excess_returns(rets, bret),
                                            args.winsorize_pct))
    if not series_map:
        raise RuntimeError("小时级宇宙为空：1h K 线历史不足，请先跑 phase_scan_klines.py --backfill-days")
    step(f"超额收益序列就绪：{len(series_map)} 币 × 公共时间轴")
    matrix, cols = align_matrix(common_ts, series_map)
    matrix, cut = trim_sparse_head(matrix)
    step(f"小时级矩阵 {matrix.shape}，头部裁掉 {cut} 行")
    report["hourly"] = build_hourly_report(matrix, cols, args.hourly_lag_max,
                                           args.min_corr, args.top_pairs, sector_map,
                                           args.fdr)
    step("小时级 lead-lag 完成")
    report["hourly"]["window_start"] = str(common_ts[cut])
    report["hourly"]["window_end"] = str(common_ts[-1])

    # ── 二、日频 lead-lag + 赛道轮动 ──
    d_idx, d_ret, d_cols = load_daily_matrix(conn, args.days_top, args.days_min_days)
    step("日频矩阵载入")
    d_matrix, d_cut = trim_sparse_head(d_ret)
    # 超额已按 BTC 计算，删掉 BTC 列避免「BTC 领先一切」占据榜单
    if "BTC" in d_cols:
        btc_col = d_cols.index("BTC")
        d_cols = [s for s in d_cols if s != "BTC"]
        d_matrix = np.delete(d_matrix, btc_col, axis=1)
    report["daily"] = build_hourly_report(d_matrix, d_cols, args.days_lag_max,
                                          args.min_corr, args.top_pairs, sector_map,
                                          args.fdr)
    report["daily"]["granularity"] = "day"
    report["daily"]["window_start"] = str(d_idx[d_cut])
    report["daily"]["window_end"] = str(d_idx[-1])
    report["sector_rotation"] = build_sector_rotation(d_matrix, d_cols,
                                                      sector_map, args.days_lag_max)
    step("日频 + 赛道轮动完成")

    # ── 三、事件研究验证 ──
    if args.events_source in ("scan", "both"):
        scan_ev = load_scan_events(conn, args.events_days, tuple(args.confidence))
    else:
        scan_ev = []
    if args.events_source in ("kline", "both"):
        surge_ev = []
        for s in universe:
            if s not in kline_all:
                continue
            ts, close = kline_all[s]
            for t in kline_surge_events(ts, close, args.surge_pct, args.cooldown_h):
                surge_ev.append((t, s))
    else:
        surge_ev = []
    events = scan_ev + surge_ev
    report["events"] = {"scan": len(scan_ev), "kline_surge": len(surge_ev),
                        "total": len(events)}
    step(f"事件：scan={len(scan_ev)} kline={len(surge_ev)}")
    if events:
        # 跟随币候选：小时级 top 对（A 领先 B）+ 同赛道
        follow_cand: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for pr in report["hourly"]["top_lead_lag"]:
            follow_cand[pr["leader"]].append((pr["follower"], pr["lag"]))
        kline_data = {s: kline_all[s] for s in universe if s in kline_all}
        report["event_study"] = build_event_study(kline_data, btc, events,
                                                  dict(follow_cand), args.min_events)
    else:
        report["event_study"] = []
    return report


# ═══════════════════════════════════════════════════════════════
#  四、输出
# ═══════════════════════════════════════════════════════════════

def print_report(rep: dict) -> None:
    w = "\u2500" * 72
    print(f"\n{w}\n\u4ee3\u5e01\u8054\u52a8 \u00b7 \u9886\u5148-\u6ede\u540e lead-lag \u7814\u7a76\u62a5\u544a"
          f"  {rep['generated_at'][:10]}\n{w}")
    h = rep.get("hourly") or {}
    if h:
        print(f"\n\u3010\u4e00\u3011\u5c0f\u65f6\u7ea7 lead-lag\uff08asset_klines 1h \u00b7 \u5df2\u6263 BTC \u8d85\u989d\uff09")
        print(f"  \u5b87\u5b99\uff1a{h['n_symbols']}\u4e2a\u5e01 \u00d7 {h['n_bars']}\u6839\u6700\u65f6\u95f4"
              f"\uff08{h.get('window_start','?')[:16]} ~ {h.get('window_end','?')[:16]} UTC\uff09"
              f"\uff0c\u626b\u63cf\u5e01\u5bf9 {h['pairs_scanned']}\uff0c\u8fc7\u95e8\u68cf "
              f"|corr|\u2265{rep['args'].get('min_corr')}\uff1a{h['pairs_above_threshold']}"
              f"\uff0c\u8fc7 FDR(q={h.get('fdr_q')})\uff1a{h['pairs_fdr_pass']}")
        print(f"  Top {len(h.get('top_lead_lag', []))} \u9886\u5148-\u6ede\u540e\u5bf9\uff08A \u2014\u8fc7\u2014>\u2014 B "
              f"= A \u9886\u5148 B\uff1a\u6700\u4f18\u6ede\u540e + \u6700\u4f18\u76f8\u5173 + \u540c\u6b65\u76f8\u5173\uff09")
        for i, p in enumerate(h.get("top_lead_lag", []), 1):
            print(f"  {i:>2}. {p['leader']:<12} \u2192 {p['follower']:<12} "
                  f"+{p['lag']:>2}h  corr={p['corr']:.3f}  sync={p['sync_corr']:.3f}"
                  f"  p={p.get('pvalue', float('nan')):.1e}"
                  f"  [{p.get('leader_sector') or '?'}/{p.get('follower_sector') or '?'}]")
        print(f"  \u540c\u6b65\u76f8\u5173\u5bf9\u7167 Top10\uff08lag=0\uff0c\u8bf4\u660e\u201c\u4e00\u8d77\u6da8\u201d\u4f46\u65e0\u5148\u540e\uff09\uff1a")
        print("   " + ",  ".join(f"{p['a']}-{p['b']}({p['corr']:.2f})" for p in h.get("top_sync", [])))
    d = rep.get("daily") or {}
    if d:
        print(f"\n\u3010\u4e8c\u3011\u65e5\u9891 lead-lag\uff08v_asset_market_daily_primary \u00b7 \u5df2\u6263 BTC \u8d85\u989d\uff09")
        print(f"  \u5b87\u5b99\uff1a{d['n_symbols']}\u4e2a\u5e01 \u00d7 {d['n_bars']}\u5929"
              f"\uff08{d.get('window_start','?')} ~ {d.get('window_end','?')}\uff09")
        for i, p in enumerate(d.get("top_lead_lag", []), 1):
            print(f"  {i:>2}. {p['leader']:<12} \u2192 {p['follower']:<12} "
                  f"+{p['lag']:>2}d  corr={p['corr']:.3f}  sync={p['sync_corr']:.3f}"
                  f"  [{p.get('leader_sector') or '?'}/{p.get('follower_sector') or '?'}]")
    sr = rep.get("sector_rotation") or {}
    if sr.get("pairs"):
        print(f"\n\u3010\u4e8c\u00b7\u8865\u3011\u8d5b\u9053\u65e5\u9891\u8f6e\u52a8\uff08\u7b49\u6743\u5e73\u5747\u8d85\u989d\u6307\u6570\uff09")
        for p in sr["pairs"][:10]:
            print(f"  {p['leader_sector']:<10} \u2192 {p['follower_sector']:<10} "
                  f"+{p['lag_days']}d  corr={p['corr']:.3f}")
    ev = rep.get("event_study") or []
    if ev:
        print(f"\n\u3010\u4e09\u3011\u4e8b\u4ef6\u7814\u7a76\u9a8c\u8bc1\uff08\u8d77\u98de\u4e8b\u4ef6\uff1a"
              f"scan={rep['events']['scan']} / kline\u6da8\u8d85={rep['events']['kline_surge']}\uff09")
        print(f"  \u5019\u9009\u8054\u52a8\u5bf9 {len(ev)}\uff1a\u201cA \u8d77\u98de \u2192 B \u8ddf\u6da8\u201d"
              f" \u8ddf\u6da8\u6982\u7387 = P(B \u8d85\u989d>0)\uff08\u7a97\u53e3\u6309\u699c\u5355\u6ede\u540e\u5bf9\u9f50\uff09"
              f"\uff0c\u63d0\u5347 = \u4e8b\u4ef6\u540e\u6982\u7387/\u57fa\u7ebf\u6982\u7387")
        for p in ev[:15]:
            l1 = p["h1"]
            print(f"  {p['leader']:<10} \u2192 {p['follower']:<10} (+{p['lag']}h)"
                  f"  n={l1['n']:>3}  \u8ddf\u6da8\u6982\u7387={fmt_pct(l1['follow_prob'],0)}"
                  f"  \u57fa\u7ebf={fmt_pct(l1['base_prob'],0)}"
                  f"  \u63d0\u5347\u00d7{fmt_num(l1['lift'])}"
                  f"  \u5e73\u5747\u8d85\u989d(1h)={fmt_num(l1['mean_excess'])}%")
    print(f"\n\u63d0\u793a\uff1a\u76f8\u5173\u2260\u56e0\u679c\uff1b\u5df2\u6263 BTC \u8d85\u989d\u4ecd\u53ef\u80fd\u5b58\u5728"
          f"\u5171\u540c\u8d8b\u52bf/\u8fc7\u6e21\u6027\u9c9c\u6d3b\u5316\u4f2a\u5173\u8054\u3002\u4e8b\u4ef6\u7814\u7a76\u7684"
          f"\u8ddf\u6da8\u6982\u7387\u63d0\u5347\u624d\u662f\u201c\u5148\u2014\u2014\u540e\u201d\u7684\u53ef\u4fe1\u4f53\u73b0\u3002")


def main() -> int:
    ap = argparse.ArgumentParser(description="代币联动 · 领先-滞后关系研究")
    ap.add_argument("--selftest", action="store_true", help="离线纯函数自检（不连库）")
    ap.add_argument("--hourly-top", type=int, default=150, help="小时级宇宙 top N（按 24h 成交额）")
    ap.add_argument("--hourly-min-bars", type=int, default=500, help="小时级最少 1h bar 数")
    ap.add_argument("--hourly-lag-max", type=int, default=48, help="小时级最大滞后（小时）")
    ap.add_argument("--hours-days", type=int, default=60, help="小时级回溯天数（远程库全窗拉取极慢，限窗提速）")
    ap.add_argument("--days-top", type=int, default=200, help="日频宇宙 top N（按市值）")
    ap.add_argument("--days-min-days", type=int, default=60, help="日频最少覆盖天数")
    ap.add_argument("--days-lag-max", type=int, default=14, help="日频最大滞后（天）")
    ap.add_argument("--winsorize-pct", type=float, default=50.0,
                    help="收益 winsorize 阈值 %%（±限幅防单币离群，A6；<=0 关闭）")
    ap.add_argument("--min-corr", type=float, default=0.15, help="候选对 |corr| 阈值")
    ap.add_argument("--fdr", type=float, default=0.05,
                    help="多重比较 FDR q（Benjamini-Hochberg，A2；<=0 关闭）")
    ap.add_argument("--top-pairs", type=int, default=30, help="报告展示 top 对")
    ap.add_argument("--events-source", choices=("scan", "kline", "both"), default="both")
    ap.add_argument("--events-days", type=int, default=30, help="scan 事件回溯天数")
    ap.add_argument("--confidence", nargs="+", default=("high", "medium"),
                    help="scan 事件置信度白名单")
    ap.add_argument("--surge-pct", type=float, default=15.0, help="K 线事件：24h 涨幅阈值 %%")
    ap.add_argument("--cooldown-h", type=float, default=12.0, help="事件同币冷却（小时）")
    ap.add_argument("--min-events", type=int, default=5, help="事件研究最少样本数")
    ap.add_argument("--output", type=str, default="", help="JSON 输出路径（默认 workbench/output/）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    with get_db() as conn:
        rep = build_report(conn, args)
    print_report(rep)
    out_path = args.output or str(_HERE / "output" / f"scan_lead_lag_{datetime.now(UTC):%Y-%m-%d}.json")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON 报告已写入：{out_path}")
    return 0


def selftest() -> int:
    """离线口径自检（同 test_scan_edge_metrics.py 风格）。"""
    passed = failed = 0

    def check(cond, name, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  \u2713 {name}")
        else:
            failed += 1
            print(f"  \u2717 {name}  {detail}")

    print("\n\u3010\u81ea\u68c0\u3011symbol \u89c4\u8303\u5316")
    check(symbol_candidates("BTCUSDT") == ["BTCUSDT", "BTC"], "BTCUSDT → 去 USDT")
    check(symbol_candidates("1000FLOKIUSDT") == ["1000FLOKIUSDT", "1000FLOKI", "FLOKI"],
          "1000FLOKIUSDT → 剥 1000X 前缀")

    print("\n\u3010\u81ea\u68c0\u3011winsorize\uff08A6\uff09")
    _w = np.array([-3.0, -0.5, 0.0, 0.5, 3.0]) / 100.0  # -3% ~ +3%
    _wc = winsorize(_w, limit_pct=1.0)  # ±1% 限幅
    check(_wc[0] == -0.01 and _wc[-1] == 0.01 and abs(_wc[1] - (-0.005)) < 1e-12,
          "±1% winsorize：越界裁到边界，界内不动", f"got={_wc}")
    check(np.array_equal(winsorize(_w, 0.0), _w), "limit<=0 → 不裁剪")
    check(winsorize(_w, 50.0)[-1] == 0.03, "默认 ±50% 不裁小波动")

    print("\n\u3010\u81ea\u68c0\u3011A2 \u591a\u91cd\u6bd4\u8f83\u6821\u6b63\uff08p \u503c + BH-FDR\uff09")
    check(abs(_corr_pvalue(0.0, 100) - 1.0) < 1e-9, "corr=0 → p=1")
    check(_corr_pvalue(0.5, 100) < 1e-6, "corr=0.5/n=100 → p 极小", f"got={_corr_pvalue(0.5, 100):.2e}")
    check(_corr_pvalue(0.5, 10) > _corr_pvalue(0.5, 100), "同相关、样本更小 → p 更大")
    # BH：5 个 p，一个极小，其余 0.5 —— 极小者通过，其余不过
    _ps = [0.5, 0.001, 0.5, 0.6, 0.7]
    _pass = bh_fdr(_ps, q=0.05)
    check(_pass == {1}, "BH-FDR：仅极小 p 通过，其余视为噪音", f"got={_pass}")
    check(bh_fdr([], 0.05) == set() and bh_fdr(_ps, 0) == set(), "空 p / q<=0 → 空集")
    # 全小 p（如全 1e-8）→ 全过
    check(bh_fdr([1e-8] * 10, 0.05) == set(range(10)), "全极小 p → 全过 FDR")
    # 用全市场 m 作分母更严格：同 p 在 m=5 时通过、m=100 时被滤
    check(bh_fdr([0.001, 0.5], 0.05, m=5) == {0}
          and bh_fdr([0.001, 0.5], 0.05, m=100) == set(),
          "分母用全市场总检验数 → 更严格（保守方向）")

    print("\n\u3010\u81ea\u68c0\u3011\u76f8\u5173/\u8d85\u989d\u53e3\u5f84")
    a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    b = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
    c = _pearson(a, b)
    check(c is not None and abs(c - 1.0) < 1e-9, "完全线性相关 = 1", f"got={c}")
    r = simple_returns(np.array([100.0, 110.0, 99.0]))
    check(abs(r[1] - 0.10) < 1e-9 and abs(r[2] + 0.10) < 1e-9 and np.isnan(r[0]),
          "simple_returns 口径", f"got={r}")
    ex = excess_returns(np.array([0.10, -0.02]), np.array([0.03, 0.01]))
    check(abs(ex[0] - 0.07) < 1e-12 and abs(ex[1] + 0.03) < 1e-12, "超额 = 币收益 − BTC 收益")

    print("\n\u3010\u81ea\u68c0\u3011\u6700\u4f18\u6ede\u540e\u627e\u56de")
    t = np.arange(1000, dtype=float)
    x = np.sin(t / 10.0)
    y = np.roll(x, 3)  # y 落后 x 3 个周期 → x 领先 y 3
    ll = lead_lag_corr(x, y, max_lag=10, min_obs=50)
    best_k = max((k for k, (c_, _n) in ll.items() if c_ is not None),
                 key=lambda k: abs(ll[k][0]))
    check(best_k == 3 and ll[3][0] > 0.99,
          "构造序列：x 领先 y 3 期 → 最优滞后 +3", f"got best_k={best_k} corr={ll[3][0]:.4f}")
    # 同步相关 + 噪声：无真实 lead 时最优滞后相关不应显著高于同步
    rng = np.random.default_rng(7)
    n2 = 400
    xx = rng.normal(size=n2)
    yy = rng.normal(size=n2)
    ll2 = lead_lag_corr(xx, yy, max_lag=5, min_obs=50)
    maxabs = max(abs(ll2[k][0]) for k in ll2 if ll2[k][0] is not None)
    check(maxabs < 0.35, "纯噪声对：各滞后相关均不显著（<0.35）", f"max={maxabs:.3f}")

    print("\n\u3010\u81ea\u68c0\u3011\u4e8b\u4ef6\u8ddf\u6da8\u7edf\u8ba1")
    ev_vals = [1.0, 2.0, -0.5, None, 3.0]
    n, prob, mean = summarize_excess(ev_vals)
    check(n == 4 and abs(prob - 0.75) < 1e-9 and abs(mean - 1.375) < 1e-9,
          "summarize_excess：None 剔除、跟涨概率 75%", f"got=({n},{prob},{mean})")

    print("\n\u3010\u81ea\u68c0\u3011\u77e9\u9635\u7248\u6700\u4f18\u6ede\u540e\uff08\u9632 -inf \u521d\u59cb\u5316\u56de\u5f52\uff09")
    _rng = np.random.default_rng(1)
    _R = _rng.normal(size=(2000, 8)) * 0.2
    _c = _rng.normal(size=2000) * 0.2
    _R[:, 1] = _c
    _R[:, 2] = np.roll(_c, 3)  # col2 落后 col1 3 期
    _bl, _bc, _sync = pairwise_lead_lag_matrix(_R, 10)
    check(_bl[1, 2] == 3 and _bc[1, 2] > 0.9,
          "矩阵版：构造 col1 领先 col2 3 期 → 最优滞后 +3",
          f"got lag={_bl[1,2]} corr={_bc[1,2]:.3f}")
    check(np.isfinite(_bc).all(), "矩阵版：无 -inf 残留（-inf 初始化会致全部滞后相关失效）",
          f"nan={np.isnan(_bc).sum()} inf={np.isinf(_bc).sum()}")

    print("\n\u3010\u81ea\u68c0\u3011K\u7ebf\u98d9\u5347\u4e8b\u4ef6\u68c0\u6d4b")
    close = np.full(300, 100.0)
    close[50:60] = np.linspace(100, 118, 10)   # 第 50 根起 24h 内 +18%
    close[60:] = 118.0
    ts = np.arange(300) * 3600
    ts64 = ts.astype("datetime64[s]")
    evs = kline_surge_events(ts64, close, surge_pct=15.0, cooldown_h=12)
    check(len(evs) == 1 and evs[0] == datetime(1970, 1, 3, 10, 0, tzinfo=UTC),
          "连续超阈只记起始点一次（首破 15% 的根 = 索引 58）", f"got={evs}")
    close2 = np.full(300, 100.0)
    evs2 = kline_surge_events(ts64, close2, surge_pct=15.0, cooldown_h=12)
    check(len(evs2) == 0, "无涨超 → 无事件")

    print(f"\n\u81ea\u68c0\u7ed3\u679c\uff1a{passed} \u901a\u8fc7 / {failed} \u5931\u8d25")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
