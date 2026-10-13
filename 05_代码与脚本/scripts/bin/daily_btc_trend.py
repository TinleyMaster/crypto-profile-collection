#!/usr/bin/env python3
"""每日 BTC 大盘趋势分析（Volume Profile + 盘面结构 + 多因子共振）。

用途
----
给「每日 BTC 大盘趋势」出报告：用 `catalyst.vp_context.estimate_regime`（VP 价值区 +
POC 位移 + 布林带宽）判断各周期处于震荡/趋势/待定，配合 `volume_profile` 的
POC / 价值区 / HVN / LVN 位置参考，再叠加四类多因子：
  ① 量价&筹码：VP + 成交量 + VWAP + ATR（`analyze_interval` 内计算）；
  ② 合约持仓：OI + 资金费率 + 多空比 + 基差 + 爆仓（读 biz 衍生品表）；
  ③ 传统指标：MA/EMA + RSI + MACD + 布林（择时参考，不单独定方向）；
  ④ 链上+宏观：MVRV + 交易所余额/净流 + 稳定币 + ETF + 恐贪 + 市值占比 + 周期。
并按「日线大方向场景（VP + OI + 费率 + 200MA + 量能）」给出多因子共振摘要。
输出 Markdown（默认落 scripts/data/daily_btc_trend.md），可选邮件 HTML。

⚠️ 口径（与 backtest_vp_levels 结论一致）：
- VP 只描述「价格大概率停留区」，触级不代表反转，**不构成方向信号**；
- estimate_regime 是**状态描述**不是预测：描述「当前所处结构」，不代表「接下来会怎样」；
- 合约/情绪指标（OI、费率、多空比）可以持续钝化，**不单靠费率猜反转**；
- 单一指标不做决策，多因子共振才有效；报告只描述状态，不是开单信号。

用法
----
    python daily_btc_trend.py                    # 默认 1h + 1d 双周期，打印 + 落 md
    python daily_btc_trend.py --interval 1h      # 单周期
    python daily_btc_trend.py --intervals 15m,1h,1d
    python daily_btc_trend.py --out report.md    # 自定义输出路径
    python daily_btc_trend.py --self-test        # 离线注入测试（无 DB）
"""
from __future__ import annotations

import argparse
import html
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"

# 兼容两种部署结构（与 catalyst_fast_daemon._setup_paths 同口径）：
#   本地开发： 05_代码与脚本/workbench/catalyst/   容器部署： /app/catalyst
_project_root = SCRIPT_DIR.parent.parent
workbench_dir = next(
    (
        d
        for d in (_project_root / "workbench", _project_root, Path("/app"))
        if (d / "catalyst" / "__init__.py").exists()
    ),
    None,
)
if workbench_dir is None:
    raise RuntimeError("找不到 catalyst 包（期望 workbench/catalyst 或 /app/catalyst）")
