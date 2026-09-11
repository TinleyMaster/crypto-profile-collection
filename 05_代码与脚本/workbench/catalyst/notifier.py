"""催化剂信号通知模块。

快提醒（即时 A 级信号）：
    - 检测到新增 A 级信号时立即推送
    - 简洁版：资产 + 事件类型 + 分数 + 核心逻辑

正式邮件（慢通道汇总）：
    - 每轮慢通道结束后汇总
    - 包括 A/B 级新信号 + 二阶受益机会 + 过期信号统计
    - 表格化展示

设计原则：
    - 失败不影响主流程（所有异常 try/except 兜底）
    - 未配置 SMTP 时静默跳过
    - 去重：通过 notification_log 表记录已发送信号，避免重复提醒
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

# 通知类型常量
NTYPE_FAST_ALERT = "fast_alert"       # 快通道 A 级即时提醒
NTYPE_SLOW_DIGEST = "slow_digest"     # 慢通道汇总邮件

# 去重窗口：同一信号同一类型 24h 内不重复发
DEDUP_WINDOW_HOURS = 24


# =====================================================================
# 去重表 DDL（首次使用自动建表）
# =====================================================================

def ensure_notification_table(conn) -> None:
    """确保 notification_log 表存在。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS biz.catalyst_notification_log (
            log_id          BIGSERIAL PRIMARY KEY,
            signal_id       BIGINT,
            notification_type   VARCHAR(32) NOT NULL,   -- fast_alert / slow_digest
            tier            VARCHAR(4),
            sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            subject         VARCHAR(256),
            status          VARCHAR(16) NOT NULL DEFAULT 'sent',  -- sent / failed / skipped
            error_msg       TEXT,
            UNIQUE(signal_id, notification_type)
        );
    """)
    # 索引
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cat_notif_type_time
        ON biz.catalyst_notification_log (notification_type, sent_at DESC);
    """)


def _is_sent(conn, signal_id: int, ntype: str) -> bool:
    """检查某信号是否已在去重窗口内发送过指定类型通知。"""
    row = conn.execute("""
        SELECT 1 FROM biz.catalyst_notification_log
        WHERE signal_id = %s AND notification_type = %s
          AND sent_at > NOW() - INTERVAL '%s hours'
        LIMIT 1
    """, (signal_id, ntype, DEDUP_WINDOW_HOURS)).fetchone()
    return row is not None


def _mark_sent(conn, signal_id: int | None, ntype: str, tier: str | None,
               subject: str, status: str = "sent", error_msg: str | None = None) -> None:
    """记录发送日志。"""
    try:
        conn.execute("""
            INSERT INTO biz.catalyst_notification_log
                (signal_id, notification_type, tier, subject, status, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (signal_id, notification_type) DO UPDATE
                SET sent_at = NOW(), status = EXCLUDED.status,
                    error_msg = EXCLUDED.error_msg, subject = EXCLUDED.subject
        """, (signal_id, ntype, tier, subject, status, error_msg))
    except Exception as e:
        logger.warning("记录通知日志失败: %s", e)


# =====================================================================
# 邮件发送（复用 crypto_research.clients.notifier）
# =====================================================================

def _get_email_notifier():
    """获取 EmailNotifier 实例。失败返回 None（静默降级）。"""
    try:
        from crypto_research.clients.notifier import EmailNotifier
        from crypto_research.config import get_settings
        # 通知器不碰 DB，避免 prod 缺 DATABASE_URL 时把邮件一起拖死
        # （与 kol/notifier.py 保持一致的写法）
        settings = get_settings(require_database=False)
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            logger.warning(
                "SMTP 未配置：SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO 存在空值"
            )
            return None
        return notifier
    except Exception as e:
        logger.warning("构建邮件通知器失败: %s", e, exc_info=True)
        return None


def _send_email(subject: str, body_html: str) -> tuple[bool, str]:
    """发送邮件。失败返回 (False, reason)。"""
    notifier = _get_email_notifier()
    if notifier is None:
        return False, "SMTP 未配置或不可用"
    try:
        ok, msg = notifier.send(subject, body_html)
        return ok, msg
    except Exception as e:
        return False, str(e)


# =====================================================================
# 快通道：A 级信号即时提醒
# =====================================================================

