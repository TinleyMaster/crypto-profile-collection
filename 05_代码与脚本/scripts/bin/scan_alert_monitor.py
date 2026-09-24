#!/usr/bin/env python3
"""盘面异动扫描 · 盘面触发层：高置信异动 → 共振核查 → 实时邮件告警。

触发条件（"极高概率异动"代理，见设计方案 §4.3 标定后置信度）：
  - 主池 high 置信信号（P↑OI↑ + 环境顺风，唯一稳定正期望组合）
  - 或蓄势池 BRK high（放量突破）
  - 同币 12h 冷却；只处理最近 20 分钟内产生的新信号

共振核查（两层碰撞）：
  - 事件先行·埋伏型：event_watchlist 命中（解锁倒计时 / 链上大额转账）
  - 盘面先行·确认型：近 7 天催化剂 / KOL prediction 同方向

用法：
    python scan_alert_monitor.py              # 扫新增信号并发送告警邮件
    python scan_alert_monitor.py --dry-run    # 打印不发送
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
from crypto_research.utils.time_utils import fmt_bj  # noqa: E402

NEW_WINDOW_MIN = 20      # 只告警最近 N 分钟内的新信号
COOLDOWN_H = 12          # 同币告警冷却
CATALYST_DAYS = 7
KOL_DAYS = 7


def load_candidates(conn, window_min: int) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT id, signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                   vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate, context_tags
            FROM biz.scan_signal
            WHERE confidence = 'high'
              AND (pool = 'main' OR (pool = 'accumulation' AND scenario = 'BRK'))
              AND alerted_at IS NULL
              AND signal_ts > NOW() - make_interval(mins => %s)
            ORDER BY signal_ts DESC
            """,
            (window_min,),
        )
        return cur.fetchall()


def in_cooldown(conn, symbol: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM biz.scan_signal "
            "WHERE symbol=%s AND confidence='high' AND alerted_at IS NOT NULL "
            "AND alerted_at > NOW() - make_interval(hours => %s) LIMIT 1",
            (symbol, COOLDOWN_H),
        )
        return cur.fetchone() is not None


def get_asset_id(conn, symbol: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = %s", (symbol,))
        r = cur.fetchone()
        return r[0] if r else None


def get_resonance(conn, symbol: str, asset_id: int | None) -> dict:
    """返回 {event: [...], catalyst: [...], kol: [...]} 三段共振信息。"""
    out: dict = {"event": [], "catalyst": [], "kol": []}
    # 1) 事件预置（领先型）
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT event_type, event_date, event_pct, detail FROM biz.event_watchlist "
            "WHERE symbol = %s", (symbol,))
        for r in cur.fetchall():
            out["event"].append(f"{'🔓解锁' if r['event_type'] == 'unlock' else '🔄链上转账'}: {r['detail']}")

    if not asset_id:
        return out
    # 2) 催化剂（已发布，确认型）
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT ac.title, ac.published_at, ci.impact_direction, ci.impact_strength
            FROM biz.catalyst_impact ci
            JOIN biz.asset_catalyst ac ON ac.catalyst_id = ci.catalyst_id
            WHERE ci.asset_id = %s AND ac.published_at > NOW() - INTERVAL '%s days'
            ORDER BY ac.published_at DESC LIMIT 4
            """,
            (asset_id, CATALYST_DAYS),
        )
        for r in cur.fetchall():
            tag = f"{r['impact_direction']}/{r['impact_strength']}"
            out["catalyst"].append(f"{r['title'][:60]}（{tag}，{str(r['published_at'])[:10]}）")
    # 3) KOL 预测（滞后确认型）
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT direction, symbol, confidence, created_at
            FROM biz.kol_signal
            WHERE asset_id = %s AND post_type = 'prediction'
              AND created_at > NOW() - INTERVAL '%s days'
            ORDER BY created_at DESC LIMIT 4
            """,
            (asset_id, KOL_DAYS),
        )
        for r in cur.fetchall():
            out["kol"].append(f"KOL {r['direction']} {r['symbol']} (conf {float(r['confidence']):.2f}, {str(r['created_at'])[:10]})")
    return out