for p in (workbench_dir, PROJECT_SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.analysis.technical_indicators import (  # noqa: E402
    PATTERN_CN,
    atr_series,
    bollinger_series,
    bbw,
    candlestick_patterns,
    ema_series,
    macd_series,
    percent_b,
    rsi_series,
    sma,
    volume_profile,
    vwap_series,
)
from catalyst.vp_context import estimate_regime  # noqa: E402

DATA_DIR = SCRIPT_DIR.parent / "data"
DEFAULT_OUT = DATA_DIR / "daily_btc_trend.md"

SYMBOL = "BTCUSDT"

# 各周期的默认口径：window_bars=VP 单窗口根数；lookback_days=取数天数（需 ≥2×window_bars+40 根，
# 且 1d 需 ≥ 200 根才能算 MA200）
# 周期根数 = bars_per_day × 天数
INTERVAL_CONFIG = {
    "5m": {"window_bars": 288, "lookback_days": 14, "bars_per_day": 288},
    "15m": {"window_bars": 96, "lookback_days": 21, "bars_per_day": 96},
    "1h": {"window_bars": 168, "lookback_days": 21, "bars_per_day": 24},
    # 4h / 1w 为**派生周期**：DB 无原生 K 线，由 1h / 1d 聚合得到（base + step_hours）
    "4h": {"window_bars": 90, "lookback_days": 60, "bars_per_day": 6, "base": "1h", "step_hours": 4},
    "1d": {"window_bars": 50, "lookback_days": 250, "bars_per_day": 1},
    "1w": {"window_bars": 50, "lookback_days": 1000, "bars_per_day": 1 / 7, "base": "1d", "step_hours": 168},
}

KLINES_SQL = (
    "SELECT open_time, open_px, high_px, low_px, close_px, quote_vol "
    "FROM biz.asset_klines WHERE symbol = %s AND interval = %s "
    "AND open_time >= NOW() - make_interval(days => %s) "
    "AND open_px > 0 AND high_px >= low_px AND quote_vol IS NOT NULL "
    "ORDER BY open_time LIMIT %s"
)


# ---------------------------------------------------------------- 取数

def load_klines(conn, interval: str, days: int, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(KLINES_SQL, (SYMBOL, interval, days, limit))
        return [
            {"t": r[0], "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "vol": float(r[5])}
            for r in cur.fetchall()
        ]


def resample_bars(bars: list[dict], step_hours: int) -> list[dict]:
    """按新周期时长（小时）聚合 OHLCV，时间桶按 UTC 边界对齐。

    用于派生周期（4h←1h step=4、1w←1d step=168）。相比「顺序每 N 根一组」，
    按时间桶对齐能保证：同一根 4h/周 K 线在不同运行里包含相同的底层 K 线
    （窗口起点滑动不会导致蜡烛错位、判定抖动）。
    """
    import datetime as _dt

    def _bucket(d):
        if hasattr(d, "timestamp"):
            return int(d.timestamp() // (step_hours * 3600))
        return int(_dt.datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                   .timestamp() // (step_hours * 3600))

    buckets: dict[int, list[dict]] = {}
    for b in bars:
        buckets.setdefault(_bucket(b["t"]), []).append(b)
    out: list[dict] = []
    for key in sorted(buckets):
        grp = buckets[key]
        out.append({
            "t": grp[0]["t"],
            "open": grp[0]["open"],
            "high": max(b["high"] for b in grp),
            "low": min(b["low"] for b in grp),
            "close": grp[-1]["close"],
            "vol": sum(b["vol"] for b in grp),
        })
    return out


def _fmt(x) -> str:
    return "N/A" if x is None else f"{x:,.2f}"


def _pct(x, digits: int = 2) -> str:
    if x is None:
        return "N/A"
    return f"{x * 100:+.{digits}f}%"


def _pct_html(x) -> str:
    """涨跌幅 → 带颜色 span（红涨绿跌，与 send_daily_brief 同口径）。"""
    if x is None:
        return '<span style="color:#64748b">N/A</span>'
    color = "#dc2626" if x > 0 else ("#16a34a" if x < 0 else "#64748b")
    return f'<span style="color:{color}">{_pct(x)}</span>'


def _fmt_usd_b(x) -> str:
    """美元金额 → 以 10 亿为单位显示（≥10 亿）或原样。"""
    if x is None:
        return "N/A"
    if abs(x) >= 1e9:
        return f"${x / 1e9:.2f}B"
    if abs(x) >= 1e6:
        return f"${x / 1e6:.2f}M"
    return f"${x:,.0f}"


def _pct_px(x, digits: int = 4) -> str:
    """小数费率（如 0.002548）→ 带符号百分比。"""
    if x is None:
        return "N/A"
    return f"{x * 100:+.{digits}f}%"


def _pctv(x, digits: int = 2) -> str:
    """值本身已是百分比（如 CoinGlass oi_change_24h_pct=0.88 表示 0.88%）→ 直接带符号输出。"""
    if x is None:
        return "N/A"
    return f"{x:+.{digits}f}%"


def _fmt_cn(dt) -> str:
    """UTC datetime → 上海时区字符串（生成时间必须真实转换，避免把 UTC 当 CST 打印）。"""
    return dt.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 单周期分析

def analyze_interval(bars: list[dict], window_bars: int) -> dict:
    """对一个周期计算：价格概况 + 盘面结构 + VP 位置 + 传统指标/量价因子。"""
    o = [b["open"] for b in bars]
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    v = [b["vol"] for b in bars]

    reg = estimate_regime(o, h, l, c, v, window_bars=window_bars)
    out = {
        "interval": bars[0]["t"].strftime("%H:%M") if bars else "",
        "last_ts": bars[-1]["t"] if bars else None,
        "close": c[-1] if c else None,
        "regime": reg,
    }

    # 涨跌幅（用本周期序列）
    def _chg(k):
        return c[-1] / c[-1 - k] - 1.0 if len(c) > k else None
    out["chg_24h"] = _chg(24) if len(c) > 25 else None
    out["chg_7d"] = _chg(168) if len(c) > 169 else None
    out["chg_30d"] = _chg(30) if len(c) > 31 else None

    # VP 位置参考
    try:
        p = volume_profile(o[-window_bars:], h[-window_bars:], l[-window_bars:],
                           c[-window_bars:], v[-window_bars:])
        out["vp"] = {
            "poc": p.get("poc"),
            "va_low": (p.get("value_area") or {}).get("low"),
            "va_high": (p.get("value_area") or {}).get("high"),
            "support": p.get("support"),
            "resistance": p.get("resistance"),
            "lvn": [x["price"] for x in (p.get("lvn") or [])[:3]],
            "bins": p.get("bins") or [],
            "row_width": p.get("row_width"),
            "price_low": p.get("price_low"),
            "price_high": p.get("price_high"),
        }
    except Exception:
        out["vp"] = None

    # 传统指标 + 量价因子（在整段序列上算，取末端值）
    out["indicators"] = _compute_indicators(o, h, l, c, v, window_bars)

    # 经典 K 线形态（最近 3 根，供「技术指标」段落展示）
    try:
        pats = candlestick_patterns(o, h, l, c)
        out["patterns"] = [
            {"ago": i, "names": pats[-1 - i] if len(pats) > i else [],
             "close": c[-1 - i] if len(c) > i else None}
            for i in range(min(3, len(pats)))
        ]
    except Exception:
        out["patterns"] = []
    return out


def _last(x):
    """序列末个非 None 值。"""
    return next((v for v in reversed(x) if v is not None), None)


def _compute_indicators(o, h, l, c, v, window_bars: int) -> dict:
    """计算 MA/EMA/RSI/MACD/BOLL/ATR/VWAP/量能（末端快照）。"""
    n = len(c)
    if n < 2:
        return {}
    cur = c[-1]
    ind = {
        "ma20": sma(c, 20)[-1] if n >= 20 else None,
        "ma50": sma(c, 50)[-1] if n >= 50 else None,
        "ma200": sma(c, 200)[-1] if n >= 200 else None,
        "ema12": ema_series(c, 12)[-1] if n >= 12 else None,
        "ema26": ema_series(c, 26)[-1] if n >= 26 else None,
        "rsi": rsi_series(c, 14)[-1] if n > 14 else None,
        "boll": (bollinger_series(c, 20, 2.0)[-1] if n >= 20 else None),
        "atr": atr_series(h, l, c, 14)[-1] if n > 14 else None,
        "vwap": vwap_series(h, l, c, v, min(window_bars, n))[-1],
    }
    # MACD(12,26,9)：hist 需要 34 根预热
    dif, dea, hist = macd_series(c, 12, 26, 9)
    ind["dif"], ind["dea"], ind["hist"] = _last(dif), _last(dea), _last(hist)
    # 金叉/死叉：最近 3 根内 DIF 上/下穿 DEA
    ind["cross"] = None
    if ind["dif"] is not None and ind["dea"] is not None:
        for k in (1, 2, 3):
            if len(dif) - 1 - k < 0:
                break
            d0, s0 = dif[-k], dea[-k]
            if d0 is not None and s0 is not None:
                if d0 > s0 and dif[-1] <= dea[-1]:
                    ind["cross"] = "死叉"
                elif d0 <= s0 and dif[-1] > dea[-1]:
                    ind["cross"] = "金叉"
    # 量能：最后一根量 vs 前 20 根均量（突破需放量）
    if len(v) >= 21 and v[-1] > 0:
        avg_prev = sum(v[-21:-1]) / 20.0
        ind["vol_ratio"] = v[-1] / avg_prev if avg_prev > 0 else None
    else:
        ind["vol_ratio"] = None
    # 90 根价格区间位置：当前价在近 90 根高低区间内的百分位（历史位置感）
    win = c[-90:]
    if len(win) >= 20:
        lo, hi = min(win), max(win)
        ind["range90_lo"], ind["range90_hi"] = lo, hi
        ind["range90_pct"] = (cur - lo) / (hi - lo) if hi > lo else None
    # 5 根短线趋势方向（供锤子/射击之星等需上下文形态的解读）
    ind["trend5_up"] = (c[-1] > c[-6]) if len(c) > 6 else None
    ind["close"] = cur
    return ind


# ---------------------------------------------------------------- 市场上下文（衍生品 + 链上 + 宏观）

# 允许的最大数据滞后天数（超过则在展示处标注 STALE）
MAX_LAG_DAYS = 2


def _returns_series(prices: dict) -> dict:
    """{date: price} → {date: 日收益}（按日期升序计算，缺值跳过）。"""
    ds = sorted(prices)
    out: dict = {}
    for i in range(1, len(ds)):
        p0, p1 = prices[ds[i - 1]], prices[ds[i]]
        if p0 and p1 and p0 > 0:
            out[ds[i]] = p1 / p0 - 1.0
    return out


def _pearson(xs, ys):
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def _compute_correlations(btc_by_date: dict, macro: list[dict], windows=(30, 90)) -> dict:
    """BTC 1d 收盘与宏观/指数日收益的滚动相关系数（按对齐日期）。"""
    btc_ret = _returns_series(btc_by_date)
    out: dict = {}
    for target in ("nasdaq", "gold", "dxy"):
        prices = {m["date"]: m[target] for m in macro if m.get(target) is not None}
        tr = _returns_series(prices)
        common = sorted(set(btc_ret) & set(tr))
        if len(common) < 20:
            continue
        for w in windows:
            seg = common[-w:]
            if len(seg) < max(15, int(w * 0.5)):
                continue
            c = _pearson([btc_ret[d] for d in seg], [tr[d] for d in seg])
            if c is not None:
                out[f"btc_vs_{target}_{w}d"] = c
    return out


def load_market_context(conn) -> dict:
    """读取四类多因子所需的衍生品/链上/宏观数据（只读 biz 各表，缺表/缺数据返回 None）。"""
    ctx: dict = {}

    def q(sql, params=()):
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception:
            conn.rollback()
            return []

    # ── ② 合约持仓：OI（跨所聚合，CoinGlass）───────────────
    rows = q("""
        SELECT ts, oi_usd, oi_change_24h_pct
        FROM biz.coinglass_derivatives_snapshot
        WHERE symbol = 'BTC' AND exchange = 'All' AND oi_usd IS NOT NULL
        ORDER BY ts DESC LIMIT 8
    """)
    if rows and rows[0][1] is not None:
        oi = {"ts": rows[0][0], "value_usd": float(rows[0][1])}
        oi["chg24h"] = float(rows[0][2]) if rows[0][2] is not None else None
        ref = next((r for r in rows[7:] if r[1]), None) or (rows[-1] if len(rows) > 1 else None)
        if ref and ref[1]:
            oi["chg7d"] = float(rows[0][1]) / float(ref[1]) - 1.0
        ctx["oi"] = oi
    else:
        ctx["oi"] = None

    # OI 60d 历史分位（Binance 单所口径，btc_oi_daily；coinglass All 历史太短）
    rows = q("SELECT open_interest FROM biz.btc_oi_daily "
             "WHERE open_interest IS NOT NULL ORDER BY metric_date ASC")
    if rows:
        vals = [float(r[0]) for r in rows]
        cur_oi = vals[-1]
        ctx["oi_pct60"] = {
            "value": cur_oi,
            "n": len(vals),
            "pct": 100.0 * sum(1 for x in vals[:-1] if x < cur_oi) / max(1, len(vals) - 1),
        }
    else:
        ctx["oi_pct60"] = None

    # ── ② 资金费率（Binance，CoinGlass 快照；btc_funding_daily 滞后故不用）──
    rows = q("""
        SELECT ts, funding_rate
        FROM biz.coinglass_derivatives_snapshot
        WHERE symbol = 'BTC' AND exchange = 'Binance' AND funding_rate IS NOT NULL
        ORDER BY ts DESC LIMIT 30
    """)
    if rows:
        rates = [float(r[1]) for r in rows if r[1] is not None]
        ctx["funding"] = {
            "ts": rows[0][0],
            "rate": rates[0] if rates else None,
            "avg7d": sum(rates) / len(rates) if rates else None,
            "n_days": len(rates),
        }
        if len(rates) >= 10 and rates[0] is not None:
            ctx["funding"]["pct30"] = 100.0 * sum(1 for x in rates[1:] if x < rates[0]) / max(1, len(rates) - 1)
    else:
        ctx["funding"] = None

    # ── ② 多空比（Binance，CoinGlass 历史）──
    rows = q("""
        SELECT ratio_type, ts, long_percent, short_percent, ls_ratio
        FROM biz.cg_ls_ratio_hist
        WHERE symbol = 'BTCUSDT'
          AND ts = (SELECT MAX(ts) FROM biz.cg_ls_ratio_hist WHERE symbol = 'BTCUSDT')
        ORDER BY ratio_type
    """)
    if rows:
        ctx["lsr"] = {
            r[0]: {"ts": r[1], "long_pct": float(r[2]) if r[2] is not None else None,
                   "short_pct": float(r[3]) if r[3] is not None else None,
                   "ratio": float(r[4]) if r[4] is not None else None}
            for r in rows
        }
    else:
        ctx["lsr"] = None

    # ── ② 基差（CoinGlass，4h）──
    rows = q("""
        SELECT ts, close_basis
        FROM biz.cg_basis_hist
        WHERE symbol = 'BTCUSDT' AND interval = '4h' AND close_basis IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """)
    ctx["basis"] = {"ts": rows[0][0], "close_pct": float(rows[0][1])} if rows else None

    # ── ② 爆仓（日度 regime + 4h 明细）──
    rows = q("""
        SELECT ts, liq_total, long_share, bucket, is_extreme
        FROM biz.liq_daily_regime WHERE symbol = 'BTCUSDT'
        ORDER BY ts DESC LIMIT 8
    """)
    if rows:
        latest = rows[0]
        ctx["liq"] = {
            "ts": latest[0],
            "total": float(latest[1]) if latest[1] is not None else None,
            "long_share": float(latest[2]) if latest[2] is not None else None,
            "bucket": latest[3],
            "is_extreme": bool(latest[4]),
        }
    else:
        ctx["liq"] = None
    rows = q("""
        SELECT ts, long_liq_usd, short_liq_usd
        FROM biz.liquidation_history
        WHERE symbol = 'BTCUSDT' AND exchange_scope = 'all'
          AND long_liq_usd IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """)
    if rows:
        ctx["liq_4h"] = {
            "ts": rows[0][0],
            "long": float(rows[0][1]) or 0.0,
            "short": float(rows[0][2]) or 0.0,
        }
    else:
        ctx["liq_4h"] = None

    # ── ④ 链上：MVRV / 交易所余额 / 活跃地址 ──────────────
    rows = q("""
        SELECT metric_date, cap_mvrv_cur, sply_ex_usd, flow_in_ex_usd, flow_out_ex_usd,
               adr_act_cnt, roi_30d, price_usd
        FROM biz.cm_asset_onchain_daily WHERE cm_symbol = 'btc'
        ORDER BY metric_date DESC LIMIT 2
    """)
    if rows:
        d0 = rows[0]
        onchain = {
            "date": d0[0],
            "mvrv": float(d0[1]) if d0[1] is not None else None,
            "sply_ex_usd": float(d0[2]) if d0[2] is not None else None,
            "flow_in_ex_usd": float(d0[3]) if d0[3] is not None else None,
            "flow_out_ex_usd": float(d0[4]) if d0[4] is not None else None,
            "adr_act_cnt": int(d0[5]) if d0[5] is not None else None,
            "roi_30d": float(d0[6]) if d0[6] is not None else None,
            "price_usd": float(d0[7]) if d0[7] is not None else None,
        }
        # 净流入（正=转入交易所，潜在抛压；负=转出，囤币）
        if onchain["flow_in_ex_usd"] is not None and onchain["flow_out_ex_usd"] is not None:
            onchain["net_ex_flow_usd"] = onchain["flow_in_ex_usd"] - onchain["flow_out_ex_usd"]
        else:
            onchain["net_ex_flow_usd"] = None
        # 已实现价格 = 价格 / MVRV（推算口径，MVRV = 市值/已实现市值）
        if onchain["mvrv"] and onchain["mvrv"] > 0 and onchain["price_usd"]:
            onchain["realized_price_usd"] = onchain["price_usd"] / onchain["mvrv"]
        else:
            onchain["realized_price_usd"] = None
        # 交易所余额 7d 变化（若有 2 天）
        if len(rows) > 1 and rows[1][2] and d0[2]:
            onchain["sply_ex_chg7d"] = float(d0[2]) / float(rows[1][2]) - 1.0
        else:
            onchain["sply_ex_chg7d"] = None
        ctx["onchain"] = onchain
    else:
        ctx["onchain"] = None

    # MVRV 全历史分位（更稳的牛熊温度计）
    rows = q("""
        SELECT metric_date, mvrv_pct_full, mvrv_extreme
        FROM biz.cm_onchain_percentile_full
        WHERE cm_symbol = 'btc' AND mvrv_pct_full IS NOT NULL
        ORDER BY metric_date DESC LIMIT 1
    """)
    if rows:
        ctx["mvrv_pct"] = {
            "date": rows[0][0],
            "pct_full": float(rows[0][1]) if rows[0][1] is not None else None,
            "extreme": rows[0][2],
        }
    else:
        ctx["mvrv_pct"] = None

    # ── ④ 宏观：恐贪 / 市值占比 / 稳定币 / ETF / 机构情绪 / 周期 ──
    rows = q("SELECT metric_date, value, value_class, source_code FROM biz.fear_greed_daily "
             "ORDER BY metric_date DESC LIMIT 1")
    if rows:
        fg = {"date": rows[0][0], "value": int(rows[0][1]), "cls": rows[0][2],
              "src": rows[0][3] or "unknown"}
        # 恐贪历史分位（全历史）
        fg_rows = q("SELECT value FROM biz.fear_greed_daily "
                    "WHERE value IS NOT NULL ORDER BY metric_date ASC")
        if len(fg_rows) > 30:
            vals = [int(r[0]) for r in fg_rows]
            fg["pct"] = 100.0 * sum(1 for x in vals[:-1] if x < vals[-1]) / (len(vals) - 1)
        ctx["fear_greed"] = fg
    else:
        ctx["fear_greed"] = None

    rows = q("SELECT metric_date, total_market_cap, btc_dominance, stablecoin_market_cap "
             "FROM biz.global_metric_daily ORDER BY metric_date DESC LIMIT 1")
    if rows:
        ctx["global"] = {
            "date": rows[0][0],
            "total_mcap": float(rows[0][1]) if rows[0][1] is not None else None,
            "btc_dom": float(rows[0][2]) if rows[0][2] is not None else None,
            "stable_mcap": float(rows[0][3]) if rows[0][3] is not None else None,
        }
    else:
        ctx["global"] = None

    rows = q("SELECT metric_date, total_supply_usd, net_flow_usd "
             "FROM biz.stablecoin_supply_daily ORDER BY metric_date DESC LIMIT 2")
    if rows:
        ctx["stablecoin"] = {
            "date": rows[0][0],
            "total_usd": float(rows[0][1]) if rows[0][1] is not None else None,
            "net_flow_usd": float(rows[0][2]) if rows[0][2] is not None else None,
        }
    else:
        ctx["stablecoin"] = None

    rows = q("""
        SELECT flow_date, SUM(net_flow_usd)
        FROM biz.etf_flow_daily
        WHERE symbol = 'BTC' AND flow_date >= CURRENT_DATE - 8
        GROUP BY flow_date ORDER BY flow_date DESC
    """)
    if rows:
        days = [(r[0], float(r[1])) for r in rows if r[1] is not None]
        week = days[:5]  # 最近 5 个交易日 = 完整交易周（与 Farside 周度口径对齐）
        ctx["etf"] = {
            "days": days,
            "sum_week": sum(x for _, x in week) if week else None,
            "n_week": len(week),
            "week_span": (week[-1][0], week[0][0]) if week else None,  # (起始, 结束) 升序
            "latest": week[0] if week else None,
        }
    else:
        ctx["etf"] = None

    # BTC 1h 最新收盘（作「现价」参考基准，统一各数据源时点差异的展示）
    rows = q("""
        SELECT close_px FROM biz.asset_klines
        WHERE symbol = 'BTCUSDT' AND interval = '1h' AND close_px > 0
        ORDER BY open_time DESC LIMIT 1
    """)
    ctx["btc_ref_price"] = float(rows[0][0]) if rows and rows[0][0] is not None else None

    rows = q("SELECT metric_date, value FROM biz.cefi_index_daily "
             "ORDER BY metric_date DESC LIMIT 1")
    ctx["cefi"] = {"date": rows[0][0], "value": float(rows[0][1])} if rows else None

    rows = q("""
        SELECT snapshot_date, btc_cycle_phase, btc_cycle_label
        FROM biz.market_snapshot_daily ORDER BY snapshot_date DESC LIMIT 1
    """)
    if rows:
        ctx["cycle"] = {"date": rows[0][0], "phase": rows[0][1], "label": rows[0][2]}
    else:
        ctx["cycle"] = None

    # ── 期权市场（Deribit 免费快照）──────────────────────────
    rows = q("""
        SELECT metric_date, spot_price, dvol_30d, call_oi, put_oi, pcr,
               max_pain_strike, nearest_expiry
        FROM biz.btc_options_snapshot ORDER BY metric_date DESC LIMIT 1
    """)
    if rows:
        r = rows[0]
        ctx["options"] = {
            "date": r[0],
            "spot": float(r[1]) if r[1] is not None else None,
            "dvol": float(r[2]) if r[2] is not None else None,
            "call_oi": float(r[3]) if r[3] is not None else None,
            "put_oi": float(r[4]) if r[4] is not None else None,
            "pcr": float(r[5]) if r[5] is not None else None,
            "max_pain": float(r[6]) if r[6] is not None else None,
            "nearest_expiry": r[7],
        }
    else:
        ctx["options"] = None

    # ── 宏观 + 跨资产联动（Yahoo 免费序列 + 滚动相关性）────────
    rows = q("""
        SELECT metric_date, dxy, us10y, nasdaq, gold
        FROM biz.macro_market_daily
        WHERE metric_date >= CURRENT_DATE - 120
        ORDER BY metric_date ASC
    """)
    if rows:
        macro = [{"date": r[0],
                  "dxy": float(r[1]) if r[1] is not None else None,
                  "us10y": float(r[2]) if r[2] is not None else None,
                  "nasdaq": float(r[3]) if r[3] is not None else None,
                  "gold": float(r[4]) if r[4] is not None else None}
                 for r in rows]
        ctx["macro"] = macro
        # 相关性需要 BTC 1d 收盘
        btc_rows = q("""
            SELECT open_time::date, close_px FROM biz.asset_klines
            WHERE symbol = 'BTCUSDT' AND interval = '1d'
              AND open_time >= CURRENT_DATE - 120 AND close_px > 0
            ORDER BY open_time ASC
        """)
        btc_by_date = {r[0]: float(r[1]) for r in btc_rows}
        ctx["corr"] = _compute_correlations(btc_by_date, macro)
    else:
        ctx["macro"] = None
        ctx["corr"] = None

    # ── 链上近似：LTH/STH 花费代理 + 矿工维度（现库 OBM）────────
    obm_names = (
        "obm_spent_value_lt155d_btc_daily",
        "obm_spent_value_ge155d_btc_daily",
        "obm_liveliness_ratio_daily",
        "obm_cdd_btcxdays_daily",
        "obm_miner_revenue_btc_daily",
        "obm_issuance_btc_daily",
    )
    rows = q("""
        SELECT metric_date, metric_name, value FROM biz.obm_btc_daily
        WHERE metric_name = ANY(%s) AND metric_date >= CURRENT_DATE - 10
        ORDER BY metric_date DESC
    """, (list(obm_names),))
    obm_by_date: dict = {}
    for d, name, val in rows:
        if val is None:
            continue
        obm_by_date.setdefault(d, {})[name] = float(val)
    if obm_by_date:
        latest_date = max(obm_by_date)
        ctx["obm"] = obm_by_date[latest_date]
        ctx["obm_date"] = latest_date
    else:
        ctx["obm"] = None
        ctx["obm_date"] = None
    return ctx


# ---------------------------------------------------------------- 多因子共振摘要（日线场景）

def _regime_dir(verdict: str) -> int:
    """盘面结构方向：趋势向上 +1 / 趋势向下 -1 / 其他 0。"""
    if not verdict:
        return 0
    if verdict.startswith("趋势向上"):
        return 1
    if verdict.startswith("趋势向下"):
        return -1
    return 0


def build_factor_summary(rows: list[dict], ctx: dict) -> str:
    """日线大方向场景（VP + OI + 费率 + 200MA + 量能）多因子共振摘要。

    返回多行文本（每行一个因子 + 结论行）；无数据时对应因子标注 N/A，不做臆断。
    """
    d1 = next((r for r in rows if r["interval_label"] == "1d"), None)
    h1 = next((r for r in rows if r["interval_label"] == "1h"), None)
    lines: list[str] = []
    scores: list[int] = []
    notes: list[str] = []

    # 1) VP 盘面结构（日线）
    verdict = (d1 or h1 or {}).get("regime", {}).get("verdict", "")
    d = _regime_dir(verdict)
    scores.append(d)
    lines.append(f"- 盘面结构（VP 价值区/POC）：**{verdict or 'N/A'}**"
                 f"{'（多头结构）' if d > 0 else ('（空头结构）' if d < 0 else '（中性/分化）')}")

    # 2) 200MA 牛熊分界（日线）
    ma200 = None
    close = None
    if d1 and d1.get("indicators"):
        ma200 = d1["indicators"].get("ma200")
        close = d1.get("close")
    if ma200 and close:
        above = close >= ma200
        scores.append(1 if above else -1)
        lines.append(f"- 200MA 牛熊分界（1d）：现价 **{_fmt(close)}** "
                     f"{'在' if above else '跌破'} 200 日线 **{_fmt(ma200)}**"
                     f"（{'牛市区间' if above else '熊市区间'}）")
    else:
        lines.append("- 200MA 牛熊分界（1d）：N/A（日线数据不足 200 根）")

    # 3) OI（跨所）与价格同向性
    oi = ctx.get("oi")
    price7d = None
    if d1 and d1.get("chg_7d") is not None:
        price7d = d1["chg_7d"]
    elif h1 and h1.get("chg_7d") is not None:
        price7d = h1["chg_7d"]
    if oi and oi.get("value_usd"):
        oi7d = oi.get("chg7d")
        txt = (f"- OI（跨所）：**{_fmt_usd_b(oi['value_usd'])}**"
               f"（{oi['ts']:%m-%d %H:%M} UTC）")
        if oi7d is not None:
            txt += f" ｜ 7d {_pct(oi7d)}"
        if oi.get("chg24h") is not None:
            txt += f" ｜ 24h {_pctv(oi.get('chg24h'), 2)}"
        lines.append(txt)
        # 解读
        if oi7d is not None and price7d is not None:
            if oi7d > 0.02 and price7d > 0:
                scores.append(1)
                lines.append("  └ OI 增 + 价涨 → 新增多头开仓，趋势健康（多头进场）")
            elif oi7d > 0.02 and price7d < 0:
                scores.append(-1)
                lines.append("  └ OI 增 + 价跌 → 新增空头开仓，空头变强")
            elif oi7d < -0.02:
                scores.append(0)
                lines.append("  └ OI 快速下降 → 大量平仓/去杠杆，行情临近拐点（方向待定）")
            else:
                scores.append(0)
                lines.append("  └ OI 变化温和 → 持仓变动有限（中性）")
        else:
            lines.append("  └（缺 7d 价格变动，无法判 OI×价格同向）")
    else:
        lines.append("- OI（跨所）：N/A")

    # 4) 资金费率（8h，Binance）
    fund = ctx.get("funding")
    if fund and fund.get("rate") is not None:
        r = fund["rate"]
        avg7 = fund.get("avg7d")
        txt = (f"- 资金费率（8h，Binance）：**{_pct_px(r)}**"
               f"（{fund['ts']:%m-%d %H:%M} UTC）")
        if avg7 is not None:
            txt += f" ｜ 近 {fund.get('n_days', 8)} 点均值 {_pct_px(avg7)}"
        lines.append(txt)
        if r > 0.0005:
            scores.append(-1)
            lines.append("  └ 正费率偏高 → 多头拥挤（泡沫积累），计为偏空风险票，警惕过热回调（费率高≠马上跌）")
        elif r > 0:
            scores.append(1)
            lines.append("  └ 正费率 → 市场偏多（多头付费）")
        elif r < -0.0005:
            scores.append(-1)
            lines.append("  └ 负费率 → 市场偏空/恐慌（空头付费），极端值常是反弹前兆")
        else:
            scores.append(-1)
            lines.append("  └ 负费率 → 市场偏空")
        if avg7 is not None and r * avg7 < 0:
            notes.append("费率近期由" + ("正转负" if r < 0 else "负转正") + "，是常见变盘窗口")
    else:
        lines.append("- 资金费率：N/A")

    # 5) 量能（1d 最后一根 vs 前 20 根均量）
    vr = None
    if d1 and d1.get("indicators"):
        vr = d1["indicators"].get("vol_ratio")
    if vr is not None:
        vol_txt = ("放量" if vr >= 1.2 else ("缩量" if vr < 0.7 else "平量"))
        lines.append(f"- 量能（1d 末根 vs 前 20 根均量）：**{vr:.2f}×（{vol_txt}）**")
        # 放量需配合结构方向
        if vr >= 1.2 and d > 0:
            scores.append(1)
        elif vr >= 1.2 and d < 0:
            scores.append(-1)
        else:
            scores.append(0)
        if vr < 0.7:
            notes.append("缩量状态下突破存疑，防假突破")
    else:
        lines.append("- 量能（1d）：N/A")

    # 结论：需要 ≥2 票的净方向差才判定「共振」，1:2 或 1:1 之类小差归为中性偏谨慎，
    # 避免「1 多 2 偏空」也被夸大成方向共振（2026-10-13 审计修正）。
    pos = sum(1 for s in scores if s > 0)
    neg = sum(1 for s in scores if s < 0)
    if pos - neg >= 2:
        verdict_line = "多因子偏多共振（多票净差≥2，看多信号明显占优），但仍需价格结构确认，勿追高。"
    elif neg - pos >= 2:
        verdict_line = "多因子偏空共振（空票净差≥2，看空信号明显占优），注意下方 HVN/VA 低承接，勿盲目抄底。"
    else:
        verdict_line = "多因子信号中性/偏谨慎（多空票差 <2，方向未形成明显共振），以观望为主，等待价值区方向选择。"
    if notes:
        verdict_line += " ｜ 关注：" + "；".join(notes) + "。"
    lines.append("")
    lines.append(f"**→ 多因子共振结论：{verdict_line}**")
    return "\n".join(lines)


def build_risk_factors(rows: list[dict], ctx: dict) -> list[str]:
    """集中罗列当前可识别的风险因子（来自各维度最新值，全部可归因、不臆断）。"""
    risks: list[str] = []
    fund = ctx.get("funding")
    if fund and fund.get("rate") is not None and fund["rate"] > 0.0005:
        risks.append(f"多头拥挤：资金费率 {_pct_px(fund['rate'])}/8h 偏高，上冲易触发多杀多")
    oi = ctx.get("oi")
    if oi and oi.get("chg7d") is not None and oi["chg7d"] < -0.02:
        risks.append(f"去杠杆：OI 7d {_pct(oi['chg7d'])}，突破需新增持仓配合，否则易假突破")
    etf = ctx.get("etf")
    if etf and etf.get("sum_week") is not None and etf["sum_week"] < 0:
        risks.append(f"ETF 资金流出：最近交易周净流出 {_fmt_usd_b(abs(etf['sum_week']))}，机构承接偏弱")
    corr = ctx.get("corr") or {}
    nas = corr.get("btc_vs_nasdaq_30d")
    if nas is not None and nas > 0.5:
        risks.append(f"纳指联动强：BTC×纳指 30d 相关性 {nas:+.2f}，美股回调会拖累 BTC")
    dxy = corr.get("btc_vs_dxy_30d")
    if dxy is not None and dxy < -0.3:
        risks.append(f"美元压制：BTC×DXY 30d 相关性 {dxy:+.2f}，美元/美债走强对 BTC 不利")
    op = ctx.get("options")
    if op and op.get("max_pain") and op.get("spot") and op["spot"] < op["max_pain"]:
        risks.append(f"期权压制：max pain {_fmt(op['max_pain'])} 高于现货，临近交割有向痛点收敛倾向")
    d1 = next((r for r in rows if r["interval_label"] == "1d"), None)
    vr = (d1 or {}).get("indicators", {}).get("vol_ratio")
    if vr is not None and vr < 0.7:
        risks.append(f"缩量：1d 量能仅 {vr:.2f}× 均量，当前突破/反弹存疑")
    if not risks:
        return ["当前未识别出显著集中风险因子（多空风险相对均衡）。"]
    return risks


# ---------------------------------------------------------------- 摘要与图表

def _verdict_code(v: str) -> str:
    if not v:
        return "mix"
    if v.startswith("趋势向上"):
        return "up"
    if v.startswith("趋势向下"):
        return "down"
    if v.startswith("震荡"):
        return "range"
    return "mix"


def build_summary(rows: list[dict]) -> str:
    d = {r["interval_label"]: r for r in rows}
    h1 = d.get("1h")
    d1 = d.get("1d")
    if h1 and d1:
        a = _verdict_code(h1["regime"].get("verdict"))
        b = _verdict_code(d1["regime"].get("verdict"))
        if a == "up" and b == "up":
            return "多周期共振向上：1h 与 1d 均站上价值区，短线与波段结构同向，趋势延续（注意追高风险）。"
        if a == "down" and b == "down":
            return "多周期共振向下：1h 与 1d 均跌破价值区，短线与波段结构同向承压（关注下方 HVN/VA 低承接）。"
        if a == "up" and b in ("range", "mix"):
            return "短线反弹、波段未确认：1h 站上价值区，1d 仍在区间内，暂看反弹修复，突破 1d VA 高才算波段转强。"
        if a == "down" and b in ("range", "mix"):
            return "短线走弱、波段未破位：1h 跌破价值区，1d 仍在区间内，警惕假摔，关注 1d VA 低/支撑承接。"
        if a == "range" and b == "up":
            return "短线盘整、波段向上：1h 在价值区内蓄势，1d 趋势向上，回踩 1h VA 低/支撑可视为顺势观察点。"
        if a == "range" and b == "down":
            return "短线盘整、波段偏空：1h 在价值区内，1d 趋势向下，反弹至 1h VA 高/阻力注意压力。"
        if a == "range" and b == "range":
            return "多周期共振震荡：1h 与 1d 均处价值区，方向不明，按区间上下沿高抛低吸，突破区间再看方向。"
        return "多周期信号分化：短线与波段结构不一致，建议观望，等待价值区方向选择。"
    if h1:
        a = _verdict_code(h1["regime"].get("verdict"))
        return {
            "up": "短线结构站上价值区，趋势向上（注意追高风险）。",
            "down": "短线结构跌破价值区，趋势承压（关注下方 HVN/VA 低承接）。",
            "range": "短线处于价值区震荡，方向不明，区间内高抛低吸。",
        }.get(a, "短线结构信号混合，建议观望。")
    if d1:
        b = _verdict_code(d1["regime"].get("verdict"))
        return {
            "up": "波段结构站上价值区，趋势向上。",
            "down": "波段结构跌破价值区，趋势承压。",
            "range": "波段处于价值区震荡，方向不明。",
        }.get(b, "波段结构信号混合，建议观望。")
    return "（无足够数据生成摘要）"


def render_chart(vp: dict, max_rows: int = 32) -> list[str]:
    bins = vp.get("bins") or []
    if not bins:
        return ["  （无 profile 数据）"]
    n = len(bins)
    step = max(1, n // max_rows)
    sample = bins[::step]
    maxv = max(b["volume"] for b in bins) or 1.0
    rw = vp.get("row_width") or 0.0
    va_lo, va_hi = vp.get("va_low"), vp.get("va_high")
    poc = vp.get("poc")
    sup = (vp.get("support") or {}).get("price")
    res = (vp.get("resistance") or {}).get("price")
    hvn_px = [x for x in (sup, res) if x]
    lvn_px = vp.get("lvn") or []
    width = 30
    lines = []
    for b in sample:
        px = b["price"]
        bar = "█" * max(1, round(b["volume"] / maxv * width))
        mark = "│" if (va_lo is not None and va_hi is not None and va_lo <= px <= va_hi) else " "
        tag = ""
        if poc is not None and abs(px - poc) <= rw / 2:
            tag = "  ◆POC"
        elif any(abs(px - x) <= rw / 2 for x in hvn_px):
            tag = "  ▲支撑/阻力"
        elif any(abs(px - x) <= rw / 2 for x in lvn_px):
            tag = "  ▼LVN"
        lines.append(f"{px:>13,.4f} {mark}{bar}{tag}")
    return lines


# ---------------------------------------------------------------- 新增段落渲染（md）

def _ma_txt(ind: dict, close: float) -> str:
    """MA20/50/200 相对现价 → '20↑ 50↑ 200↓'。"""
    parts = []
    for k in ("ma20", "ma50", "ma200"):
        m = ind.get(k)
        if m is None:
            parts.append(f"{k[2:]}–")
        else:
            parts.append(f"{k[2:]}{'↑' if close >= m else '↓'}")
    return " ".join(parts)


def _macd_txt(ind: dict) -> str:
    hist = ind.get("hist")
    dif, dea = ind.get("dif"), ind.get("dea")
    if hist is None or dif is None or dea is None:
        return "N/A"
    state = "多头" if hist > 0 else "空头"
    cross = ind.get("cross")
    if cross:
        return f"{state}（{cross}）"
    return state


def render_indicators_table(rows: list[dict]) -> list[str]:
    lines = ["| 周期 | MA20/50/200 | RSI | MACD | %B | ATR% | 现价 vs VWAP | 量能 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ind = r.get("indicators") or {}
        close = r.get("close")
        iv = r["interval_label"]
        ma = _ma_txt(ind, close) if close else "N/A"
        rsi = ind.get("rsi")
        rsi_txt = f"{rsi:.1f}" if rsi is not None else "N/A"
        if rsi is not None and rsi >= 70:
            rsi_txt += " 超买"
        elif rsi is not None and rsi <= 30:
            rsi_txt += " 超卖"
        macd_txt = _macd_txt(ind)
        boll = ind.get("boll")
        if boll:
            pb = percent_b(close, boll[1], boll[2])
            bw = bbw(boll[0], boll[1], boll[2])
            boll_txt = f"{pb:.2f}" + (f"（带宽{bw * 100:.1f}%）" if bw else "")
        else:
            boll_txt = "N/A"
        atr = ind.get("atr")
        atr_txt = f"{atr / close * 100:.2f}%" if (atr and close) else "N/A"
        vwap = ind.get("vwap")
        if vwap and close:
            vwap_txt = f"{'上' if close >= vwap else '下'}方（{abs(close / vwap - 1) * 100:.2f}%）"
        else:
            vwap_txt = "N/A"
        vr = ind.get("vol_ratio")
        if vr is not None:
            vol_txt = f"{vr:.2f}×" + (" 放量" if vr >= 1.2 else (" 缩量" if vr < 0.7 else " 平量"))
        else:
            vol_txt = "N/A"
        lines.append(f"| {iv} | {ma} | {rsi_txt} | {macd_txt} | {boll_txt} | "
                     f"{atr_txt} | {vwap_txt} | {vol_txt} |")
    return lines


def _lag_days(dt, now) -> int:
    if dt is None:
        return 999
    if hasattr(dt, "date"):
        dt = dt.date() if getattr(dt, "date", None) else dt
    return (now.date() - dt).days


def render_market_md(ctx: dict, generated_at: datetime) -> list[str]:
    now = generated_at
    lines: list[str] = []

    # ── ② 合约持仓与资金 ──
    lines.append("## 合约持仓与资金（衍生品）")
    lines.append("")
    oi = ctx.get("oi")
    if oi:
        lag = _lag_days(oi["ts"], now)
        lines.append(f"- **OI（跨所，CoinGlass）**：{_fmt_usd_b(oi['value_usd'])}"
                     f"（{oi['ts']:%m-%d %H:%M} UTC）"
                     + (f"｜24h {_pctv(oi['chg24h'])}" if oi.get("chg24h") is not None else "")
                     + (f"｜7d {_pct(oi['chg7d'])}" if oi.get("chg7d") is not None else "")
                     + (" ｜⏳ 滞后" if lag > MAX_LAG_DAYS else ""))
    else:
        lines.append("- OI（跨所）：N/A")
    op = ctx.get("oi_pct60")
    if op and op.get("pct") is not None:
        lines.append(f"  └ OI 近 {op['n']} 日历史分位（Binance 口径，单位 BTC）：{op['pct']:.0f}%"
                     f"（现值 {op['value']:,.0f} BTC）"
                     + ("，偏高区" if op["pct"] >= 80 else ("，偏低区" if op["pct"] <= 20 else "")))
    fund = ctx.get("funding")
    if fund and fund.get("rate") is not None:
        r = fund["rate"]
        tag = ""
        if r > 0.0005:
            tag = "（偏高，多头拥挤）"
        elif r > 0:
            tag = "（偏多）"
        elif r < -0.0005:
            tag = "（偏低，偏空/恐慌）"
        else:
            tag = "（偏空）"
        lines.append(f"- **资金费率（8h，Binance）**：{_pct_px(r)}{tag}"
                     f"（{fund['ts']:%m-%d %H:%M} UTC）"
                     + (f"｜近{fund.get('n_days', 8)}点均值 {_pct_px(fund['avg7d'])}"
                        if fund.get("avg7d") is not None else "")
                     + (f"｜近{fund.get('n_days', 8)}点分位 {fund['pct30']:.0f}%"
                        if fund.get("pct30") is not None else ""))
    else:
        lines.append("- 资金费率：N/A")
    lsr = ctx.get("lsr")
    if lsr:
        tp = lsr.get("top_position") or {}
        ta = lsr.get("top_account") or {}
        ga = lsr.get("global_account") or {}
        ts = tp.get("ts") or ta.get("ts")
        lag = _lag_days(ts, now) if ts else 999
        def _ratio(d):
            return f"{d['ratio']:.2f}" if d.get("ratio") is not None else "N/A"
        lines.append(f"- **多空比（Binance，CoinGlass）**：大户持仓 {_ratio(tp)}"
                     f" ｜ 大户账户 {_ratio(ta)} ｜ 全体账户 {_ratio(ga)}"
                     + (f"（{ts:%m-%d} UTC" if ts else "") + ("，⏳滞后" if lag > MAX_LAG_DAYS else "") + "）")
    else:
        lines.append("- 多空比：N/A（表无数据）")
    basis = ctx.get("basis")
    if basis:
        lag = _lag_days(basis["ts"], now)
        b = basis["close_pct"]
        tag = "期货升水（乐观）" if b > 0 else "期货贴水（悲观）"
        lines.append(f"- **基差（BTCUSDT 4h，CoinGlass）**：{b:+.2f}%（{tag}）"
                     f"（{basis['ts']:%m-%d} UTC" + ("，⏳滞后" if lag > MAX_LAG_DAYS else "") + "）")
    else:
        lines.append("- 基差：N/A")
    liq = ctx.get("liq")
    if liq:
        total = liq["total"]
        long_share = liq["long_share"]
        tag = ""
        if liq.get("bucket"):
            tag = f"｜极端方向：{liq['bucket']}"
        txt = f"- **爆仓（全所，24h regime）**：{_fmt_usd_b(total)}"
        if long_share is not None:
            txt += f"｜多头占爆仓 {long_share * 100:.1f}%"
            if long_share > 0.7:
                txt += "（多头被集中清算=砸盘）"
            elif long_share < 0.3:
                txt += "（空头被集中清算=轧空）"
        txt += tag
        lines.append(txt)
    else:
        lines.append("- 爆仓：N/A")
    liq4 = ctx.get("liq_4h")
    if liq4:
        total4 = liq4["long"] + liq4["short"]
        lines.append(f"  └ 近 4h 明细：多 {_fmt_usd_b(liq4['long'])}"
                     f" ｜ 空 {_fmt_usd_b(liq4['short'])}"
                     f"（合计 {_fmt_usd_b(total4)}，{liq4['ts']:%m-%d %H:%M} UTC）")
    lines.append("")

    # ── 期权市场（Deribit 免费快照）─────────────────────────────
    lines.append("## 期权市场（Deribit 免费快照）")
    lines.append("")
    op = ctx.get("options")
    if op:
        lag = _lag_days(op["date"], now)
        spot = op.get("spot")
        dvol = op.get("dvol")
        ref = ctx.get("btc_ref_price")
        txt = f"- **现货指数**：{_fmt(spot)}（{op['date']:%m-%d}"
        if op.get("nearest_expiry"):
            txt += f"，最近到期 {op['nearest_expiry']:%m-%d}"
        txt += ("，⏳滞后" if lag > MAX_LAG_DAYS else "") + "）"
        # Deribit 指数为多所口径且为快照时点值，与 Binance 现价偏差过大时明确标注（2026-10-13 审计）
        if spot and ref:
            dev = spot / ref - 1.0
            if abs(dev) > 0.01:
                txt += f" ｜ ⚠️ 与 Binance 现价偏差 {dev * 100:+.1f}%（{_fmt(ref)}，快照时点差异/滞后）"
            else:
                txt += f" ｜ vs Binance 现价 {_fmt(ref)}（{dev * 100:+.2f}%）"
        lines.append(txt)
        if dvol is not None:
            iv_tag = "（偏低，期权市场低估波动）" if dvol < 30 else ("（偏高，恐慌/波动预期大）" if dvol > 60 else "（中性）")
            lines.append(f"- **DVOL 30d 隐含波动率**：{dvol:.1f}%{iv_tag}")
        if op.get("call_oi") is not None and op.get("put_oi") is not None:
            pcr = op.get("pcr")
            pcr_tag = ""
            if pcr is not None:
                pcr_tag = "（Put<Call，持仓偏多）" if pcr < 0.8 else ("（Put>Call，看跌对冲偏重）" if pcr > 1.2 else "（中性）")
            lines.append(f"- **OI**：Call {op['call_oi']:,.0f} ｜ Put {op['put_oi']:,.0f}"
                         f" ｜ PCR(put/call) **{pcr:.2f}**{pcr_tag}" if pcr is not None
                         else f"- **OI**：Call {op['call_oi']:,.0f} ｜ Put {op['put_oi']:,.0f}")
        if op.get("max_pain") is not None:
            mp = op["max_pain"]
            rel = ""
            if spot:
                rel = "（现货在其" + ("上方" if spot > mp else "下方") + "）"
            lines.append(f"- **最大痛点(max pain)**：{_fmt(mp)}{rel}"
                         "（临近交割价格向 OI 最大痛点收敛是经验现象，非规律）")
        lines.append("- 说明：期权 OI/PCR 是**持仓与对冲结构**，DVOL 是波动预期，均不是方向信号；"
                     "Deribit 名义口径，跨所不可直接相加。")
    else:
        lines.append("- 期权数据：N/A（未接入或表空，运行 ingest_btc_options.py）")
    lines.append("")

    # ── ④ 链上与宏观 ──
    lines.append("## 链上与宏观")
    lines.append("")
    oc = ctx.get("onchain")
    if oc:
        lag = _lag_days(oc["date"], now)
        mvrv_txt = f"{oc['mvrv']:.2f}" if oc.get("mvrv") is not None else "N/A"
        mvrv_tag = ""
        if oc.get("mvrv") is not None:
            m = oc["mvrv"]
            mvrv_tag = "（偏高）" if m > 3 else ("（中性偏低）" if m < 1 else "（中性）")
        rp_txt = _fmt(oc["realized_price_usd"]) if oc.get("realized_price_usd") else "N/A"
        lines.append(f"- **MVRV**：{mvrv_txt}{mvrv_tag}"
                     f"｜已实现价格（推算=CoinMetrics链上价/MVRV，{oc['date']:%m-%d}快照）{rp_txt}"
                     + (" ｜⏳滞后" if lag > MAX_LAG_DAYS else "")
                     + "（与现价为不同时点，勿直接互推）")
        sply = _fmt_usd_b(oc["sply_ex_usd"]) if oc.get("sply_ex_usd") else "N/A"
        netf = oc.get("net_ex_flow_usd")
        if netf is not None:
            netf_txt = _fmt_usd_b(abs(netf))
            netf_tag = "转入（抛压）" if netf > 0 else "转出（囤币）"
        else:
            netf_txt, netf_tag = "N/A", ""
        lines.append(f"- **交易所 BTC 余额**：{sply}"
                     + (f"（7d {_pct(oc['sply_ex_chg7d'])}" if oc.get("sply_ex_chg7d") is not None else "")
                     + "）"
                     + f"｜24h 净流 {netf_txt}（{netf_tag}）")
        if oc.get("adr_act_cnt"):
            lines.append(f"- 活跃地址：{oc['adr_act_cnt']:,}｜30d ROI："
                         + _pctv(oc.get("roi_30d")))
    else:
        lines.append("- 链上（MVRV/交易所余额）：N/A")
    mp = ctx.get("mvrv_pct")
    if mp and mp.get("pct_full") is not None:
        lines.append(f"- **MVRV 历史分位**：{mp['pct_full']:.0f}%（近全历史）"
                     + (f"｜极端标记：{mp['extreme']}" if mp.get("extreme") else ""))
    # 链上近似：LTH/STH 花费代理 + 矿工维度（现库 OBM，免费）
    obm = ctx.get("obm")
    if obm:
        lt = obm.get("obm_spent_value_lt155d_btc_daily")
        ge = obm.get("obm_spent_value_ge155d_btc_daily")
        if lt is not None and ge is not None and (lt + ge) > 0:
            sth_pct = lt / (lt + ge) * 100
            lines.append(f"- **持有者花费代理（OBM，近1日）**：短期(≤155d) {sth_pct:.1f}%"
                         f" ｜ 长期(>155d) {100 - sth_pct:.1f}%"
                         + ("（长期占比升高=老币换手/潜在派发，降低=囤币）"
                            if (100 - sth_pct) > 60 else "")
                         + (f"（{ctx['obm_date']:%m-%d}）"))
        liv = obm.get("obm_liveliness_ratio_daily")
        if liv is not None:
            lines.append(f"- Liveliness：{liv:.3f}（已实现资本活化度，越高=存量币越活跃）")
        cdd = obm.get("obm_cdd_btcxdays_daily")
        if cdd is not None:
            lines.append(f"- 币天销毁(CDD)：{cdd:,.0f}（上升=老币被移动，留意派发压力）")
        mr = obm.get("obm_miner_revenue_btc_daily")
        iss = obm.get("obm_issuance_btc_daily")
        if mr is not None:
            txt = f"- 矿工：收入 {mr:,.0f} BTC/日"
            if iss is not None:
                txt += f" ｜ 发行 {iss:,.0f} BTC/日"
            lines.append(txt)
    sc = ctx.get("stablecoin")
    if sc:
        lines.append(f"- **稳定币总供给（DefiLlama）**：{_fmt_usd_b(sc['total_usd'])}"
                     + (f"｜净流 {_fmt_usd_b(sc['net_flow_usd'])}" if sc.get("net_flow_usd") is not None else "")
                     + (f"（{sc['date']:%m-%d}）"))
    etf = ctx.get("etf")
    if etf and etf.get("sum_week") is not None:
        span = ""
        if etf.get("week_span"):
            span = f"（{etf['week_span'][0]:%m-%d}~{etf['week_span'][1]:%m-%d}，{etf['n_week']} 个交易日）"
        lines.append(f"- **BTC ETF 最近交易周净流入**：{_fmt_usd_b(etf['sum_week'])}{span}")
        lines.append("  └ 源：cryptoetf.today；与 Farside/SoSoValue 口径可能略有出入，周度=最近 5 个交易日")
    elif etf:
        lines.append("- BTC ETF 最近交易周净流入：N/A")
    fg = ctx.get("fear_greed")
    if fg:
        lines.append(f"- **恐贪指数**：{fg['value']}（{fg['cls']}，{fg['date']:%m-%d}，源{fg.get('src','')}）"
                     + (f"｜历史分位 {fg['pct']:.0f}%" if fg.get("pct") is not None else ""))
        lines.append("  └ 不同源口径差异较大（CMC/alternative.me 可差 10+ 点），以入库源为准")
    g = ctx.get("global")
    if g:
        lines.append(f"- **BTC 市值占比**：{g['btc_dom']:.2f}%"
                     f"（总市值 {_fmt_usd_b(g['total_mcap'])}）" if g.get("btc_dom") is not None else "- BTC 市值占比：N/A")
        if g.get("btc_dom") is not None:
            lines.append("  └ CMC 当日快照口径，与 Binance 现价/流通量为不同时点，存在约 1% 级口径差异")
    cyc = ctx.get("cycle")
    if cyc and cyc.get("label"):
        lines.append(f"- **BTC 周期阶段**：{cyc['label']}（{cyc['phase']}）")
    cefi = ctx.get("cefi")
    if cefi:
        lines.append(f"- **机构情绪指数（CEFI）**：{cefi['value']:.0f}/100（0-100，越高越乐观）")
    lines.append("")

    # ── 宏观与跨资产联动（Yahoo 免费序列 + 滚动相关性）──────────
    lines.append("## 宏观与跨资产联动")
    lines.append("")
    macro = ctx.get("macro") or []
    if macro:
        def _last_of(key):
            for m in reversed(macro):
                if m.get(key) is not None:
                    return m[key], m["date"]
            return None, None

        dxy_v, dxy_d = _last_of("dxy")
        us10y_v, us10y_d = _last_of("us10y")
        nas_v, nas_d = _last_of("nasdaq")
        gold_v, gold_d = _last_of("gold")

        def _tag(d):
            return " ⏳滞后" if _lag_days(d, now) > MAX_LAG_DAYS else ""

        parts = []
        if dxy_v is not None:
            parts.append(f"DXY **{dxy_v:.2f}**（{dxy_d:%m-%d}{_tag(dxy_d)}）")
        if us10y_v is not None:
            parts.append(f"美债10Y **{us10y_v:.2f}%**（{us10y_d:%m-%d}{_tag(us10y_d)}）")
        if nas_v is not None:
            parts.append(f"纳指 **{nas_v:,.0f}**（{nas_d:%m-%d}{_tag(nas_d)}）")
        if gold_v is not None:
            parts.append(f"黄金 **{gold_v:,.0f}**（{gold_d:%m-%d}{_tag(gold_d)}）")
        lines.append(f"- 最新：" + (" ｜ ".join(parts) if parts else "N/A"))
    corr = ctx.get("corr") or {}
    if corr:
        lines.append("- **BTC 日收益滚动相关性**：")
        for key, label in (
            ("btc_vs_nasdaq_30d", "vs 纳指 30d"), ("btc_vs_nasdaq_90d", "vs 纳指 90d"),
            ("btc_vs_gold_30d", "vs 黄金 30d"), ("btc_vs_gold_90d", "vs 黄金 90d"),
            ("btc_vs_dxy_30d", "vs DXY 30d"), ("btc_vs_dxy_90d", "vs DXY 90d"),
        ):
            v = corr.get(key)
            if v is not None:
                tag = "（强正相关，同向联动）" if v > 0.5 else ("（强负相关，反向）" if v < -0.5 else "")
                lines.append(f"  - {label}：{v:+.2f}{tag}")
    elif ctx.get("macro"):
        lines.append("- BTC 滚动相关性：数据不足（需 ≥20 天对齐样本）")
    else:
        lines.append("- 宏观/相关性：N/A（未接入，运行 ingest_macro_market.py）")
    if macro or corr:
        lines.append("- 说明：DXY/美债走强通常压制风险资产（BTC 负相关倾向），相关性是描述不是规律。")
    lines.append("")
    return lines


# ---------------------------------------------------------------- K线形态 / 关键价位 / 情景观察


def _patterns_txt(row: dict) -> str:
    """最近 3 根 K 线形态（中文，含需上下文形态的方向解读）。"""
    pats = row.get("patterns") or []
    ind = row.get("indicators") or {}
    trend5_up = ind.get("trend5_up")
    if not pats or not any(p["names"] for p in pats):
        return "未识别显著K线反转/持续形态（近3根）"
    parts = []
    for p in pats:
        if not p["names"]:
            continue
        labels = []
        for n in p["names"]:
            cn = PATTERN_CN.get(n, n)
            # 需要趋势上下文的形态：结合近 5 根方向补充解读
            if n == "hammer" and trend5_up is not None:
                cn += ("（下跌后，偏反转向上）" if not trend5_up else "（上涨中，注意滞涨）")
            elif n == "shooting_star" and trend5_up is not None:
                cn += ("（上涨后，偏反转向下）" if trend5_up else "（下跌中，留意支撑）")
            labels.append(cn)
        ago = "当前" if p["ago"] == 0 else f"前{p['ago']}根"
        parts.append(f"{ago}·{p['close']:,.0f}：{'、'.join(labels)}")
    return " ｜ ".join(parts)


def build_level_list(rows: list[dict]) -> dict:
    """从 1d/4h/1h 的 VP 提取关键价位，分两档：
      - `intraday`：4h + 1h 短周期（日内博弈关注位）
      - `swing`   ：1d 日线级别（波段关注位）
    LVN（低量真空区）**不是支撑/阻力**，单独收进 `lvn`（真空加速带）。
    去重：价格相近（≤ 现价的 0.15%）只保留来源优先、更接近现价的一个。
    """
    def _row(iv):
        return next((r for r in rows if r["interval_label"] == iv), None)
    d1, f4, h1 = _row("1d"), _row("4h"), _row("1h")
    price = None
    for r in (d1, f4, h1):
        if r and r.get("close"):
            price = r["close"]
            break
    swing: list[tuple[float, str]] = []  # (px, "1d POC") 波段
    intra: list[tuple[float, str]] = []  # (px, "4h VA下沿") 日内
    lvn: list[tuple[float, str]] = []

    def add_into(tgt, vp, src):
        if not vp:
            return
        for px, label in ((vp.get("poc"), "POC"),
                          (vp.get("va_low"), "VA下沿"),
                          (vp.get("va_high"), "VA上沿")):
            if px:
                tgt.append((px, f"{src} {label}"))
        sup, res = vp.get("support"), vp.get("resistance")
        if sup:
            tgt.append((sup["price"], f"{src} HVN支撑({sup['kind']})"))
        if res:
            tgt.append((res["price"], f"{src} HVN阻力({res['kind']})"))
        for x in (vp.get("lvn") or []):
            lvn.append((x, src))

    if d1:
        add_into(swing, d1.get("vp"), "1d")
    if f4:
        add_into(intra, f4.get("vp"), "4h")
    if h1:
        add_into(intra, h1.get("vp"), "1h")

    tol = price * 0.0015 if price else 1.0

    def dedup(cands, prefer):
        out: dict[int, tuple] = {}
        for px, label in sorted(cands, key=lambda c: (0 if c[1].startswith(prefer) else 1, c[1])):
            k = round(px / max(tol, 1e-9))
            out.setdefault(k, (px, label))
        return list(out.values())

    intra = dedup(intra, "4h")
    swing = dedup(swing, "1d")
    lvn_dedup: dict[int, tuple] = {}
    for px, src in lvn:
        k = round(px / max(tol, 1e-9))
        lvn_dedup.setdefault(k, (px, src))
    lvn = sorted(lvn_dedup.values(), key=lambda x: -x[0])

    def split(cands, ref):
        sup = sorted([c for c in cands if c[0] < ref], key=lambda c: -c[0])
        res = sorted([c for c in cands if c[0] > ref], key=lambda c: c[0])
        return sup, res

    # 分档参考价：日内档参考 4h/1h 现价，波段档参考 1d 收盘（两者在真实数据上接近）
    intra_price = ((f4 or h1) or {}).get("close") or price
    swing_price = (d1 or {}).get("close") or price
    isup, ires = split(intra, intra_price)
    ssup, sres = split(swing, swing_price)
    return {"price": price, "intraday_price": intra_price, "swing_price": swing_price,
            "intraday": {"supports": isup, "resistances": ires},
            "swing": {"supports": ssup, "resistances": sres},
            "lvn": lvn}


def _render_level_block(title: str, price, supports, resistances) -> list[str]:
    """渲染一个分档价位块（日内/波段），最近一档标 ★。"""
    lines = [f"**{title}**", ""]
    if not supports and not resistances:
        lines.append("（无）")
        lines.append("")
        return lines
    lines.append("| 方向 | 价位 | 来源 | 现价相对 | 关注 |")
    lines.append("|---|---|---|---|---|")
    for kind, cands in (("支撑", supports), ("阻力", resistances)):
        for i, (px, label) in enumerate(cands):
            dist = f"{(px / price - 1) * 100:+.2f}%" if price else "—"
            star = "★ 首要关注" if i == 0 else ""
            lines.append(f"| {kind} | **{_fmt(px)}** | {label} | {dist} | {star} |")
    lines.append("")
    return lines


def _render_levels_md(levels: dict) -> list[str]:
    """关键价位清单：日内（1h/4h）+ 波段（1d）两档，明确标注。"""
    price = levels.get("price")
    lines = [f"> 现价 {_fmt(price)}（{price is not None and '参考' or ''}）。"
             "日内看 1h/4h 短周期位，波段看 1d 位。", ""]
    lines.extend(_render_level_block("日内关键位（1h/4h 短周期）", levels.get("intraday_price"),
                                     levels["intraday"]["supports"],
                                     levels["intraday"]["resistances"]))
    lines.extend(_render_level_block("波段关键位（1d 日线级别）", levels.get("swing_price"),
                                     levels["swing"]["supports"],
                                     levels["swing"]["resistances"]))
    return lines


def _render_lvn_md(levels: dict) -> list[str]:
    """LVN 真空加速带：不是支撑/阻力，单独罗列避免误导。"""
    lvn = levels.get("lvn") or []
    if not lvn:
        return ["（无 LVN）"]
    lines = [f"- {_fmt(px)}（{src}）" for px, src in lvn]
    lines.append("> LVN 为**低量真空加速带**：不是支撑/阻力，价格穿过时易快速加速（缺乏承接/抛压），"
                 "勿当作支撑承接或阻力压制位。")
    return lines


def build_scenario_watch(rows: list[dict], ctx: dict, levels: dict) -> list[str]:
    """多情景推演：多头/空头确认条件、目标、失效条件（日线口径，纯 VP 价位推导）。"""
    d1 = next((r for r in rows if r["interval_label"] == "1d"), None)
    h1 = next((r for r in rows if r["interval_label"] == "1h"), None)
    d1_vp = (d1 or {}).get("vp") or {}
    h1_vp = (h1 or {}).get("vp") or {}
    sup = levels["swing"]["supports"]
    res = levels["swing"]["resistances"]
    lines: list[str] = []
    if not sup or not res:
        lines.append("（无足够 VP 关键位生成情景推演）")
        return lines

    price = levels["price"]
    # 多头锚：1d VA 上沿（未站上则作为突破位）；空头锚：1h VA 下沿（跌破则转空）
    bull_lv = d1_vp.get("va_high")
    bear_lv = h1_vp.get("va_low") or d1_vp.get("va_low")
    if bull_lv is None and res:
        bull_lv = res[0][0]
    if bear_lv is None and sup:
        bear_lv = sup[0][0]

    # 目标：锚上方/下方最近两个波段位（swing，1d 口径）
    bull_targets = [x for x in res if x[0] > bull_lv][:2]
    bear_targets = [x for x in sup if x[0] < bear_lv][:2]

    ind = (d1 or {}).get("indicators") or {}
    vr = ind.get("vol_ratio")
    vol_txt = f"且 1d 量能≥20日均量（当前 {vr:.2f}×）" if vr is not None else "且放量"

    bull_txt = (f"- 多头确认：1d 收盘站稳 **{_fmt(bull_lv)}**（{d1_vp.get('va_high') is not None and '1d VA 上沿' or '上方阻力'}）"
                f"{vol_txt}")
    if bull_targets:
        t1, t2 = bull_targets[0][0], (bull_targets[1][0] if len(bull_targets) > 1 else None)
        bull_txt += f"；第一目标 **{_fmt(t1)}**"
        if t2:
            bull_txt += f"，第二目标 **{_fmt(t2)}**"
    bull_txt += f"；失效：1d 收盘跌回 **{_fmt(bull_lv)}** 之下"
    lines.append(bull_txt)

    bear_txt = (f"- 空头确认：1d 收盘跌破 **{_fmt(bear_lv)}**（1h VA 下沿）{vol_txt}")
    if bear_targets:
        t1, t2 = bear_targets[0][0], (bear_targets[1][0] if len(bear_targets) > 1 else None)
        bear_txt += f"；第一目标 **{_fmt(t1)}**"
        if t2:
            bear_txt += f"，第二目标 **{_fmt(t2)}**"
    bear_txt += f"；失效：1d 收盘收回 **{_fmt(bear_lv)}** 之上"
    lines.append(bear_txt)

    # 辅助情景条件（来自多因子 ctx，不臆断方向）
    conds = []
    oi = ctx.get("oi")
    if oi and oi.get("chg7d") is not None:
        if oi["chg7d"] < -0.02:
            conds.append("OI 7d 收缩，突破需新增持仓配合，否则易假突破")
        elif oi["chg7d"] > 0.02:
            conds.append("OI 7d 扩张，若配合放量突破则趋势更可信")
    fund = ctx.get("funding")
    if fund and fund.get("rate") is not None and fund["rate"] > 0.0005:
        conds.append("费率偏高，上冲过程防多杀多（多头拥挤）")
    if conds:
        lines.append("- 辅助条件：" + "；".join(conds) + "。")
    lines.append(f"- 现价（1d 收盘）**{_fmt(price)}**："
                 + ("已站上" if price >= bull_lv else "尚未站上")
                 + f"多头确认位 {_fmt(bull_lv)}"
                 + ("，且" if price >= bull_lv else "，")
                 + ("已跌破" if price <= bear_lv else "尚未跌破")
                 + f"空头确认位 {_fmt(bear_lv)}（仅作位置参考，需收盘+量能确认）。")
    lines.append("- 说明：以上价位为 VP 静态筹码参考，触发需「收盘确认 + 量能验证」，单一价位不构成信号。")
    lines.append("- 适用口径：本清单为**日线级别规则**（以 1d 收盘确认），小时级别震荡博弈不适用本表。")
    return lines


def _tf_verdict(row: dict) -> dict:
    """单周期偏多/偏空判定：结构 + 均线 + VWAP + RSI + MACD + 布林 打分。"""
    reg = row.get("regime") or {}
    ind = row.get("indicators") or {}
    close = row.get("close")
    score = 0
    reasons: list[str] = []
    v = reg.get("verdict", "")
    if v.startswith("趋势向上"):
        score += 2
        reasons.append("结构趋势向上")
    elif v.startswith("趋势向下"):
        score -= 2
        reasons.append("结构趋势向下")
    else:
        reasons.append("结构震荡/待定")
    vwap = ind.get("vwap")
    if vwap and close:
        if close >= vwap:
            score += 1
            reasons.append("VWAP上方")
        else:
            score -= 1
            reasons.append("VWAP下方")
    for k in ("ma20", "ma50", "ma200"):
        m = ind.get(k)
        if m and close:
            score += 1 if close >= m else -1
    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi > 60:
            score += 1
            reasons.append(f"RSI {rsi:.0f}偏强")
        elif rsi < 40:
            score -= 1
            reasons.append(f"RSI {rsi:.0f}偏弱")
    hist = ind.get("hist")
    if hist is not None:
        if hist > 0:
            score += 1
            reasons.append("MACD多头")
        else:
            score -= 1
            reasons.append("MACD空头")
    boll = ind.get("boll")
    if boll and close:
        pb = percent_b(close, boll[1], boll[2])
        if pb > 0.8:
            score += 1
        elif pb < 0.2:
            score -= 1
    vr = ind.get("vol_ratio")
    if vr is not None:
        reasons.append("放量" if vr >= 1.2 else ("缩量" if vr < 0.7 else "平量"))
    if score >= 4:
        label = "偏多"
    elif score >= 2:
        label = "中性偏多"
    elif score <= -4:
        label = "偏空"
    elif score <= -2:
        label = "中性偏空"
    else:
        label = "中性"
    return {"label": label, "score": score, "reasons": reasons}


_TF_ORDER = ["5m", "15m", "1h", "4h", "1d", "1w"]


def build_tf_judgment(rows: list[dict]) -> list[str]:
    """分周期偏多/偏空判断表 + 一句综合结论。"""
    by = {r["interval_label"]: r for r in rows}
    ordered = [by[k] for k in _TF_ORDER if k in by]
    lines = ["| 周期 | 倾向 | 依据 |", "|---|---|---|"]
    for r in ordered:
        j = _tf_verdict(r)
        reason = "；".join(j["reasons"][:5]) or "—"
        lines.append(f"| {r['interval_label']} | **{j['label']}** | {reason} |")
    # 综合结论：短(1h/4h) / 日(1d) / 周(1w)
    j = {k: _tf_verdict(by[k]) for k in ("1h", "4h", "1d", "1w") if k in by}
    parts = []
    if "1h" in j or "4h" in j:
        s = [j[k]["score"] for k in ("1h", "4h") if k in j]
        avg = sum(s) / len(s)
        parts.append(f"短周期(1h/4h){'偏空' if avg <= -2 else ('偏多' if avg >= 2 else '中性')}"
                     f"（均分{avg:+.1f}）")
    if "1d" in j:
        parts.append(f"日线{j['1d']['label']}")
    if "1w" in j:
        parts.append(f"周线{j['1w']['label']}")
    if len(parts) >= 2:
        # 短空长多 / 短多长空 分化提示
        short_bear = any("偏空" in p for p in parts if p.startswith("短周期"))
        short_bull = any("偏多" in p for p in parts if p.startswith("短周期"))
        long_bull = "周线偏多" in parts or "周线中性偏多" in parts
        long_bear = "周线偏空" in parts or "周线中性偏空" in parts
        if short_bear and long_bull:
            parts.append("结构：短空长多（短线回调、中期趋势未破），日线级回调中段")
        elif short_bull and long_bear:
            parts.append("结构：短多长空（短线反弹、中期承压），反弹不宜当反转")
    lines.append("")
    lines.append("**综合：**" + " ｜ ".join(parts) + "。")
    lines.append("> 判定为多周期结构打分（规则性、观察性），非交易信号；需配合关键价位与情景清单使用。")
    return lines


# ---------------------------------------------------------------- 报告渲染

def render_markdown(rows: list[dict], ctx: dict, generated_at: datetime) -> str:
    lines = []
    lines.append("# BTC 大盘趋势分析（每日）")
    lines.append("")
    lines.append(f"> 生成时间：{_fmt_cn(generated_at)}（Asia/Shanghai）｜"
                 f"数据源：Binance USDT 永续 {SYMBOL}（biz.asset_klines）+ 衍生品/链上/宏观各表")
    lines.append("")
    lines.append("## 一句话摘要")
    lines.append("")
    lines.append(build_summary(rows))
    lines.append("")

    # 多因子共振摘要（日线大方向场景）
    lines.append("## 多因子共振摘要（日线场景：VP + OI + 费率 + 200MA + 量能）")
    lines.append("")
    lines.append(build_factor_summary(rows, ctx))
    lines.append("")

    # 风险因子提示（集中展示当前可归因风险）
    lines.append("## 风险因子提示")
    lines.append("")
    for rf in build_risk_factors(rows, ctx):
        lines.append(f"- {rf}")
    lines.append("")

    # 价格概况（用 1h 给短周期涨跌，1d 给 30d）
    h1 = next((r for r in rows if r["interval_label"] == "1h"), None)
    d1 = next((r for r in rows if r["interval_label"] == "1d"), None)
    price_row = h1 or d1 or rows[0]
    lines.append("## 价格概况")
    lines.append("")
    lines.append(f"- 现价：**{_fmt(price_row['close'])}**"
                 f"（{price_row['last_ts']:%Y-%m-%d %H:%M} UTC）")
    if h1:
        lines.append(f"- 24h：{_pct(h1['chg_24h'])} ｜ 7d：{_pct(h1['chg_7d'])}")
    if d1:
        lines.append(f"- 30d：{_pct(d1['chg_30d'])}")
        ind = d1.get("indicators") or {}
        if ind.get("range90_pct") is not None:
            lines.append(f"- 90d 区间位置：{ind['range90_pct'] * 100:.0f}%"
                         f"（区间 {_fmt(ind['range90_lo'])} ~ {_fmt(ind['range90_hi'])}）")
    lines.append("")

    # 多周期盘面结构表
    lines.append("## 多周期盘面结构")
    lines.append("")
    lines.append("| 周期 | 判定 | 现价 vs 价值区 | POC 位移 | 备注 |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        reg = r["regime"]
        vp = r.get("vp") or {}
        vs = reg.get("close_vs_va", "?")
        vs_txt = {"inside": "价值区内", "above": "价值区上沿外", "below": "价值区下沿外"}.get(vs, vs)
        poc_shift = reg.get("poc_shift_rows")
        poc_txt = f"{poc_shift:.1f} 档" if poc_shift is not None else "N/A"
        note = []
        if reg.get("outside_persist"):
            note.append(f"连续{reg['outside_persist']}根在带外" if vs != "inside" else "")
        if reg.get("bbw_narrowing"):
            note.append("带宽收窄")
        note_txt = "；".join(x for x in note if x) or "—"
        lines.append(f"| {r['interval_label']} | {reg.get('verdict','?')} | {vs_txt} | "
                     f"{poc_txt} | {note_txt} |")
    lines.append("")

    # 分周期偏多/偏空判断（新）
    lines.append("## 分周期偏多/偏空判断")
    lines.append("")
    lines.extend(build_tf_judgment(rows))
    lines.append("")

    # 技术指标表
    lines.append("## 技术指标（量价 + 择时，多周期）")
    lines.append("")
    lines.append("> RSI/MACD/布林为择时参考：单边趋势会钝化，不单独用于方向判断。")
    lines.append("")
    lines.extend(render_indicators_table(rows))
    lines.append("")
    # K 线形态（近 3 根）
    lines.append("**K 线形态（近 3 根）**")
    lines.append("")
    for r in rows:
        lines.append(f"- {r['interval_label']}：{_patterns_txt(r)}")
    lines.append("")
    lines.append("> 说明：K 线形态仅为图形观察，需配合量能、VP 支撑/阻力共同确认，不单独作为信号。")
    lines.append("")

    # VP 位置参考 + ASCII 图（每周期）
    lines.append("## VP 位置参考（量价筹码）")
    lines.append("")
    for r in rows:
        vp = r.get("vp")
        if not vp:
            continue
        lines.append(f"### {r['interval_label']}（近 {r['window_label']}）")
        lines.append("")
        lines.append(f"- POC：{_fmt(vp['poc'])}"
                     f"（现价 {_fmt(r['close'])} "
                     f"{'上方' if r['close'] and vp['poc'] and r['close'] >= vp['poc'] else '下方'}）")
        lines.append(f"- 价值区(70%)：{_fmt(vp['va_low'])} ~ {_fmt(vp['va_high'])}")
        sup, res = vp.get("support"), vp.get("resistance")
        sup_txt = f"{_fmt(sup['price'])}（{sup['kind']}）" if sup else "无"
        res_txt = f"{_fmt(res['price'])}（{res['kind']}）" if res else "无"
        lines.append(f"- 最近 HVN 支撑：{sup_txt}｜最近 HVN 阻力：{res_txt}")
        lvn_txt = "、".join(_fmt(x) for x in vp.get("lvn") or []) or "无"
        lines.append(f"- 低量真空区(LVN)：{lvn_txt}")
        lines.append("")
        lines.append("```")
        lines.extend(render_chart(vp))
        lines.append("```")
        lines.append("")

    # 合约持仓与资金 + 链上与宏观
    lines.extend(render_market_md(ctx, generated_at))

    # 关键价位清单 + 情景观察清单（多情景推演）
    levels = build_level_list(rows)
    lines.append("## 关键价位清单（支撑/阻力）")
    lines.append("")
    lines.extend(_render_levels_md(levels))
    lines.append("")
    lines.append("**LVN 真空加速带（非支撑阻力）**")
    lines.append("")
    lines.extend(_render_lvn_md(levels))
    lines.append("")
    lines.append("## 情景观察清单（多头/空头确认与失效）")
    lines.append("")
    lines.extend(build_scenario_watch(rows, ctx, levels))
    lines.append("")

    lines.append("## 口径与风险提示")
    lines.append("")
    lines.append("- VP 为 OHLCV 估算口径（分档 + 实体加权），仅描述价格大概率停留区；触级不代表反转。")
    lines.append("- 盘面结构（震荡/趋势）是**状态描述**，不是预测；多周期“共振”只表示结构一致性，不构成开单信号。")
    lines.append("- 合约数据（OI/费率/多空比）是情绪指标，**情绪可以持续很久，不要单靠费率猜反转**；OI 快速下降多为平仓/拐点。")
    lines.append("- VP 筹码是**历史静态数据**，突发大额清算会直接击穿筹码支撑，请结合爆仓数据核对。")
    lines.append("- RSI/MACD 单边趋势中会长期钝化，不单独用来做方向判断；所有指标需配合 K 线结构验证。")
    lines.append("- 数据截至各周期最后一根已收盘 K 线；衍生品/链上/宏观为各源最新快照，滞后超 2 天处已标注 ⏳。")
    lines.append("- 报告内少量价格差异（如不同数据源的现价）来自各 API 快照时间错位，以 K 线（biz.asset_klines）现价为准。")
    lines.append("- 未纳入维度：aSOPR / STH-LTH 成本基础真值、矿工持仓精确值（Glassnode/CryptoQuant 付费指标，"
                 "当前用 OBM 花费年龄代理近似）；期权为 Deribit 单所名义口径，未做 delta 加权。"
                 "本报告为现状+情景观察，非策略回测/交易信号。")
    lines.append("")
    return "\n".join(lines)


def render_html(rows: list[dict], ctx: dict, generated_at: datetime) -> str:
    """邮件 HTML 正文（内联样式，红涨绿跌与 send_daily_brief 同口径）。"""
    def esc(s):
        return html.escape(str(s or ""))

    h1 = next((r for r in rows if r["interval_label"] == "1h"), None)
    d1 = next((r for r in rows if r["interval_label"] == "1d"), None)
    price_row = h1 or d1 or rows[0]
    chg24 = h1["chg_24h"] if h1 else None
    chg7d = h1["chg_7d"] if h1 else None
    chg30 = d1["chg_30d"] if d1 else None

    p = []
    p.append('<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
             'font-size:14px;color:#171717;line-height:1.6">')
    p.append("<h2 style='margin:0 0 2px'>📊 BTC 大盘趋势分析（每日）</h2>")
    p.append(f"<p style='margin:0 0 12px;color:#64748b;font-size:12px'>"
             f"生成时间 {_fmt_cn(generated_at)}（Asia/Shanghai）· "
             f"数据源：Binance USDT 永续 {SYMBOL} + 衍生品/链上/宏观</p>")

    p.append("<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;"
             "padding:10px 14px;margin:0 0 16px'>"
             "<b>一句话摘要</b><br/>" + esc(build_summary(rows)) + "</div>")

    # 多因子共振摘要
    p.append("<div style='background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;"
             "padding:10px 14px;margin:0 0 16px'>"
             "<b>多因子共振（日线场景：VP + OI + 费率 + 200MA + 量能）</b><br/>"
             + esc(build_factor_summary(rows, ctx)).replace("\n", "<br/>") + "</div>")

    # 风险因子提示
    p.append("<div style='background:#fef2f2;border:1px solid #fecaca;border-radius:8px;"
             "padding:10px 14px;margin:0 0 16px'>"
             "<b>风险因子提示</b><br/>"
             + "<br/>".join("• " + esc(rf) for rf in build_risk_factors(rows, ctx)) + "</div>")

    p.append("<h3 style='margin:12px 0 6px'>价格概况</h3>")
    p.append(f"<p style='margin:0 0 10px'>现价 <b>{_fmt(price_row['close'])}</b>"
             f" · 24h {_pct_html(chg24)} · 7d {_pct_html(chg7d)}"
             + (f" · 30d {_pct_html(chg30)}" if chg30 is not None else "")
             + "</p>")

    p.append("<h3 style='margin:12px 0 6px'>多周期盘面结构</h3>")
    p.append("<table style='border-collapse:collapse;width:100%;font-size:13px'>")
    p.append("<tr style='background:#f1f5f9'>"
             "<th style='border:1px solid #e2e8f0;padding:6px 8px;text-align:left'>周期</th>"
             "<th style='border:1px solid #e2e8f0;padding:6px 8px;text-align:left'>判定</th>"
             "<th style='border:1px solid #e2e8f0;padding:6px 8px;text-align:left'>现价 vs 价值区</th>"
             "<th style='border:1px solid #e2e8f0;padding:6px 8px;text-align:left'>POC 位移</th></tr>")
    vs_txt = {"inside": "价值区内", "above": "价值区上沿外", "below": "价值区下沿外"}
    for r in rows:
        reg = r["regime"]
        poc_shift = reg.get("poc_shift_rows")
        poc_txt = f"{poc_shift:.1f} 档" if poc_shift is not None else "N/A"
        p.append("<tr>"
                 f"<td style='border:1px solid #e2e8f0;padding:6px 8px'><b>{esc(r['interval_label'])}</b></td>"
                 f"<td style='border:1px solid #e2e8f0;padding:6px 8px'>{esc(reg.get('verdict','?'))}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:6px 8px'>{vs_txt.get(reg.get('close_vs_va'),'?')}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:6px 8px'>{poc_txt}</td></tr>")
    p.append("</table>")

    # 分周期偏多/偏空判断
    p.append("<h3 style='margin:16px 0 6px'>分周期偏多/偏空判断</h3>")
    p.append("<table style='border-collapse:collapse;width:100%;font-size:12px'>")
    p.append("<tr style='background:#f1f5f9'>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>周期</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>倾向</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>依据</th></tr>")
    _tf_by = {r["interval_label"]: r for r in rows}
    for _k in _TF_ORDER:
        if _k not in _tf_by:
            continue
        _j = _tf_verdict(_tf_by[_k])
        p.append("<tr>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'><b>{esc(_k)}</b></td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'><b>{esc(_j['label'])}</b></td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc('；'.join(_j['reasons'][:5]))}</td></tr>")
    p.append("</table>")
    _tf_summary = [ln for ln in build_tf_judgment(rows) if ln.startswith(("**综合", ">"))]
    if _tf_summary:
        p.append("<p style='margin:6px 0 0;font-size:13px'>" +
                 esc("\n".join(_tf_summary)).replace("\n", "<br/>") + "</p>")

    # 技术指标表
    p.append("<h3 style='margin:16px 0 6px'>技术指标（量价 + 择时）</h3>")
    p.append("<p style='margin:0 0 6px;color:#64748b;font-size:12px'>"
             "RSI/MACD/布林为择时参考：单边趋势会钝化，不单独用于方向判断。</p>")
    p.append("<table style='border-collapse:collapse;width:100%;font-size:12px'>")
    p.append("<tr style='background:#f1f5f9'>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>周期</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>MA20/50/200</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>RSI</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>MACD</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>%B</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>ATR%</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>vs VWAP</th>"
             "<th style='border:1px solid #e2e8f0;padding:4px 6px'>量能</th></tr>")
    for r in rows:
        ind = r.get("indicators") or {}
        close = r.get("close")
        rsi = ind.get("rsi")
        rsi_txt = f"{rsi:.1f}" if rsi is not None else "N/A"
        rsi_color = "#dc2626" if (rsi is not None and rsi >= 70) else ("#16a34a" if (rsi is not None and rsi <= 30) else "")
        boll = ind.get("boll")
        if boll and close:
            pb = percent_b(close, boll[1], boll[2])
            boll_txt = f"{pb:.2f}"
        else:
            boll_txt = "N/A"
        atr = ind.get("atr")
        atr_txt = f"{atr / close * 100:.2f}%" if (atr and close) else "N/A"
        vwap = ind.get("vwap")
        vwap_txt = f"{'上' if close >= vwap else '下'}方" if (vwap and close) else "N/A"
        vr = ind.get("vol_ratio")
        if vr is not None:
            vol_txt = f"{vr:.2f}×" + (" 放量" if vr >= 1.2 else (" 缩量" if vr < 0.7 else " 平量"))
        else:
            vol_txt = "N/A"
        p.append("<tr>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'><b>{esc(r['interval_label'])}</b></td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(_ma_txt(ind, close))}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px;color:{rsi_color}'>{esc(rsi_txt)}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(_macd_txt(ind))}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(boll_txt)}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(atr_txt)}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(vwap_txt)}</td>"
                 f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(vol_txt)}</td></tr>")
    p.append("</table>")

    # K 线形态（近 3 根）
    p.append("<p style='margin:8px 0 2px'><b>K 线形态（近 3 根）</b></p>")
    for r in rows:
        p.append(f"<p style='margin:0 0 2px;font-size:13px'>"
                 f"<b>{esc(r['interval_label'])}</b>：{esc(_patterns_txt(r))}</p>")

    p.append("<h3 style='margin:16px 0 6px'>VP 位置参考（量价筹码）</h3>")
    for r in rows:
        vp = r.get("vp")
        if not vp:
            continue
        sup = vp.get("support")
        res = vp.get("resistance")
        sup_txt = f"{_fmt(sup['price'])}（{sup['kind']}）" if sup else "无"
        res_txt = f"{_fmt(res['price'])}（{res['kind']}）" if res else "无"
        lvn_txt = "、".join(_fmt(x) for x in vp.get("lvn") or []) or "无"
        p.append(f"<p style='margin:8px 0 2px'><b>{esc(r['interval_label'])}</b>"
                 f"（近 {esc(r['window_label'])}）</p>")
        p.append(f"<p style='margin:0 0 4px;font-size:13px'>"
                 f"POC <b>{_fmt(vp['poc'])}</b> · 价值区(70%) {_fmt(vp['va_low'])} ~ {_fmt(vp['va_high'])}<br/>"
                 f"最近 HVN 支撑：{esc(sup_txt)} ｜ 最近 HVN 阻力：{esc(res_txt)}<br/>"
                 f"低量真空区(LVN)：{esc(lvn_txt)}</p>")
        p.append("<pre style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;"
                 "padding:8px 12px;font-size:11px;line-height:1.35;overflow-x:auto'>"
                 + html.escape("\n".join(render_chart(vp))) + "</pre>")

    # 合约持仓 / 期权 / 链上与宏观（复用 md 渲染再转 html 段落）
    for sec in ("合约持仓与资金（衍生品）", "期权市场（Deribit 免费快照）",
                "链上与宏观", "宏观与跨资产联动"):
        p.append(f"<h3 style='margin:16px 0 6px'>{sec}</h3>")
        p.append("<p style='margin:0 0 4px;font-size:13px'>" +
                 esc(_section_md(ctx, sec, generated_at)).replace("\n", "<br/>") + "</p>")

    # 关键价位清单 + 情景观察清单
    levels = build_level_list(rows)
    p.append("<h3 style='margin:16px 0 6px'>关键价位清单（日内/波段）</h3>")
    p.append(f"<p style='margin:0 0 4px;color:#64748b;font-size:12px'>现价 {_fmt(levels.get('price'))}"
             "（参考）。日内看 1h/4h 短周期位，波段看 1d 位。</p>")

    def _html_level_block(title, price, supports, resistances):
        p.append(f"<p style='margin:8px 0 2px'><b>{esc(title)}</b></p>")
        p.append("<table style='border-collapse:collapse;width:100%;font-size:12px'>")
        p.append("<tr style='background:#f1f5f9'>"
                 "<th style='border:1px solid #e2e8f0;padding:4px 6px'>方向</th>"
                 "<th style='border:1px solid #e2e8f0;padding:4px 6px'>价位</th>"
                 "<th style='border:1px solid #e2e8f0;padding:4px 6px'>来源</th>"
                 "<th style='border:1px solid #e2e8f0;padding:4px 6px'>现价相对</th>"
                 "<th style='border:1px solid #e2e8f0;padding:4px 6px'>关注</th></tr>")
        for kind, cands in (("支撑", supports), ("阻力", resistances)):
            for i, (px, label) in enumerate(cands):
                dist = f"{(px / price - 1) * 100:+.2f}%" if price else "—"
                star = "★ 首要关注" if i == 0 else ""
                p.append("<tr>"
                         f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{kind}</td>"
                         f"<td style='border:1px solid #e2e8f0;padding:4px 6px'><b>{_fmt(px)}</b></td>"
                         f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(label)}</td>"
                         f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{esc(dist)}</td>"
                         f"<td style='border:1px solid #e2e8f0;padding:4px 6px'>{star}</td></tr>")
        if not supports and not resistances:
            p.append("<tr><td style='border:1px solid #e2e8f0;padding:4px 6px' colspan='5'>无</td></tr>")
        p.append("</table>")

    _html_level_block("日内关键位（1h/4h 短周期）", levels.get("intraday_price"),
                      levels["intraday"]["supports"], levels["intraday"]["resistances"])
    _html_level_block("波段关键位（1d 日线级别）", levels.get("swing_price"),
                      levels["swing"]["supports"], levels["swing"]["resistances"])

    # LVN 真空加速带（非支撑阻力）
    lvn = levels.get("lvn") or []
    if lvn:
        p.append("<p style='margin:8px 0 2px'><b>LVN 真空加速带（非支撑阻力）</b></p>")
        p.append("<p style='margin:0 0 2px;font-size:13px'>" +
                 " ｜ ".join(f"{_fmt(px)}（{esc(src)}）" for px, src in lvn) + "</p>")
        p.append("<p style='margin:0 0 4px;color:#64748b;font-size:12px'>"
                 "LVN 为低量真空加速带：不是支撑/阻力，价格穿过时易快速加速，勿当作承接或压制位。</p>")

    p.append("<h3 style='margin:16px 0 6px'>情景观察清单（多头/空头确认与失效）</h3>")
    p.append("<p style='margin:0 0 4px;font-size:13px'>" +
             esc("\n".join(build_scenario_watch(rows, ctx, levels))).replace("\n", "<br/>") + "</p>")

    p.append("<p style='margin:14px 0 0;color:#64748b;font-size:12px'>"
             "口径：VP 为 OHLCV 估算（分档+实体加权），仅描述价格大概率停留区，触级不代表反转；"
             "盘面结构为状态描述，不构成方向信号；合约/情绪指标（OI/费率/多空比）可持续钝化，"
             "不单靠费率猜反转；滞后超 2 天的衍生品/链上数据已标注 ⏳。</p>")
    p.append("</div>")
    return "\n".join(p)


def _section_md(ctx: dict, sec: str, generated_at: datetime) -> str:
    """从 render_market_md 抽取指定段落（供 html 复用）。"""
    block = render_market_md(ctx, generated_at)
    out = []
    in_sec = False
    for line in block:
        if line.startswith("## "):
            in_sec = line.strip("# ").strip() == sec
            continue
        if in_sec:
            out.append(line.rstrip())
    return "\n".join(out).strip()


# ---------------------------------------------------------------- 自测

def _fake_context() -> dict:
    """构造离线假市场上下文（用于 self-test，不读 DB）。"""
    ts = datetime(2026, 10, 11, 19, 10, tzinfo=timezone.utc)
    d1 = datetime(2026, 10, 10, tzinfo=timezone.utc).date()
    return {
        "oi": {"ts": ts, "value_usd": 51_965_025_834.0, "chg24h": 0.0088, "chg7d": 0.013},
        "oi_pct60": {"value": 92592.85, "n": 61, "pct": 55.0},
        "funding": {"ts": ts, "rate": 0.002548, "avg7d": 0.0001, "n_days": 30, "pct30": 96.0},
        "lsr": {
            "top_position": {"ts": d1, "ratio": 1.72, "long_pct": 63.21, "short_pct": 36.79},
            "top_account": {"ts": d1, "ratio": 1.18, "long_pct": 54.11, "short_pct": 45.89},
            "global_account": {"ts": d1, "ratio": 1.11, "long_pct": 52.61, "short_pct": 47.39},
        },
        "basis": {"ts": d1, "close_pct": 5.49},
        "liq": {"ts": d1, "total": 13_233_593.14, "long_share": 0.173, "bucket": None,
                "is_extreme": False},
        "liq_4h": {"ts": ts, "long": 482_989.53, "short": 11_794_749.64},
        "onchain": {"date": d1, "mvrv": 1.541, "sply_ex_usd": 221_778_107_737.59,
                    "flow_in_ex_usd": 1_395_808_273.09, "flow_out_ex_usd": 1_335_553_787.40,
                    "adr_act_cnt": 596_693, "roi_30d": 0.0818, "price_usd": 82_948.86,
                    "net_ex_flow_usd": 60_254_485.69, "realized_price_usd": 53_827.0,
                    "sply_ex_chg7d": -0.005},
        "mvrv_pct": {"date": d1, "pct_full": 40.21, "extreme": "NONE"},
        "fear_greed": {"date": d1, "value": 61, "cls": "Greed", "src": "cmc"},
        "global": {"date": d1, "total_mcap": 2.826e12, "btc_dom": 59.46, "stable_mcap": 2.839e11},
        "stablecoin": {"date": d1, "total_usd": 3.1107e11, "net_flow_usd": 87_240_094.3},
        "etf": {"days": [(d1 - timedelta(days=i), -1e8) for i in range(5)],
                "sum_week": -5.88e8, "n_week": 5,
                "week_span": (d1 - timedelta(days=4), d1), "latest": (d1, -1e8)},
        "btc_ref_price": 83673.22,
        "cefi": {"date": d1, "value": 42.0},
        "cycle": {"date": d1, "phase": "neutral", "label": "中性"},
        "options": {"date": d1, "spot": 83673.22, "dvol": 37.68,
                    "call_oi": 236853.0, "put_oi": 137650.0, "pcr": 0.581,
                    "max_pain": 84000.0, "nearest_expiry": ts},
        "macro": [
            {"date": d1 - timedelta(days=k), "dxy": 102.0 + 0.1 * k, "us10y": 5.2,
             "nasdaq": 27000 + 100 * k, "gold": 4200 + 10 * k}
            for k in range(100, 0, -1)
        ],
        "corr": {"btc_vs_nasdaq_30d": 0.62, "btc_vs_nasdaq_90d": 0.55,
                 "btc_vs_gold_30d": 0.31, "btc_vs_gold_90d": 0.28,
                 "btc_vs_dxy_30d": -0.45, "btc_vs_dxy_90d": -0.38},
        "obm": {
            "obm_spent_value_lt155d_btc_daily": 12000.0,
            "obm_spent_value_ge155d_btc_daily": 18000.0,
            "obm_liveliness_ratio_daily": 0.523,
            "obm_cdd_btcxdays_daily": 1_234_567.0,
            "obm_miner_revenue_btc_daily": 450.0,
            "obm_issuance_btc_daily": 450.0,
        },
        "obm_date": d1,
    }


def self_test() -> int:
    random.seed(5)

    def mk(n, base, drift):
        o, h, l, c, v = [], [], [], [], []
        px = base
        for _ in range(n):
            px = px * (1 + drift) * (1 + random.gauss(0, 0.002))
            o.append(px); c.append(px * (1 + random.gauss(0, 0.001)))
            h.append(max(o[-1], c[-1]) * (1 + random.uniform(0.001, 0.004)))
            l.append(min(o[-1], c[-1]) * (1 - random.uniform(0.001, 0.004)))
            v.append(random.uniform(1e6, 5e6))
        return o, h, l, c, v

    def mk_range(n, base, k=0.1, sd=0.005):
        # OU 均值回归：围绕 base 有界波动（真实区间模型）
        o, h, l, c, v = [], [], [], [], []
        px = base
        for _ in range(n):
            px += -k * (px - base) + random.gauss(0, base * sd)
            o.append(px); c.append(px * (1 + random.gauss(0, 0.001)))
            h.append(max(o[-1], c[-1]) * (1 + random.uniform(0.001, 0.004)))
            l.append(min(o[-1], c[-1]) * (1 - random.uniform(0.001, 0.004)))
            v.append(random.uniform(1e6, 5e6))
        return o, h, l, c, v

    # 构造两个假"周期"（趋势 + 震荡），验证 analyze_interval + 摘要 + 新段落不崩、判定符合预期
    fake_rows = []
    # 1h 用温和漂移（0.08%/根），避免 VP 区间被指数放大导致价位清单失真
    o, h, l, c, v = mk(600, 100, 0.0008)
    bars = [{"t": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i),
             "open": o[i], "high": h[i], "low": l[i], "close": c[i], "vol": v[i]}
            for i in range(len(c))]
    r = analyze_interval(bars, 168)
    r["interval_label"] = "1h"
    r["window_label"] = "7d"
    fake_rows.append(r)

    o, h, l, c, v = mk_range(400, 100)
    # 让 1d 收盘落在区间中部附近，保证波段档两侧都有位（假数据可控性）
    o[-1] = 100.0; c[-1] = 100.5
    h[-1] = max(h[-1], 101.6); l[-1] = min(l[-1], 99.2)
    bars = [{"t": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i),
             "open": o[i], "high": h[i], "low": l[i], "close": c[i], "vol": v[i]}
            for i in range(len(c))]
    r = analyze_interval(bars, 50)
    r["interval_label"] = "1d"
    r["window_label"] = "50d"
    fake_rows.append(r)

    assert fake_rows[0]["regime"].get("verdict", "").startswith("趋势向上"), fake_rows[0]
    assert fake_rows[1]["regime"].get("verdict") == "震荡", fake_rows[1]
    # 新指标快照存在且不崩
    for rr in fake_rows:
        ind = rr["indicators"]
        for k in ("ma20", "ma50", "rsi", "atr", "vwap", "hist", "vol_ratio"):
            assert k in ind, k

    ctx = _fake_context()
    md = render_markdown(fake_rows, ctx, datetime.now(timezone.utc))
    for sec in ("一句话摘要", "多因子共振摘要", "风险因子提示", "多周期盘面结构",
                "分周期偏多/偏空判断", "技术指标",
                "K 线形态", "VP 位置参考", "合约持仓与资金", "期权市场",
                "链上与宏观", "宏观与跨资产联动",
                "关键价位清单", "LVN 真空加速带", "情景观察清单", "口径与风险提示"):
        assert sec in md, sec
    htm = render_html(fake_rows, ctx, datetime.now(timezone.utc))
    assert "多因子共振" in htm and "<table" in htm and "POC" in htm
    assert "合约持仓与资金" in htm and "链上与宏观" in htm
    assert "情景观察清单" in htm and "关键价位清单" in htm
    assert "期权市场" in htm and "宏观与跨资产联动" in htm
    assert "风险因子提示" in htm and "分周期偏多/偏空判断" in htm
    # 风险因子在假数据下必须非空（高费率/ETF流出/纳指强相关均命中）
    assert build_risk_factors(fake_rows, ctx), build_risk_factors(fake_rows, ctx)
    # 价位清单/情景在双周期数据下必须产出（fake 无 4h，日内档用 1h、波段档用 1d；
    # 假数据非真实行情，支撑/阻力可能单侧为空，只校验两档结构存在）
    lv = build_level_list(fake_rows)
    assert "intraday" in lv and "swing" in lv and "lvn" in lv
    assert lv["intraday"]["supports"] or lv["intraday"]["resistances"]
    assert lv["swing"]["supports"] or lv["swing"]["resistances"]
    scen = build_scenario_watch(fake_rows, ctx, lv)
    assert any("多头确认" in s for s in scen), scen
    print(f"self-test OK: 1h={fake_rows[0]['regime']['verdict']}, "
          f"1d={fake_rows[1]['regime']['verdict']}, md={len(md)} chars, html={len(htm)} chars")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="每日 BTC 大盘趋势分析（VP + 盘面结构 + 多因子共振）")
    parser.add_argument("--interval", default="", help="单周期（5m/15m/1h/1d）；与 --intervals 二选一")
    parser.add_argument("--intervals", default="1h,4h,1d,1w", help="逗号分隔多周期（默认 1h,4h,1d,1w）。"
                        "⚠️ PowerShell 会把 `1d` 当十进制数吞掉，传参请加引号：--intervals \"1h,4h,1d,1w\"")
    parser.add_argument("--out", default="", help=f"Markdown 输出路径（默认 {DEFAULT_OUT.name}）")
    parser.add_argument("--send-email", action="store_true", help="生成后把报告发到 SMTP_TO 邮箱")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    intervals = [s.strip() for s in (args.interval or args.intervals).split(",") if s.strip()]
    intervals = [iv for iv in intervals if iv in INTERVAL_CONFIG]
    if not intervals:
        print("无有效周期（可选：5m/15m/1h/1d）")
        return 2
    out_path = Path(args.out) if args.out else DEFAULT_OUT

    settings = get_settings(require_database=True)
    rows = []
    with get_connection(settings.database_url) as conn:
        for iv in intervals:
            cfg = INTERVAL_CONFIG[iv]
            base = cfg.get("base")
            if base:
                # 派生周期：先拉基础周期再按时间桶聚合（4h←1h、1w←1d）
                bcfg = INTERVAL_CONFIG[base]
                raw = load_klines(conn, base, cfg["lookback_days"],
                                  cfg["lookback_days"] * bcfg["bars_per_day"])
                bars = resample_bars(raw, cfg["step_hours"])
            else:
                bars = load_klines(conn, iv, cfg["lookback_days"],
                                   cfg["lookback_days"] * cfg["bars_per_day"])
            if len(bars) < cfg["window_bars"] * 2 + 40:
                print(f"[{iv}] 数据不足（{len(bars)} 根），跳过")
                continue
            r = analyze_interval(bars, cfg["window_bars"])
            r["interval_label"] = iv
            r["window_label"] = f"{cfg['window_bars']} 根"
            rows.append(r)
            print(f"[{iv}] bars={len(bars)} 判定={r['regime'].get('verdict', '?')}", flush=True)

        ctx = load_market_context(conn)
        print(f"[ctx] OI={'Y' if ctx.get('oi') else '-'} 费率={'Y' if ctx.get('funding') else '-'} "
              f"多空比={'Y' if ctx.get('lsr') else '-'} 基差={'Y' if ctx.get('basis') else '-'} "
              f"爆仓={'Y' if ctx.get('liq') else '-'} 链上={'Y' if ctx.get('onchain') else '-'} "
              f"恐贪={'Y' if ctx.get('fear_greed') else '-'} ETF={'Y' if ctx.get('etf') else '-'} "
              f"期权={'Y' if ctx.get('options') else '-'} "
              f"宏观={'Y' if ctx.get('macro') else '-'} 相关性={'Y' if ctx.get('corr') else '-'} "
              f"OBM代理={'Y' if ctx.get('obm') else '-'}", flush=True)

    if not rows:
        print("无数据可分析")
        return 1

    generated_at = datetime.now(timezone.utc)
    md = render_markdown(rows, ctx, generated_at)
    print("\n" + md)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n已写 → {out_path}")

    if args.send_email:
        return _send_report_email(rows, ctx, generated_at)
    return 0


def _send_report_email(rows: list[dict], ctx: dict, generated_at: datetime) -> int:
    """用 EmailNotifier 发送报告邮件；SMTP 未配置/失败按失败处理（避免静默成功）。"""
    try:
        from crypto_research.config import get_settings
        from crypto_research.clients.notifier import EmailNotifier

        settings = get_settings(require_database=False)
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[ERROR] SMTP 未配置，邮件未发送（按失败处理）")
            return 1
        body = render_html(rows, ctx, generated_at)
        subject = f"📊 BTC 大盘趋势分析 {generated_at:%Y-%m-%d}"
        ok, msg = notifier.send(subject=subject, body_html=body, from_name="BTC大盘趋势")
        if ok:
            print(f"[OK] 邮件已发送: {msg}")
            return 0
        print(f"[ERROR] 邮件发送失败: {msg}")
        return 1
    except Exception as e:
        print(f"[ERROR] 邮件发送异常: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
