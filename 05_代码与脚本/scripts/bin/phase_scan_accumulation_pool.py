#!/usr/bin/env python3
"""盘面异动扫描 P3 · 蓄势埋伏观察池：OI 持续累积识别 → 突破转主池。

纯 DB 读取（biz.oi_cvd_snapshot / biz.asset_klines / biz.asset_derivatives），
不依赖 Binance 实时接口。

蓄势判定（ACC，设计文档 §5，全部满足）：
  1. 价格平静：1h/15m 最近单根涨跌幅 < 阈值（默认 1.5%）
  2. 量能未放大：最新 1h 成交额 < 近 20 周期均值 × 1.5
  3. OI 持续抬升：近 12 个 1h 周期中 OI 上升占比 ≥70% 且累计变化 >5%
  4. （可选）funding 拥挤度观察，仅标注不作为进出池条件

转主池（BRK）：对已有 ACC 信号的币，出现放量（量比 ≥3×）+ 突破蓄势区间
上/下沿 → 写入 BRK 信号，供主池 L2 校验。

用法：
    python phase_scan_accumulation_pool.py            # 全量（读库）
    python phase_scan_accumulation_pool.py --dry-run  # 只打印不落库
"""
from __future__ import annotations

import argparse
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

PRICE_QUIET_THR = 1.5          # 价格平静：单根涨跌幅 < 该值(%)
VOL_CAP_RATIO = 1.5            # 量能未放大上限：< 近 20 周期均值 × 该值
OI_HOURS = 12                  # 观察窗口（小时）
OI_RISE_RATIO = 0.7            # OI 上升小时占比阈值
OI_CUM_CHG_PCT = 5.0           # OI 累计变化阈值(%)
BRK_VOL_RATIO = 3.0            # 转主池：放量 ≥ 3 × 均值
LOOKBACK_BARS = 20

INSERT_SQL = """
    INSERT INTO biz.scan_signal
        (signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
         vol_state, vol_ratio, oi_dir, oi_chg_pct, funding_rate, confidence,
         context_tags, status)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
"""


def has_active_acc(conn, symbol: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM biz.scan_signal WHERE symbol=%s AND pool='accumulation' "
            "AND scenario='ACC' AND status='active' AND created_at > NOW() - INTERVAL '7 days' "
            "LIMIT 1",
            (symbol,),
        )
        return cur.fetchone() is not None


def detect_acc(symbol: str, oi_hourly: list[dict], k1h: list[dict],
               funding_map: dict[str, float]) -> dict | None:
    """蓄势判定。oi_hourly 按时间升序。"""
    if len(oi_hourly) < OI_HOURS:
        return None

    # 1) 价格平静：1h / 15m 最近单根涨跌幅 < 阈值（15m 缺数据则只看 1h）
    if len(k1h) >= 2:
        chg1h = (k1h[-1]["close_px"] - k1h[-2]["close_px"]) / k1h[-2]["close_px"] * 100
        if abs(chg1h) >= PRICE_QUIET_THR:
            return None
    else:
        chg1h = None
    # 2) 量能未放大
    vols = [float(k["quote_vol"] or 0) for k in k1h]
    if len(vols) >= LOOKBACK_BARS + 1:
        vol_mean = sum(vols[-(LOOKBACK_BARS + 1):-1]) / LOOKBACK_BARS
        vol_ratio = vols[-1] / vol_mean if vol_mean else 0.0
        if vol_mean and vol_ratio >= VOL_CAP_RATIO:
            return None
    else:
        vol_ratio = None

    # 3) OI 持续抬升
    ois = [float(o["oi"]) for o in oi_hourly if o.get("oi") is not None]
    if len(ois) < OI_HOURS:
        return None
    rising = sum(1 for i in range(1, len(ois)) if ois[i] > ois[i - 1]) / (len(ois) - 1)
    cum_chg = (ois[-1] - ois[0]) / ois[0] * 100 if ois[0] else 0.0
    if rising < OI_RISE_RATIO or cum_chg <= OI_CUM_CHG_PCT:
        return None

    # funding 观察标签（不参与进出池）
    fr = funding_map.get(symbol)
    if fr is not None:
        if fr <= 0.0001:
            fund_label = "OI↑+费率偏低/负：偏逼空潜力"
        elif fr >= 0.0005:
            fund_label = "OI↑+费率偏高：警惕多头派发"
        else:
            fund_label = "OI↑+费率中性：多空均衡吸筹"
    else:
        fund_label = "费率无数据"
    return {"chg1h": chg1h, "vol_ratio": vol_ratio,
            "oi_rise_ratio": rising * 100, "oi_cum_chg": cum_chg, "fund_label": fund_label}