def render_alert_email(items: list[dict]) -> str:
    now = fmt_bj(datetime.now(timezone.utc), "%Y-%m-%d %H:%M") + "（北京时间）"
    blocks = []
    for it in items:
        sig = it["signal"]
        tags = " ".join(sig["context_tags"] or [])
        head = (f"<h3 style='margin:16px 0 6px'>{sig['symbol']} "
                f"<span style='color:#c0392b'>[{sig['scenario']}]</span> "
                f"<span style='color:#666;font-weight:normal'>P={sig['p_dir']} {sig['price_chg_pct']}% "
                f"VOL={sig['vol_ratio']}x OI={sig['oi_dir']}({sig['oi_chg_pct']}%) CVD={sig['cvd_dir']} "
                f"· {sig['timeframe']} · {tags}</span></h3>")
        lines = [head]
        res = it["resonance"]
        if res["event"]:
            lines.append("<p style='margin:4px 0'><b style='color:#8e44ad'>事件先行·埋伏型</b><br>" +
                         "<br>".join(f"&nbsp;&nbsp;· {x}" for x in res["event"]) + "</p>")
        if res["catalyst"]:
            lines.append("<p style='margin:4px 0'><b style='color:#2980b9'>催化剂共振</b><br>" +
                         "<br>".join(f"&nbsp;&nbsp;· {x}" for x in res["catalyst"]) + "</p>")
        if res["kol"]:
            lines.append("<p style='margin:4px 0'><b style='color:#27ae60'>KOL 确认</b><br>" +
                         "<br>".join(f"&nbsp;&nbsp;· {x}" for x in res["kol"]) + "</p>")
        if not (res["event"] or res["catalyst"] or res["kol"]):
            lines.append("<p style='margin:4px 0;color:#999'>无共振信息（纯盘面信号）</p>")
        blocks.append("".join(lines))
    body = "".join(blocks)
    footnote = ("<p style='color:#999;font-size:12px'>盘面触发=主池高置信（P↑OI↑，标定最优组合）或蓄势突破；"
                "本邮件为盘面数据分析参考，不构成投资建议。</p>")
    return (f"<html><body style='font-family:Arial,\"Microsoft YaHei\",sans-serif'>"
            f"<h2 style='margin:0'>🚨 盘面异动告警</h2>"
            f"<p style='margin:0 0 8px;color:#666;font-size:13px'>生成于 {now} · 共 {len(items)} 个币</p>"
            f"{body}{footnote}</body></html>")


def main() -> int:
    parser = argparse.ArgumentParser(description="盘面高置信异动实时告警（含共振核查）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不发送")
    parser.add_argument("--window-min", type=int, default=NEW_WINDOW_MIN,
                        help=f"回溯窗口分钟（默认 {NEW_WINDOW_MIN}，手动验证可调大）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        candidates = load_candidates(conn, args.window_min)
        # 同币冷却过滤（同一币只取最新一条）
        seen: set[str] = set()
        to_alert: list[dict] = []
        for c in candidates:
            if c["symbol"] in seen or in_cooldown(conn, c["symbol"]):
                continue
            seen.add(c["symbol"])
            to_alert.append({
                "signal": c,
                "resonance": get_resonance(conn, c["symbol"], get_asset_id(conn, c["symbol"])),
            })

        print(f"[alert] 候选 {len(candidates)} 条 → 冷却/去重后 {len(to_alert)} 条")
        for it in to_alert:
            res = it["resonance"]
            print(f"  {it['signal']['symbol']} {it['signal']['scenario']} "
                  f"event={len(res['event'])} catalyst={len(res['catalyst'])} kol={len(res['kol'])}")

        if not to_alert:
            return 0
        html = render_alert_email(to_alert)
        if args.dry_run:
            print(html)
            return 0

        from crypto_research.clients.notifier import EmailNotifier
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[WARN] SMTP 未配置，跳过发送")
            print(html)
            return 0
        ok, msg = notifier.send(
            f"🚨 盘面异动告警：{len(to_alert)} 币高置信信号（含共振）",
            html,
            from_name="盘面信号扫描",
        )
        if ok:
            ids = [it["signal"]["id"] for it in to_alert]
            with conn.cursor() as cur:
                cur.execute("UPDATE biz.scan_signal SET alerted_at = NOW() WHERE id = ANY(%s)", (ids,))
            print(f"[alert] 已发送并标记 {len(ids)} 条信号 alerted_at")
        else:
            print(f"[alert] 发送失败: {msg}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
