#!/usr/bin/env python3
"""盘面异动扫描 · CVD 数据就绪检查：精确 CVD 积累满阈值 → 邮件提醒复校。

CVD 维度回测（拆 S1/S2、S7/S8，设计方案 §8 局限项）依赖 oi_cvd_snapshot 中
cvd_5m_usd 连续积累。本脚本每天检查积累天数（最早非空桶 → 今天），
达到阈值后通过 EmailNotifier 发一封"可以复校了"的提醒邮件，仅发一次
（本地 state 文件去重），避免重复打扰。

用法：
    python phase_check_cvd_ready.py                 # 检查并（达阈值时）提醒
    python phase_check_cvd_ready.py --days 10       # 自定义阈值（默认 14）
    python phase_check_cvd_ready.py --dry-run       # 只打印状态，不发送
"""
from __future__ import annotations

import argparse
import json
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
from crypto_research.utils.time_utils import fmt_bj  # noqa: E402

STATE_FILE = SCRIPT_DIR.parent / "data" / "cvd_ready_state.json"
DEFAULT_MIN_DAYS = 14   # 精确 CVD 需积累 N 天（WS 落地后 1-2 周）
SYMBOL_THRESHOLD = 10   # 至少 N 个符号有数据（防个别符号噪音）

SUBJECT = "🧪 CVD 数据已就绪：可跑维度回测（拆 S1/S2）"
BODY_TEMPLATE = """<html><body style='font-family:Arial,"Microsoft YaHei",sans-serif'>
<h2 style='margin:0'>CVD 数据积累就绪</h2>
<p style='margin:0 0 8px;color:#666'>生成于 {ts} · 盘面异动扫描自动检查</p>
<ul>
<li>数据覆盖天数：<b>{days} 天</b>（阈值 {min_days} 天）</li>
<li>有数据的符号数：<b>{symbols} 个</b></li>
<li>5m 桶总量：<b>{buckets:,} 桶</b></li>
</ul>
<p>现在可以执行 CVD 维度回测，拆分 S1/S2（P↑OI↑CVD↑ vs P↑OI↑CVD↓）与 S7/S8，验证 8 场景理论：</p>
<pre style='background:#f5f5f5;padding:10px'>{cmd}</pre>
<p style='color:#999;font-size:12px'>复校后更新设计方案 §8 标定表；若拆出 CVD 维度有显著差异，需同步修订 L2 置信度分级。</p>
</body></html>"""


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check_cvd(conn) -> dict:
    """返回 {days, symbols, buckets, oldest_ts}。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT symbol) AS symbols,
                   COUNT(*) AS buckets,
                   MIN(ts) AS oldest_ts
            FROM biz.oi_cvd_snapshot
            WHERE cvd_5m_usd IS NOT NULL
            """
        )
        r = cur.fetchone()
    if not r or r[0] is None:
        return {"days": 0, "symbols": 0, "buckets": 0, "oldest_ts": None}
    days = (datetime.now(timezone.utc) - r[2]).days if r[2] else 0
    return {"days": days, "symbols": r[0], "buckets": r[1], "oldest_ts": r[2]}


def main() -> int:
    parser = argparse.ArgumentParser(description="CVD 数据积累检查 → 达阈值邮件提醒复校")
    parser.add_argument("--days", type=int, default=DEFAULT_MIN_DAYS,
                        help=f"积累天数阈值（默认 {DEFAULT_MIN_DAYS}）")
    parser.add_argument("--dry-run", action="store_true", help="只打印状态，不发送")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        status = check_cvd(conn)

    print(f"[cvd] 积累 {status['days']} 天 / {status['symbols']} 符号 / "
          f"{status['buckets']:,} 桶（阈值 {args.days} 天）")
    if status["oldest_ts"]:
        print(f"[cvd] 最早桶: {status['oldest_ts'].isoformat()}")

    if status["days"] < args.days or status["symbols"] < SYMBOL_THRESHOLD:
        print(f"[cvd] 未达阈值（需要 {args.days} 天 + {SYMBOL_THRESHOLD} 符号），跳过提醒")
        return 0

    state = load_state()
    if state.get("notified_at"):
        print(f"[cvd] 已提醒过（{state['notified_at']}），跳过")
        return 0

    if args.dry_run:
        print("[cvd] [dry-run] 达阈值，将发送提醒邮件（本次不发送）")
        return 0

    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("[cvd] SMTP 未配置，跳过发送")
        return 0

    cmd = ("python backtest_scan_scenarios.py --min-n 15\n"
           "python backtest_scan_scenarios.py --sweep  # 可选：阈值复校")
    ts = fmt_bj(datetime.now(timezone.utc), "%Y-%m-%d %H:%M") + "（北京时间）"
    body = BODY_TEMPLATE.format(
        ts=ts, days=status["days"], min_days=args.days,
        symbols=status["symbols"], buckets=status["buckets"], cmd=cmd,
    )
    ok, msg = notifier.send(SUBJECT, body, from_name="盘面信号扫描")
    if ok:
        save_state({**state, "notified_at": ts, "days": status["days"]})
        print(f"[cvd] 提醒邮件已发送: {ts}")
    else:
        print(f"[cvd] 发送失败: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