def detect_brk(symbol: str, k1h: list[dict], acc_range: tuple[float, float]) -> dict | None:
    """转主池：放量 + 突破蓄势区间。k1h 为最近 1 天 1h K 线（升序）。"""
    if len(k1h) < LOOKBACK_BARS + 1:
        return None
    vols = [float(k["quote_vol"] or 0) for k in k1h]
    vol_mean = sum(vols[-(LOOKBACK_BARS + 1):-1]) / LOOKBACK_BARS
    if not vol_mean:
        return None
    vol_ratio = vols[-1] / vol_mean
    if vol_ratio < BRK_VOL_RATIO:
        return None
    lo, hi = acc_range
    close = float(k1h[-1]["close_px"])
    if close > hi:
        return {"dir": "up", "vol_ratio": vol_ratio, "break_px": close}
    if close < lo:
        return {"dir": "down", "vol_ratio": vol_ratio, "break_px": close}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="蓄势池扫描 ACC/BRK → biz.scan_signal")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    now = datetime.now(timezone.utc)
    with get_connection(settings.database_url) as conn:
        # 小时级 OI（近 13h）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, date_trunc('hour', ts) AS h, AVG(oi_usd) AS oi "
                "FROM biz.oi_cvd_snapshot WHERE ts >= NOW() - INTERVAL '13 hours' "
                "GROUP BY symbol, date_trunc('hour', ts) ORDER BY symbol, h"
            )
            oi_rows = cur.fetchall()
        by_sym_oi: dict[str, list[dict]] = {}
        for r in oi_rows:
            by_sym_oi.setdefault(r["symbol"], []).append(r)

        # 1h K 线（近 1 天）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, open_time, close_px, quote_vol FROM biz.asset_klines "
                "WHERE interval='1h' AND open_time >= NOW() - INTERVAL '1 day' "
                "ORDER BY symbol, open_time"
            )
            k_rows = cur.fetchall()
        by_sym_k: dict[str, list[dict]] = {}
        for r in k_rows:
            by_sym_k.setdefault(r["symbol"], []).append(r)

        # funding
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT symbol, funding_rate FROM biz.asset_derivatives")
            funding_map = {r["symbol"]: float(r["funding_rate"]) for r in cur.fetchall()
                           if r["funding_rate"] is not None}

        # 已有 ACC 信号（用于 BRK 判定）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol FROM biz.scan_signal WHERE pool='accumulation' "
                "AND scenario='ACC' AND status='active' AND created_at > NOW() - INTERVAL '7 days'"
            )
            acc_symbols = {r["symbol"] for r in cur.fetchall()}

        signals: list[tuple] = []
        # ① ACC 蓄势判定
        for sym in sorted(by_sym_oi):
            acc = detect_acc(sym, by_sym_oi[sym], by_sym_k.get(sym, []), funding_map)
            if not acc:
                continue
            tags = [f"oi_rise={acc['oi_rise_ratio']:.0f}%", f"oi_cum={acc['oi_cum_chg']:.1f}%",
                    acc["fund_label"]]
            signals.append((now, sym, "accumulation", "ACC", "1h", "flat",
                            acc["chg1h"] or 0, "flat", acc["vol_ratio"] or 0.0,
                            "up", acc["oi_cum_chg"], funding_map.get(sym), "medium", tags))

        # ② BRK 突破转主池
        for sym in sorted(acc_symbols):
            k1h = by_sym_k.get(sym, [])
            if len(k1h) < OI_HOURS + 1:
                continue
            # 蓄势区间 = ACC 窗口内收盘价高低
            lo = min(float(k["close_px"]) for k in k1h[-(OI_HOURS + 1):])
            hi = max(float(k["close_px"]) for k in k1h[-(OI_HOURS + 1):])
            brk = detect_brk(sym, k1h, (lo, hi))
            if not brk:
                continue
            tags = [f"brk_{brk['dir']}", f"vol_x={brk['vol_ratio']:.1f}"]
            signals.append((now, sym, "accumulation", "BRK", "1h", brk["dir"],
                            None, "up" if brk["vol_ratio"] >= VOL_CAP_RATIO else "flat",
                            round(brk["vol_ratio"], 2), None, None,
                            funding_map.get(sym), "high", tags))

        print(f"[accumulation] OI 小时序列 {len(by_sym_oi)} 符号；ACC={sum(1 for s in signals if s[2]=='ACC')}，"
              f"BRK={sum(1 for s in signals if s[2]=='BRK')}")
        if args.dry_run:
            for s in signals[:20]:
                print("  ", s[1], s[2], s[3], s[13])
            return 0
        if not signals:
            return 0
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, signals)
        print(f"[db] 写入 {len(signals)} 条信号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
