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
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .asset_filter import ASSET_NAME_FILTER_SQL, IS_STOCK_SQL, is_non_crypto, is_stock

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

# 通知类型常量
NTYPE_FAST_ALERT = "fast_alert"       # 快通道 A 级即时提醒
NTYPE_SLOW_DIGEST = "slow_digest"     # 慢通道汇总邮件（加密货币）
NTYPE_SLOW_DIGEST_STOCK = "slow_digest_stock"  # 慢通道汇总邮件（美股/商品）

# 去重窗口：同一信号同一类型 24h 内不重复发
DEDUP_WINDOW_HOURS = 24

# slow_digest 的 signal_id 哨兵值（NULL 不触发 UNIQUE 约束，用负数占位保证去重生效）
SENTINEL_SLOW_DIGEST_SIGNAL_ID = -1      # 加密货币汇总
SENTINEL_SLOW_DIGEST_STOCK_SIGNAL_ID = -2  # 美股/商品汇总


# =====================================================================
# 去重表 DDL（首次使用自动建表）
# =====================================================================

def ensure_notification_table(conn) -> None:
    """确保 notification_log 表存在。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS biz.catalyst_notification_log (
            log_id              BIGSERIAL PRIMARY KEY,
            signal_id           BIGINT NOT NULL,       -- 快提醒=真实ID，慢汇总=-1（哨兵值，保证UNIQUE生效）
            notification_type   VARCHAR(32) NOT NULL,  -- fast_alert / slow_digest
            tier                VARCHAR(4),
            sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            subject             VARCHAR(256),
            status              VARCHAR(16) NOT NULL DEFAULT 'sent',
            error_msg           TEXT,
            UNIQUE (signal_id, notification_type)
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
        ok, msg = notifier.send(subject, body_html, from_name="催化剂信号")
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
               c.title AS catalyst_title, c.source_code,
               s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
               s.investment_cycle, s.ai_reason
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

        # 构建邮件（美股/商品加标记，便于在收件箱区分）
        cls_tag = "[美股·商品]" if is_stock(row["canonical_name"], row["symbol"]) else "[加密]"
        subject = f"🚀 {cls_tag} A级催化剂信号: {row['symbol']} - {row['catalyst_title']}"
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
    cycle = row.get("investment_cycle")
    reason = row.get("ai_reason")
    tp = row.get("take_profit")
    sl = row.get("stop_loss")
    rr = row.get("rr_ratio")

    cycle_html = f'<div style="font-size:18px;font-weight:600;margin-top:8px">{cycle}</div>' if cycle else \
        '<div style="font-size:12px;color:#9ca3af;margin-top:8px">慢通道 G7 补全中</div>'

    price_rows = ""
    if tp is not None or sl is not None:
        price_rows = f"""
        <div style="display:flex;gap:16px;margin-top:14px;flex-wrap:wrap">
          <div style="flex:1;min-width:120px;padding:10px;background:#f0fdf4;border-radius:8px;text-align:center">
            <div style="font-size:11px;color:#059669;text-transform:uppercase">目标价</div>
            <div style="font-size:16px;font-weight:700;color:#059669;margin-top:4px">{_fmt_price(tp)}</div>
          </div>
          <div style="flex:1;min-width:120px;padding:10px;background:#fef2f2;border-radius:8px;text-align:center">
            <div style="font-size:11px;color:#dc2626;text-transform:uppercase">止损价</div>
            <div style="font-size:16px;font-weight:700;color:#dc2626;margin-top:4px">{_fmt_price(sl)}</div>
          </div>
          <div style="flex:1;min-width:120px;padding:10px;background:#eff6ff;border-radius:8px;text-align:center">
            <div style="font-size:11px;color:#2563eb;text-transform:uppercase">盈亏比</div>
            <div style="font-size:16px;font-weight:700;color:#2563eb;margin-top:4px">{f"{rr:.1f}" if rr is not None else "—"}</div>
          </div>
        </div>
        """

    reason_html = ""
    if reason:
        reason_html = f"""
        <div style="border-top:1px solid #f3f4f6;padding-top:14px;margin-top:14px">
          <div style="font-size:12px;color:#6b7280;margin-bottom:6px">🤖 AI 推荐原因</div>
          <div style="font-size:13px;line-height:1.6;color:#111827">{reason}</div>
        </div>
        """

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
          <div style="flex:1">
            <div style="font-size:11px;color:#6b7280;text-transform:uppercase">投资周期</div>
            {cycle_html}
          </div>
        </div>

        {price_rows}

        {reason_html}

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
            - expired_count: 过期信号数

    Returns:
        dict: {sent, skipped, reason}
    """
    ensure_notification_table(conn)

    # 加密货币汇总邮件
    crypto_result = _send_slow_digest_class(conn, stats, asset_class="crypto")
    # 美股/商品汇总邮件
    stock_result = _send_slow_digest_class(conn, stats, asset_class="stock")

    return {
        "sent": int(bool(crypto_result["sent"])) + int(bool(stock_result["sent"])),
        "skipped": crypto_result["skipped"] + stock_result["skipped"],
        "failed": crypto_result["failed"] + stock_result["failed"],
        "reason": "; ".join(
            [r for r in (crypto_result.get("reason"), stock_result.get("reason")) if r]
        ) or None,
        "crypto": crypto_result,
        "stock": stock_result,
    }