def send_fast_alerts_for_new_signals(conn, new_signal_ids: list[int]) -> dict:
    """对新入库的 A 级信号发送快提醒。

    Args:
        conn: 数据库连接
        new_signal_ids: 本次新生成的信号 ID 列表

    Returns:
        dict: {sent, skipped, failed, signals: [...]}
    """
    if not new_signal_ids:
        return {"sent": 0, "skipped": 0, "failed": 0, "signals": []}

    ensure_notification_table(conn)

    # 找出 A 级 open 信号
    rows = conn.execute("""
        SELECT s.signal_id, s.tier, s.composite_score, s.kind,
               s.asset_id, a.canonical_name, a.canonical_symbol AS symbol,
               c.title AS catalyst_title, c.source_code
        FROM biz.catalyst_signal s
        JOIN core.asset a ON s.asset_id = a.asset_id
        JOIN biz.asset_catalyst c ON s.catalyst_id = c.catalyst_id
        WHERE s.signal_id = ANY(%s)
          AND s.tier = 'A'
          AND s.status = 'open'
        ORDER BY s.composite_score DESC
    """, (new_signal_ids,)).fetchall()

    if not rows:
        return {"sent": 0, "skipped": len(new_signal_ids), "failed": 0, "signals": []}

    sent = 0
    skipped = 0
    failed = 0
    alert_signals = []

    for row in rows:
        if _is_sent(conn, row["signal_id"], NTYPE_FAST_ALERT):
            skipped += 1
            continue

        # 构建邮件
        subject = f"🚀 A级催化剂信号: {row['symbol']} - {row['catalyst_title']}"
        body = _build_fast_alert_html(row)

        ok, msg = _send_email(subject, body)

        if ok:
            sent += 1
            _mark_sent(conn, row["signal_id"], NTYPE_FAST_ALERT, "A", subject)
            alert_signals.append({"signal_id": row["signal_id"], "symbol": row["symbol"]})
        else:
            failed += 1
            _mark_sent(conn, row["signal_id"], NTYPE_FAST_ALERT, "A", subject,
                       status="failed", error_msg=msg)
            logger.warning("快提醒发送失败 [%s]: %s", row["symbol"], msg)

    return {
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "total_a_grade": len(rows),
        "signals": alert_signals,
    }


def _build_fast_alert_html(row) -> str:
    """构建 A 级快提醒邮件 HTML。"""
    score = row["composite_score"] or 0
    return f"""
    <div style="font-family:sans-serif;max-width:640px;margin:auto;padding:20px">
      <div style="background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;padding:20px;border-radius:12px 12px 0 0">
        <div style="font-size:12px;opacity:.8;text-transform:uppercase;letter-spacing:1px">A级催化剂信号</div>
        <div style="font-size:28px;font-weight:700;margin-top:6px">{row['symbol']} / {row['canonical_name']}</div>
        <div style="margin-top:8px;font-size:14px;opacity:.9">{row['catalyst_title']}</div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 12px 12px">
        <div style="display:flex;gap:20px;margin-bottom:16px">
          <div style="flex:1">
            <div style="font-size:11px;color:#6b7280;text-transform:uppercase">综合评分</div>
            <div style="font-size:32px;font-weight:700;color:#7c3aed">{score:.0f}</div>
          </div>
          <div style="flex:1">
            <div style="font-size:11px;color:#6b7280;text-transform:uppercase">信息源</div>
            <div style="font-size:18px;font-weight:600;margin-top:8px">{row['source_code']}</div>
          </div>
        </div>

        <div style="border-top:1px solid #f3f4f6;padding-top:14px">
          <div style="font-size:12px;color:#6b7280;margin-bottom:6px">催化剂标题</div>
          <div style="font-size:14px;line-height:1.6;color:#111827">{row['catalyst_title'] or '—'}</div>
        </div>

        <div style="margin-top:20px;padding:12px;background:#f5f3ff;border-radius:8px">
          <div style="font-size:12px;color:#7c3aed;font-weight:500">⚡ 快通道信号 · 请关注慢通道深度分析</div>
          <div style="font-size:11px;color:#6b7280;margin-top:4px">慢通道将补全 G3-G5 二阶受益、持续性、基本面、技术面分析</div>
        </div>

        <div style="margin-top:20px;font-size:11px;color:#9ca3af;text-align:center">
          由催化剂决策管道自动生成 · 24h 内去重
        </div>
      </div>
    </div>
    """


# =====================================================================
# 慢通道：汇总邮件
# =====================================================================

