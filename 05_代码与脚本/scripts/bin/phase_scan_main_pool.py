#!/usr/bin/env python3
"""盘面异动扫描 P2 · 主池扫描：L0 环境过滤 + L1 粗筛 + L2 深度校验 → biz.scan_signal。

纯 DB 读取（biz.asset_klines / biz.oi_cvd_snapshot / biz.asset_derivatives /
biz.fear_greed_daily / biz.global_metric_daily），不依赖 Binance 实时接口。

- L0 市场环境：BTC 方向 / 恐慌贪婪 / 总市值趋势 → context_tags
- L1 粗筛：5m/15m/1h 单周期价量异动 + 多周期共振升级
    （5m 单独 → 观察；15m 同向 → 入池；1h 同向 → 高置信，权重 ×2）
- L2 校验：OI/CVD 方向 + funding 拥挤度标签 → 8 场景 S1..S8 → 置信度分级

用法：
    python phase_scan_main_pool.py                # 全量扫描（读库）
    python phase_scan_main_pool.py --limit-symbols 20   # 只扫前 20 个符号（测试）
    python phase_scan_main_pool.py --dry-run             # 只打印不落库
    python phase_scan_main_pool.py --cooldown-h 6        # 同币信号冷却小时
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

INTERVALS = ("5m", "15m", "1h")
# 周期 → 单根涨跌幅阈值（%），回测标定前为占位默认值
PRICE_THR = {"5m": 1.5, "15m": 2.0, "1h": 3.0}
VOL_RATIO_THR = 2.0            # 量 ≥ N × 近 20 周期均值
LOOKBACK_BARS = 20             # 量能均值窗口
OI_RISE_BARS = 2               # OI 方向取最近 2 桶对比
LEVEL_RANK = {"5m": 1, "15m": 2, "1h": 3}

INSERT_SQL = """
    INSERT INTO biz.scan_signal
        (signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
         vol_state, vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate,
         confidence, context_tags, status)
    VALUES (%s,%s,'main',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
"""


# ── L0 市场环境 ──
def build_regime(conn) -> dict:
    """返回 {tags: [...], long_fav: bool, short_fav: bool}。"""
    tags: list[str] = []
    long_fav = short_fav = True

    # BTC 1h 方向（来自 asset_klines BTCUSDT）
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT close_px, open_time FROM biz.asset_klines "
                "WHERE symbol='BTCUSDT' AND interval='1h' "
                "ORDER BY open_time DESC LIMIT 5"
            )
            closes = [r["close_px"] for r in cur.fetchall()]
        if len(closes) >= 2:
            btc_1h = (closes[0] - closes[1]) / closes[1] * 100
            tags.append(f"btc_1h={'up' if btc_1h >= 0 else 'down'}({btc_1h:+.2f}%)")
            if btc_1h < -1.0:
                long_fav = False
            elif btc_1h > 1.0:
                short_fav = False
    except Exception:
        pass

    # 恐慌贪婪
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT value, value_classification FROM biz.fear_greed_daily "
                "ORDER BY metric_date DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row:
            fgi = float(row["value"])
            tags.append(f"fgi={fgi:.0f}({row['value_classification'] or '?'})")
            if fgi < 25:      # 极端恐慌：空头顺风
                long_fav = False
            elif fgi > 75:    # 极端贪婪：多头过热
                short_fav = False
    except Exception:
        pass

    # 总市值趋势（近 2 日）
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT metric_date, total_market_cap FROM biz.global_metric_daily "
                "ORDER BY metric_date DESC LIMIT 2"
            )
            rows = cur.fetchall()
        if len(rows) >= 2 and rows[0]["total_market_cap"] and rows[1]["total_market_cap"]:
            trend = (rows[0]["total_market_cap"] - rows[1]["total_market_cap"]) / rows[1]["total_market_cap"] * 100
            tags.append(f"cap_trend={trend:+.2f}%")
            if trend < -1.0:
                long_fav = False
            elif trend > 1.0:
                short_fav = False
    except Exception:
        pass

    return {"tags": tags, "long_fav": long_fav, "short_fav": short_fav}


# ── L1 粗筛 ──
def eval_timeframe(klines: list[dict]) -> dict | None:
    """单周期价量：返回 {chg, vol_ratio, vol_up, dir}；数据不足返回 None。"""
    if len(klines) < LOOKBACK_BARS + 2:
        return None
    closes = [k["close_px"] for k in klines]
    vols = [float(k["quote_vol"] or 0) for k in klines]
    last, prev = closes[-1], closes[-2]
    if not prev:
        return None
    chg = (last - prev) / prev * 100
    vol_mean = sum(vols[-(LOOKBACK_BARS + 1):-1]) / LOOKBACK_BARS
    vol_ratio = vols[-1] / vol_mean if vol_mean else 0.0
    return {"chg": chg, "vol_ratio": vol_ratio,
            "vol_up": vol_ratio >= VOL_RATIO_THR,
            "dir": "up" if chg >= 0 else "down"}


def l1_screen(by_iv: dict[str, list[dict]]) -> dict | None:
    """多周期共振：返回 {level, timeframe, dir, chg, vol_ratio} 或 None。"""
    hits = []  # (rank, interval, dir, chg, vol_ratio)
    for iv in INTERVALS:
        res = eval_timeframe(by_iv.get(iv, []))
        if res and abs(res["chg"]) >= PRICE_THR[iv] and res["vol_up"]:
            hits.append((LEVEL_RANK[iv], iv, res["dir"], res["chg"], res["vol_ratio"]))
    if not hits:
        return None
    hits.sort(key=lambda x: x[0], reverse=True)
    rank, iv, d, chg, vr = hits[0]
    # 方向一致性：多周期触发但方向冲突 → 降级观察
    dirs = {h[2] for h in hits}
    if len(dirs) > 1:
        return {"level": 1, "timeframe": "5m", "dir": d, "chg": chg, "vol_ratio": vr, "conflict": True}
    return {"level": rank, "timeframe": iv, "dir": d, "chg": chg, "vol_ratio": vr, "conflict": False}


# ── L2 深度校验 ──
def compute_l2(symbol: str, oi_rows: list[dict], funding_map: dict[str, float],
               direction: str) -> dict | None:
    """基于 oi_cvd_snapshot 最近桶计算场景。数据不足（<2 桶 / 无 cvd）返回 None。"""
    if len(oi_rows) < OI_RISE_BARS:
        return None
    oi_rows = oi_rows[-2:]
    prev_oi, last_oi = oi_rows[0].get("oi_usd"), oi_rows[1].get("oi_usd")
    cvd = oi_rows[1].get("cvd_5m_usd")
    if last_oi is None or prev_oi is None or cvd is None:
        return None
    oi_chg = (last_oi - prev_oi) / prev_oi * 100 if prev_oi else 0.0
    oi_dir = "up" if oi_chg >= 0 else "down"
    cvd_dir = "up" if cvd >= 0 else "down"

    if direction == "up":
        scenario = {"up": {"up": "S1", "down": "S2"},
                    "down": {"up": "S5", "down": "S6"}}[oi_dir][cvd_dir]
    else:
        scenario = {"up": {"down": "S3", "up": "S4"},
                    "down": {"down": "S7", "up": "S8"}}[oi_dir][cvd_dir]

    funding_rate = funding_map.get(symbol)
    return {"scenario": scenario, "oi_dir": oi_dir, "oi_chg_pct": oi_chg,
            "cvd_dir": cvd_dir, "funding_rate": funding_rate,
            "label": FUNDING_LABELS.get(scenario, "")}


# 场景 → 资金费率拥挤度标签（设计文档 §4.3）
FUNDING_LABELS = {
    "S1": "健康多头(费率中性)/多头过热(大幅正)",
    "S2": "诱多:高正费率多头拥挤",
    "S3": "健康空头(费率中性)/空头过热(大幅负)",
    "S4": "诱空:高负费率空头拥挤",
    "S5": "多头兑现:高正费率反转风险",
    "S6": "修复反弹:负费率空头回补",
    "S7": "跌势衰竭:高负费率空头平仓",
    "S8": "见底反弹:高负费率抛压释放",
}


def in_cooldown(conn, symbol: str, hours: float) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM biz.scan_signal WHERE symbol=%s AND status='active' "
            "AND created_at > NOW() - INTERVAL '%s hours' LIMIT 1",
            (symbol, hours),
        )
        return cur.fetchone() is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="主池扫描 L0+L1+L2 → biz.scan_signal")
    parser.add_argument("--limit-symbols", type=int, default=0, help="只扫前 N 个符号")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    parser.add_argument("--cooldown-h", type=float, default=6.0, help="同币信号冷却小时")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        regime = build_regime(conn)
        print(f"[L0] 环境: {', '.join(regime['tags']) or '无数据'} "
              f"long_fav={regime['long_fav']} short_fav={regime['short_fav']}")

        # 拉最近 1 天全部 K 线（按 symbol,interval 分组）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, interval, open_time, close_px, quote_vol "
                "FROM biz.asset_klines WHERE open_time >= NOW() - INTERVAL '1 day' "
                "ORDER BY symbol, interval, open_time"
            )
            rows = cur.fetchall()

        by_symbol: dict[str, dict[str, list[dict]]] = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], {}).setdefault(r["interval"], []).append(r)

        symbols = sorted(by_symbol)
        if args.limit_symbols:
            symbols = symbols[: args.limit_symbols]

        # OI/CVD 近 3h
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, ts, oi_usd, cvd_5m_usd FROM biz.oi_cvd_snapshot "
                "WHERE ts >= NOW() - INTERVAL '3 hours' ORDER BY symbol, ts"
            )
            oi_rows = cur.fetchall()
        by_sym_oi: dict[str, list[dict]] = {}
        for r in oi_rows:
            by_sym_oi.setdefault(r["symbol"], []).append(r)

        # funding（asset_derivatives 快照）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT symbol, funding_rate FROM biz.asset_derivatives")
            funding_map = {r["symbol"]: float(r["funding_rate"]) for r in cur.fetchall()
                           if r["funding_rate"] is not None}

        now = datetime.now(timezone.utc)
        signals: list[tuple] = []
        skipped = 0
        for sym in symbols:
            l1 = l1_screen(by_symbol[sym])
            if not l1:
                continue
            if l1["level"] < 2:   # 5m 单独异动只观察，不写信号
                continue
            if args.cooldown_h > 0 and in_cooldown(conn, sym, args.cooldown_h):
                skipped += 1
                continue

            l2 = compute_l2(sym, by_sym_oi.get(sym, []), funding_map, l1["dir"])
            if not l2:
                continue

            # 置信度：S1/S3 + 环境顺风 → high；S1/S3 逆风 → low；其余 medium
            base_high = l2["scenario"] in ("S1", "S3")
            fav = regime["long_fav"] if l1["dir"] == "up" else regime["short_fav"]
            if base_high and fav:
                confidence = "high"
            elif base_high:
                confidence = "low"
            else:
                confidence = "medium"

            row = (now, sym, l2["scenario"], l1["timeframe"], l1["dir"],
                   round(l1["chg"], 2), "up" if l1["vol_ratio"] >= VOL_RATIO_THR else "down",
                   round(l1["vol_ratio"], 2), l2["oi_dir"], round(l2["oi_chg_pct"], 2),
                   l2["cvd_dir"], l2["funding_rate"], confidence, regime["tags"])
            signals.append(row)

        print(f"[scan] 扫描 {len(symbols)} 个符号，L1 命中(≥入池)后经 L2 校验产出 {len(signals)} 条信号，"
              f"冷却跳过 {skipped}")
        if args.dry_run:
            for s in signals[:20]:
                print("  ", s[1], s[2], s[3], f"P={s[4]}({s[5]}%)", f"VOL={s[7]}x",
                      f"OI={s[9]}({s[10]}%)", f"CVD={s[11]}", f"conf={s[13]}", s[14])
            return 0
        if not signals:
            return 0
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, signals)
        print(f"[db] 写入 {len(signals)} 条信号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
