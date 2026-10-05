#!/usr/bin/env python3
"""追涨风险告警（盘面扫描）：币安涨幅 ≥ 阈值 + 正资金费率 + 放量 = 拥挤追涨高危。

回测依据（《涨幅榜冲高回落回测方案_2026-10-03.md》Part C · 币安永续版）：
  - 涨幅 ≥20% + 正资金费率（拥挤多头）：次日跌概率 62~64%（vs 近零费率 48%，差 ~15pp）；
  - 放量冲榜更差（高成交额档 7 日跌概率 54% vs 低档 40%）。
⇒ 组合「24h 涨幅 ≥20% + 正资金费率 + 放量」是短期追涨高危信号：**提示规避/勿追**（不是做空信号）。

数据源（实时，覆盖全部 Binance USDT 永续）：
  - `GET /fapi/v1/ticker/24hr`（全量）：现价 / 24h 涨幅 / 24h 成交额
  - `GET /fapi/v1/premiumIndex`（全量）：最新资金费率
  - 放量倍数：近 7 日日均成交额取自 biz.asset_klines（1h，无则跳过该维度）

输出：
  - 落库 `biz.scan_chase_risk`（迁移 fix_089_scan_chase_risk.sql）
  - EmailNotifier 邮件告警（默认 --dry-run 只打印不落库不发信）
  - 冷却：同一 symbol 最近 N 小时已告警则跳过（默认 6h）

风险分级：
  - high：24h 涨幅 ≥50% 或（≥20% 且 资金费率 ≥0.05%）
  - medium：其余命中（≥20% + 正费率 + 放量）

用法：
    python scan_chase_risk.py                     # dry-run：打印命中（不落库不发信）
    python scan_chase_risk.py --apply             # 落库 + 发信
    python scan_chase_risk.py --gain-thr 30 --fund-thr 0.0005 --min-vol 100000000
    python scan_chase_risk.py --apply --cooldown-h 12 --dry-run-email
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.binance_http import fapi_get  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

GAIN_THR = 20.0                 # 24h 涨幅阈值（%）
FUND_THR = 0.0001               # 资金费率阈值（正费率 = 拥挤多头；1bp）
MIN_VOL = 50_000_000            # 最小 24h 成交额（USDT，过滤无流动性）
GAIN_HIGH = 50.0                # high 分级：涨幅 ≥50%
FUND_HIGH = 0.0005              # high 分级：费率 ≥5bp
COOLDOWN_H = 6                  # 同 symbol 冷却（小时）
VOL_RATIO_MIN = 2.0             # 放量倍数阈值（可选维度，缺 klines 数据则忽略）


def _fmt(t: datetime) -> str:
    return t.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def load_vol_avg_7d(conn) -> dict[str, float]:
    """近 7 日日均成交额（USDT）→ {symbol: avg_daily_quote_vol}。"""
    out: dict[str, float] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, SUM(quote_vol) / 7.0 AS avg_daily "
            "FROM biz.asset_klines "
            "WHERE interval = '1h' AND open_time >= NOW() - INTERVAL '7 days' "
            "  AND quote_vol IS NOT NULL "
            "GROUP BY symbol",
        )
        for sym, avg in cur.fetchall():
            if avg is not None and float(avg) > 0:
                out[sym] = float(avg)
    return out


def recent_alerts(conn, hours: float) -> set[str]:
    """最近 hours 小时内已告警的 symbol 集合（冷却去重）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.scan_chase_risk "
            "WHERE alert_ts >= NOW() - make_interval(hours => %s)",
            (hours,),
        )
        return {r[0] for r in cur.fetchall()}


def scan(conn, gain_thr: float, fund_thr: float, min_vol: float,
         cooldown_h: float) -> list[dict]:
    fapi = "https://fapi.binance.com"
    ticker = fapi_get(f"{fapi}/fapi/v1/ticker/24hr")
    premium = fapi_get(f"{fapi}/fapi/v1/premiumIndex")

    funding: dict[str, float] = {}
    for p in premium:
        sym = p.get("symbol", "")
        if p.get("lastFundingRate") is not None:
            funding[sym] = float(p["lastFundingRate"])

    vol_avg = load_vol_avg_7d(conn)
    cooled = recent_alerts(conn, cooldown_h)

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
        fund = funding.get(sym)
        if chg < gain_thr or vol < min_vol:
            continue
        if fund is None or fund <= fund_thr:
            continue
        reason = ["chg_ge20", "funding_positive"]
        if chg >= GAIN_HIGH or fund >= FUND_HIGH:
            reason.append("extreme")
        ratio = None
        if sym in vol_avg and vol_avg[sym] > 0:
            ratio = vol / vol_avg[sym]
            if ratio >= VOL_RATIO_MIN:
                reason.append("volume_spike")
        if sym in cooled:
            reason.append("cooldown_skip")
            continue
        level = "high" if (chg >= GAIN_HIGH or fund >= FUND_HIGH) else "medium"
        hits.append({
            "symbol": sym, "price": price, "chg": chg, "fund": fund,
            "vol": vol, "vol_ratio": ratio, "level": level, "reason": reason,
        })
    hits.sort(key=lambda x: -x["chg"])
    return hits


