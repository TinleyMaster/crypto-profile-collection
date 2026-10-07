#!/usr/bin/env python3
"""v2 盘面异动扫描引擎：涨幅榜定位 × 强度确认 → 信号落库（盘面异动扫描系统设计方案 v2）。

回测依据（《盘面异动扫描系统设计方案_v2_2026-10-05.md》+ 交易参数回测）：
  - 方向只来自涨幅榜档位；价量异动=强度确认；时间窗口定多空（T+24h 动量 / T+7d 反转）。
  - 实盘口径：24h 涨幅用 ticker/24hr 滚动口径（日收盘口径的实盘近似），文档已标注。

信号（scan_gainer_signal）：
  SHORT_LONG  短线做多  chg24h≥50%   + 放量(24h量≥2×7日均)   窗口 T+24h LONG
  MID_LONG    次档做多  chg24h 20~50% + 放量                  窗口 T+24h LONG
  TRAP_SHORT  诱多做空  chg24h<5%     + 放量（未上榜却放量）    窗口 T+24h SHORT
  EXT_SHORT   极端做空  chg24h≥75%    + 低量(≤1×7日均,静默上涨) 窗口 T+7d  SHORT

说明：
  - 「涨异动(放量大阳) ∩ 极端涨幅」做空 7 日为负期望（回测 -4.14%），故 EXT_SHORT 要求**低量静默**
    而非放量；放量极端涨幅反而应做多（动量延续）。
  - 冷却：同 symbol 同信号类型最近 12h 已出信号则跳过。
  - 默认 dry-run（只打印不落库）；--apply 才落库。

用法：
    python bin/scan_gainers_v2.py                 # dry-run
    python bin/scan_gainers_v2.py --apply         # 落库
    python bin/scan_gainers_v2.py --apply --cooldown-h 12 --min-vol 20000000
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

from crypto_research.clients.binance_http import fapi_get  # noqa: E402
from crypto_research.clients.notifier import EmailNotifier  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

GAIN_SHORT_LONG = 50.0        # 短线做多：24h 涨幅 ≥50%
GAIN_MID_LONG_LO, GAIN_MID_LONG_HI = 20.0, 50.0
GAIN_EXT_SHORT = 75.0         # 极端做空：≥75%
GAIN_TRAP_SHORT = 5.0         # 诱多做空：<5%
VOL_RATIO_LONG = 2.0          # 放量（做多确认）≥2× 7日均量
VOL_RATIO_SHORT_MAX = 1.0     # 静默（做空确认）≤1× 7日均量
MIN_VOL = 20_000_000          # 最小 24h 成交额（USDT）
MIN_FUND_NEG = -0.0002        # 极端做空可选：费率不过分负（可调）
COOLDOWN_H = 12               # 同币同类型冷却（小时）

# 信号类型 → (窗口, 方向, 涨幅判定)
SIGNAL_DEFS = [
    ("SHORT_LONG", "T24H", "LONG",
     lambda c: c >= GAIN_SHORT_LONG, lambda vr: vr >= VOL_RATIO_LONG),
    ("MID_LONG", "T24H", "LONG",
     lambda c: GAIN_MID_LONG_LO <= c < GAIN_MID_LONG_HI, lambda vr: vr >= VOL_RATIO_LONG),
    ("TRAP_SHORT", "T24H", "SHORT",
     lambda c: c < GAIN_TRAP_SHORT, lambda vr: vr >= VOL_RATIO_LONG),
    ("EXT_SHORT", "T7D", "SHORT",
     lambda c: c >= GAIN_EXT_SHORT, lambda vr: vr <= VOL_RATIO_SHORT_MAX),
]


def _fmt(t: datetime) -> str:
    return t.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def build_gainer_alert_html(hits: list[dict]) -> str:
    """构建涨幅榜信号告警邮件 HTML。"""
    rows = ""
    for h in hits:
        vr_s = "n/a" if h["vr"] is None else f"{h['vr']:.2f}"
        fund_s = "n/a" if h["fund"] is None else f"{h['fund']:.4%}"
        color = "#16a34a" if h["direction"] == "LONG" else "#dc2626"
        emoji = "🟢" if h["direction"] == "LONG" else "🔴"
        rows += f"""
        <tr>
          <td style="padding:6px;border:1px solid #eee">{emoji} {h['symbol']}</td>
          <td style="padding:6px;border:1px solid #eee;color:{color}"><b>{h['sig_type']}</b></td>
          <td style="padding:6px;border:1px solid #eee">{h['direction']}</td>
          <td style="padding:6px;border:1px solid #eee">{h['chg']:+.1f}%</td>
          <td style="padding:6px;border:1px solid #eee">${h['price']:,.4f}</td>
          <td style="padding:6px;border:1px solid #eee">{vr_s}</td>
          <td style="padding:6px;border:1px solid #eee">{fund_s}</td>
          <td style="padding:6px;border:1px solid #eee">${h['vol']/1e6:.0f}M</td>
        </tr>"""
    return f"""
    <div style="font-family:sans-serif;max-width:800px;margin:auto">
      <h2 style="color:#1e40af">📊 涨幅榜信号告警（v2）</h2>
      <p>检测到 <b>{len(hits)}</b> 个新信号：</p>
      <table style="border-collapse:collapse;width:100%">
        <tr style="background:#f3f4f6">
          <th style="padding:6px;border:1px solid #eee">代币</th>
          <th style="padding:6px;border:1px solid #eee">信号类型</th>
          <th style="padding:6px;border:1px solid #eee">方向</th>
          <th style="padding:6px;border:1px solid #eee">24h涨幅</th>
          <th style="padding:6px;border:1px solid #eee">价格</th>
          <th style="padding:6px;border:1px solid #eee">量比</th>
          <th style="padding:6px;border:1px solid #eee">资金费率</th>
          <th style="padding:6px;border:1px solid #eee">24h成交额</th>
        </tr>
        {rows}
      </table>
      <p style="color:#666;margin-top:16px">
        <b>信号说明：</b><br>
        • SHORT_LONG：短线做多（≥50%涨幅+放量）→ T+24h<br>
        • MID_LONG：次档做多（20~50%涨幅+放量）→ T+24h<br>
        • TRAP_SHORT：诱多做空（<5%涨幅+放量）→ T+24h<br>
        • EXT_SHORT：极端做空（≥75%涨幅+静默）→ T+7d
      </p>
      <p style="color:#999;font-size:12px">盘面异动扫描系统 v2 · 回测依据：2023~2026全周期529合约</p>
    </div>
    """


def load_vol_avg_7d(conn) -> dict[str, float]:
    """近 7 日日均成交额（USDT）→ {symbol: avg_daily_quote_vol}。"""
    out: dict[str, float] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, SUM(quote_vol) / 7.0 AS avg_daily "
            "FROM biz.asset_klines "
            "WHERE interval = '1h' AND open_time >= NOW() - INTERVAL '7 days' "
            "  AND quote_vol IS NOT NULL GROUP BY symbol",
        )
        for sym, avg in cur.fetchall():
            if avg is not None and float(avg) > 0:
                out[sym] = float(avg)
    return out


def recent_signals(conn, hours: float) -> set[tuple[str, str]]:
    """最近 hours 小时内已出信号的 (symbol, signal_type) 集合（冷却去重）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol, signal_type FROM biz.scan_gainer_signal "
            "WHERE scan_ts >= NOW() - make_interval(hours => %s)",
            (hours,),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