def _send_slow_digest_class(conn, stats: dict, asset_class: str) -> dict:
    """发送某个资产类别的慢通道汇总邮件（加密货币 / 美股·商品）。

    各自独立去重（select sentinel + ntype），互不影响。
    """
    is_crypto = asset_class == "crypto"
    label = "加密货币" if is_crypto else "美股·商品"
    ntype = NTYPE_SLOW_DIGEST if is_crypto else NTYPE_SLOW_DIGEST_STOCK
    sentinel = SENTINEL_SLOW_DIGEST_SIGNAL_ID if is_crypto else SENTINEL_SLOW_DIGEST_STOCK_SIGNAL_ID

    rows = _recent_new_ab_signals(conn, hours=24, asset_class=asset_class)
    if not rows:
        return {"sent": 0, "skipped": 1, "failed": 0, "reason": f"{label} 24h 内无 A/B 级新信号，跳过"}

    # 每资产去重留最高分（一次查询完成），A/B 互斥分组 → 主题/正文/卡片三处数字统一
    a_rows = [r for r in rows if r["tier"] == "A"]
    b_rows = [r for r in rows if r["tier"] != "A"]
    new_count = len(rows)

    subject = f"📊 催化剂日报·{label} · 24h新增 {new_count} 条 · A级 {len(a_rows)} · B级 {len(b_rows)}"
    body = _build_slow_digest_html(a_rows, b_rows, new_count, stats, class_label=label)

    if _is_sent(conn, sentinel, ntype):
        return {"sent": 0, "skipped": 1, "failed": 0, "reason": f"{label} 24h 内已发送过汇总，跳过"}

    ok, msg = _send_email(subject, body)
    _mark_sent(conn, sentinel, ntype, None, subject,
               status="sent" if ok else "failed", error_msg=msg if not ok else None)

    return {
        "sent": 1 if ok else 0,
        "skipped": 0,
        "failed": 0 if ok else 1,
        "reason": msg,
        "new_signals_24h": new_count,
    }


def _recent_new_ab_signals(conn, hours: int = 24, asset_class: str = "crypto") -> list[dict]:
    """过去 N 小时内 A/B 级新信号（按资产类别，按资产去重留最高分）。

    一次查询完成 A/B 全量 + DISTINCT ON(asset_id) 留最高分，
    避免「同一资产在 A 级和 B 级各出现一次」导致主题/卡片数字矛盾
    （邮件审计 2026-09-15）。
    """
    filter_sql = ASSET_NAME_FILTER_SQL if asset_class == "crypto" else IS_STOCK_SQL
    return conn.execute(f"""
        SELECT DISTINCT ON (a.asset_id)
               s.signal_id, s.tier, s.composite_score, s.kind,
               s.technical_state, s.persistence,
               s.investment_cycle, s.ai_reason,
               s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
               a.canonical_name, a.canonical_symbol AS symbol,
               ac.title AS catalyst_title
        FROM biz.catalyst_signal s
        JOIN core.asset a ON s.asset_id = a.asset_id
        JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
        WHERE s.status = 'open'
          AND s.created_at > NOW() - INTERVAL '%s hours'
          AND s.tier IN ('A', 'B')
          AND {filter_sql}
        ORDER BY a.asset_id, s.composite_score DESC
    """, (hours,)).fetchall()


# ---- 中文化映射 ----

_TECH_STATE_CN = {
    "up": "看涨",
    "range": "震荡",
    "down": "看跌",
    "strong": "强势",
    "weak": "弱势",
    "neutral": "中性",
}

_PERSISTENCE_CN = {
    "structural": "结构性",
    "one_off": "事件驱动",
    "decaying": "衰减性",
    "cyclical": "周期性",
    "contagion": "传导性",
}

_KIND_CN = {
    "structural": "结构性",
    "event": "事件",
    "sentiment": "情绪",
    "noise": "噪音",
}


def _tech_cn(v) -> str:
    return _TECH_STATE_CN.get(v, v or "—")


def _persist_cn(v) -> str:
    return _PERSISTENCE_CN.get(v, v or "—")


def _kind_cn(v) -> str:
    return _KIND_CN.get(v, "—")


def _truncate(text: str, length: int = 40) -> str:
    """截断过长标题。"""
    if not text:
        return "—"
    text = str(text).strip()
    return text if len(text) <= length else text[: length - 1] + "…"


