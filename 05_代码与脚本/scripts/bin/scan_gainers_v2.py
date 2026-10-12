#!/usr/bin/env python3
"""v2 盘面异动扫描引擎：放量大阳事件 × 涨幅榜档位 → 信号落库。

口径（**严格对齐回测** backtest_trade_params.py，2026-10-11 改造）
-----------------------------------------------------------------
  - 数据源：`biz.asset_klines` interval='1h' 的**最后一根已收盘 1h bar**（不再读滚动 24h 快照）。
  - 事件 = 放量大阳 1h bar：`chg1h = close/open − 1 ≥ 3%` 且 `vr = quote_vol ÷ 前 20 根 1h 均量 ≥ 2`。
  - `chg24` = 事件 bar 收盘 ÷ 24h 前最近 bar 收盘 − 1（回看上限 36h，容忍缺 bar）；
    **入场价 = 事件 bar 收盘**（与回测 entry 完全一致）。
  - 档位由 `chg24` 定：A ≥50% / B 30~50% / C <5%；`vr` 只用于 B 右上角过滤。

信号（scan_gainer_signal）：
  SHORT_LONG  短线做多  chg24≥50%                    窗口 T+24h LONG
  MID_LONG    B右上角    chg24 30~50% 且 vr(1h)≥5.6   窗口 T+24h LONG
  TRAP_SHORT  诱多做空  chg24<5%                      窗口 T+24h SHORT

说明：
  - 旧版读 `/fapi/v1/ticker/24hr`（滚动快照）、vr 用「24h量÷7日日均量」的**日量比**口径，
    与回测（1h 事件 bar）不一致，信号时点滞后中位 ~14.5h、87% 不满足 chg1h≥3%。
    本次改为读 1h bar（治本）。`vol_ratio_7d` 列语义随之变为 **1h 量比**（全库唯一消费方=本脚本）。
  - 去重：同一「事件 bar」只出一次（`params->>'event_bar_ts'` 幂等）；同币 12h 冷却可选（`--cooldown-h`，默认 0=关）。
  - EXT_SHORT（极端涨幅 ≥75% + 低量静默做空）经量比口径修复后回测为 0 事件，已于 2026-10-07 移除。
  - 默认 dry-run（只打印不落库）；--apply 才落库。

用法：
    python bin/scan_gainers_v2.py                 # dry-run
    python bin/scan_gainers_v2.py --apply         # 落库
    python bin/scan_gainers_v2.py --apply --cooldown-h 12 --min-vol 20000000
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from psycopg.rows import dict_row  # noqa: E402

from crypto_research.clients.binance_http import fapi_get  # noqa: E402
from crypto_research.clients.notifier import EmailNotifier  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 邮件告警范围 = 实盘白名单（A 短线做多 SHORT_LONG + B 右上角 MID_LONG）；C(TRAP_SHORT) 不实盘、不告警。
# 单一事实来源：优先复用 execute_gainers_v2.LIVE_SIGNALS（其顶层仅 import + 常量，
# 无网络/下单副作用），import 失败才回退本地常量 —— ⚠ 回退值须与
# bin/execute_gainers_v2.py:LIVE_SIGNALS 保持同步。
try:
    from execute_gainers_v2 import LIVE_SIGNALS as _LIVE_SIGNALS  # noqa: E402
    ALERT_SIGNALS: tuple[str, ...] = tuple(_LIVE_SIGNALS)
except Exception:  # noqa: BLE001 —— 任何异常都回退，保证扫描器可用
    ALERT_SIGNALS = ("SHORT_LONG", "MID_LONG")

GAIN_SHORT_LONG = 50.0        # 短线做多：chg24 ≥50%
GAIN_MID_LONG_HI = 50.0
GAIN_TRAP_SHORT = 5.0         # 诱多做空：chg24 <5%
B_TOP_CHG_LO = 30.0           # B 右上角：chg24 ≥30%
B_TOP_VR = 5.6                # B 右上角：**1h 量比** ≥5.6（回测 Q5 下界，口径=1h）
EVENT_CHG1H_MIN = 0.03        # 事件：1h 涨幅 ≥3%
EVENT_VR_MIN = 2.0            # 事件：1h 量比 ≥2
# 最小 24h 成交额（USDT，流动性护栏）。
# ⚠️ 2026-10-12 决策：**对齐回测**——回测无此过滤（backtest_trade_params.py 对全部
# 529 合约做事件扫描），实盘若加 2000 万护栏会导致样本偏差（错过「平时低流动、
# 事件瞬间爆量」的币，如 RSRUSDT：vr=103 事件 bar 爆量 1377 万，但事件时点 24h
# 累计 1682 万 < 2000 万被误杀）。故默认 0=不过滤，让实盘样本 = 回测样本；
# 滑点风险实盘自担（100U 实验阶段可接受，CLI --min-vol 可随时收紧）。
MIN_VOL = 0
COOLDOWN_H = 0                # 同币同类型冷却（小时，0=关；由事件 bar 幂等去重替代）
EVENT_LOOKBACK_DAYS = 5       # 事件 SQL 回看天数（需覆盖 20 根均量窗 + 36h chg24 回看）

# 信号类型 → (窗口, 方向, 判定)。c24=chg24(百分数)，vr=1h 量比。
SIGNAL_DEFS = [
    ("SHORT_LONG", "T24H", "LONG",
     lambda c24, vr: c24 >= GAIN_SHORT_LONG),
    ("MID_LONG", "T24H", "LONG",
     lambda c24, vr: B_TOP_CHG_LO <= c24 < GAIN_MID_LONG_HI and vr >= B_TOP_VR),
    ("TRAP_SHORT", "T24H", "SHORT",
     lambda c24, vr: c24 < GAIN_TRAP_SHORT),
]

# 事件 SQL（严格镜像 backtest_trade_params.py 的 evt/dchg）：最后一根【已收盘】1h bar。
EVENT_SQL = """
WITH kl AS (
    SELECT symbol, open_time, open_px, close_px, quote_vol,
           AVG(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
               ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg20,
           SUM(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
               ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS vol24
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0
      AND open_time >= date_trunc('hour', NOW()) - (%(lookback_days)s * INTERVAL '1 day')
),
lastc AS (
    -- 每 symbol 的最后一根【已收盘】1h bar（显式排除当前未收盘 bar）
    SELECT symbol, MAX(open_time) AS eo
    FROM kl
    WHERE open_time < date_trunc('hour', NOW())
    GROUP BY symbol
),
evt AS (
    SELECT k.symbol, k.open_time AS eo, k.close_px AS entry,
           k.close_px / k.open_px - 1 AS chg1h,
           k.quote_vol / NULLIF(k.avg20, 0) AS vr,
           k.vol24 AS vol_24h
    FROM kl k
    JOIN lastc l ON l.symbol = k.symbol AND l.eo = k.open_time
    WHERE k.avg20 > 0
      AND k.quote_vol / NULLIF(k.avg20, 0) >= %(vr_min)s
      AND k.close_px / k.open_px - 1 >= %(chg1h_min)s
),
dchg AS (
    -- 事件时点可得的滚动 24h 涨幅：事件 bar 收盘 ÷ 24h 前最近 bar 收盘（回看上限 36h）
    SELECT DISTINCT ON (e.symbol, e.eo)
           e.symbol, e.eo, e.entry / p.close_px - 1 AS chg24
    FROM evt e
    JOIN biz.asset_klines p
      ON p.symbol = e.symbol AND p.interval = '1h' AND p.close_px > 0
     AND p.open_time <= e.eo - INTERVAL '24 hours'
     AND p.open_time >  e.eo - INTERVAL '36 hours'
    ORDER BY e.symbol, e.eo, p.open_time DESC
)
SELECT e.symbol, e.eo, e.entry, e.chg1h, e.vr, e.vol_24h, d.chg24
FROM evt e
JOIN dchg d ON d.symbol = e.symbol AND d.eo = e.eo
"""


def _fmt(t: datetime) -> str:
    return t.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def alert_subject(hits: list[dict]) -> str:
    """告警邮件标题：按实盘类型统计，只列本次有命中的类型（A 短线 / B 右上角）。"""
    n_a = sum(1 for h in hits if h["sig_type"] == "SHORT_LONG")
    n_b = sum(1 for h in hits if h["sig_type"] == "MID_LONG")
    parts = []
    if n_a:
        parts.append(f"A 信号 {n_a} 个")
    if n_b:
        parts.append(f"B 信号 {n_b} 个")
    return "[涨幅榜告警] " + " / ".join(parts)


def load_exit_profile() -> dict:
    """加载 backtest_exit_profile.py 生成的出场画像（胜率/期望/PF/持仓时长）。

    文件不存在或损坏时返回空 dict（告警邮件不附预期，不影响扫描主流程）。
    """
    p = SCRIPT_DIR.parent / "data" / "exit_profile.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("signals", {})
    except Exception:
        return {}


def build_gainer_alert_html(hits: list[dict], profiles: dict | None = None) -> str:
    """构建涨幅榜信号告警邮件 HTML。profiles 提供各信号回测预期。"""
    profiles = profiles or {}
    rows = ""
    for h in hits:
        vr_s = "n/a" if h["vr"] is None else f"{h['vr']:.2f}"
        fund_s = "n/a" if h["fund"] is None else f"{h['fund']:.4%}"
        color = "#16a34a" if h["direction"] == "LONG" else "#dc2626"
        emoji = "🟢" if h["direction"] == "LONG" else "🔴"
        # 回测预期
        prof = profiles.get(h["sig_type"])
        exp_s = "—"
        if prof:
            win = prof.get("win_rate")
            mean = prof.get("mean")
            if win is not None and mean is not None:
                exp_s = f"{win*100:.0f}% / {mean*100:+.1f}%"
        rows += f"""
        <tr>
          <td style="padding:6px;border:1px solid #eee">{emoji} {h['symbol']}</td>
          <td style="padding:6px;border:1px solid #eee;color:{color}"><b>{h['sig_type']}</b></td>
          <td style="padding:6px;border:1px solid #eee">{h['direction']}</td>
          <td style="padding:6px;border:1px solid #eee">{h['chg']:+.1f}%</td>
          <td style="padding:6px;border:1px solid #eee">${h['price']:,.4f}</td>
          <td style="padding:6px;border:1px solid #eee">{h['chg1h']:+.1f}%</td>
          <td style="padding:6px;border:1px solid #eee">{vr_s}</td>
          <td style="padding:6px;border:1px solid #eee">{fund_s}</td>
          <td style="padding:6px;border:1px solid #eee">${h['vol']/1e6:.0f}M</td>
          <td style="padding:6px;border:1px solid #eee">{exp_s}</td>
        </tr>"""
    return f"""
    <div style="font-family:sans-serif;max-width:920px;margin:auto">
      <h2 style="color:#1e40af">📊 涨幅榜信号告警（v2）</h2>
      <p>检测到 <b>{len(hits)}</b> 个新信号（事件 bar：放量大阳 1h）：</p>
      <table style="border-collapse:collapse;width:100%">
        <tr style="background:#f3f4f6">
          <th style="padding:6px;border:1px solid #eee">代币</th>
          <th style="padding:6px;border:1px solid #eee">信号类型</th>
          <th style="padding:6px;border:1px solid #eee">方向</th>
          <th style="padding:6px;border:1px solid #eee">24h涨幅</th>
          <th style="padding:6px;border:1px solid #eee">入场价(事件收盘)</th>
          <th style="padding:6px;border:1px solid #eee">1h涨幅</th>
          <th style="padding:6px;border:1px solid #eee">1h量比</th>
          <th style="padding:6px;border:1px solid #eee">资金费率</th>
          <th style="padding:6px;border:1px solid #eee">24h成交额</th>
          <th style="padding:6px;border:1px solid #eee">回测胜率/期望</th>
        </tr>
        {rows}
      </table>
      <p style="color:#666;margin-top:16px">
        <b>信号说明：</b>（实盘白名单 A+B；C 诱多做空不实盘、不告警）<br>
        • SHORT_LONG（A）：短线做多 ≥50%涨幅（事件 bar：1h≥3% 放量）<br>
        • MID_LONG（B 右上角）：次档做多 30~50%涨幅 + 1h量比≥5.6
      </p>
      <p style="color:#999;font-size:12px">盘面异动扫描系统 v2 · 回测依据：2023~2026全周期529合约<br>
      回测口径：1h 事件 bar（chg1h≥3% & vr≥2），入场价=事件 bar 收盘，与 backtest_trade_params.py 对齐。</p>
    </div>
    """


def load_events(conn) -> list[dict]:
    """读 1h 事件（放量大阳 + 滚动 24h 涨幅），严格对齐回测 SQL。"""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(EVENT_SQL, {
            "vr_min": EVENT_VR_MIN,
            "chg1h_min": EVENT_CHG1H_MIN,
            "lookback_days": EVENT_LOOKBACK_DAYS,
        })
        return cur.fetchall()


def load_funding() -> dict[str, float]:
    """当前资金费率（best-effort）。拉取失败返回空 dict（funding 维度记 n/a，不影响主流程）。"""
    out: dict[str, float] = {}
    try:
        premium = fapi_get("https://fapi.binance.com/fapi/v1/premiumIndex")
        for p in premium:
            sym = p.get("symbol", "")
            if p.get("lastFundingRate") is not None:
                out[sym] = float(p["lastFundingRate"])
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 资金费率拉取失败（funding=n/a）：{e}")
    return out


def load_emitted_events(conn) -> set[tuple[str, str]]:
    """近 2 天已落库的 (symbol, event_bar_ts) 集合（事件 bar 幂等去重）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol, params->>'event_bar_ts' FROM biz.scan_gainer_signal "
            "WHERE scan_ts >= NOW() - INTERVAL '2 days' "
            "  AND params->>'event_bar_ts' IS NOT NULL",
        )
        return {(r[0], r[1]) for r in cur.fetchall() if r[1]}


def recent_signals(conn, hours: float) -> set[tuple[str, str]]:
    """最近 hours 小时内已出信号的 (symbol, signal_type) 集合（可选冷却去重）。"""
    if hours <= 0:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol, signal_type FROM biz.scan_gainer_signal "
            # 用乘法而非 make_interval(hours => %s)：后者只接受 int，CLI 传 float 会报
            # "function make_interval(hours => double precision) does not exist"。
            "WHERE scan_ts >= NOW() - (%s * INTERVAL '1 hour')",
            (hours,),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


def scan(conn, min_vol: float, cooldown_h: float) -> list[dict]:
    events = load_events(conn)
    funding = load_funding()
    emitted = load_emitted_events(conn)
    cooled = recent_signals(conn, cooldown_h)
    now = datetime.now(timezone.utc)

    hits: list[dict] = []
    for e in events:
        sym = e["symbol"]
        vol24 = float(e["vol_24h"] or 0)
        if vol24 < min_vol or e["chg24"] is None:
            continue
        chg24_pct = float(e["chg24"]) * 100.0
        chg1h = float(e["chg1h"]) * 100.0
        vr = float(e["vr"])
        eo_iso = e["eo"].isoformat()
        # 同一事件 bar 只出一次（不再依赖 30min 轮询频率去重）
        if (sym, eo_iso) in emitted:
            continue
        for sig_type, window, direction, ok in SIGNAL_DEFS:
            if not ok(chg24_pct, vr):
                continue
            if (sym, sig_type) in cooled:
                continue
            hits.append({
                "symbol": sym, "price": float(e["entry"]), "chg": chg24_pct,
                "chg1h": chg1h, "vol": vol24, "vr": vr, "fund": funding.get(sym),
                "sig_type": sig_type, "window": window, "direction": direction,
                "ts": now, "event_bar_ts": eo_iso,
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

    print(f"[scan] 命中 {len(hits)} 条（{'APPLY 落库' if args.apply else 'DRY-RUN'}，"
          f"冷却 {args.cooldown_h}h，最小24h额 ${args.min_vol/1e6:.0f}M）")
    for h in hits:
        vr_s = "n/a" if h["vr"] is None else f"{h['vr']:.2f}"
        fund_s = "n/a" if h["fund"] is None else f"{h['fund']:.4%}"
        print(f"  {_fmt(h['ts'])} {h['symbol']:<12} {h['sig_type']:<12} {h['window']:<5} "
              f"{h['direction']:<6} chg24={h['chg']:+.1f}% chg1h={h['chg1h']:+.1f}% "
              f"vr1h={vr_s} fund={fund_s} vol=${h['vol']/1e6:.0f}M "
              f"@${h['price']:,.4f} bar={h['event_bar_ts']}")
        if args.apply:
            with get_connection(settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO biz.scan_gainer_signal "
                        "(scan_ts, symbol, price_usd, chg_24h_pct, vol_24h_usd, vol_ratio_7d, "
                        " funding_rate, signal_type, signal_window, direction, params) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (h["ts"], h["symbol"], h["price"], h["chg"], h["vol"], h["vr"],
                         h["fund"], h["sig_type"], h["window"], h["direction"],
                         json.dumps({"caliber": "1h_event", "event_bar_ts": h["event_bar_ts"],
                                     "chg1h_pct": round(h["chg1h"], 4),
                                     "vr_1h": round(h["vr"], 4),
                                     "vol_24h_usd": h["vol"]})),
                    )

    # 邮件告警（实盘白名单 A=SHORT_LONG + B=MID_LONG 右上角；C 不实盘、不告警）
    if args.alert and hits:
        alert_hits = [h for h in hits if h["sig_type"] in ALERT_SIGNALS]
        if not alert_hits:
            print("[alert] 本次无 A/B 实盘信号，不发送邮件")
        else:
            notifier = EmailNotifier(settings)
            if notifier.configured:
                profiles = load_exit_profile()
                html = build_gainer_alert_html(alert_hits, profiles)
                ok, msg = notifier.send(
                    subject=alert_subject(alert_hits),
                    body_html=html,
                    from_name="盘面异动扫描 v2",
                )
                print(f"[alert] 邮件告警: {msg}")
            else:
                print("[alert] SMTP 未配置，跳过邮件告警")

    return 0


if __name__ == "__main__":
    sys.exit(main())