def scan(conn, min_vol: float, cooldown_h: float) -> list[dict]:
    fapi = "https://fapi.binance.com"
    ticker = fapi_get(f"{fapi}/fapi/v1/ticker/24hr")
    premium = fapi_get(f"{fapi}/fapi/v1/premiumIndex")

    funding: dict[str, float] = {}
    for p in premium:
        sym = p.get("symbol", "")
        if p.get("lastFundingRate") is not None:
            funding[sym] = float(p["lastFundingRate"])

    vol_avg = load_vol_avg_7d(conn)
    cooled = recent_signals(conn, cooldown_h)
    now = datetime.now(timezone.utc)

    hits: list[dict] = []
    for t in ticker:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            chg = float(t.get("priceChangePercent") or 0)
            price = float(t.get("lastPrice") or 0)
            vol = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if vol < min_vol:
            continue
        avg = vol_avg.get(sym)
        # 24h量 / 7日日均量。注意 avg 已是「日均」成交额（见 load_vol_avg_7d），
        # 不能再 ×24，否则量比被缩小 24 倍：做空「≤1×」恒真、做多「≥2×」永不触发。
        vr = vol / avg if avg and avg > 0 else None
        fund = funding.get(sym)
        for sig_type, window, direction, c_ok, v_ok in SIGNAL_DEFS:
            if not c_ok(chg):
                continue
            if vr is None:      # 无量数据（非 529 回填合约，如美股代币）→ 量能维度不可判定，跳过
                continue
            if not v_ok(vr):
                continue
            if (sym, sig_type) in cooled:
                continue
            hits.append({
                "symbol": sym, "price": price, "chg": chg, "vol": vol,
                "vr": vr, "fund": fund, "sig_type": sig_type,
                "window": window, "direction": direction, "ts": now,
            })
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="落库（默认 dry-run）")
    ap.add_argument("--alert", action="store_true", help="发送邮件告警（需配置 SMTP）")
    ap.add_argument("--min-vol", type=float, default=MIN_VOL)
    ap.add_argument("--cooldown-h", type=float, default=COOLDOWN_H)
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        hits = scan(conn, args.min_vol, args.cooldown_h)

    print(f"[scan] 命中 {len(hits)} 条（{'APPLY 落库' if args.apply else 'DRY-RUN'}，冷却 {args.cooldown_h}h）")
    for h in hits:
        vr_s = "n/a" if h["vr"] is None else f"{h['vr']:.2f}"
        fund_s = "n/a" if h["fund"] is None else f"{h['fund']:.4%}"
        print(f"  {_fmt(h['ts'])} {h['symbol']:<12} {h['sig_type']:<12} {h['window']:<5} "
              f"{h['direction']:<6} chg={h['chg']:+.1f}% vr={vr_s} "
              f"fund={fund_s} vol=${h['vol']/1e6:.0f}M")
        if args.apply:
            with get_connection(settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO biz.scan_gainer_signal "
                        "(scan_ts, symbol, price_usd, chg_24h_pct, vol_24h_usd, vol_ratio_7d, "
                        " funding_rate, signal_type, signal_window, direction) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (h["ts"], h["symbol"], h["price"], h["chg"], h["vol"], h["vr"],
                         h["fund"], h["sig_type"], h["window"], h["direction"]),
                    )

    # 邮件告警
    if args.alert and hits:
        notifier = EmailNotifier(settings)
        if notifier.configured:
            html = build_gainer_alert_html(hits)
            ok, msg = notifier.send(
                subject=f"[涨幅榜告警] 检测到 {len(hits)} 个新信号",
                body_html=html,
                from_name="盘面异动扫描 v2",
            )
            print(f"[alert] 邮件告警: {msg}")
        else:
            print("[alert] SMTP 未配置，跳过邮件告警")

    return 0


if __name__ == "__main__":
    sys.exit(main())
