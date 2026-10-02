#!/usr/bin/env python3
"""实证联动因子（asset_linkage_factor）效果验证 + 邮件通知（只读 + 一次性通知）。

背景（审计 2026-10-02）：二阶受益已接入实证联动对（derived_from='linkage_factor'，
置信度=实测 lag1 跟涨率）。本脚本在样本足够后自动验证「联动对 vs sector 泛化」
谁更能预测收益，并邮件通知用户（避免遗忘）。

触发逻辑：
- 每日跑一次（调度注册，如 09:40 避开早报/周报槽位）
- 仅当 linkage_factor 来源的已结算信号（excess_72h 非空）≥ MIN_SAMPLES 时才发信
- 幂等：复用 biz.catalyst_notification_log，notification_type='linkage_validation'，
  signal_id=-100（哨兵），每 VALIDATION_COOLDOWN_DAYS 天最多一封
- 邮件内容：linkage_factor vs sector 的 hit_rate / avg_excess 对比 + 样本量

用法：
    python validate_linkage_factor.py          # 只读验证；样本足够才发信
    python validate_linkage_factor.py --force  # 忽略样本阈值与去重，强制发信（调试）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.clients.notifier import EmailNotifier  # noqa: E402

MIN_SAMPLES = 25            # linkage_factor 已结算信号的最小样本量（72h 窗口）
VALIDATION_COOLDOWN_DAYS = 7  # 通知冷却（天）
SENTINEL_SIGNAL_ID = -100   # notification_log 哨兵（避免与真实信号冲突）
NOTIF_TYPE = "linkage_validation"


def _ensure_notif_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS biz.catalyst_notification_log (
            log_id              BIGSERIAL PRIMARY KEY,
            signal_id           BIGINT NOT NULL,
            notification_type   VARCHAR(32) NOT NULL,
            tier                VARCHAR(4),
            sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            subject             VARCHAR(256),
            status              VARCHAR(16) NOT NULL DEFAULT 'sent',
            error_msg           TEXT,
            UNIQUE (signal_id, notification_type)
        );
    """)


def _within_cooldown(conn) -> bool:
    """冷却期内（7 天内已发过）则跳过。"""
    row = conn.execute("""
        SELECT 1 FROM biz.catalyst_notification_log
        WHERE signal_id = %s AND notification_type = %s
          AND sent_at > NOW() - (%s::int * INTERVAL '1 day')
        LIMIT 1
    """, (SENTINEL_SIGNAL_ID, NOTIF_TYPE, VALIDATION_COOLDOWN_DAYS)).fetchone()
    return row is not None


def _mark_sent(conn) -> None:
    """记录本次通知（幂等 upsert，重置冷却窗口）。"""
    conn.execute("""
        INSERT INTO biz.catalyst_notification_log
            (signal_id, notification_type, tier, subject, status)
        VALUES (%s, %s, NULL, %s, 'sent')
        ON CONFLICT (signal_id, notification_type) DO UPDATE SET
            sent_at = NOW(), status = 'sent', error_msg = NULL
    """, (SENTINEL_SIGNAL_ID, NOTIF_TYPE,
          "联动因子效果验证通知"))


def load_validation(conn) -> dict:
    """对比 linkage_factor vs sector 二阶信号的 72h 命中质量。"""
    rows = conn.execute("""
        SELECT so.derived_from AS src,
               count(*) AS n,
               round(avg(o.excess_72h)::numeric, 2) AS avg_excess,
               round(avg(o.hit_72h::int)::numeric, 3) AS hit_rate,
               round(avg(o.ret_72h)::numeric, 2) AS avg_ret,
               round(avg(o.vol_ratio_72h)::numeric, 2) AS avg_vol
        FROM biz.catalyst_second_order so
        JOIN biz.catalyst_outcome o
          ON o.catalyst_id = so.catalyst_id AND o.asset_id = so.asset_id
        WHERE o.excess_72h IS NOT NULL
          AND so.derived_from IN ('linkage_factor', 'sector')
        GROUP BY so.derived_from
        ORDER BY so.derived_from
    """).fetchall()
    out = {}
    for r in rows:
        out[r[0]] = {"src": r[0], "n": r[1], "avg_excess": r[2],
                     "hit_rate": r[3], "avg_ret": r[4], "avg_vol": r[5]}
    return out


def _fmt(v, suffix: str = "") -> str:
    return f"{v}{suffix}" if v is not None else "N/A"


def _render_html(stats: dict, linkage_n: int) -> str:
    rows_html = []
    for src, label in (("linkage_factor", "实证联动对"), ("sector", "板块泛化")):
        s = stats.get(src)
        if not s:
            rows_html.append(
                f"<tr><td>{label}</td><td colspan='5' style='color:#999'>暂无已结算样本</td></tr>"
            )
            continue
        rows_html.append(
            f"<tr><td>{label}</td>"
            f"<td>{s['n']}</td>"
            f"<td>{_fmt(s['hit_rate']*100, '%')}</td>"
            f"<td>{_fmt(s['avg_excess'], '%')}</td>"
            f"<td>{_fmt(s['avg_ret'], '%')}</td>"
            f"<td>{_fmt(s['avg_vol'], 'x')}</td></tr>"
        )
    return ("<table style='border-collapse:collapse;font-size:13px'>"
            "<tr><th style='padding:4px 10px;border:1px solid #ddd'>来源</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>样本</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>72h命中率</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>72h平均超额</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>72h平均收益</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>量比</th></tr>"
            + "".join(rows_html) + "</table>")


def main() -> int:
    parser = argparse.ArgumentParser(description="联动因子效果验证 + 邮件通知")
    parser.add_argument("--force", action="store_true",
                        help="忽略样本阈值与冷却，强制发信（调试用）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        _ensure_notif_table(conn)
        stats = load_validation(conn)
        linkage = stats.get("linkage_factor") or {}
        linkage_n = int(linkage.get("n") or 0)

        print(f"[validate_linkage_factor] linkage_factor 已结算样本: {linkage_n}")

        if not args.force:
            if linkage_n < MIN_SAMPLES:
                print(f"样本不足（{linkage_n} < {MIN_SAMPLES}），跳过通知，等待积累")
                return 0
            if _within_cooldown(conn):
                print(f"冷却期内（{VALIDATION_COOLDOWN_DAYS} 天内已通知过），跳过")
                return 0

        body_html = _render_html(stats, linkage_n)
        subject = f"联动因子效果验证：已累计 {linkage_n} 条结算样本（对比 sector）"

        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("SMTP 未配置，跳过发信")
            return 0
        ok, err = notifier.send(subject, body_html, from_name="联动因子验证")
        if not ok:
            print(f"邮件发送失败: {err}")
            return 1
        _mark_sent(conn)
        conn.commit()
        print(f"[validate_linkage_factor] 邮件已发送: {subject}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