def _build_signal_table(rows, tier: str) -> str:
    """构建单级别信号决策卡片：代币 + 周期 + AI推荐原因 + 目标价/止损/盈亏比。"""
    tier_color = {"A": "#7c3aed", "B": "#3b82f6"}.get(tier, "#6b7280")
    blocks = ""
    for r in rows[:15]:  # 每级别最多 15 条
        tech = _tech_cn(r.get("technical_state"))
        persist = _persist_cn(r.get("persistence"))
        cycle = r.get("investment_cycle") or "—"
        reason = r.get("ai_reason") or r.get("catalyst_title") or "暂无 AI 推荐原因（慢通道 G7 补全中）"
        reason = _truncate(reason, 160)

        # 价格区间（目标/止损/盈亏比）
        tp = r.get("take_profit")
        sl = r.get("stop_loss")
        rr = r.get("rr_ratio")
        price_html = "<span style='color:#9ca3af'>待补</span>"
        if tp is not None or sl is not None:
            tp_txt = _fmt_price(tp)
            sl_txt = _fmt_price(sl)
            rr_txt = f"{rr:.1f}" if rr is not None else "—"
            price_html = (
                f"目标 <b style='color:#059669'>{tp_txt}</b> &nbsp;·&nbsp; "
                f"止损 <b style='color:#dc2626'>{sl_txt}</b> &nbsp;·&nbsp; 盈亏比 {rr_txt}"
            )

        badge = (f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:{tier_color};'
                 f'color:#fff;font-size:11px;font-weight:700">{tier}</span>')
        cycle_badge = (f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
                       f'background:#eef2ff;color:#4338ca;font-size:11px;font-weight:600">{cycle}</span>')

        blocks += f"""
        <div style="border:1px solid #e5e7eb;border-left:4px solid {tier_color};border-radius:8px;padding:12px 14px;margin-bottom:10px">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            {badge}
            <span style="font-weight:700;font-size:14px">{r["symbol"]}</span>
            <span style="color:#6b7280;font-size:12px">{r["canonical_name"]}</span>
            {cycle_badge}
            <span style="margin-left:auto;font-weight:600;font-size:13px;color:#374151">评分 {(r["composite_score"] or 0):.0f}</span>
          </div>
          <div style="margin-top:8px;font-size:13px;color:#111827;line-height:1.5">{reason}</div>
          <div style="margin-top:8px;font-size:12px">{price_html}</div>
          <div style="margin-top:6px;font-size:11px;color:#6b7280">
            技术面 {tech} · 持续性 {persist}
          </div>
        </div>
        """
    if not blocks:
        return f'<div style="padding:16px;text-align:center;color:#9ca3af;background:#f9fafb;border-radius:8px">过去 24h 无 {tier} 级新信号</div>'
    return blocks


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        if f >= 1000:
            return f"{f:,.1f}"
        if f >= 1:
            return f"{f:,.4f}"
        return f"{f:.8f}"
    except (TypeError, ValueError):
        return "—"


def _build_slow_digest_html(a_rows, b_rows, new_count: int, stats: dict,
                            class_label: str = "加密货币") -> str:
    """构建慢通道汇总邮件 HTML。

    Args:
        a_rows: 24h 内 A 级去重新信号
        b_rows: 24h 内 B 级去重新信号
        new_count: 24h 内新增 A/B 信号总数（去重）
        stats: 慢通道统计（second_order_count / expired_count）
        class_label: 资产类别中文标签（加密货币 / 美股·商品）
    """
    so_count = stats.get("second_order_count", 0)
    expired = stats.get("expired_count", 0)

    a_table = _build_signal_table(a_rows, "A")
    b_table = _build_signal_table(b_rows, "B")

    return f"""
    <div style="font-family:sans-serif;max-width:720px;margin:auto;padding:16px">
      <div style="background:linear-gradient(135deg,#0f172a,#1e293b);color:#fff;padding:24px;border-radius:12px">
        <div style="font-size:12px;opacity:.7;text-transform:uppercase;letter-spacing:1px">催化剂决策管道 · {class_label} · 慢通道汇总</div>
        <div style="font-size:24px;font-weight:700;margin-top:8px">{class_label} 24h 新增 {new_count} 条信号</div>
        <div style="margin-top:4px;font-size:13px;opacity:.8">A级 {len(a_rows)} · B级 {len(b_rows)} · 二阶受益 {so_count} 条 · 过期 {expired} 条</div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 12px 12px">
        <!-- A 级 -->
        <h3 style="font-size:15px;margin:0 0 10px;color:#111827">🟣 A 级信号（过去 24h 新增，含 AI 推荐 Reasons 与价位）</h3>
        {a_table}

        <!-- B 级 -->
        <h3 style="font-size:15px;margin:20px 0 10px;color:#111827">🔵 B 级信号（过去 24h 新增，含 AI 推荐 Reasons 与价位）</h3>
        {b_table}

        <div style="margin-top:20px;padding:12px;background:#f0f9ff;border-radius:8px;font-size:12px;color:#0369a1">
          💡 AI 推荐原因与投资周期由慢通道 G7 生成；目标价位/止损价位为 AI 在规则值基础上的评审。慢通道每 4 小时运行一次。
        </div>

        <div style="margin-top:20px;font-size:11px;color:#9ca3af;text-align:center">
          由催化剂决策管道自动生成 · {class_label} 独立汇总
        </div>
      </div>
    </div>
    """