def render_html(hits: list[dict]) -> str:
    rows = []
    for h in hits:
        ratio = f"{h['vol_ratio']:.1f}x" if h["vol_ratio"] is not None else "--"
        rows.append(
            f"<tr><td><b>{html.escape(h['symbol'])}</b></td>"
            f"<td style='color:#c0392b'><b>+{h['chg']:.1f}%</b></td>"
            f"<td>{h['fund'] * 100:.3f}%</td>"
            f"<td>{h['vol'] / 1e6:.0f}M</td>"
            f"<td>{ratio}</td>"
            f"<td>{h['level']}</td></tr>"
        )
    body = (
        "<h3>🚨 追涨风险告警（币安永续）</h3>"
        "<p>以下合约同时命中「24h 涨幅 ≥20% + 正资金费率（拥挤多头）+ 放量」，"
        "回测显示该组合次日跌概率 62~64%（vs 近零费率 48%）——<b>提示规避/勿追</b>，"
        "不构成做空信号。</p>"
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;font-size:13px'>"
        "<tr><th>合约</th><th>24h涨幅</th><th>资金费率</th><th>24h成交额</th>"
        "<th>放量倍数</th><th>风险级</th></tr>"
        + "".join(rows)
        + "</table>"
        "<p style='color:#999;font-size:12px'>依据：《涨幅榜冲高回落回测方案_2026-10-03.md》"
        "Part C（币安永续版，~3.5 个月样本，provisional）。</p>"
    )
    return f"<html><body style='font-family:Arial,\"Microsoft YaHei\",sans-serif'>{body}</body></html>"


def main() -> int:
    parser = argparse.ArgumentParser(description="追涨风险告警（涨幅+正费率+放量）")
    parser.add_argument("--gain-thr", type=float, default=GAIN_THR, help="24h 涨幅阈值（%）")
    parser.add_argument("--fund-thr", type=float, default=FUND_THR, help="资金费率阈值（正=拥挤）")
    parser.add_argument("--min-vol", type=float, default=MIN_VOL, help="最小 24h 成交额（USDT）")
    parser.add_argument("--cooldown-h", type=float, default=COOLDOWN_H, help="同合约冷却（小时）")
    parser.add_argument("--apply", action="store_true", help="落库 + 发信（默认 dry-run）")
    parser.add_argument("--dry-run-email", action="store_true",
                        help="--apply 下只打印邮件不发送（测试渲染）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        hits = scan(conn, args.gain_thr, args.fund_thr, args.min_vol, args.cooldown_h)

    print(f"[chase-risk] 命中 {len(hits)} 个合约 "
          f"(涨幅≥{args.gain_thr:.0f}% + 费率>{args.fund_thr:.4%} + 成交额≥${args.min_vol / 1e6:.0f}M)")
    print(f"{'合约':<16}{'24h涨幅%':>8}{'费率%':>9}{'成交额M':>9}{'放量':>7}{'级别':>7}")
    for h in hits:
        ratio = f"{h['vol_ratio']:.1f}x" if h["vol_ratio"] is not None else "--"
        print(f"{h['symbol']:<16}{h['chg']:>8.1f}{h['fund'] * 100:>9.3f}"
              f"{h['vol'] / 1e6:>9.0f}{ratio:>7}{h['level']:>7}")
    if not hits:
        print("[chase-risk] 无命中")
        return 0
    if not args.apply:
        print("[chase-risk] dry-run：加 --apply 落库并发信")
        return 0

    # 落库（含冷却去重后的命中）
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            for h in hits:
                cur.execute(
                    "INSERT INTO biz.scan_chase_risk "
                    "(alert_ts, symbol, price_usd, chg_24h_pct, funding_rate, "
                    " vol_24h_usd, vol_ratio_7d, risk_level, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (datetime.now(timezone.utc), h["symbol"], h["price"], h["chg"],
                     h["fund"], h["vol"], h["vol_ratio"], h["level"], h["reason"]),
                )
    print(f"[chase-risk] 已落库 {len(hits)} 条 → biz.scan_chase_risk")

    if args.dry_run_email:
        print(render_html(hits))
        return 0
    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("[WARN] SMTP 未配置，跳过发送")
        print(render_html(hits))
        return 0
    ok, msg = notifier.send(
        f"🚨 追涨风险告警（{len(hits)} 币 · {_fmt(datetime.now(timezone.utc))}）",
        render_html(hits),
        from_name="盘面扫描",
    )
    if ok:
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.scan_chase_risk SET emailed_at = %s "
                    "WHERE alert_ts >= %s AND emailed_at IS NULL",
                    (datetime.now(timezone.utc),
                     datetime.now(timezone.utc) - timedelta(minutes=10)),
                )
    print(f"[chase-risk] 发送结果: {'成功' if ok else '失败'} - {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