def send_slow_digest(conn, stats: dict) -> dict:
    """发送慢通道汇总邮件。

    Args:
        conn: 数据库连接
        stats: 慢通道统计信息，包括：
            - second_order_count: 二阶映射数
            - g3g5_processed: G3-G5 处理数
            - tier_distribution: tier 分布
            - expired_count: 过期信号数
            - new_signal_ids: 本轮新信号 ID 列表（用于去重）

    Returns:
        dict: {sent, skipped, reason}
    """
    ensure_notification_table(conn)

    # 获取当前 A/B 级 open 信号
    rows = conn.execute("""
        SELECT s.signal_id, s.tier, s.composite_score, s.kind,
               s.technical_state, s.persistence,
               a.canonical_name, a.canonical_symbol AS symbol,
               ac.title AS catalyst_title
        FROM biz.catalyst_signal s
        JOIN core.asset a ON s.asset_id = a.asset_id
        JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
        WHERE s.status = 'open'
          AND s.tier IN ('A', 'B')
        ORDER BY
            CASE s.tier WHEN 'A' THEN 1 WHEN 'B' THEN 2 END,
            s.composite_score DESC
        LIMIT 30
    """).fetchall()

    # 只发当日有新信号时才发，避免噪音
    today_new = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM biz.catalyst_signal
        WHERE created_at > NOW() - INTERVAL '24 hours'
          AND tier IN ('A', 'B')
    """).fetchone()

    new_count = today_new["cnt"] if today_new else 0
    if new_count == 0 and stats.get("expired_count", 0) == 0:
        return {"sent": 0, "skipped": 1, "reason": "24h 内无 A/B 级新信号且无过期，跳过汇总"}

    subject = f"📊 催化剂日报 · {new_count} 条新信号 · A级 {_count_by_tier(rows, 'A')} / B级 {_count_by_tier(rows, 'B')}"
    body = _build_slow_digest_html(rows, stats, new_count)

    ok, msg = _send_email(subject, body)

    # 记录（慢通道汇总无 signal_id，记 NULL）
    _mark_sent(conn, None, NTYPE_SLOW_DIGEST, None, subject,
               status="sent" if ok else "failed", error_msg=msg if not ok else None)

    return {
        "sent": 1 if ok else 0,
        "skipped": 0,
        "failed": 0 if ok else 1,
        "reason": msg,
        "new_signals_24h": new_count,
    }


def _count_by_tier(rows, tier: str) -> int:
    return sum(1 for r in rows if r["tier"] == tier)


def _build_slow_digest_html(rows, stats: dict, new_count: int) -> str:
    """构建慢通道汇总邮件 HTML。"""
    tier_dist = stats.get("tier_distribution", {})

    # 信号表格行
    signal_rows = ""
    for r in rows[:20]:  # 最多 20 条
        tier_color = {"A": "#7c3aed", "B": "#3b82f6", "C": "#6b7280"}.get(r["tier"], "#6b7280")
        tech_badge = f'<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:#e0e7ff;color:#3730a3">{r["technical_state"] or "—"}</span>' if r.get("technical_state") else "—"
        horizon = r.get("persistence") or "—"
        signal_rows += f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #f3f4f6">
            <span style="display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border-radius:4px;background:{tier_color};color:#fff;font-size:11px;font-weight:700">{r["tier"]}</span>
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-weight:600">{r["symbol"]}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:13px">{r["canonical_name"]}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:13px">{r["catalyst_title"] or r["kind"] or "—"}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;text-align:center;font-weight:600">{(r["composite_score"] or 0):.0f}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #f3f4f6">{tech_badge}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#6b7280">{horizon}</td>
        </tr>
        """

    # 统计卡片
    dist_items = ""
    for tier, cnt in sorted(tier_dist.items()):
        tier_color = {"A": "#7c3aed", "B": "#3b82f6", "C": "#6b7280"}.get(tier, "#6b7280")
        dist_items += f"""
        <div style="flex:1;text-align:center;padding:10px;background:#f9fafb;border-radius:6px">
          <div style="font-size:20px;font-weight:700;color:{tier_color}">{cnt}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">{tier} 级</div>
        </div>
        """

    so_count = stats.get("second_order_count", 0)
    expired = stats.get("expired_count", 0)

    return f"""
    <div style="font-family:sans-serif;max-width:720px;margin:auto;padding:16px">
      <div style="background:linear-gradient(135deg,#0f172a,#1e293b);color:#fff;padding:24px;border-radius:12px">
        <div style="font-size:12px;opacity:.7;text-transform:uppercase;letter-spacing:1px">催化剂决策管道 · 慢通道汇总</div>
        <div style="font-size:24px;font-weight:700;margin-top:8px">今日 {new_count} 条新信号</div>
        <div style="margin-top:4px;font-size:13px;opacity:.8">二阶受益 {so_count} 条 · 过期 {expired} 条</div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 12px 12px">
        <!-- 统计概览 -->
        <div style="display:flex;gap:10px;margin-bottom:20px">
          {dist_items or '<div style="color:#6b7280;font-size:13px">暂无分级数据</div>'}
        </div>

        <!-- 信号列表 -->
        <div style="margin-bottom:12px">
          <h3 style="font-size:15px;margin:0 0 10px;color:#111827">🔥 当前活跃信号（Top 20）</h3>
        </div>
        <div style="overflow-x:auto">
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead>
              <tr style="background:#f9fafb">
                <th style="padding:8px 10px;text-align:left;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">级别</th>
                <th style="padding:8px 10px;text-align:left;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">代币</th>
                <th style="padding:8px 10px;text-align:left;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">名称</th>
                <th style="padding:8px 10px;text-align:left;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">事件类型</th>
                <th style="padding:8px 10px;text-align:center;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">评分</th>
                <th style="padding:8px 10px;text-align:left;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">技术面</th>
                <th style="padding:8px 10px;text-align:left;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">持续性</th>
              </tr>
            </thead>
            <tbody>
              {signal_rows or '<tr><td colspan="7" style="padding:20px;text-align:center;color:#9ca3af">暂无活跃信号</td></tr>'}
            </tbody>
          </table>
        </div>

        <div style="margin-top:20px;padding:12px;background:#f0f9ff;border-radius:8px;font-size:12px;color:#0369a1">
          💡 慢通道每 4 小时运行一次，补全二阶受益、持续性预判、基本面、技术面分析
        </div>

        <div style="margin-top:20px;font-size:11px;color:#9ca3af;text-align:center">
          由催化剂决策管道自动生成
        </div>
      </div>
    </div>
    """
