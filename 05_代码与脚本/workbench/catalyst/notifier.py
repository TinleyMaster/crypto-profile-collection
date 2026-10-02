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
import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

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
    """只读检查：某信号是否已在去重窗口内发送过指定类型通知。

    用于预检（慢通道），不修改数据，不占锁。快通道请用 _try_acquire_send_lock。
    """
    row = conn.execute("""
        SELECT 1 FROM biz.catalyst_notification_log
        WHERE signal_id = %s AND notification_type = %s
          AND sent_at > NOW() - (%s::int * INTERVAL '1 hour')
        LIMIT 1
    """, (signal_id, ntype, DEDUP_WINDOW_HOURS)).fetchone()
    return row is not None


def _try_acquire_send_lock(conn, signal_id: int, ntype: str,
                           tier: str | None, subject: str | None) -> bool:
    """原子性获取发送锁（防重发核心机制）。

    利用 UNIQUE(signal_id, notification_type) 约束的 INSERT ON CONFLICT 实现：
    - 无记录 → 插入 pending 记录，获得发送权 → 返回 True
    - 有记录但已超过去重窗口 → 更新 sent_at 重置，获得发送权 → 返回 True
    - 有记录且在去重窗口内 → 不更新，未获得发送权 → 返回 False

    线程/进程安全：PostgreSQL 的 INSERT ON CONFLICT 是原子操作，
    并发情况下只有一个事务能成功插入/更新，其余会等锁后发现冲突。
    """
    try:
        row = conn.execute("""
            INSERT INTO biz.catalyst_notification_log
                (signal_id, notification_type, tier, subject, status)
            VALUES (%s, %s, %s, %s, 'sending')
            ON CONFLICT (signal_id, notification_type)
            DO UPDATE
               SET sent_at = NOW(), status = 'sending',
                   subject = COALESCE(EXCLUDED.subject, biz.catalyst_notification_log.subject),
                   tier = COALESCE(EXCLUDED.tier, biz.catalyst_notification_log.tier),
                   error_msg = NULL
             WHERE biz.catalyst_notification_log.sent_at
                   < NOW() - (%s::int * INTERVAL '1 hour')
            RETURNING log_id
        """, (signal_id, ntype, tier, subject, DEDUP_WINDOW_HOURS)).fetchone()
        return row is not None
    except Exception as e:
        logger.warning("获取发送锁失败 sig=%s type=%s: %s", signal_id, ntype, e)
        return False


def _mark_sent(conn, signal_id: int | None, ntype: str, tier: str | None,
               subject: str, status: str = "sent", error_msg: str | None = None) -> None:
    """记录发送日志（INSERT ON CONFLICT 幂等）。

    既可用于首次记录，也可用于更新已有记录的状态。
    """
    try:
        conn.execute("""
            INSERT INTO biz.catalyst_notification_log
                (signal_id, notification_type, tier, subject, status, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (signal_id, notification_type) DO UPDATE
                SET sent_at = NOW(), status = EXCLUDED.status,
                    error_msg = EXCLUDED.error_msg,
                    subject = COALESCE(EXCLUDED.subject, biz.catalyst_notification_log.subject),
                    tier = COALESCE(EXCLUDED.tier, biz.catalyst_notification_log.tier)
        """, (signal_id, ntype, tier, subject, status, error_msg))
    except Exception as e:
        logger.warning("记录通知日志失败: %s", e)


def _mark_signal_notified(conn, signal_ids: list[int], column: str) -> None:
    """回写信号的发送时间戳（审计 P1-2）。

    去重仍以 biz.catalyst_notification_log 为准，这里只做审计留痕：
    - 快提醒（fast_alert）→ pre_alert_sent_at
    - 汇总/正式通知（slow_digest）→ notified_at

    仅在字段仍为 NULL 时写入，避免覆盖首次发送时间；失败不阻断主流程。
    """
    if column not in ("notified_at", "pre_alert_sent_at"):
        return
    ids = [sid for sid in signal_ids if sid is not None and sid > 0]
    if not ids:
        return
    try:
        cur = conn.execute(
            f"UPDATE biz.catalyst_signal SET {column} = NOW(), updated_at = NOW() "
            f"WHERE signal_id = ANY(%s::BIGINT[]) AND {column} IS NULL",
            (ids,),
        )
        logger.info("回写 catalyst_signal.%s: %s/%s 条", column,
                    getattr(cur, "rowcount", -1), len(ids))
    except Exception as e:
        logger.warning("回写 catalyst_signal.%s 失败（%s 条）: %s", column, len(ids), e)


def _slow_digest_sent_recently(conn, signal_ids: list[int]) -> set[int]:
    """近 DEDUP_WINDOW_HOURS 内已被慢通道 A 级 digest 覆盖的 signal_id 集合。

    跨通道去重（诊断_催化剂A级邮件延迟链路_XRP_BCH_2026-09-24）：快讯与慢通道 digest
    都发 A 级 Alert，但各用独立去重（快讯按 `(signal_id, fast_alert)`；digest 按类别
    sentinel）⇒ 同一信号会收到两封 A 级邮件（实测 XRP signal=1085989：16:30 digest +
    16:50 快讯）。`notified_at` 仅由 digest 在发送成功后写入，故可作为「已被 digest
    覆盖」的判据；digest 侧则在 `_recent_new_a_signals` 排除近窗口内已发过快讯的行
    （`pre_alert_sent_at`），双向合围后同一信号 24h 内只发一封。
    查询失败按「未发送」处理（宁可多发一封，也不静默漏发）。
    """
    ids = [sid for sid in (signal_ids or []) if sid is not None and sid > 0]
    if not ids:
        return set()
    try:
        rows = conn.execute(
            """
            SELECT signal_id FROM biz.catalyst_signal
             WHERE signal_id = ANY(%s::BIGINT[])
               AND notified_at > NOW() - (%s::int * INTERVAL '1 hour')
            """,
            (ids, DEDUP_WINDOW_HOURS),
        ).fetchall()
        return {r["signal_id"] for r in rows}
    except Exception as e:
        logger.warning("跨通道去重查询失败（按未发送处理）: %s", e)
        return set()


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


def _send_email(subject: str, body_html: str, to: str | None = None) -> tuple[bool, str]:
    """发送邮件。失败返回 (False, reason)。

    Args:
        subject / body_html: 主题与 HTML 正文
        to: 收件人（逗号分隔）。**运维告警专用**：留空则用 SMTP_TO 全量收件人；
            系统/运维类邮件（通道空转、守护进程异常等）应传 `ops_recipients()`，
            只发管理员（ADMIN_EMAIL），未配置才回退 SMTP_TO。
    """
    notifier = _get_email_notifier()
    if notifier is None:
        return False, "SMTP 未配置或不可用"
    try:
        ok, msg = notifier.send(subject, body_html, from_name="催化剂信号", to=to)
        return ok, msg
    except Exception as e:
        return False, str(e)


def ops_recipients() -> str | None:
    """运维告警收件人：优先 ADMIN_EMAIL，未配置回退 SMTP_TO。

    与盘面扫描停摆 / 合约地址卫生 / 链上快照看门狗等既有运维邮件同口径
    （见 crypto_research.clients.notifier.send 的 docstring）。
    """
    try:
        from crypto_research.config import get_settings
        s = get_settings(require_database=False)
        return s.admin_email or s.smtp_to
    except Exception:
        return None


# =====================================================================
# 快通道：A 级信号即时提醒
# =====================================================================

# AI 深度评审的否决判据（审计_催化剂A级邮件_XRP_BCH_2026-09-24 P0-D1/D2）。
#
# 问题：综合分 86 的信号拿了 A 级推送位并附「📈 做多 · 入场/目标/止损」档位，
# 而同封邮件里 AI 评审写着「资产匹配 low · 不建议参与 · 0% 仓位」——自相矛盾。
#
# 为什么不在 signal.py 的 _score_to_tier 里封顶 tier：ai_deep_review 由 G7（AI 增强）
# 产出，tier 由 G6（build）计算，同一轮内算 tier 时 AI 结论还不存在（首轮必然为 NULL）。
# 因此否决只能落在通知层——这是两者都在手上的唯一位置。
AI_VETO_ASSET_MATCH = "low"     # asset_match_confidence 取该值时否决
AI_VETO_VERDICT_MARKER = "不建议"  # verdict 含该子串时否决（与下方配色判据同一约定）


def _ai_review_blocks_alert(ai_deep) -> tuple[bool, str]:
    """AI 深度评审是否否决 A 级快讯。返回 (是否否决, 原因文案)。

    仅在审计列出的两条判据成立时否决：
      - `asset_match_confidence == 'low'`（代币与催化剂可能不匹配）
      - `verdict` 含「不建议」（AI 自身结论即不建议参与）

    评审缺失 / 非 dict / 字段为空时一律**不否决**，保持既有行为不变——本闸门只做减法，
    不因 AI 异常而扩大拦截面。
    """
    if not isinstance(ai_deep, dict):
        return False, ""
    match = str(ai_deep.get("asset_match_confidence") or "").strip().lower()
    if match == AI_VETO_ASSET_MATCH:
        return True, "资产匹配度 low（代币与催化剂可能不匹配）"
    verdict = str(ai_deep.get("verdict") or "")
    if AI_VETO_VERDICT_MARKER in verdict:
        return True, f"AI 交易结论「{verdict}」"
    return False, ""


# ---------------------------------------------------------------------
# 行情 / 流动性列口径（审计_催化剂A级邮件_XRP_BCH_2026-09-24 P1-D3/D4/D5）
#
# 修前：发送路径的两条 SQL（首查 + AI 增强后重查）与 AI 评审输入 SQL（_fetch_signal_row）
# 各自手写行情列，**三处口径互不一致**：
#   - 只有 _fetch_signal_row 查了 volume_ratio_7d ⇒ 发送路径 row.get("volume_ratio_7d")
#     恒为 None，渲染层按 0 处理并标「📈 量比 0.00x 极度缩量」，而同封邮件 AI 核心逻辑
#     写的是「量比 1.71x 温和放量」——同源数据两条通道自打脸（审计 P1-D4）。
#   - 7 日均量的窗口也不同：_fetch_signal_row 取「最近 7 个交易日」，发送路径取
#     「MAX(market_date) 之前的全部历史」（实测 119 天）⇒ 同一字段两个值。
# 现抽成常量供三处共用，口径统一为「最近 7 个交易日」，避免再次漂移。
#
# 流动性：biz.asset_liquidity 是**按链分行的 DEX 池快照**（实测每资产 1~21 行，
# chain ∈ ethereum/solana/base/...），原 SQL 无 ORDER BY 直接 LIMIT 1 ⇒ 取哪条不确定。
# XRP(1127) 命中的是 Solana 上的 wXRP 池（$1.91M），与「24h 成交量 $7.80B」并列展示为
# 「流动性（24h）」，又被喂给 LLM 当「总流动性」⇒ 风险文案写成「极度稀薄」（审计 P1-D5）。
# 现改为确定性取最大池，并把 chain/source 一并带出，供展示与 prompt 标注口径。
_MARKET_LATERAL_SQL = """
        LEFT JOIN LATERAL (
            SELECT md.price_usd, md.change_24h, md.change_7d, md.volume_24h
              FROM biz.v_asset_market_daily_primary md
             WHERE md.asset_id = s.asset_id
             ORDER BY md.market_date DESC
             LIMIT 1
        ) md_latest ON true
        LEFT JOIN LATERAL (
            SELECT AVG(md2.volume_24h) AS avg_volume_7d
              FROM (
                SELECT md2.volume_24h
                  FROM biz.v_asset_market_daily_primary md2
                 WHERE md2.asset_id = s.asset_id
                       AND md2.market_date < (
                           SELECT MAX(md3.market_date)
                             FROM biz.v_asset_market_daily_primary md3
                            WHERE md3.asset_id = s.asset_id
                       )
                 ORDER BY md2.market_date DESC
                 LIMIT 7
              ) md2
        ) md_avg ON true
        LEFT JOIN LATERAL (
            SELECT al.total_liquidity_usd, al.chain, al.source
              FROM biz.asset_liquidity al
             WHERE al.asset_id = s.asset_id
             ORDER BY al.total_liquidity_usd DESC NULLS LAST, al.chain
             LIMIT 1
        ) liq ON true
"""

_MARKET_COLS_SQL = """               md_latest.price_usd AS current_price,
               md_latest.change_24h AS change_24h_pct,
               md_latest.change_7d AS change_7d_pct,
               md_latest.volume_24h AS volume_24h_usd,
               md_avg.avg_volume_7d,
               CASE
                   WHEN md_latest.volume_24h > 0 AND md_avg.avg_volume_7d > 0
                   THEN ROUND((md_latest.volume_24h / md_avg.avg_volume_7d)::numeric, 2)
                   ELSE NULL
               END AS volume_ratio_7d,
               CASE
                   WHEN md_latest.volume_24h > 0 AND md_avg.avg_volume_7d > 0
                        AND md_latest.volume_24h / md_avg.avg_volume_7d >= 2.0
                   THEN true ELSE false
               END AS is_volume_spike,
               liq.total_liquidity_usd AS liquidity_score,
               liq.chain AS liquidity_chain,
               liq.source AS liquidity_source
"""

# CMC 的 description_short 是静态文案，尾部固定拼接过期行情句（审计 P1-D3）。
_STALE_PRICE_SENTENCE_RE = re.compile(
    r"last known price\b"
    r"|is (?:up|down) [\d.,]+\s*over the last 24 hours"
    r"|traded over the last 24 hours"
    r"|active market\(s\)",
    re.I,
)


def _strip_stale_price_sentences(text) -> str:
    """剥离资产简介里内嵌的**过期行情句**（审计 2026-09-24 P1-D3）。

    `description_short` 尾部固定带「The last known price of XRP is 1.08727528 USD and is
    up 3.13 over the last 24 hours.」这类句子，其中的价格/涨跌幅不随行情刷新。邮件正文
    另有实时 `current_price` 区块，两者同屏出现约 30% 价差（审计原例 $1.57 vs $1.087）。

    按句切分、丢弃命中过期行情特征的句子，只保留项目介绍本身；同时供渲染层与 AI 评审
    输入共用，避免 LLM 把 stale 价格写进核心逻辑。
    """
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", str(text))
    kept = [p for p in parts if not _STALE_PRICE_SENTENCE_RE.search(p)]
    return " ".join(kept).strip()


def _liquidity_label(row) -> str:
    """链上池流动性标签（审计 2026-09-24 P1-D5）。

    `total_liquidity_usd` 来自单条链的 DEX 池快照，**不等于**资产全局流动性。原标签
    「流动性（24h）」与同屏「24h 成交量」并列，会被读成同一口径；这里把来源链写进标签，
    让读者一眼看出这是「某条链上的池子」而非全网流动性。
    """
    chain = str(row.get("liquidity_chain") or "").strip()
    return f"链上池流动性（{chain}）" if chain else "链上池流动性"


def send_fast_alerts_for_new_signals(conn, new_signal_ids: list[int]) -> dict:
    """对「本轮转为可动作」的 A 级信号发送快提醒。

    d3 分层：入参只包含 status 由非 open 变为 open 的信号（新插入，或 watch→open 晋升）。
    已定价（resonance_state='confirmed'）的信号在库中为 status='watch'，会被下方查询过滤，
    因此不会再出现「价格已涨完才推送」的追高提醒。

    AI 否决闸门（审计 2026-09-24 P0-D2）：AI 深度评审判「资产匹配 low」或「不建议参与」
    的信号**不发出**，但会在 biz.catalyst_notification_log 落一条 status='suppressed'
    记录（含原因），使「有意不发」可观测——原先的静默跳过正是本次审计的投诉点。

    Args:
        conn: 数据库连接
        new_signal_ids: 本轮转为 open 的信号 ID 列表

    Returns:
        dict: {sent, suppressed, skipped, failed, total_a_grade, signals: [...]}
    """
    if not new_signal_ids:
        return {"sent": 0, "suppressed": 0, "skipped": 0, "failed": 0, "signals": []}

    ensure_notification_table(conn)

    # 找出 A 级 open 信号（带完整代币详情）
    rows = conn.execute("""
        SELECT s.signal_id, s.tier, s.composite_score, s.kind,
               s.asset_id, a.canonical_name, a.canonical_symbol AS symbol,
               a.asset_type, a.primary_sector, a.categories,
               a.market_cap, a.market_cap_rank,
               a.circulating_supply, a.total_supply,
               a.ath_usd, a.launch_date, a.description_short,
               c.title AS catalyst_title, c.title_cn, c.source_code,
               c.body_text AS catalyst_summary, c.ai_summary,
               s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
               s.investment_cycle, s.ai_reason, s.ai_deep_review,
               s.technical_state, s.resonance_state,
               s.invalidation, s.persistence,
               s.base_strength, s.resonance_score,
               s.confidence, s.regime,
               -- 风险标签（risk_label 存的是 high/medium/low）
               (SELECT json_build_array(json_build_object('level', arl.risk_label, 'label',
                     CASE arl.risk_label
                       WHEN 'high' THEN '高风险'
                       WHEN 'medium' THEN '中风险'
                       WHEN 'low' THEN '低风险'
                       WHEN 'critical' THEN '极高风险'
                       ELSE '风险等级未知'
                     END))
                  FROM biz.asset_risk_labels arl
                 WHERE arl.asset_id = s.asset_id) AS risk_labels,
               -- 最新日行情 + 7 日均量 + 量比 + 链上池流动性（口径见 _MARKET_COLS_SQL）
""" + _MARKET_COLS_SQL + """
        FROM biz.catalyst_signal s
        JOIN core.asset a ON s.asset_id = a.asset_id
        JOIN biz.asset_catalyst c ON s.catalyst_id = c.catalyst_id
""" + _MARKET_LATERAL_SQL + """
        WHERE s.signal_id = ANY(%s)
          AND s.tier = 'A'
          AND s.status = 'open'
        ORDER BY s.composite_score DESC
    """, (new_signal_ids,)).fetchall()

    if not rows:
        return {"sent": 0, "suppressed": 0, "skipped": len(new_signal_ids),
                "failed": 0, "signals": []}

    # ==============================================================
    # 快通道 AI 增强：翻译 + A级深度评审（发邮件前做）
    # 失败静默降级，不影响邮件发送
    # ==============================================================
    translator = None
    reviewer = None
    try:
        from .ai_enhance import CatalystTranslator, AISignalDeepReviewer
        translator = CatalystTranslator.from_settings()
        reviewer = AISignalDeepReviewer.from_settings()
    except Exception as e:
        logger.warning("AI增强模块加载失败，跳过翻译和深度评审: %s", e)

    ai_enhanced = 0
    ai_translated = 0
    if translator or reviewer:
        for row in rows:
            # --- 翻译催化剂（title_cn + ai_summary）---
            if translator and not row.get("title_cn"):
                try:
                    result = translator.translate(
                        row.get("catalyst_title") or "",
                        row.get("catalyst_summary") or "",
                    )
                    if result and result.get("title_cn"):
                        # 写入数据库
                        conn.execute(
                            "UPDATE biz.asset_catalyst "
                            "SET title_cn = %s, ai_summary = COALESCE(%s, ai_summary), "
                            "    ai_processed = true, ai_processed_at = NOW() "
                            "WHERE catalyst_id = %s",
                            (
                                result["title_cn"],
                                result.get("summary_cn"),
                                row["catalyst_id"],
                            ),
                        )
                        # 同时更新 row 里的值，便于邮件渲染
                        row = dict(row) if hasattr(row, 'keys') else row
                        # psycopg3 的 Row 对象是 dict-like 但不可变，重新查询
                        ai_translated += 1
                except Exception as e:
                    logger.warning("催化剂翻译失败 [%s]: %s", row.get("symbol"), e)

            # --- A 级信号深度评审 ---
            if reviewer and row.get("tier") == "A" and not row.get("ai_deep_review"):
                try:
                    # 重新查一次完整行（确保有翻译后的中文字段）
                    full_row = _fetch_signal_row(conn, row["signal_id"])
                    if full_row:
                        review_data = _signal_row_to_deep_review_input(full_row)
                        deep = reviewer.review(review_data)
                        if deep:
                            import json
                            conn.execute(
                                "UPDATE biz.catalyst_signal "
                                "SET ai_deep_review = %s::jsonb, ai_reason = %s "
                                "WHERE signal_id = %s",
                                (json.dumps(deep, ensure_ascii=False),
                                 deep.get("overall_review")[:500],
                                 row["signal_id"]),
                            )
                            ai_enhanced += 1
                except Exception as e:
                    logger.warning("AI深度评审失败 [%s]: %s", row.get("symbol"), e)

    # 重新查询一次（拿最新的翻译 + 深度评审结果）
    if ai_translated > 0 or ai_enhanced > 0:
        rows = conn.execute("""
            SELECT s.signal_id, s.tier, s.composite_score, s.kind,
                   s.asset_id, a.canonical_name, a.canonical_symbol AS symbol,
                   a.asset_type, a.primary_sector, a.categories,
                   a.market_cap, a.market_cap_rank,
                   a.circulating_supply, a.total_supply,
                   a.ath_usd, a.launch_date, a.description_short,
                   c.title AS catalyst_title, c.title_cn, c.source_code,
                   c.body_text AS catalyst_summary, c.ai_summary,
                   s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
                   s.investment_cycle, s.ai_reason, s.ai_deep_review,
                   s.technical_state, s.resonance_state,
                   s.invalidation, s.persistence,
                   s.base_strength, s.resonance_score,
                   s.confidence, s.regime,
                   -- 风险标签（risk_label 存的是 high/medium/low）
                   (SELECT json_build_array(json_build_object('level', arl.risk_label, 'label',
                         CASE arl.risk_label
                           WHEN 'high' THEN '高风险'
                           WHEN 'medium' THEN '中风险'
                           WHEN 'low' THEN '低风险'
                           WHEN 'critical' THEN '极高风险'
                           ELSE '风险等级未知'
                         END))
                      FROM biz.asset_risk_labels arl
                     WHERE arl.asset_id = s.asset_id) AS risk_labels,
                   -- 最新日行情 + 7 日均量 + 量比 + 链上池流动性（口径见 _MARKET_COLS_SQL）
""" + _MARKET_COLS_SQL + """
            FROM biz.catalyst_signal s
            JOIN core.asset a ON s.asset_id = a.asset_id
            JOIN biz.asset_catalyst c ON s.catalyst_id = c.catalyst_id
""" + _MARKET_LATERAL_SQL + """
            WHERE s.signal_id = ANY(%s)
              AND s.tier = 'A'
              AND s.status = 'open'
            ORDER BY s.composite_score DESC
        """, (new_signal_ids,)).fetchall()

    sent = 0
    skipped = 0
    failed = 0
    suppressed = 0
    alert_signals = []

    # 跨通道去重：慢通道 digest 已发过的信号不再发快讯（避免同一信号两封 A 级邮件）。
    # 放在取锁之前，避免为「注定跳过」的行留下 sending 残迹。
    _slow_sent = _slow_digest_sent_recently(conn, [r["signal_id"] for r in rows])

    for row in rows:
        # 慢通道 digest 近 24h 已覆盖 ⇒ 跳过（双向去重的快讯侧）
        if row["signal_id"] in _slow_sent:
            skipped += 1
            continue

        # 原子获取发送锁（防并发重复）；同时检查 24h 去重窗口
        acquired = _try_acquire_send_lock(
            conn, row["signal_id"], NTYPE_FAST_ALERT,
            tier="A", subject=None,
        )
        if not acquired:
            skipped += 1
            continue

        # AI 否决闸门（P0-D1/D2）：拿到发送权后再判「该不该发」。
        # 顺序有意放在加锁之后——已有 sent 记录的信号会走上面的 skipped 分支，
        # 不会在这里被改写成 suppressed 而污染历史留痕。
        blocked, why = _ai_review_blocks_alert(row.get("ai_deep_review"))
        if blocked:
            suppressed += 1
            _mark_sent(conn, row["signal_id"], NTYPE_FAST_ALERT, "A", None,
                       status="suppressed", error_msg=f"AI 否决：{why}")
            # 审计 2026-10-02 P1-2：AI 否定结论必须消费到信号层质量门。
            # 此前 AI 判「资产错配/不建议」只抑制发信，DB 里 tier 仍挂 A，
            # 早报/看板/回测读 tier 时这条「被否决的高分」照样占据 A 级。
            # 与慢通道 run_ai_decision 的降级口径一致：tier 封顶 C（只降级不改分）。
            if (row.get("tier") or "").strip().upper() in ("A", "B"):
                conn.execute(
                    "UPDATE biz.catalyst_signal SET tier = 'C', updated_at = NOW() "
                    "WHERE signal_id = %s AND status IN ('open', 'watch')",
                    (row["signal_id"],),
                )
            logger.info("A 级快讯已抑制 [%s] signal=%s：%s",
                        row["symbol"], row["signal_id"], why)
            continue

        # 优先用中文标题，兜底用原文
        display_title = row.get("title_cn") or row.get("catalyst_title") or ""
        # 构建邮件（美股/商品加标记，便于在收件箱区分）
        cls_tag = "[美股·商品]" if is_stock(row["canonical_name"], row["symbol"]) else "[加密]"
        subject = f"🚀 {cls_tag} A级催化剂信号: {row['symbol']} - {display_title}"
        body = _build_fast_alert_html(row)

        ok, msg = _send_email(subject, body)

        if ok:
            sent += 1
            _mark_sent(conn, row["signal_id"], NTYPE_FAST_ALERT, "A", subject,
                       status="sent")
            _mark_signal_notified(conn, [row["signal_id"]], "pre_alert_sent_at")
            alert_signals.append({"signal_id": row["signal_id"], "symbol": row["symbol"]})
        else:
            failed += 1
            _mark_sent(conn, row["signal_id"], NTYPE_FAST_ALERT, "A", subject,
                       status="failed", error_msg=msg)
            logger.warning("快提醒发送失败 [%s]: %s", row["symbol"], msg)

    return {
        "sent": sent,
        "suppressed": suppressed,
        "skipped": skipped,
        "failed": failed,
        "total_a_grade": len(rows),
        "signals": alert_signals,
    }


def _build_fast_alert_html(row) -> str:
    """构建 A 级快提醒邮件 HTML（全面中文化 + AI 深度评审版）。"""
    import html as _html

    score = row.get("composite_score") or 0
    cycle = row.get("investment_cycle")
    reason = row.get("ai_reason")
    ai_deep = row.get("ai_deep_review")
    tp = row.get("take_profit")
    sl = row.get("stop_loss")
    entry = row.get("entry_price")
    rr = row.get("rr_ratio")
    symbol = row.get("symbol", "?")
    name = row.get("canonical_name", "")
    # 优先展示中文标题/摘要
    catalyst_title = row.get("title_cn") or row.get("catalyst_title") or ""
    catalyst_summary = row.get("ai_summary") or row.get("catalyst_summary") or ""
    source_code = row.get("source_code", "")
    asset_type = row.get("asset_type", "")
    primary_sector = row.get("primary_sector", "")
    categories = row.get("categories") or []
    market_cap = row.get("market_cap")
    market_cap_rank = row.get("market_cap_rank")
    circulating_supply = row.get("circulating_supply")
    total_supply = row.get("total_supply")
    ath_usd = row.get("ath_usd")
    launch_date = row.get("launch_date")
    description_short = row.get("description_short")
    current_price = row.get("current_price")
    change_24h_pct = row.get("change_24h_pct")
    technical_state = row.get("technical_state")
    resonance_state = row.get("resonance_state")
    invalidation = row.get("invalidation")
    persistence = row.get("persistence")
    risk_labels = row.get("risk_labels") or []
    liquidity_score = row.get("liquidity_score")
    confidence = row.get("confidence")
    regime = row.get("regime")
    kind = row.get("kind")

    # 审计 P1-D3：资产简介里 CMC 拼的过期行情句必须先剥掉，否则与实时价格区块同屏打架
    desc_clean = _strip_stale_price_sentences(description_short)
    # 审计 P3-D12：symbol 与 canonical_name 相同时（如 XRP / XRP）去重，避免标题冗余
    header_name = symbol if not name or str(name) == str(symbol) else f"{symbol} / {name}"

    # ---------- 枚举翻译字典 ----------
    KIND_MAP = {"structural": "结构性催化", "event": "事件型催化", "sentiment": "情绪型催化", "noise": "噪声"}
    TECH_MAP = {"up": "上升趋势", "range": "震荡整理", "down": "下降趋势", "unknown": "未知"}
    RESONANCE_MAP = {"confirmed": "强共振", "weak": "弱共振", "divergent": "背离", "pending": "待确认"}
    REGIME_MAP = {"risk_on": "风险偏好（Risk On）", "neutral": "中性（Neutral）", "risk_off": "风险规避（Risk Off）"}
    PERSIST_MAP = {"structural": "持续性（结构性，7天+）", "one_off": "一次性催化（3天内）", "decaying": "衰减型（1天内）"}
    RISK_MAP = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}
    ASSET_TYPE_MAP = {"coin": "公链币", "token": "代币", "stablecoin": "稳定币", "stock": "股票", "commodity": "商品", "index": "指数"}

    def _cn(val, mapping, default=None):
        if not val:
            return default or "—"
        return mapping.get(str(val).lower(), str(val))

    # ---------- 辅助函数 ----------
    def _fmt_mcap(val):
        if val is None:
            return "—"
        v = float(val)
        if v >= 1e12:
            return f"${v/1e12:.2f}T"
        if v >= 1e9:
            return f"${v/1e9:.2f}B"
        if v >= 1e6:
            return f"${v/1e6:.2f}M"
        if v >= 1e3:
            return f"${v/1e3:.1f}K"
        return f"${v:.2f}"

    def _fmt_supply(val):
        if val is None:
            return "—"
        v = float(val)
        if v >= 1e12:
            return f"{v/1e12:.2f}T"
        if v >= 1e9:
            return f"{v/1e9:.2f}B"
        if v >= 1e6:
            return f"{v/1e6:.2f}M"
        return f"{v:,.0f}"

    def _fmt_pct(val):
        if val is None:
            return "—"
        sign = "+" if val > 0 else ""
        return f"{sign}{val:.2f}%"

    def _pct_color(val):
        if val is None:
            return "#6b7280"
        if val > 0:
            return "#059669"
        if val < 0:
            return "#dc2626"
        return "#6b7280"

    def _build_anomaly_quick_card(row, kind: str) -> str:
        """构建盘面异动速览小卡片（OI/CVD/资金费率/量比）。"""
        if kind == "oi":
            oi_chg = row.get("oi_change_24h_pct")
            oi_1h = row.get("oi_1h_chg_pct")
            if oi_chg is None:
                return ""
            oi_val = float(oi_chg) if oi_chg is not None else 0
            color = _pct_color(oi_val)
            arrow = "↑" if oi_val > 0 else ("↓" if oi_val < 0 else "→")
            sub = f"1h {_fmt_pct(oi_1h)}" if oi_1h is not None else ""
            return f"""
            <div style="flex:1;min-width:100px;background:#fff;border-radius:6px;padding:6px 8px">
              <div style="color:#6b7280;font-size:10.5px">📊 OI (24h)</div>
              <div style="font-weight:600;color:{color};margin-top:2px;font-size:13px">{arrow} {_fmt_pct(oi_chg)}</div>
              {f'<div style="color:#6b7280;font-size:10px">{sub}</div>' if sub else ''}
            </div>
            """

        elif kind == "cvd":
            cvd_r = row.get("cvd_ratio_24h")
            cvd_1h = row.get("cvd_1h_total")
            if cvd_r is None and cvd_1h is None:
                return ""
            try:
                cvd_val = float(cvd_r) if cvd_r is not None else 0
            except (TypeError, ValueError):
                cvd_val = 0
            color = _pct_color(cvd_val)
            arrow = "↑" if cvd_val > 0 else ("↓" if cvd_val < 0 else "→")
            sub = f"1h ${float(cvd_1h)/1e3:.1f}K" if cvd_1h is not None else ""
            return f"""
            <div style="flex:1;min-width:100px;background:#fff;border-radius:6px;padding:6px 8px">
              <div style="color:#6b7280;font-size:10.5px">💧 CVD (24h)</div>
              <div style="font-weight:600;color:{color};margin-top:2px;font-size:13px">{arrow} {cvd_val:+.2f}</div>
              {f'<div style="color:#6b7280;font-size:10px">{sub}</div>' if sub else ''}
            </div>
            """

        elif kind == "funding":
            fr = row.get("funding_rate_pct")
            if fr is None:
                return ""
            try:
                fr_val = float(fr)
            except (TypeError, ValueError):
                fr_val = 0
            # 正费率=多头拥挤（偏空警报），负费率=空头拥挤（偏多警报）
            if fr_val > 0.05:
                color = "#dc2626"  # 多头拥挤，红色警告
            elif fr_val < -0.05:
                color = "#059669"  # 空头拥挤，绿色机会
            else:
                color = "#6b7280"
            label = "多头拥挤" if fr_val > 0.01 else ("空头拥挤" if fr_val < -0.01 else "多空均衡")
            return f"""
            <div style="flex:1;min-width:100px;background:#fff;border-radius:6px;padding:6px 8px">
              <div style="color:#6b7280;font-size:10.5px">💰 资金费率</div>
              <div style="font-weight:600;color:{color};margin-top:2px;font-size:13px">{fr_val:.4f}%</div>
              <div style="color:#6b7280;font-size:10px">{label}</div>
            </div>
            """

        elif kind == "volume":
            vr = row.get("volume_ratio_7d")
            v24 = row.get("volume_24h_usd")
            if vr is None and v24 is None:
                return ""
            # 审计 2026-09-24 P1-D4：缺失值**不得**默认成 0——0 会命中下方「≤0.5 极度缩量」，
            # 让同一封邮件同时出现「量比 0.00x 极度缩量」与 AI 的「量比 1.71x 温和放量」。
            # 量比算不出来时如实标「未知」（与 ai_enhance 对 None 的「未知」约定一致）。
            vr_val = None
            if vr is not None:
                try:
                    vr_val = float(vr)
                except (TypeError, ValueError):
                    vr_val = None
            if vr_val is None:
                return """
            <div style="flex:1;min-width:100px;background:#fff;border-radius:6px;padding:6px 8px">
              <div style="color:#6b7280;font-size:10.5px">📈 量比 (24h/7d)</div>
              <div style="font-weight:600;color:#6b7280;margin-top:2px;font-size:13px">—</div>
              <div style="color:#9ca3af;font-size:10px">7 日均量缺失，无法计算</div>
            </div>
            """
            if vr_val >= 2.0:
                color = "#dc2626"  # 大幅放量，警戒
                label = "大幅放量"
            elif vr_val >= 1.5:
                color = "#d97706"  # 温和放量
                label = "温和放量"
            elif vr_val <= 0.5:
                color = "#6b7280"
                label = "极度缩量"
            else:
                color = "#6b7280"
                label = "量能正常"
            return f"""
            <div style="flex:1;min-width:100px;background:#fff;border-radius:6px;padding:6px 8px">
              <div style="color:#6b7280;font-size:10.5px">📈 量比 (24h/7d)</div>
              <div style="font-weight:600;color:{color};margin-top:2px;font-size:13px">{vr_val:.2f}x</div>
              <div style="color:#6b7280;font-size:10px">{label}</div>
            </div>
            """

        return ""

    def _fmt_price(val):
        if val is None:
            return "—"
        v = float(val)
        # 定点档统一去尾零（与模块级 _fmt_price 同口径）：$620.50 → $620.5、$100.00 → $100。
        # 科学计数档刻意不 trim（_trim_trailing_zeros("1.0000e-10") 会退化成 "1.0000e-1"）。
        if v >= 1000:
            return "$" + _trim_trailing_zeros(f"{v:,.2f}")
        if v >= 1:
            return "$" + _trim_trailing_zeros(f"{v:.2f}")
        if v >= 0.01:
            return "$" + _trim_trailing_zeros(f"{v:.4f}")
        if v >= 1e-4:
            return "$" + _trim_trailing_zeros(f"{v:.6f}")
        return f"${v:.4e}"

    # ---------- 各区块构建 ----------

    # 1. 交易档位（审计 2026-09-24 P0-D1）
    # 原实现只看 entry/tp/sl 是否非空，完全不消费 AI 结论——于是「AI 说不建议参与」
    # 的同封邮件里照样渲染「📈 做多 · 入场/目标/止损」，扫一眼档位的人会得到与警告
    # 相反的动作。判据与发送侧共用 _ai_review_blocks_alert，避免两处口径漂移。
    trade_section = ""
    has_levels = tp is not None or sl is not None or entry is not None
    vetoed, veto_reason = _ai_review_blocks_alert(ai_deep)
    if vetoed and has_levels:
        trade_section = f"""
        <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:16px;margin-top:16px">
          <div style="font-size:13px;font-weight:600;color:#991b1b;margin-bottom:6px">📊 交易计划：已抑制</div>
          <div style="font-size:12px;color:#991b1b;line-height:1.6">
            本条信号附带的入场/目标/止损档位<b>不予展示</b>——{_html.escape(veto_reason)}，
            规则档位与 AI 结论方向相反，并列展示会误导。请以 AI 深度评审的结论与风控建议为准。
          </div>
        </div>
        """
    elif has_levels:
        # 判断方向
        direction = "—"
        if entry is not None and tp is not None and sl is not None:
            if tp > sl:  # 做多
                direction = "📈 做多"
            else:
                direction = "📉 做空"

        trade_section = f"""
        <div style="background:#fafafa;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin-top:16px">
          <div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:4px">📊 交易计划（规则计算）</div>
          <div style="font-size:11px;color:#9ca3af;margin-bottom:12px;line-height:1.5">
            档位由规则按价格结构派生，<b>未与 AI 风控建议校准</b>；两者不一致时请以下方
            「🤖 AI 深度评审」的进场/止损/止盈建议为准。
          </div>
          <div style="display:flex;gap:12px;flex-wrap:wrap">
            <div style="flex:1;min-width:110px;text-align:center">
              <div style="font-size:11px;color:#6b7280">方向</div>
              <div style="font-size:15px;font-weight:600;color:#111827;margin-top:4px">{direction}</div>
            </div>
            <div style="flex:1;min-width:110px;text-align:center">
              <div style="font-size:11px;color:#6b7280">入场价</div>
              <div style="font-size:15px;font-weight:600;color:#111827;margin-top:4px">{_fmt_price(entry)}</div>
            </div>
            <div style="flex:1;min-width:110px;text-align:center;background:#f0fdf4;border-radius:6px;padding:8px 4px">
              <div style="font-size:11px;color:#059669">目标价</div>
              <div style="font-size:15px;font-weight:700;color:#059669;margin-top:4px">{_fmt_price(tp)}</div>
            </div>
            <div style="flex:1;min-width:110px;text-align:center;background:#fef2f2;border-radius:6px;padding:8px 4px">
              <div style="font-size:11px;color:#dc2626">止损价</div>
              <div style="font-size:15px;font-weight:700;color:#dc2626;margin-top:4px">{_fmt_price(sl)}</div>
            </div>
            <div style="flex:1;min-width:110px;text-align:center">
              <div style="font-size:11px;color:#6b7280">盈亏比</div>
              <div style="font-size:15px;font-weight:600;color:#2563eb;margin-top:4px">{f"{rr:.1f}" if rr is not None else "—"}</div>
            </div>
          </div>
          {f'<div style="font-size:12px;color:#6b7280;margin-top:10px;line-height:1.5"><span style="font-weight:500">失效条件：</span>{_html.escape(invalidation)}</div>' if invalidation else ''}
        </div>
        """

    # 2. 代币基本面
    sector_tags = ""
    if categories:
        tag_html = " ".join(
            f'<span style="display:inline-block;padding:2px 8px;background:#f3f4f6;color:#4b5563;border-radius:12px;font-size:11px;margin:2px">{_html.escape(c)}</span>'
            for c in categories[:5]
        )
        sector_tags = f'<div style="margin-top:6px">{tag_html}</div>'

    circ_ratio = ""
    if circulating_supply and total_supply and float(total_supply) > 0:
        ratio = float(circulating_supply) / float(total_supply) * 100
        circ_ratio = f"（{ratio:.1f}%流通）"

    fundamentals_section = f"""
    <div style="margin-top:16px">
      <div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:10px">🪙 代币基本面</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px">
        <div style="flex:1;min-width:140px">
          <div style="color:#6b7280">市值 / 排名</div>
          <div style="font-weight:600;color:#111827;margin-top:2px">{_fmt_mcap(market_cap)} / #{market_cap_rank if market_cap_rank else '—'}</div>
        </div>
        <div style="flex:1;min-width:140px">
          <div style="color:#6b7280">当前价格 / 24h / 7d</div>
          <div style="font-weight:600;margin-top:2px">
            <span style="color:#111827">{_fmt_price(current_price)}</span>
            <span style="color:{_pct_color(change_24h_pct)};font-size:11.5px"> {_fmt_pct(change_24h_pct)}</span>
            {f'<span style="color:{_pct_color(row.get("change_7d_pct"))};font-size:11.5px"> / 7d {_fmt_pct(row.get("change_7d_pct"))}</span>' if row.get("change_7d_pct") is not None else ''}
          </div>
        </div>
        <div style="flex:1;min-width:140px">
          <div style="color:#6b7280">历史高点 / 距离</div>
          <div style="font-weight:600;color:#111827;margin-top:2px">{_fmt_price(ath_usd)} / {
              f"{((float(current_price)/float(ath_usd)-1)*100):.1f}%"
              if current_price and ath_usd and float(ath_usd) > 0 else "—"
          }</div>
        </div>
        <div style="flex:1;min-width:140px">
          <div style="color:#6b7280">总供应量{circ_ratio}</div>
          <div style="font-weight:600;color:#111827;margin-top:2px">{_fmt_supply(total_supply)}</div>
        </div>
        <div style="flex:1;min-width:140px">
          <div style="color:#6b7280">24h 成交量</div>
          <div style="font-weight:600;color:#111827;margin-top:2px">{_fmt_mcap(row.get("volume_24h_usd")) if row.get("volume_24h_usd") is not None else '—'}</div>
        </div>
        <div style="flex:1;min-width:140px">
          <div style="color:#6b7280">上线时间 / 主赛道</div>
          <div style="font-weight:600;color:#111827;margin-top:2px">{str(launch_date) if launch_date else '未收录'} / {primary_sector or _cn(asset_type, ASSET_TYPE_MAP)}</div>
          <div style="color:#9ca3af;font-size:10px;margin-top:2px">赛道为 CMC 分类口径，仅作参考</div>
        </div>
      </div>
      {sector_tags}
      {f'<div style="font-size:12px;color:#6b7280;margin-top:8px;line-height:1.5">{_html.escape(desc_clean[:200])}{"..." if len(desc_clean) > 200 else ""}</div>' if desc_clean else ''}
    </div>
    """

    # 3. 信号评分明细
    base_strength = row.get("base_strength")
    resonance_score = row.get("resonance_score")
    score_breakdown = ""
    if base_strength is not None or resonance_score is not None:
        # 审计 P2-D7：原标签「置信度 86%」与 AI 评审的「信心度：低」并列，会被读成
        # 「系统 86% 确信」——两者口径不同（前者是 composite_score/100 的模型方向置信，
        # 后者是 AI 对资产匹配与参与价值的信心），标签必须区分开。
        score_breakdown = f"""
        <div style="font-size:11px;color:#6b7280;margin-top:4px">
          催化强度 {base_strength or 0:.0f} · 共振分 {resonance_score or 0:.0f} · 模型方向置信 {(confidence or 0)*100:.0f}%
        </div>
        """

    # 4. 技术面状态（中文枚举）
    tech_section = ""
    if technical_state or resonance_state or regime:
        tech_items = []
        if kind:
            tech_items.append(f"催化类型：<b>{_cn(kind, KIND_MAP)}</b>")
        if technical_state:
            tech_items.append(f"技术形态：<b>{_cn(technical_state, TECH_MAP)}</b>")
        if resonance_state:
            # 审计 P2-D6：共振分（加权分 0-100）与共振状态（涨跌幅/量能阈值判定）是两套
            # 口径，高分 + 弱共振是常态（近 14 天 A 级实测状态全为 weak，分 67~98）。
            # 并列展示而不说明，会被读成「90 分却弱共振」的自相矛盾。
            tech_items.append(
                f"共振状态：<b>{_cn(resonance_state, RESONANCE_MAP)}</b>"
                "（按涨跌幅/量能阈值判定，与上方共振分不同口径）"
            )
        if regime:
            tech_items.append(f"市场环境：<b>{_cn(regime, REGIME_MAP)}</b>")
        if persistence:
            tech_items.append(f"催化持续性：<b>{_cn(persistence, PERSIST_MAP)}</b>")
        if liquidity_score is not None:
            # 审计 P1-D5：该值来自单条链的 DEX 池快照，不是资产全局流动性，
            # 标签必须写明口径，否则会与同屏「24h 成交量」被读成同一件事。
            tech_items.append(f"{_liquidity_label(row)}：<b>{_fmt_mcap(liquidity_score)}</b>")
        tech_section = f"""
        <div style="margin-top:16px">
          <div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:8px">📈 信号维度</div>
          <div style="font-size:12px;color:#374151;line-height:1.8">
            {' · '.join(tech_items)}
          </div>
        </div>
        """

    # 5. 风险标签
    risk_section = ""
    if risk_labels:
        risk_items = []
        for rl in risk_labels:
            level = rl.get("level", "medium")
            label = rl.get("label", "")
            color_map = {
                "critical": "#dc2626",
                "high": "#ea580c",
                "medium": "#d97706",
                "low": "#059669",
            }
            bg_map = {
                "critical": "#fef2f2",
                "high": "#fff7ed",
                "medium": "#fefce8",
                "low": "#f0fdf4",
            }
            c = color_map.get(level, "#6b7280")
            bg = bg_map.get(level, "#f3f4f6")
            risk_items.append(
                f'<span style="display:inline-block;padding:3px 10px;background:{bg};color:{c};border-radius:12px;font-size:11px;font-weight:500;margin:2px">⚠ {_html.escape(label)}</span>'
            )
        risk_section = f"""
        <div style="margin-top:16px">
          <div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:8px">⚠️ 风险标签</div>
          <div>{''.join(risk_items)}</div>
        </div>
        """

    # 6. AI 深度评审（新增：A 级信号快通道专属）
    ai_deep_section = ""
    if ai_deep and isinstance(ai_deep, dict) and ai_deep.get("verdict"):
        verdict = ai_deep.get("verdict", "")
        conf = ai_deep.get("confidence_level", "")
        pos = ai_deep.get("position_suggestion", "")
        core_logic = ai_deep.get("core_logic", "")
        key_risks = ai_deep.get("key_risks") or []
        catalyst_stage = ai_deep.get("catalyst_stage", "")
        timing_advice = ai_deep.get("timing_advice", "")
        stop_loss_advice = ai_deep.get("stop_loss_advice", "")
        take_profit_advice = ai_deep.get("take_profit_advice", "")
        overall_review = ai_deep.get("overall_review", "")
        asset_match = ai_deep.get("asset_match_confidence", "high")

        # 资产匹配置信度（low 时用红色警告）
        # 审计 P2-D8：原文案对所有 low 一律写死「ticker同名但不同项目」——审计原例 XRP
        # 是「BCH/UNI 新闻里的被动提及」，并非撞名，硬编码成因会误导。改为优先用 AI 给出的
        # asset_match_reason，缺失时回退到不臆断成因的通用文案。
        match_warning = ""
        if asset_match == "low":
            match_reason = str(ai_deep.get("asset_match_reason") or "").strip()
            match_detail = (
                f"AI 判定原因：{_html.escape(match_reason)}"
                if match_reason
                else "系统检测到该代币与催化剂所述项目可能不一致，请谨慎核实后再做决策。"
            )
            match_warning = f"""
          <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:10px 12px;margin-bottom:10px">
            <div style="font-size:12px;font-weight:700;color:#dc2626">⚠️ 资产匹配警告：代币与催化剂可能不匹配</div>
            <div style="font-size:11px;color:#991b1b;margin-top:2px">{match_detail}</div>
          </div>
            """

        # verdict 配色
        verdict_color = "#059669" if "强烈" in verdict or "建议" in verdict and "不" not in verdict else (
            "#dc2626" if "不建议" in verdict else "#d97706"
        )

        # AI 深度评审区块边框色：资产不匹配时用红色系
        if asset_match == "low":
            border_color = "#fca5a5"
            bg_gradient = "linear-gradient(135deg,#fef2f2,#fff1f2)"
            header_color = "#991b1b"
        else:
            border_color = "#a7f3d0"
            bg_gradient = "linear-gradient(135deg,#f0fdf4,#ecfeff)"
            header_color = "#065f46"

        risk_list_html = ""
        if key_risks:
            risk_list_html = "".join(
                f'<li style="margin-bottom:4px;color:#374151;line-height:1.6">{_html.escape(r)}</li>'
                for r in key_risks
            )
            risk_list_html = f'<ul style="margin:6px 0 0 0;padding-left:20px;font-size:12px">{risk_list_html}</ul>'

        ai_deep_section = f"""
        <div style="margin-top:20px;background:{bg_gradient};border:1px solid {border_color};border-radius:10px;padding:16px">
          <div style="font-size:14px;font-weight:700;color:{header_color};margin-bottom:10px">
            🤖 AI 深度评审 · A级信号
            <span style="font-size:12px;font-weight:500;color:#10b981;margin-left:8px">信心度：{_html.escape(conf)}</span>
          </div>

          {match_warning}

          <div style="background:#fff;border-radius:8px;padding:10px 12px;margin-bottom:10px">
            <div style="font-size:12px;color:#6b7280">交易结论</div>
            <div style="font-size:17px;font-weight:700;color:{verdict_color};margin-top:4px">{_html.escape(verdict)}</div>
            {f'<div style="font-size:12px;color:#4b5563;margin-top:4px">建议仓位：<b>{_html.escape(pos)}</b></div>' if pos else ''}
          </div>

          <!-- 盘面异动速览 -->
          <div style="display:flex;gap:8px;flex-wrap:wrap;font-size:11.5px;margin-bottom:10px">
            {_build_anomaly_quick_card(row, 'oi')}
            {_build_anomaly_quick_card(row, 'cvd')}
            {_build_anomaly_quick_card(row, 'funding')}
            {_build_anomaly_quick_card(row, 'volume')}
          </div>

          <div style="font-size:12px;color:#374151;line-height:1.7;margin-bottom:8px">
            <span style="font-weight:600;color:#065f46">核心逻辑：</span>{_html.escape(core_logic)}
          </div>

          <div style="display:flex;gap:10px;flex-wrap:wrap;font-size:12px;margin-bottom:8px">
            <div style="flex:1;min-width:130px;background:#fff;border-radius:6px;padding:8px">
              <div style="color:#6b7280;font-size:11px">催化剂阶段</div>
              <div style="font-weight:600;color:#111827;margin-top:2px">{_html.escape(catalyst_stage)}</div>
            </div>
            <div style="flex:1;min-width:130px;background:#fff;border-radius:6px;padding:8px">
              <div style="color:#6b7280;font-size:11px">进场时机</div>
              <div style="font-weight:600;color:#111827;margin-top:2px">{_html.escape(timing_advice)}</div>
            </div>
          </div>

          <div style="display:flex;gap:10px;flex-wrap:wrap;font-size:12px;margin-bottom:8px">
            <div style="flex:1;min-width:130px;background:#fff;border-radius:6px;padding:8px">
              <div style="color:#6b7280;font-size:11px">止损建议</div>
              <div style="font-weight:500;color:#dc2626;margin-top:2px;font-size:11.5px;line-height:1.5">{_html.escape(stop_loss_advice)}</div>
            </div>
            <div style="flex:1;min-width:130px;background:#fff;border-radius:6px;padding:8px">
              <div style="color:#6b7280;font-size:11px">止盈建议</div>
              <div style="font-weight:500;color:#059669;margin-top:2px;font-size:11.5px;line-height:1.5">{_html.escape(take_profit_advice)}</div>
            </div>
          </div>

          {f'<div style="font-size:12px;color:#374151;margin-bottom:6px"><span style="font-weight:600;color:#b91c1c">关键风险：</span>{risk_list_html}</div>' if risk_list_html else ''}

          <div style="background:#f0fdfa;border-radius:6px;padding:10px 12px;margin-top:8px">
            <div style="font-size:11.5px;color:#0f766e;font-weight:500;margin-bottom:4px">📝 综合评审</div>
            <div style="font-size:12px;color:#115e59;line-height:1.7">{_html.escape(overall_review)}</div>
          </div>
        </div>
        """

    # 7. 催化剂详情摘要（优先中文）
    catalyst_detail_section = ""
    if catalyst_summary:
        # 标注是否 AI 翻译版
        label = "🤖 AI 中文摘要" if row.get("ai_summary") else "原文摘要"
        catalyst_detail_section = f"""
        <div style="margin-top:16px">
          <div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:8px">📰 催化剂详情 <span style="font-size:11px;font-weight:400;color:#7c3aed">{label}</span></div>
          <div style="font-size:12px;line-height:1.7;color:#374151">
            {_html.escape(catalyst_summary[:500])}{"..." if catalyst_summary and len(catalyst_summary) > 500 else ""}
          </div>
        </div>
        """

    # 8. AI 信号解读（如果没有深度评审，展示普通 ai_reason）
    reason_section = ""
    if reason and not (ai_deep and ai_deep.get("overall_review")):
        reason_section = f"""
        <div style="margin-top:16px">
          <div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:8px">🤖 AI 信号解读</div>
          <div style="font-size:12px;line-height:1.7;color:#374151;background:#f9fafb;border-left:3px solid #7c3aed;padding:10px 12px;border-radius:0 6px 6px 0">
            {_html.escape(reason)}
          </div>
        </div>
        """

    cycle_html = f'<div style="font-size:16px;font-weight:600;margin-top:8px">{cycle}</div>' if cycle else \
        '<div style="font-size:12px;color:#9ca3af;margin-top:10px">慢通道补全中</div>'

    return f"""
    <div style="font-family:sans-serif;max-width:680px;margin:auto;padding:16px;background:#f9fafb">
      <!-- 顶部卡片 -->
      <div style="background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;padding:20px 22px;border-radius:12px 12px 0 0">
        <div style="font-size:11px;opacity:.75;letter-spacing:1.5px">A级催化剂信号 · 快通道</div>
        <div style="font-size:26px;font-weight:700;margin-top:8px">{_html.escape(header_name)}</div>
        <div style="margin-top:6px;font-size:13px;opacity:.92;line-height:1.4">{_html.escape(catalyst_title)}</div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:18px 20px;border-radius:0 0 12px 12px">

        <!-- 评分行 -->
        <div style="display:flex;gap:16px;align-items:flex-start">
          <div style="flex:0 0 100px;text-align:center;background:#faf5ff;border-radius:10px;padding:12px">
            <div style="font-size:10px;color:#7c3aed;letter-spacing:1px">综合评分</div>
            <div style="font-size:36px;font-weight:800;color:#7c3aed;line-height:1">{score:.0f}</div>
            {score_breakdown}
          </div>
          <div style="flex:1">
            <div style="display:flex;gap:16px;flex-wrap:wrap">
              <div style="flex:1;min-width:100px">
                <div style="font-size:10px;color:#6b7280;letter-spacing:.5px">信息源</div>
                <div style="font-size:14px;font-weight:600;color:#111827;margin-top:4px">{_html.escape(source_code)}</div>
              </div>
              <div style="flex:1;min-width:100px">
                <div style="font-size:10px;color:#6b7280;letter-spacing:.5px">投资周期</div>
                {cycle_html}
              </div>
              <div style="flex:1;min-width:100px">
                <div style="font-size:10px;color:#6b7280;letter-spacing:.5px">主赛道</div>
                <div style="font-size:14px;font-weight:600;color:#111827;margin-top:4px">{_html.escape(primary_sector or _cn(asset_type, ASSET_TYPE_MAP))}</div>
              </div>
            </div>
          </div>
        </div>

        <!-- AI 深度评审（最前面展示，A 级核心价值） -->
        {ai_deep_section}

        <!-- 交易计划 -->
        {trade_section}

        <!-- 代币基本面 -->
        {fundamentals_section}

        <!-- 信号维度 -->
        {tech_section}

        <!-- 催化剂详情 -->
        {catalyst_detail_section}

        <!-- AI 解读（无深度评审时展示） -->
        {reason_section}

        <!-- 风险标签 -->
        {risk_section}

        <!-- 底部提示 -->
        <div style="margin-top:18px;padding:12px 14px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a">
          <div style="font-size:12px;color:#92400e;font-weight:500">⚠️ 免责声明</div>
          <div style="font-size:11px;color:#78350f;margin-top:4px;line-height:1.5">
            本邮件由 AI 自动生成，仅供研究参考，不构成投资建议。加密货币市场波动极大，请务必做好仓位管理与风险控制，切勿重仓单一标的。
          </div>
        </div>

        <div style="margin-top:14px;padding:10px 14px;background:#f5f3ff;border-radius:8px">
          <div style="font-size:12px;color:#7c3aed;font-weight:500">⚡ 快通道信号 · 慢通道将补充更多维度</div>
          <div style="font-size:11px;color:#6b7280;margin-top:4px">慢通道将补全 G3-G5 二阶受益展开、持续性验证、基本面验证、技术面量化分析</div>
        </div>

        <div style="margin-top:16px;font-size:11px;color:#9ca3af;text-align:center">
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
        "new_signals_24h": int(crypto_result.get("new_signals_24h", 0))
                           + int(stock_result.get("new_signals_24h", 0)),
        "crypto": crypto_result,
        "stock": stock_result,
    }


def _send_slow_digest_class(conn, stats: dict, asset_class: str) -> dict:
    """发送某个资产类别的 A 级 Alert 邮件（加密货币 / 美股·商品）。

    定位（OPT-CATALYST-ALERT-001 P0-1）：从「24h B/C 汇总」转型为「高置信度 A 级 Alert」。
    - 仅 tier='A' 且 status='open'（d3：未充分定价的可动作信号）+ 有完整交易档位
      （entry/stop/tp）的信号入选，按分取前 2
    - 无 A 级信号 → **静默跳过，不发空窗邮件**（`058c246` 2026-09-16 决议：空窗不发信避免刷屏）。
      「通道空转」这件事改由 `send_channel_silence_alert()` 在**运维侧**告警，不在本函数内
    - 各自独立去重（sentinel + ntype），互不影响
    """
    is_crypto = asset_class == "crypto"
    label = "加密货币" if is_crypto else "美股·商品"
    ntype = NTYPE_SLOW_DIGEST if is_crypto else NTYPE_SLOW_DIGEST_STOCK
    sentinel = SENTINEL_SLOW_DIGEST_SIGNAL_ID if is_crypto else SENTINEL_SLOW_DIGEST_STOCK_SIGNAL_ID

    # 只读预检：24h 内已发过则跳过（慢通道并发风险低，用只读即可）
    if _is_sent(conn, sentinel, ntype):
        return {"sent": 0, "skipped": 1, "failed": 0,
                "reason": f"{label} 24h 内已发送过 Alert，跳过",
                "new_signals_24h": 0}

    rows = _recent_new_a_signals(conn, hours=24, asset_class=asset_class)
    new_count = len(rows)
    tier_label = "A级"

    if not rows:
        # 审计 2026-10-02 P0-3：A 级 open 长期为 0（全库 A 级 24 条、open 0 条），
        # 导致 slow_digest 连续静默。方案 B：无 A 级时回退到「B 级高置信度」
        # （composite>=70 的 open 信号，有完整档位）兜底出信，避免通道长期空转。
        # 仅 crypto 走此回退；美股/商品通道 A 级候选本就几乎为零，维持原静默语义
        # （058c246 决议不破，避免 stock 通道刷屏）。
        if is_crypto:
            rows = _recent_new_a_signals(conn, hours=24, asset_class=asset_class,
                                         min_tier="B", min_composite=70)
            new_count = len(rows)
            if rows:
                tier_label = "B级"

    if not rows:
        # 无 A/B 高置信度信号 → 静默跳过，不发空窗邮件（058c246 决议）。
        # 通道长期空转由 send_channel_silence_alert() 在运维侧告警。
        return {"sent": 0, "skipped": 1, "failed": 0,
                "reason": f"{label} 24h 内无 A/B 级高置信度信号，静默跳过",
                "new_signals_24h": 0}
    else:
        subject = f"🎯 催化剂 Alert·{label}·{tier_label} {new_count} 条"
        body = _build_slow_digest_html(rows, stats, class_label=label,
                                       tier_label=tier_label)

    ok, msg = _send_email(subject, body)
    _mark_sent(conn, sentinel, ntype, None, subject,
               status="sent" if ok else "failed", error_msg=msg if not ok else None)
    if ok:
        _mark_signal_notified(conn, [r["signal_id"] for r in rows], "notified_at")

    return {
        "sent": 1 if ok else 0,
        "skipped": 0,
        "failed": 0 if ok else 1,
        "reason": msg,
        "new_signals_24h": new_count,
    }


def _recent_new_a_signals(conn, hours: int = 24, asset_class: str = "crypto",
                          min_tier: str = "A", min_composite: int | None = None) -> list[dict]:
    """过去 N 小时内新信号（按资产类别，按资产去重留最高分，取前 2）。

    入选条件（OPT-CATALYST-ALERT-001 P0-1/P0-2 语义闸门，d3 修订）：
    - tier = min_tier（composite_score → tier 单点真源不变，DB tier 不改）
    - status = 'open'（d3：open 已表示「价格未充分定价」，即真正的可动作集合。
      原先额外要求 resonance_state='confirmed' 是反向的——实测 confirmed 的
      72h 前瞻超额 -2.16%（n=47）远弱于 weak +1.58%（n=231），
      即「等价格确认再开单」等于追高；confirmed 现已归入观察池 status='watch'）
    - entry/stop/tp 齐全（可交易性）
    - 可选 min_composite 门槛（方案 B 回退用：无 A 级时取 B 级 composite≥70 的高置信度信号）
    - 近 DEDUP_WINDOW_HOURS 内**未被快讯发过**（pre_alert_sent_at 判据）——
      跨通道去重，digest 仅作快讯的兜底（诊断_催化剂A级邮件延迟链路_XRP_BCH_2026-09-24）
    composite_score DESC 取前 2 条（每日 1~2 idea）。

    返回字段覆盖「决策链 G0-G7 + 代币快照」全量：signal 全维度 + catalyst 原文
    + grade/impact/resonance 分项 + core.asset 基本面 + 最新日线 + 在池信号计数，
    供 _build_a_alert_card 一次渲染，邮件不依赖外链网页。
    """
    filter_sql = ASSET_NAME_FILTER_SQL if asset_class == "crypto" else IS_STOCK_SQL
    composite_clause = "AND s.composite_score >= %s" if min_composite is not None else ""
    params: list = [hours, min_tier]
    if min_composite is not None:
        params.append(min_composite)
    params.append(DEDUP_WINDOW_HOURS)
    return conn.execute(f"""
        SELECT * FROM (
            SELECT DISTINCT ON (a.asset_id)
                   -- 信号本体（G3-G7 结果 + 档位）
                   s.signal_id, s.catalyst_id, s.asset_id,
                   s.tier, s.composite_score, s.kind, s.base_strength,
                   s.resonance_score, s.resonance_state,
                   s.persistence, s.persistence_verified,
                   s.fundamental_pass, s.fundamental_detail,
                   s.technical_state, s.technical_detail,
                   s.entry_trigger, s.entry_trigger_price,
                   s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
                   s.confidence, s.invalidation, s.regime,
                   s.ai_reason, s.investment_cycle,
                   s.expires_at, s.created_at,
                   -- 代币基础信息
                   a.canonical_name, a.canonical_symbol AS symbol,
                   a.asset_type, a.primary_sector, a.categories,
                   a.market_cap AS asset_market_cap, a.market_cap_rank,
                   a.circulating_supply, a.total_supply,
                   a.ath_usd, a.launch_date,
                   -- 催化剂原文（G0）
                   ac.title AS catalyst_title, ac.title_cn,
                   ac.ai_summary, ac.ai_event_type, ac.ai_sentiment,
                   ac.rule_event_type, ac.source_code, ac.source_url,
                   ac.published_at, ac.body_text AS catalyst_body,
                   ac.event_category,
                   -- G1 分级分项
                   cg.authority_score, cg.event_weight, cg.scope_score,
                   cg.mcap_score, cg.prelaunch_ret_24h, cg.prelaunch_penalty,
                   cg.catalyst_kind, cg.event_type_src, cg.tradable,
                   -- G2 影响 + 共振分项
                   ci.impact_direction, ci.impact_strength, ci.horizon_days,
                   ci.derived_from AS impact_derived_from,
                   cr.excess_ret_1h, cr.excess_ret_4h,
                   cr.excess_ret_24h, cr.excess_ret_72h,
                   cr.vol_zscore_24h, cr.peer_median_ret_24h,
                   cr.direction_match, cr.ret_source, cr.computed_at AS resonance_computed_at,
                   -- 代币快照：最新日线
                   md.price_usd AS current_price, md.change_24h, md.change_7d,
                   md.volume_24h, md.market_cap AS md_market_cap,
                   md.market_date AS md_date,
                   -- 代币快照：在池信号计数
                   pool.open_cnt, pool.watch_cnt
            FROM biz.catalyst_signal s
            JOIN core.asset a ON s.asset_id = a.asset_id
            JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
            LEFT JOIN biz.catalyst_grade cg ON cg.catalyst_id = s.catalyst_id
            LEFT JOIN biz.catalyst_impact ci
              ON ci.catalyst_id = s.catalyst_id AND ci.asset_id = s.asset_id
            LEFT JOIN biz.catalyst_resonance cr
              ON cr.catalyst_id = s.catalyst_id AND cr.asset_id = s.asset_id
            LEFT JOIN LATERAL (
                SELECT m.price_usd, m.change_24h, m.change_7d,
                       m.volume_24h, m.market_cap, m.market_date
                FROM biz.asset_market_daily m
                WHERE m.asset_id = s.asset_id
                ORDER BY m.market_date DESC
                LIMIT 1
            ) md ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) FILTER (WHERE x.status = 'open')  AS open_cnt,
                       COUNT(*) FILTER (WHERE x.status = 'watch') AS watch_cnt
                FROM biz.catalyst_signal x
                WHERE x.asset_id = s.asset_id
            ) pool ON TRUE
            WHERE s.status = 'open'
              AND s.created_at > NOW() - (%s::int * INTERVAL '1 hour')
              AND s.tier = %s
              {composite_clause}
              AND s.entry_price IS NOT NULL
              AND s.stop_loss IS NOT NULL
              AND s.take_profit IS NOT NULL
              -- 跨通道去重（诊断_催化剂A级邮件延迟链路_XRP_BCH_2026-09-24）：近 24h 已由
              -- 快讯发过的信号不再进 digest（快讯侧在 send_fast_alerts_for_new_signals
              -- 反向排除 notified_at 近窗口行）⇒ 同一信号 24h 内只发一封 A 级邮件。
              AND (s.pre_alert_sent_at IS NULL
                   OR s.pre_alert_sent_at < NOW() - (%s::int * INTERVAL '1 hour'))
              AND {filter_sql}
            ORDER BY a.asset_id, s.composite_score DESC
        ) t
        ORDER BY t.composite_score DESC
        LIMIT 2
    """, params).fetchall()


# =====================================================================
# 通道静默可观测（空窗不发用户邮件，只在运维侧告警）
# =====================================================================
#
# 背景（2026-09-28 排查用户报「最近几天都没收到催化剂邮件」）：
# A 级 Alert 的入选口径是 `tier='A' AND status='open'`。该口径为 0 行时通道**静默**——
# 读者无法区分「今天真的没有可动作信号」与「通道坏了」。实测物证：`fast_alert` 最后
# 发送 2026-09-23 16:50 UTC、`slow_digest` 最后 2026-09-23 16:30 UTC 之后连续 5 天无信，
# 而同期的 `major_event` 仍在每天发送 ⇒ 管道与 SMTP 都正常，停的只是候选供给。
#
# 2026-09-16 的 `058c246` 已显式决定「无 A 级信号时**不发**空窗邮件」（避免刷屏）；
# 本节**维持该决定**：不新增用户可见邮件，只把「通道空转」变成运维侧可见的告警。
#
# 判据刻意不落表、无 DDL，直接读地面事实：
#     MAX(created_at) FROM biz.catalyst_signal WHERE tier='A' AND status='open'
# 该值距今超过阈值即视为空转。无状态 ⇒ 不受容器重启影响、历史可复算、无需初始化计数器。
# 发送频率由既有 `_try_acquire_send_lock` 的 `DEDUP_WINDOW_HOURS`(=24h) 约束
# ⇒ 空转期间至多每天一封。
#
# 口径边界：只用一个「全资产」的 MAX(created_at)，不拆 crypto / stock。理由：两封 digest
# 的候选都来自该集合，且实测 `slow_digest_stock` 从未有过 A 级候选（唯一一次发送是
# 2026-09-15 的空窗期）。若日后美股通道真的独立出量，需拆分为两个哨兵分别判定。

NTYPE_CHANNEL_SILENCE = "channel_silence"
SENTINEL_CHANNEL_SILENCE_SIGNAL_ID = -3   # 负号哨兵，同 -1/-2 约定（NULL 不触发 UNIQUE）
CHANNEL_SILENCE_DAYS = int(os.getenv("CATALYST_SILENCE_DAYS", "3"))


def _channel_silence_snapshot(conn) -> dict:
    """通道健康度快照（只读，纯查询，不发信）。"""
    return conn.execute("""
        SELECT
            (SELECT MAX(created_at) FROM biz.catalyst_signal
              WHERE tier = 'A' AND status = 'open')                         AS last_a_open_at,
            (SELECT COUNT(*) FROM biz.catalyst_signal
              WHERE tier = 'A' AND status = 'open')                         AS a_open_total,
            (SELECT COUNT(*) FROM biz.catalyst_signal
              WHERE tier = 'A' AND created_at > NOW() - INTERVAL '7 days')  AS a_new_7d,
            (SELECT COUNT(*) FROM biz.asset_catalyst
              WHERE COALESCE(published_at, created_at)
                    > NOW() - INTERVAL '24 hours')                          AS upstream_24h,
            (SELECT COUNT(*) FROM biz.asset_catalyst
              WHERE COALESCE(published_at, created_at)
                    > NOW() - INTERVAL '7 days')                            AS upstream_7d,
            (SELECT MAX(sent_at) FROM biz.catalyst_notification_log
              WHERE notification_type = %s AND status = 'sent')             AS last_major_event_at,
            (SELECT MAX(sent_at) FROM biz.catalyst_notification_log
              WHERE notification_type = %s AND status = 'sent')             AS last_fast_alert_at
    """, (NTYPE_MAJOR_EVENT, NTYPE_FAST_ALERT)).fetchone()


def _build_channel_silence_html(snap: dict, threshold: int, days_silent) -> str:
    """构建通道空转告警邮件 HTML（运维视角，非投资建议）。"""
    days_txt = f"{days_silent:.1f} 天" if days_silent is not None else "—（从未产生候选）"
    rows = [
        ("最后一条 A 级候选（tier=A 且 status=open）", _fmt_ts(snap.get("last_a_open_at"))),
        ("距今", days_txt),
        ("告警阈值", f"{threshold} 天"),
        ("近 7 天新增 A 级信号（任意状态）", f"{snap.get('a_new_7d', 0)} 条"),
        ("当前 A+open 存量", f"{snap.get('a_open_total', 0)} 条"
                          "（均超出 24h 窗口，不会触发发送）"),
        ("上游催化剂入库 · 近 24h / 近 7 天",
         f"{snap.get('upstream_24h', 0)} / {snap.get('upstream_7d', 0)} 条"),
    ]
    cross = [
        ("最后一条 📢 重大事件通报", _fmt_ts(snap.get("last_major_event_at"))),
        ("最后一条 🚀 A 级快讯", _fmt_ts(snap.get("last_fast_alert_at"))),
    ]
    tr = "".join(
        f"<tr><td style='padding:6px 10px;color:#6b7280;font-size:12px'>{k}</td>"
        f"<td style='padding:6px 10px;font-size:13px;color:#111827'>{v}</td></tr>"
        for k, v in rows
    )
    tr_cross = "".join(
        f"<tr><td style='padding:6px 10px;color:#6b7280;font-size:12px'>{k}</td>"
        f"<td style='padding:6px 10px;font-size:13px;color:#111827'>{v}</td></tr>"
        for k, v in cross
    )
    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:720px;margin:0 auto;padding:20px">
      <div style="background:#fffbeb;border-left:4px solid #f59e0b;
                  padding:14px 16px;border-radius:8px">
        <div style="font-size:16px;font-weight:600;color:#92400e">
          ⚠️ 催化剂 A 级 Alert 通道已空转
        </div>
        <div style="font-size:13px;color:#78350f;margin-top:6px">
          「🚀 A级催化剂信号」与「🎯 催化剂 Alert·A级」两封邮件都要求
          <b>tier=A 且 status=open</b>，该口径已连续 {days_txt} 没有新候选，
          因此这两封邮件都不会发出。<b>这不是发送故障</b>——请勿按 SMTP/调度问题排查。
        </div>
      </div>

      <div style="margin-top:16px;font-size:13px;font-weight:600;color:#111827">
        候选供给现状
      </div>
      <table style="width:100%;border-collapse:collapse;margin-top:6px;
                    background:#f9fafb;border-radius:8px">{tr}</table>

      <div style="margin-top:16px;font-size:13px;font-weight:600;color:#111827">
        交叉判定（用于区分「候选为 0」与「管道已死」）
      </div>
      <table style="width:100%;border-collapse:collapse;margin-top:6px;
                    background:#f9fafb;border-radius:8px">{tr_cross}</table>
      <div style="font-size:12px;color:#6b7280;margin-top:6px">
        若「重大事件通报」仍在近期发送，说明摄入、分级、发信链路均正常，
        问题只在 A 级候选供给。
      </div>

      <div style="margin-top:16px;font-size:13px;font-weight:600;color:#111827">
        常见原因（按历史发生频次）
      </div>
      <ol style="font-size:12.5px;color:#374151;line-height:1.7;margin:6px 0 0 0;
                 padding-left:20px">
        <li><b>上游供给下降</b>：`biz.asset_catalyst` 日入库量塌陷 ⇒ 分母变小。
            核对上面的「上游催化剂入库」两列与历史同期。</li>
        <li><b>高分信号全被判为「已定价」</b>：`resonance_state='confirmed'` 会按设计
            降级为 `status='watch'`（依据：confirmed 72h 前瞻超额 -2.16%，n=47），
            被 `status='open'` 排除。核对 `a_new_7d` 是否 &gt; 0 —— 若 &gt; 0 但候选仍为 0，
            基本就是这一条。</li>
      </ol>

      <div style="margin-top:16px;font-size:13px;font-weight:600;color:#111827">
        排查 SQL（可直接复制）
      </div>
      <pre style="background:#111827;color:#e5e7eb;padding:12px;border-radius:8px;
                  font-size:11.5px;overflow-x:auto;line-height:1.6">
-- 1) 高分信号为何没进 open
SELECT signal_id, asset_id, tier, status, composite_score, resonance_state, created_at
FROM biz.catalyst_signal
WHERE created_at &gt; NOW() - INTERVAL '7 days' AND composite_score &gt;= 80
ORDER BY created_at DESC;

-- 2) 上游供给趋势（按发布日）
SELECT COALESCE(published_at, created_at)::date AS d, COUNT(*)
FROM biz.asset_catalyst
WHERE COALESCE(published_at, created_at) &gt; NOW() - INTERVAL '14 days'
GROUP BY 1 ORDER BY 1;</pre>

      <div style="margin-top:18px;font-size:11px;color:#9ca3af;text-align:center;
                  line-height:1.6">
        本邮件为<b>运维告警</b>（notification_type=channel_silence），24h 内至多一封。<br>
        按 2026-09-16 <code>058c246</code> 的决议，空窗期<b>不</b>向用户发送「今日无信号」邮件。<br>
        阈值可通过环境变量 <code>CATALYST_SILENCE_DAYS</code> 调整（当前 {threshold} 天）。
      </div>
    </div>
    """


def send_channel_silence_alert(conn, days: int | None = None) -> dict:
    """A 级 Alert 通道长期无新候选时，发一封运维告警邮件。

    与 `send_slow_digest` 完全分开：独立 `notification_type`（`channel_silence`）
    ⇒ 独立去重，不影响 A 级 Alert 自身的 24h 去重位。

    Args:
        conn: 数据库连接
        days: 空转阈值（天），缺省取 `CHANNEL_SILENCE_DAYS`
              （env `CATALYST_SILENCE_DAYS`，默认 3）

    Returns:
        dict: {sent, skipped, failed, reason, days_silent, last_a_open_at}
    """
    try:
        ensure_notification_table(conn)
    except Exception as e:
        logger.warning("确保通知表存在失败: %s", e)

    threshold = CHANNEL_SILENCE_DAYS if days is None else int(days)
    try:
        snap = _channel_silence_snapshot(conn)
    except Exception as e:
        logger.warning("通道静默快照查询失败: %s", e, exc_info=True)
        return {"sent": 0, "skipped": 0, "failed": 0,
                "reason": f"快照查询失败: {e}",
                "days_silent": None, "last_a_open_at": None}

    last_at = snap.get("last_a_open_at")
    days_silent = None
    if last_at is not None:
        days_silent = (datetime.now(timezone.utc) - last_at).total_seconds() / 86400.0
    base = {"days_silent": None if days_silent is None else round(days_silent, 2),
            "last_a_open_at": last_at}

    # 通道正常 → 既不发信，也不占用去重位（避免把正常期的窗口浪费掉）
    if days_silent is not None and days_silent < threshold:
        return {**base, "sent": 0, "skipped": 1, "failed": 0,
                "reason": f"通道正常：最近候选 {days_silent:.1f} 天前（阈值 {threshold} 天）"}

    subject = (f"⚠️ 催化剂 A 级 Alert 通道已空转 {int(days_silent)} 天（无新候选）"
               if days_silent is not None
               else "⚠️ 催化剂 A 级 Alert 通道从未产生过候选")
    if not _try_acquire_send_lock(conn, SENTINEL_CHANNEL_SILENCE_SIGNAL_ID,
                                  NTYPE_CHANNEL_SILENCE, None, subject):
        return {**base, "sent": 0, "skipped": 1, "failed": 0,
                "reason": "24h 内已告警过，跳过"}

    try:
        body = _build_channel_silence_html(snap, threshold, days_silent)
    except Exception as e:
        logger.warning("通道静默告警渲染失败: %s", e, exc_info=True)
        return {**base, "sent": 0, "skipped": 0, "failed": 1,
                "reason": f"渲染失败: {e}"}

    ok, msg = _send_email(subject, body, to=ops_recipients())
    _mark_sent(conn, SENTINEL_CHANNEL_SILENCE_SIGNAL_ID, NTYPE_CHANNEL_SILENCE,
               None, subject, status="sent" if ok else "failed",
               error_msg=None if ok else msg)
    return {**base, "sent": 1 if ok else 0, "skipped": 0,
            "failed": 0 if ok else 1, "reason": msg}


# =====================================================================
# 重大事件通道（重要性闸门，独立于 tier 的可交易性闸门）
# =====================================================================
#
# 背景：
# · 2026-09-23 建通道：A 级 Alert 口径是 `tier='A' AND status='open'`，而 tier 同时
#   承担「重要性」与「可交易性」——价格档位不齐/RR 不足/方向不符都会封顶到 C，
#   于是「CME 将上线 BCH 与 UNI 期货」这类重大利好因给不出档位而静默。
# · 2026-09-29 用户质疑「重大事件邮件是否也漏了大事」后**重建**：旧口径
#   `s.tier IN ('A','B') AND s.status='open'` 把可交易性当重要性用，叠加 4 处结构性
#   缺陷，实测历史仅 8 封（09-23~09-28），类型全为 partnership/funding/listing，
#   security/regulation/etf/macro 类 **0 封**：
#   ① tier 语义错位：tier 由档位/RR/方向闸门决定（event_type_weights 里 listing=95），
#      与「事件是否重大」无关；
#   ② 排序口径错：inner 以 composite_score 截断（DISTINCT ON 后 A 级高分占满名额）；
#   ③ market_update 过滤可绕过：只精确匹配 ai_event_type，误分类即放行——实测
#      Rhea「24H 涨幅 131%」判 partnership、Bitwise「NEAR 现货 ETF 递表」判 funding
#      入池，且被渲染成「融资到账 → 基本面改善」（解释错误）；
#   ④ prelaunch_ret_24h >= 5 单向性：要求事件**发生前已涨**≥5%，结构性灭杀利空型
#      重大事件（有 asset_id 的 security 类 71 条仅 4 条通过、macro 13→0）；
#   ⑤ 权重表缺 security 键 + structural_event_types 不含 security：hack 类 event_weight
#      兜底 15 分，且被 kind 白名单挡在门外。
# 现口径（与周报同源，口径 = **市场显著性**，非可交易性）：
#   · 权重复用周报 _WEEKLY_TYPE_WEIGHT（security95/macro88/etf85/regulation80/…），
#     不再读 grade 的 event_weight；
#   · 分类先做**标题关键词兜底**（security/etf/macro）+ 行情播报识别（「涨幅…%」/
#     「价格突破/暴涨」→ market_update 并剔除），再回落 AI/规则类型；
#   · importance = 类型权重 × 资产权重（market_cap_rank 分档，NULL→0.5）+ confirmed 加成；
#   · 双向门槛：利空型（security/delisting 或 ai_sentiment=bearish）由类型权重直达，
#     不以涨幅为准；利好型仍要求事件前 24h |异动| ≥5% 且未被计入降权（市场已确认）；
#   · 仍要求 asset_id（无标的的宏观/监管事件只进周报、不进逐条告警）。
#
# 邮件刻意不出现任何交易档位，并显式标注「非交易建议」，避免被读成开单指令。

NTYPE_MAJOR_EVENT = "major_event"     # 重大事件通道（与 A 级 Alert 分开渲染/去重）

MAJOR_EVENT_MIN_MOVE = 5.0            # 利好型门槛：事件前 24h |异动| ≥5%（市场已确认）
# 审计 2026-10-02 P0-3：通知停发根因之一是 major_event 门槛过高 —— importance=类型权重×资产权重
# ≥70 时，只有 security/macro/etf 类且市值 top10 的资产才可能过线（近 7 天每天过线候选 2~33 条，
# 其中 BTC 占大半；10-02 仅 2 条全被 24h 去重拦截 → 通道 0 封）。放宽到 55：让
#   · regulation(80)×市值≤30(0.8)=64 ✓、tech_upgrade(68)×市值≤10(1.0)=68 ✓
#   · funding(55)×市值≤10(1.0)=55 ✓、listing(62)×市值≤30(0.8)=49.6 → 仍不够（listing 数量大
#     保持降权，避免刷屏）
# 同时去重窗口 24h→12h、单轮上限 3→5，让通道在候选稀疏时仍能产出。
MAJOR_EVENT_MIN_IMPORTANCE = 55.0     # 市场显著性门槛（类型权重 × 资产权重）
MAJOR_EVENT_COOLDOWN_HOURS = 12       # 同一资产 12h 内只发一次（事件级去重）
MAJOR_EVENT_MAX_PER_RUN = 5           # 单轮上限，配合「日均 ≤5 条」目标
_MAJOR_BEARISH_TYPES = ("security", "delisting")  # 利空型：不以涨幅确认，权重直达


def _recent_major_events(conn, hours: int = 24,
                         limit: int = MAJOR_EVENT_MAX_PER_RUN) -> list[dict]:
    """过去 N 小时发布的「重大事件」候选（每资产留重要性最高一条）。

    入选条件（缺一不可）：
    - `importance >= MAJOR_EVENT_MIN_IMPORTANCE`：类型权重（**市场显著性**）× 资产权重
    - `is_bearish OR (|prelaunch_ret_24h| >= MAJOR_EVENT_MIN_MOVE
      AND prelaunch_penalty = 0)`：利空型（security/delisting/bearish）由事件类型权重
      直达、不以涨幅为准；利好型要求事件前市场已确认（**双向绝对值**，不限涨跌）
    - `category <> 'market_update'`：剔除行情播报（负 alpha）
    - `catalyst_kind <> 'noise'`：剔除噪音
    - 标题非占位（'null' 等）且非「要闻汇总/日报」聚合帖
    - 事件发布时间在 N 小时内：只通报新鲜事件，避免停摆后补发陈旧事件
    - 同一资产 N 小时内已发过 major_event 则跳过：一条新闻常被多家媒体重复采集
      （实测 BCH 那条来自 4 家媒体、5 条 catalyst），事件级去重后只发一封
    - 额外 LEFT JOIN LATERAL 取 `biz.catalyst_second_order` 的二阶标的（只消费、不生成），
      供邮件「板块联动」模块渲染

    口径说明：`category` 先按**标题**关键词兜底归类 security/etf/macro（与周报同源，
    不看 ai_summary 以免无关事件蹭词），并识别行情播报；`importance` 的权重表直接复用
    周报 `_WEEKLY_TYPE_WEIGHT`，避免两处权重漂移。
    """
    # 类型权重表 → SQL CASE（复用周报权重，避免第二份口径）
    weight_case = "\n                    ".join(
        f"WHEN '{k}' THEN {int(v)}" for k, v in _WEEKLY_TYPE_WEIGHT.items()
    )
    return conn.execute(f"""
        WITH base AS (
            SELECT s.signal_id, s.catalyst_id, s.asset_id,
                   s.tier, s.composite_score, s.resonance_state, s.resonance_score,
                   s.kind, s.created_at,
                   a.canonical_name, a.canonical_symbol AS symbol,
                   a.primary_sector, a.market_cap AS asset_market_cap, a.market_cap_rank,
                   ac.title AS catalyst_title, ac.title_cn,
                   ac.ai_summary, ac.ai_event_type, ac.ai_sentiment,
                   ac.rule_event_type, ac.source_code, ac.source_url,
                   ac.published_at, ac.body_text AS catalyst_body,
                   cg.authority_score, cg.event_weight, cg.scope_score,
                   cg.prelaunch_ret_24h, cg.prelaunch_penalty,
                   cg.catalyst_kind, cg.tradable,
                   ci.impact_direction, ci.impact_strength,
                   md.price_usd AS current_price, md.change_24h, md.change_7d,
                   md.volume_24h,
                   so.second_order_symbols, so.second_order_sector,
                   so.second_order_confidence, so.second_order_count,
                   LOWER(COALESCE(ac.title_cn, '') || ' ' || COALESCE(ac.title, '')) AS head,
                   COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS raw_event_type
            FROM biz.catalyst_signal s
            JOIN core.asset a ON s.asset_id = a.asset_id
            JOIN biz.asset_catalyst ac
              ON ac.catalyst_id = s.catalyst_id AND ac.asset_id = s.asset_id
            JOIN biz.catalyst_grade cg ON cg.catalyst_id = s.catalyst_id
            LEFT JOIN biz.catalyst_impact ci
              ON ci.catalyst_id = s.catalyst_id AND ci.asset_id = s.asset_id
            LEFT JOIN LATERAL (
                SELECT m.price_usd, m.change_24h, m.change_7d, m.volume_24h
                FROM biz.asset_market_daily m
                WHERE m.asset_id = s.asset_id
                ORDER BY m.market_date DESC
                LIMIT 1
            ) md ON TRUE
            LEFT JOIN LATERAL (
                -- 二阶/板块传导：同 catalyst 下、本资产之外的其他受益标的（只消费已有数据）
                SELECT
                    array_agg(DISTINCT COALESCE(a2.canonical_symbol, a2.canonical_name))
                        FILTER (WHERE COALESCE(a2.canonical_symbol, a2.canonical_name) IS NOT NULL)
                        AS second_order_symbols,
                    MAX(cso.sector_name) AS second_order_sector,
                    MAX(cso.confidence)  AS second_order_confidence,
                    COUNT(*)             AS second_order_count
                FROM biz.catalyst_second_order cso
                JOIN core.asset a2 ON a2.asset_id = cso.asset_id
                WHERE cso.catalyst_id = s.catalyst_id
                  AND cso.asset_id <> s.asset_id
            ) so ON TRUE
            WHERE ac.published_at > NOW() - (%s::int * INTERVAL '1 hour')
              AND COALESCE(cg.catalyst_kind, '') <> 'noise'
              AND {ASSET_NAME_FILTER_SQL}
              -- 剔除占位标题（LLM 漏译写成的字面量 'null'）与「要闻汇总/日报」类
              -- 无单一事件的聚合帖：其标题不含事件，只会污染关键词归类
              AND LOWER(COALESCE(ac.title_cn, ac.title, ''))
                  NOT IN ('', 'null', 'none', 'nan', 'tl;dr')
              AND LOWER(COALESCE(ac.title_cn, ac.title, ''))
                  !~ '^(今日要闻|要闻预告|要闻提示|一、|二、|热点新闻|行情|盘前|盘后|每日|快讯汇总|市场综述)'
        ),
        categorized AS (
            SELECT *,
                CASE
                    -- 关键词只匹配**标题**（ai_summary 过长，会在无关事件里蹭到关键词）
                    WHEN head ~ 'hack|exploit|stolen|steal|breach|drain|被盗|被黑|遭攻击|漏洞|rug ?pull'
                        THEN 'security'
                    WHEN head ~ 'etf' THEN 'etf'
                    WHEN head ~ '美联储|federal reserve|rate hike|rate cut|加息|降息|基点|basis point|通胀|非农'
                        THEN 'macro'
                    -- 审计 2026-09-29 P1-2：上游 AI/规则把「基金会推出某安全/生态计划」误标
                    -- regulation（Arbitrum Security Program 被当监管事件，传导路径错配成
                    -- 「监管口径变化」）。**仅在标题无任何监管线索时**降级：有合作/推出线索 →
                    -- partnership，否则 → other。先判「无监管线索」再判合作线索，避免
                    -- 「SEC 推出新规」这类真监管被误降级。监管线索刻意不含裸 sec/ban
                    -- （会误命中 security/arbitrum 等）。
                    WHEN raw_event_type = 'regulation'
                         AND head !~ '监管|合规|法案|立法|条例|诉讼|起诉|禁止|禁令|制裁|罚款|证券|法院|国会|听证|cftc|lawsuit|regulation|regulatory|compliance|sanction|enforcement|court|congress|证监会|反垄断|antitrust|sec[[:space:]]'
                         AND head ~ '基金会|foundation|合作|伙伴|partnership|联合|推出|上线|launch|program|计划|倡议|initiative'
                        THEN 'partnership'
                    WHEN raw_event_type = 'regulation'
                         AND head !~ '监管|合规|法案|立法|条例|诉讼|起诉|禁止|禁令|制裁|罚款|证券|法院|国会|听证|cftc|lawsuit|regulation|regulatory|compliance|sanction|enforcement|court|congress|证监会|反垄断|antitrust|sec[[:space:]]'
                        THEN 'other'
                    -- 行情播报识别（修复旧口径漏网：AI 误分类即放行）：
                    -- 实测 Rhea「24H 涨幅 131%%」被判 partnership 入池，属负 alpha
                    WHEN (head ~ '涨幅' AND head ~ '%%') OR head ~ '价格突破|暴涨'
                        THEN 'market_update'
                    ELSE raw_event_type
                END AS category
            FROM base
        ),
        scored AS (
            SELECT *,
                CASE category
                    {weight_case}
                    ELSE 32
                END::numeric AS type_w,
                (CASE
                    WHEN market_cap_rank IS NULL THEN 0.5
                    WHEN market_cap_rank <= 10 THEN 1.00
                    WHEN market_cap_rank <= 30 THEN 0.80
                    WHEN market_cap_rank <= 100 THEN 0.62
                    WHEN market_cap_rank <= 500 THEN 0.45
                    ELSE 0.30
                END)::numeric AS asset_w
            FROM categorized
        ),
        gated AS (
            SELECT *,
                (category IN ('security', 'delisting')
                 OR LOWER(COALESCE(ai_sentiment, '')) = 'bearish') AS is_bearish,
                ROUND(type_w * asset_w
                      + CASE WHEN resonance_state = 'confirmed' THEN 6 ELSE 0 END, 1)
                    AS importance
            FROM scored
            WHERE category <> 'market_update'
        )
        SELECT * FROM (
            SELECT DISTINCT ON (asset_id)
                   signal_id, catalyst_id, asset_id,
                   tier, composite_score, resonance_state, resonance_score,
                   kind, created_at, canonical_name, symbol, primary_sector,
                   asset_market_cap, market_cap_rank,
                   catalyst_title, title_cn, ai_summary, ai_event_type, ai_sentiment,
                   rule_event_type, source_code, source_url, published_at, catalyst_body,
                   authority_score, event_weight, scope_score,
                   prelaunch_ret_24h, prelaunch_penalty, catalyst_kind, tradable,
                   impact_direction, impact_strength,
                   current_price, change_24h, change_7d, volume_24h,
                   second_order_symbols, second_order_sector,
                   second_order_confidence, second_order_count,
                   category AS event_type_norm, is_bearish, importance
            FROM gated
            WHERE importance >= %s
              AND (is_bearish
                   OR (ABS(COALESCE(prelaunch_ret_24h, 0)) >= %s
                       AND COALESCE(prelaunch_penalty, 0) = 0))
              AND NOT EXISTS (
                  SELECT 1
                  FROM biz.catalyst_notification_log nl
                  JOIN biz.catalyst_signal ns ON ns.signal_id = nl.signal_id
                  WHERE ns.asset_id = gated.asset_id
                    AND nl.notification_type = %s
                    AND nl.status = 'sent'
                    AND nl.sent_at > NOW() - (%s::int * INTERVAL '1 hour')
              )
            ORDER BY asset_id, importance DESC, composite_score DESC NULLS LAST
        ) t
        ORDER BY t.importance DESC, t.composite_score DESC NULLS LAST
        LIMIT %s
    """, (hours, MAJOR_EVENT_MIN_IMPORTANCE, MAJOR_EVENT_MIN_MOVE,
          NTYPE_MAJOR_EVENT, MAJOR_EVENT_COOLDOWN_HOURS, limit)).fetchall()


# ---- 重大事件「传导逻辑」渲染（输出层优化：只解释机制，不给买卖建议）----
# 说明：本组只做「复用已有字段 + 规则映射」，不新增上游字段、不调用 LLM。

# 事件类型 → 传导路径（受影响主体 → 行为变化 → 代币层面结果）
_TRANSMISSION_PATH_CN = {
    "partnership": "合作方背书/资源注入 → 采用与关注度提升 → 叙事强化 + 需求预期",
    "listing": "上线/纳入交易 → 可及性与曝光提升 → 流动性 + 需求提升",
    "regulation": "监管口径变化 → 合规预期重估 → 赛道资金再配置",
    "tech_upgrade": "技术升级落地 → 可用性与效率提升 → 链上活性 + 使用需求提升",
    "funding": "融资到账 → 开发与运营投入提升 → 基本面预期改善",
    "airdrop": "激励发放 → 用户与资金流入 → 短期活跃度提升",
    "burn": "供给销毁 → 流通量收缩 → 稀缺性预期提升",
    "adoption": "机构/协议采用 → 真实使用与锁仓增加 → 需求提升",
    "hack": "安全事件 → 信任受损 → 抛压与资金流出",
    "security": "安全事件 → 信任受损与资产风险 → 抛压 + 风控重估（利空传导）",
    "etf": "ETF 递表/审批/资金流 → 机构通道打开 → 增量需求预期",
    "delisting": "下线/移除 → 可及性下降 → 流动性与需求下降",
    "macro": "宏观变量 → 风险偏好变化 → 板块资金流向变化",
}
_DEFAULT_TRANSMISSION_PATH = "事件触发 → 相关主体行为变化 → 代币需求/叙事/链上活性 → 价格表达"

# 持续性 → 传导节奏（即时 / 短期 / 中期），给读者节奏感
_TRANSMISSION_TIMELINE = {
    "structural": (
        "即时：叙事与情绪引爆，关注度骤升",
        "短期：资金流入 / 采用开始落地",
        "中期：基本面数据兑现并接受验证",
    ),
    "event": (
        "即时：事件驱动的情绪反应",
        "短期：资金与关注度变化是否延续",
        "中期：能否沉淀为持续基本面",
    ),
}

# 「自身受益」动作关键词：命中说明事件直接作用于该代币本身（被买/被锁/被纳入/被采用/上线）
# 注：刻意不含「支持」——「某协议支持 X 链」多指承载关系，方向易误判（复验 P2-2）
_DIRECT_ACTION_KEYWORDS = (
    "购入", "买入", "增持", "纳入", "回购", "销毁", "质押", "锁仓", "托管",
    "合作", "推出", "上线", "采用", "接入", "集成",
)
# 英文动作词（审计 2026-09-29 P1-3）：ai_summary 常为英文，中文词表恒不命中，
# 导致一律落入「生态间接受益」。补英文后与中文同判。
_DIRECT_ACTION_KEYWORDS_EN = (
    "buy", "purchase", "acquire", "acquired", "add", "added to", "treasury",
    "stake", "staked", "lock", "locked", "custody", "burn", "burned",
    "integrate", "integrates", "integrated", "adopt", "adopts", "adopted",
    "launch", "launches", "launched", "list", "listed", "listing",
    "partnership", "partnered", "invest", "invested", "backed",
)
# 媒体/出版方名（审计 2026-09-29 P1-1）：邮件「来源」此前打印发现渠道 id
# （kol_news_media_binance_square_9），非真实媒体。标题/正文常带出版方名，抽出来展示。
_MEDIA_NAMES = (
    "ChainCatcher", "Foresight News", "BlockBeats", "PANews", "Odaily",
    "The Block", "CoinDesk", "Cointelegraph", "Decrypt", "CryptoSlate",
    "CryptoBriefing", "Protos", "Cryptonomist", "NS3.AI", "ZDNet", "Decrypt",
    "DL News", "Bloomberg", "Reuters", "CoinGape", "U.Today", "BeInCrypto",
    "火星财经", "金色财经", "巴比特", "律动", "深潮", "Foresight", "动察",
    "星球日报", "链捕手", "PANews", "ForesightNews", "CoinVoice", "Blocklike",
)
_MEDIA_RE = re.compile(
    "|".join(re.escape(m) for m in sorted(set(_MEDIA_NAMES), key=len, reverse=True)),
    re.IGNORECASE)


def _extract_publisher(*texts: str) -> str | None:
    """从标题/正文首段抽真实媒体/出版方名（找不到返回 None，绝不臆造）。"""
    for text in texts:
        if not text:
            continue
        m = _MEDIA_RE.search(text[:200])
        if m:
            return m.group(0)
    return None


def _display_source(r: dict) -> str:
    """邮件「来源」展示真实出版方；抽不到再回落发现渠道 id（source_code）。"""
    pub = _extract_publisher(r.get("catalyst_title"), r.get("title_cn"),
                             r.get("catalyst_body"))
    return pub or (r.get("source_code") or "—")

# 承载关系线索：代币紧邻这些词时多为「底层链/网络」角色（生态间接），非自身受益
_CARRIER_CUES = ("链", "网络", "主网", "公链", "生态", "链上")
# 匹配 token 后紧邻位置前需跳过的空白/标点（用于判定「SUI 链」这类承载后缀）
_CARRIER_TRIM = re.compile(r"^[\s，,。.、:：;；/\\|()\[\]（）【】\"'“”‘’\-—]+")


def _mentions_token(text: str, token: str) -> tuple[bool, bool]:
    """判断 text 是否点名 token，返回 (是否点名, 是否仅作承载角色)。

    - ASCII 词用词边界匹配，避免 `ETH ⊂ ETHEREUM`、`GPT ⊂ CGPT` 误命中（复验 P2-1）；
      CJK 名称用子串匹配。
    - 大小写不敏感（复验 P3）。
    - 紧邻后接「链/网络/主网/公链/生态/链上」→ 视为承载角色（复验 P2-2）。
    """
    if not token:
        return False, False
    tl, tok = (text or "").lower(), token.lower()
    if re.fullmatch(r"[a-z0-9$_.]+", tok):
        pattern = r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])"
    else:
        pattern = re.escape(tok)
    spans = list(re.finditer(pattern, tl))
    if not spans:
        return False, False
    for m in spans:
        tail = _CARRIER_TRIM.sub("", tl[m.end(): m.end() + 6])
        if not any(tail.startswith(c) for c in _CARRIER_CUES):
            return True, False      # 存在非承载角色的点名 → 自身受益
    return True, True               # 仅以「X 链/生态」形式出现 → 承载角色


def _transmission_directness(r: dict) -> tuple[str, str, str]:
    """规则映射「传导直接度」（不新增上游字段）。

    直接利好标的：标题/摘要以自身角色点名该币，且事件属「被买/被锁/被采用/被纳入/合作」；
    生态间接受益：事件作用在底层链/赛道/协议而非该币自身（含「X 链」承载角色）。
    **利空型（审计 2026-09-29 P1-3）**：被点名 → 「直接受损标的」（直接利空传导）；
    仅承载 → 「生态间接承压」——不得再写成「受益」，否则与利空型结论自相矛盾。

    Returns:
        (level, label, confidence_cn)，level ∈ {'direct','indirect','harm','harm_indirect'}
    """
    text = " ".join(
        str(x) for x in (
            r.get("catalyst_title"), r.get("title_cn"), r.get("ai_summary"),
        ) if x
    )
    text_l = text.lower()
    sym_hit, sym_carrier = _mentions_token(text, (r.get("symbol") or "").strip())
    name_hit, name_carrier = _mentions_token(text, (r.get("canonical_name") or "").strip())
    self_mentioned = (sym_hit and not sym_carrier) or (name_hit and not name_carrier)
    has_action = (any(k in text for k in _DIRECT_ACTION_KEYWORDS)
                  or any(k in text_l for k in _DIRECT_ACTION_KEYWORDS_EN))
    is_bearish = bool(r.get("is_bearish"))
    if is_bearish:
        if self_mentioned:
            return "harm", "直接受损标的（利空传导）", "高"
        return "harm_indirect", "生态间接承压（利空传导）", "中"
    if self_mentioned and has_action:
        return "direct", "直接利好标的", "高"
    return "indirect", "生态间接受益", "中"


def _transmission_path(r: dict) -> str:
    # 优先用归一化事件类别（含关键词兜底的 security/etf/macro），再回落 AI/规则类型
    for key in (r.get("event_type_norm"), r.get("ai_event_type"),
                r.get("rule_event_type"), r.get("catalyst_kind")):
        k = (key or "").strip().lower()
        if k in _TRANSMISSION_PATH_CN:
            return _TRANSMISSION_PATH_CN[k]
    return _DEFAULT_TRANSMISSION_PATH


def _transmission_timeline(r: dict) -> tuple[str, str, str]:
    k = (r.get("catalyst_kind") or "").strip().lower()
    return _TRANSMISSION_TIMELINE.get(k, _TRANSMISSION_TIMELINE["event"])


def _second_order_symbols(r: dict, limit: int = 5) -> list[str]:
    """取出二阶/板块传导标的（去重、截断），供「板块联动」模块渲染。"""
    syms = r.get("second_order_symbols")
    if not syms:
        return []
    if isinstance(syms, str):
        syms = [syms]
    out: list[str] = []
    for s in syms:
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


# 板块枚举 → 中文标签（审计 2026-09-29 P2-2：原始 `l1` 枚举泄漏到邮件）。
# 优先用 mapping/sector.SECTOR_LABELS 单一真源；不可导入时用等价兜底表。
try:  # pragma: no cover - 依赖导入环境
    from crypto_research.mapping.sector import SECTOR_LABELS as _SECTOR_LABELS
except Exception:  # noqa: BLE001
    _SECTOR_LABELS = {
        "l1": "L1 公链", "l2": "L2 二层", "defi": "DeFi", "launchpad": "Launchpad 打新平台",
        "meme": "Meme", "gamefi": "GameFi / NFT", "rwa": "RWA", "ai": "AI + Crypto",
        "stablecoin": "稳定币", "cex_token": "平台币", "derivatives": "衍生品",
        "depin": "DePIN", "infra": "基础设施", "other": "其他",
    }


def _sector_label(code) -> str:
    """板块枚举 → 中文标签；未知/空原样返回（不臆造）。"""
    c = str(code or "").strip()
    if not c:
        return ""
    return _SECTOR_LABELS.get(c.lower(), c)


# 稳定币/计价代币（展示价≈$1）：若其「公告前 24h 异动」显著，几乎必是归因错位
# （审计 2026-09-29 P2-1：USDC 显示 -11.89%，实为真主题币 ARB 的跌幅——展示 symbol 取
#  signal.asset_id，而 prelaunch_ret_24h 是按 catalyst 绑定，二者不同源时即错挂）。
_STABLE_SYMBOLS = frozenset({
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "USDE", "USDD",
    "PYUSD", "FRAX", "GUSD", "USDS", "USD1", "EURT", "EURC", "USDY", "USD0",
})


def _prelaunch_attribution_warning(r: dict) -> str:
    """检测「展示币种」与「事件前异动」不同源的迹象，返回披露文案（无则空串，不臆断）。"""
    sym = str(r.get("symbol") or "").upper()
    pre = _to_float(r.get("prelaunch_ret_24h"))
    if pre is not None and sym in _STABLE_SYMBOLS and abs(pre) >= 2.0:
        return (f"⚠️ 展示币 {sym} 为稳定币，却出现「事件前 24h 异动 {pre:+.2f}%」，"
                f"与稳定币常识不符，多为归因/数据源错位；请以催化剂原文为准。")
    return ""


def _major_event_subject(r: dict) -> str:
    sym = r.get("symbol") or "?"
    title = _full_title(r)
    if len(title) > 60:
        title = title[:60] + "…"
    return f"📢 [重大事件] {sym} - {title}"


def _build_major_event_html(r: dict) -> str:
    """构建单条「重大事件」通报邮件（自包含，刻意不含交易档位）。

    板块顺序（传导逻辑可读性优化，仅输出层）：
      ① 影响传导（传导路径 + 事件要点 + 传导直接度规则映射）
      ② 预期已消化（prelaunch_ret_24h 重解为「公告前是否已被提前消化」）
      ③ 传导节奏（即时/短期/中期）
      ④ 系统评分表（仅供参考）
      ⑤ 板块联动（二阶传导数据，有则显示）
    """
    sym = r.get("symbol") or "—"
    name = r.get("canonical_name") or ""
    title = _full_title(r)
    body = (r.get("catalyst_body") or "").strip()
    summary = (r.get("ai_summary") or "").strip()

    direction = _DIRECTION_CN.get(r.get("ai_sentiment"), r.get("ai_sentiment") or "—")
    impact = _DIRECTION_CN.get(r.get("impact_direction"), r.get("impact_direction") or "—")
    strength = _IMPACT_STRENGTH_CN.get(r.get("impact_strength"),
                                       r.get("impact_strength") or "—")
    res = _RESONANCE_CN.get(r.get("resonance_state"), r.get("resonance_state") or "—")
    kind = _KIND_CN.get(r.get("catalyst_kind"), "—")

    pre = _to_float(r.get("prelaunch_ret_24h"))
    pre_txt = _pct(pre)
    chg24 = _to_float(r.get("change_24h"))
    price_txt = _fmt_price(r.get("current_price"))

    def _kv(label, value, color="#111827"):
        return (f'<tr>'
                f'<td style="padding:6px 10px;color:#6b7280;font-size:13px;'
                f'white-space:nowrap;vertical-align:top">{label}</td>'
                f'<td style="padding:6px 10px;color:{color};font-size:13px;'
                f'font-weight:600">{value}</td>'
                f'</tr>')

    score_line = (
        f"权威 {r.get('authority_score', '—')} · 事件权重 {r.get('event_weight', '—')}"
        f" · 影响范围 {r.get('scope_score', '—')}"
    )
    src = r.get("source_url") or ""
    src_line = (f'<a href="{src}" style="color:#2563eb">原文链接</a>' if src else "—")

    # ---- 传导逻辑（输出层规则映射：只解释机制，不构成买卖建议）----
    _dir_level, dir_label, dir_conf = _transmission_directness(r)
    dir_color = {"direct": "#b45309", "harm": "#dc2626",
                 "harm_indirect": "#b45309"}.get(_dir_level, "#4b5563")
    path_txt = _transmission_path(r)
    tl_immediate, tl_short, tl_mid = _transmission_timeline(r)
    res_note = _RESONANCE_NOTE.get(r.get("resonance_state"), "")

    is_bearish = bool(r.get("is_bearish"))
    if is_bearish:
        # 利空型（安全/下架类）不以「事件前已涨」作确认——旧文案会把利空事件写反
        consume_note = (
            f'本条属利空型重大事件，不以事件前涨幅作为确认条件，改由事件类型权重直达；'
            f'{res_note}。' if res_note else
            '本条属利空型重大事件，不以事件前涨幅作为确认条件，改由事件类型权重直达。'
        )
    elif pre is not None:
        consume_note = (
            f'公告前 24h 已涨 {pre_txt} —— 说明部分预期已被市场提前消化，非“零成本”；'
            f'{res_note}。' if res_note else
            f'公告前 24h 已涨 {pre_txt} —— 说明部分预期已被市场提前消化，非“零成本”。'
        )
    else:
        consume_note = "公告前 24h 无异动数据，预期消化度暂无法判定。"
    _attr_warn = _prelaunch_attribution_warning(r)
    if _attr_warn:
        consume_note = f"{consume_note}<br>{_attr_warn}"

    summary_line = (
        f'<div style="font-size:13px;line-height:1.7;color:#374151;margin-top:4px">'
        f'<span style="color:#6b7280">事件要点：</span>{summary}</div>' if summary else ""
    )
    transmit_block = f'''<div style="background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:14px">
    <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:6px">影响传导</div>
    <div style="font-size:13px;line-height:1.7;color:#374151">
      <span style="color:#6b7280">传导路径：</span>{path_txt}
    </div>
    {summary_line}
    <div style="font-size:13px;line-height:1.7;color:#374151;margin-top:6px">
      <span style="color:#6b7280">传导直接度：</span>
      <b style="color:{dir_color}">{dir_label}</b>（置信度 {dir_conf}）
    </div>
  </div>'''

    consume_block = f'''<div style="background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:14px">
    <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:6px">预期已消化</div>
    <div style="font-size:13px;line-height:1.7;color:#374151">{consume_note}</div>
  </div>'''

    timeline_block = f'''<div style="background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:14px">
    <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:6px">传导节奏</div>
    <div style="font-size:13px;line-height:1.7;color:#374151">
      · {tl_immediate}<br>· {tl_short}<br>· {tl_mid}
    </div>
  </div>'''

    so_syms = _second_order_symbols(r)
    so_sector_txt = _sector_label(r.get("second_order_sector"))
    so_block = f'''<div style="background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:14px">
    <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:6px">板块联动</div>
    <div style="font-size:13px;line-height:1.7;color:#374151">
      同「{so_sector_txt or "相关"}」赛道相关标的：{'、'.join(so_syms)}
      （二阶传导映射，非直接建议；若主题币归因有误，此联动亦可能失真）。
    </div>
  </div>''' if so_syms else ""

    return f"""<html><body style="margin:0;padding:0;background:#f3f4f6">
<div style="max-width:720px;margin:0 auto;padding:20px;font-family:-apple-system,
  BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif">

  <div style="background:#fff7ed;border:1px solid #fdba74;border-radius:8px;
       padding:12px 16px;margin-bottom:16px">
    <div style="font-size:15px;font-weight:700;color:#9a3412">📢 重大事件通报</div>
    <div style="font-size:12px;color:#9a3412;margin-top:4px">
      本条为「重要性」通道通报，不含交易档位，<b>非交易建议</b>；
      系统未给出可交易计划（档位/RR 未达标），请自行判断。
    </div>
  </div>

  <div style="background:#fff;border-radius:8px;padding:18px 20px;margin-bottom:14px">
    <div style="font-size:18px;font-weight:700;color:#111827">
      {sym} <span style="font-size:13px;font-weight:400;color:#6b7280">{name}</span>
    </div>
    <div style="font-size:15px;line-height:1.6;color:#111827;margin-top:10px">{title}</div>
    <div style="font-size:12px;color:#6b7280;margin-top:8px">
      发布：{_fmt_ts(r.get('published_at'))} · 来源 {_display_source(r)}
      · 事件类别 {r.get('event_type_norm') or r.get('ai_event_type')
                  or r.get('rule_event_type') or '—'}
    </div>
  </div>

  {transmit_block}

  {consume_block}

  {timeline_block}

  <div style="background:#fff;border-radius:8px;padding:6px 10px;margin-bottom:14px">
    <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:6px">
      系统评分 · 仅供参考
    </div>
    <table style="width:100%;border-collapse:collapse">
      {_kv('重要性', f'{score_line}（{kind}）')}
      {_kv('催化方向', f'{direction} · 影响强度 {strength}（{impact}）')}
      {_kv('市场确认',
           '事件类型直达（利空型不以异动确认）' if is_bearish
           else f'事件前 24h 已异动 {pre_txt}',
           '#6b7280' if is_bearish else _pct_color(pre))}
      {_kv('共振状态', res)}
      {_kv('当前价', f'{price_txt} · 24h {_pct(chg24)}', _pct_color(chg24))}
      {_kv('信号分层', f"tier {r.get('tier') or '—'} · 合成分 "
                       f"{r.get('composite_score') if r.get('composite_score') is not None else '—'}"
                       f" · 共振分 {r.get('resonance_score') if r.get('resonance_score') is not None else '—'}")}
      {_kv('原文', src_line)}
    </table>
  </div>

  {so_block}

  {f'''<div style="background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:14px">
    <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:6px">催化剂原文</div>
    <div style="font-size:13px;line-height:1.7;color:#374151;white-space:pre-wrap">{body[:2000]}</div>
  </div>''' if body else ''}

  <div style="font-size:11px;color:#9ca3af;text-align:center;padding:8px">
    由催化剂管道「重大事件」通道自动发送 · 判据与 A 级 Alert 独立
  </div>
</div></body></html>"""


def send_major_event_alerts(conn, hours: int = 24) -> dict:
    """重大事件通道：对「重要性高且市场已确认」的事件发送独立通报邮件。

    与 A 级 Alert 完全分开：独立 notification_type（`major_event`）→ 独立去重、
    独立渲染、独立邮件，互不影响。

    Returns:
        dict: {sent, skipped, failed, signals, reason}
    """
    try:
        ensure_notification_table(conn)
    except Exception as e:
        logger.warning("确保通知表存在失败: %s", e)

    try:
        rows = _recent_major_events(conn, hours=hours)
    except Exception as e:
        logger.warning("查询重大事件候选失败: %s", e, exc_info=True)
        return {"sent": 0, "skipped": 0, "failed": 0, "signals": [], "reason": str(e)}

    if not rows:
        return {"sent": 0, "skipped": 0, "failed": 0, "signals": [], "reason": None}

    sent_ids, failed_ids, skipped = [], [], 0
    for r in rows:
        sid = r["signal_id"]
        subject = _major_event_subject(r)
        # 原子占锁：同信号 24h 内只会有一个执行流拿到发送权
        if not _try_acquire_send_lock(conn, sid, NTYPE_MAJOR_EVENT,
                                      r.get("tier"), subject):
            skipped += 1
            continue
        ok, msg = _send_email(subject, _build_major_event_html(r))
        _mark_sent(conn, sid, NTYPE_MAJOR_EVENT, r.get("tier"), subject,
                   status="sent" if ok else "failed",
                   error_msg=None if ok else msg)
        if ok:
            sent_ids.append(sid)
            logger.info("重大事件通报已发送 sig=%s %s", sid, subject)
        else:
            failed_ids.append(sid)
            logger.warning("重大事件通报发送失败 sig=%s: %s", sid, msg)

    return {
        "sent": len(sent_ids),
        "skipped": skipped,
        "failed": len(failed_ids),
        "signals": sent_ids + failed_ids,
        "reason": None,
    }


# ---- 中文化映射 ----

# 趋势状态必须与「交易方向」区分（审计 P0-1：technical_state='up' 曾让做空信号
# 被渲染成「技术面 看涨」）。这里只描述均线结构，不下方向结论。
_TECH_STATE_CN = {
    "up": "上升趋势（up）",
    "range": "震荡整理（range）",
    "down": "下降趋势（down）",
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

# 催化影响方向（与「交易方向」并列展示，两者可能相反：如上升趋势中的利空做空）
_DIRECTION_CN = {
    "bullish": "利好",
    "bearish": "利空",
    "neutral": "中性",
}

_IMPACT_STRENGTH_CN = {
    "strong": "强",
    "medium": "中",
    "weak": "弱",
}

# d3 动作闸门语义（fix_053）：resonance_state → status
_RESONANCE_CN = {
    "confirmed": "已定价（confirmed → watch 观察池）",
    "weak": "未充分定价（weak → open 可动作）",
    "divergent": "方向背离（divergent → invalid 剔除）",
    "pending": "未反应（pending → watch 观察池）",
}

_REGIME_CN = {
    "risk_on": "风险偏好（Risk On）",
    "neutral": "中性（Neutral）",
    "risk_off": "风险规避（Risk Off）",
}

# 共振状态 → 「预期消化」补充说明（重大事件邮件「预期已消化」模块）
_RESONANCE_NOTE = {
    "confirmed": "系统判定已定价（预期消化较充分）",
    "weak": "系统判定未充分定价，仍有空间但也可能已部分兑现",
    "divergent": "系统判定方向背离（价格与事件方向不一致）",
    "pending": "系统判定尚未反应（价格暂未跟随）",
}


def _tech_cn(v) -> str:
    return _TECH_STATE_CN.get(v, v or "—")


def _persist_cn(v) -> str:
    return _PERSISTENCE_CN.get(v, v or "—")


def _kind_cn(v) -> str:
    return _KIND_CN.get(v, "—")


def _build_a_alert_card(r: dict) -> str:
    """构建 A 级 Alert 单条完整卡片（邮件内自包含，不依赖外链网页）。

    四段结构（OPT-CATALYST-ALERT-001 改版）：
      ① 交易计划：方向（做多/做空/中性）+ 触发条件 + 档位 + 失效条件
      ② 催化剂原文：标题 / 正文全文 / AI 摘要 / 来源链接（不截断）
      ③ 决策链 G0-G7：分级分项、影响与共振、持续性、基本面 checks、
         技术面 MA/ATR、合成评分、AI 结论（ai_reason 全文）
      ④ 代币快照：现价与涨跌、成交量、市值排名、供应、ATH、板块、在池信号

    方向语义：signal 表无 direction 列，只能从档位结构推导——
      止损 > 入场 > 止盈 → 做空；止损 < 入场 < 止盈 → 做多；其余视为区间/中性。
    同时并列展示「催化方向」（impact_direction），避免把趋势状态误读为交易方向
    （审计 P0-1：做空信号曾因 technical_state='up' 被渲染成「技术面 看涨」）。
    """
    import html as _html

    entry = _to_float(r.get("entry_price"))
    sl = _to_float(r.get("stop_loss"))
    tp = _to_float(r.get("take_profit"))
    rr = _to_float(r.get("rr_ratio"))
    score = float(r.get("composite_score") or 0)
    td = r.get("technical_detail") or {}
    fd = r.get("fundamental_detail") or {}

    dir_cn, dir_color = _trade_direction(entry, sl, tp)
    cycle = r.get("investment_cycle") or "待补（G7 生成中）"

    # ---------- 通用渲染小件 ----------
    def _kv(label: str, value) -> str:
        return (
            '<div style="display:flex;gap:8px;align-items:baseline;margin:2px 0">'
            f'<div style="flex:0 0 104px;color:#6b7280;font-size:11.5px">{label}</div>'
            f'<div style="flex:1;color:#111827;font-size:12px;line-height:1.6">{value}</div>'
            "</div>"
        )

    def _g(tag: str, title: str, inner: str) -> str:
        return (
            '<div style="border:1px solid #eef2f7;border-left:3px solid #a78bfa;'
            'border-radius:6px;padding:9px 11px;margin-bottom:7px;background:#fcfcff">'
            f'<div style="font-size:12px;font-weight:700;color:#5b21b6;margin-bottom:5px">{tag} · {title}</div>'
            f"{inner}</div>"
        )

    def _badge(text: str, bg: str, color: str = "#fff") -> str:
        return (f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:{bg};'
                f'color:{color};font-size:11px;font-weight:700">{text}</span>')

    # ---------- ① 交易计划 ----------
    plan = (
        '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">'
        + _plan_cell("方向", dir_cn, dir_color)
        + _plan_cell("入场", _fmt_price(entry)
                     + (f'<div style="font-size:10px;color:#6b7280;font-weight:400">'
                        f'触发：{_html.escape(str(r.get("entry_trigger") or "—"))}</div>' if r.get("entry_trigger") else ""),
                     "#111827")
        + _plan_cell("止损", _fmt_price(sl), "#dc2626")
        + _plan_cell("止盈", _fmt_price(tp), "#059669")
        + _plan_cell("盈亏比", f"{rr:.2f}" if rr else "—", "#111827")
        + _plan_cell("置信度", f"{_to_float(r.get('confidence')):.3f}" if r.get("confidence") is not None else "—", "#111827")
        + "</div>"
    )
    plan += _kv("失效条件", _html.escape(str(r.get("invalidation") or "—")))
    plan += _kv("有效期至", _fmt_ts(r.get("expires_at")))
    # 技术面基准价：入场/止损/止盈由 G5 的技术面窗口推导（区间中轨 / ATR / 30d 高低点），
    # 该窗口末端即最新日线，与「代币快照」同源。必须标明基准，否则用户会拿入场价与
    # 快照现价直接比较而误判（ZEC 实测 档位 1134.7 / 快照 1513.6，差异来自行情继续上行）。
    if td and td.get("last_price") is not None:
        plan += _kv("技术面基准",
                    f'<span style="font-weight:600">{_fmt_price(td.get("last_price"))}</span>'
                    f'<span style="color:#6b7280;font-size:11px">'
                    f'（G5 技术面窗口末端收盘，入场/止损/止盈由该窗口推导；'
                    f'与下方「代币快照」同源）</span>')

    # ---------- ② 催化剂原文 ----------
    title = _full_title(r)
    body = r.get("catalyst_body") or ""
    body_html = ""
    if body:
        # 全文展示（用户口径：邮件内完整展开，不依赖外链网页）
        body_html = (
            '<div style="font-size:12px;line-height:1.75;color:#374151;white-space:pre-wrap;'
            'background:#f9fafb;border-radius:6px;padding:9px 11px;margin-top:6px">'
            + _html.escape(body)
            + "</div>"
            f'<div style="font-size:10.5px;color:#9ca3af;margin-top:3px">'
            f'正文全文 {len(body)} 字</div>'
        )
    summary = _complete_text(r.get("ai_summary") or "", body)
    summary_html = ""
    # ai_summary 被采集层截断时补齐后可能与标题同句，此时不再重复渲染
    if summary and summary != "—" and summary != title:
        summary_html = (
            '<div style="margin-top:6px">'
            '<div style="font-size:11px;color:#7c3aed;font-weight:600">🤖 AI 摘要</div>'
            '<div style="font-size:12px;line-height:1.7;color:#374151;margin-top:3px">'
            + _html.escape(summary)
            + "</div></div>"
        )
    url = r.get("source_url") or ""
    url_html = (_html.escape(url)) if url else "—"
    # 审计 2026-09-22 P3：Binance Square 帖子转成 uni-qr 二维码落地页，桌面端打不开
    if url and "uni-qr" in url:
        url_html += (' <span style="color:#b45309;font-size:10.5px">'
                     '（Binance Square 二维码落地页，桌面端可能无法直接打开）</span>')
    source_section = (
        _g("G0", "事件来源与原文",
           _kv("发布时间", _fmt_ts(r.get("published_at")))
           + _kv("信息源", _html.escape(_display_source(r)))
           + _kv("事件分类", _html.escape(str(r.get("event_category") or "—")))
           + _kv("原文链接", url_html)
           + '<div style="font-size:12.5px;font-weight:600;color:#111827;margin-top:8px;line-height:1.5">'
           + _html.escape(str(title)) + "</div>"
           + body_html + summary_html)
    )

    # ---------- ③ 决策链 G1-G7 ----------
    def _num(v, fmt="{:.4g}"):
        f = _to_float(v)
        return "—" if f is None else fmt.format(f)

    _rule_et = r.get("rule_event_type")
    _ai_et = r.get("ai_event_type")
    _et_note = ("（⚠️ 规则与 AI 分类分歧，展示以规则为准）"
                if (_rule_et and _ai_et and str(_rule_et) != str(_ai_et)) else "")
    g1 = _g("G1", "事件分级",
            _kv("事件类型", f'{_html.escape(str(_rule_et or "—"))}'
                           f'（AI 判定：{_html.escape(str(_ai_et or "—"))}，'
                           f'来源 {_html.escape(str(r.get("event_type_src") or "—"))}）'
                           f'{_et_note}')
            + _kv("催化性质", _kind_cn(r.get("catalyst_kind")))
            + _kv("分项得分", f'权威度 {r.get("authority_score") if r.get("authority_score") is not None else "—"}'
                             f' · 事件权重 {r.get("event_weight") if r.get("event_weight") is not None else "—"}'
                             f' · 覆盖范围 {r.get("scope_score") if r.get("scope_score") is not None else "—"}'
                             f' · 市值适配 {r.get("mcap_score") if r.get("mcap_score") is not None else "—"}')
            + _kv("基础强度", f'{r.get("base_strength") if r.get("base_strength") is not None else "—"} / 100'
                             f' · 可交易 {"是" if r.get("tradable") else "否"}')
            + _kv("发布前启动", f'{_pct(r.get("prelaunch_ret_24h"))}（24h）'
                               f' · 追高扣分 {r.get("prelaunch_penalty") if r.get("prelaunch_penalty") is not None else 0}')
            )

    _hz = r.get("horizon_days")
    # 审计 2026-09-22 P2：rule 判定常给 0 天，与信号级有效期（如 7 天）矛盾 → 显示「—」
    _hz_txt = f"{_hz} 天" if _hz not in (None, 0) else "—"
    g2 = _g("G2", "影响判定与价格共振",
            _kv("催化方向", f'{_DIRECTION_CN.get(r.get("impact_direction"), r.get("impact_direction") or "—")}'
                           f' · 强度 {_IMPACT_STRENGTH_CN.get(r.get("impact_strength"), r.get("impact_strength") or "—")}'
                           f' · 有效期 {_hz_txt}'
                           f'（判定来源 {_html.escape(str(r.get("impact_derived_from") or "—"))}）')
            + _kv("共振状态", f'<span style="color:#5b21b6;font-weight:600">'
                             f'{_RESONANCE_CN.get(r.get("resonance_state"), r.get("resonance_state") or "—")}</span>'
                             f' · 共振分 {r.get("resonance_score") if r.get("resonance_score") is not None else "—"}')
            + _kv("超额收益", f'1h {_pct(r.get("excess_ret_1h"))}'
                             f' · 4h {_pct(r.get("excess_ret_4h"))}'
                             f' · 24h {_pct(r.get("excess_ret_24h"))}'
                             f' · 72h {_pct(r.get("excess_ret_72h"))}')
            + _kv("量能 / 板块", f'量能 Z {_num(r.get("vol_zscore_24h"), "{:.2f}")}'
                               f' · 板块中位收益 {_pct(r.get("peer_median_ret_24h"))}')
            + _kv("方向一致性", f'{"一致" if r.get("direction_match") else "背离"}'
                               f' · 数据源 {_html.escape(str(r.get("ret_source") or "—"))}'
                               f' · 计算于 {_fmt_ts(r.get("resonance_computed_at"))}')
            )

    g3 = _g("G3", "持续性预判",
            _kv("持续性", _persist_cn(r.get("persistence")))
            + _kv("是否已验证", "已验证" if r.get("persistence_verified") else "未验证（预判值）")
            )

    checks = (fd.get("checks") or []) if isinstance(fd, dict) else []
    checks_html = "".join(
        f'<div style="font-size:11.5px;color:#374151;line-height:1.7">· {_html.escape(str(c))}</div>'
        for c in checks
    ) or '<div style="font-size:11.5px;color:#9ca3af">无明细</div>'
    g4 = _g("G4", "基本面检查",
            _kv("结论", ('<span style="color:#059669;font-weight:600">通过</span>'
                        if r.get("fundamental_pass") else '<span style="color:#dc2626;font-weight:600">未通过</span>')
                       + f'（得分 {fd.get("score", "—")} / 阈值 {fd.get("threshold", "—")}）')
            + _kv("六项明细", "<div>" + checks_html + "</div>")
            )

    if td:
        trend_txt = (
            f'<span style="font-weight:600">{_tech_cn(td.get("state"))}</span>'
            f'（价格 {_fmt_price(td.get("last_price"))} vs 20MA {_fmt_price(td.get("ma20"))}）'
        )
        g5 = _g("G5", "技术面结构（趋势描述，非交易方向）",
                _kv("趋势状态", trend_txt)
                + _kv("均线", f'MA5 {_fmt_price(td.get("ma5"))}'
                             f' · MA20 {_fmt_price(td.get("ma20"))}'
                             f' · MA60 {_fmt_price(td.get("ma60"))}')
                + _kv("30d 区间", f'高 {_fmt_price(td.get("high_30d"))}'
                                 f' · 低 {_fmt_price(td.get("low_30d"))}')
                + _kv("波动", f'ATR(30d) {_fmt_price(td.get("atr_30d"))}'
                             f' · 明细方向 {_DIRECTION_CN.get(td.get("impact_direction"), td.get("impact_direction") or "—")}')
                + _kv("档位推导", f'触发 {_html.escape(str(r.get("entry_trigger") or "—"))}'
                                 f' → 入场 {_fmt_price(entry)}'
                                 f' / 止损 {_fmt_price(sl)}'
                                 f' / 止盈 {_fmt_price(tp)}')
                )
    else:
        g5 = _g("G5", "技术面结构", '<div style="font-size:11.5px;color:#9ca3af">技术明细待补（本信号由旧版本写入）</div>')

    g6 = _g("G6", "合成评分与闸门",
            _kv("综合评分", f'<b style="font-size:14px;color:#7c3aed">{score:.0f}</b> → 档级 '
                           f'<b>{_html.escape(str(r.get("tier") or "—"))}</b>'
                           f'<span style="font-size:11px;color:#6b7280">'
                           f'（权重：事件 0.25 + 共振 0.30 + 持续性 0.15 + 基本面 0.15 + 技术 0.15）</span>')
            + _kv("动作状态", '<span style="color:#059669;font-weight:600">open</span>'
                             '（未充分定价，可动作；已定价→watch 观察池，方向背离→invalid 剔除）')
            + _kv("市场环境", _REGIME_CN.get(r.get("regime"), r.get("regime") or "—"))
            )

    reason = r.get("ai_reason") or "待补（G7 生成中）"
    g7 = _g("G7", "AI 决策结论",
            _kv("投资周期", _html.escape(str(cycle)))
            + _kv("推理全文", '<div style="line-height:1.8">' + _html.escape(str(reason)) + "</div>")
            )

    # ---------- ④ 代币快照 ----------
    snap = _build_token_snapshot(r)

    # ---------- 头部 ----------
    header = (
        '<div style="background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;'
        'padding:16px 18px;border-radius:10px 10px 0 0">'
        '<div style="font-size:11px;opacity:.8;letter-spacing:1px">A 级催化剂信号 · 慢通道完整决策</div>'
        '<div style="margin-top:6px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">'
        f'<span style="font-size:22px;font-weight:800">{_html.escape(str(r.get("symbol") or "?"))}</span>'
        f'<span style="font-size:13px;opacity:.9">{_html.escape(str(r.get("canonical_name") or ""))}</span>'
        f'<span style="margin-left:auto;font-size:13px;font-weight:700">评分 {score:.0f} · {_html.escape(str(r.get("tier") or ""))} 级</span>'
        "</div>"
        '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        + _badge(f"交易方向 {dir_cn}", dir_color)
        + _badge(f"周期 {_html.escape(str(cycle))}", "#1d4ed8")
        + _badge(_DIRECTION_CN.get(r.get("impact_direction"), "—") + "催化", "#0f766e")
        + f'<span style="font-size:11.5px;opacity:.85">信号 #{r.get("signal_id")} · 创建 {_fmt_ts(r.get("created_at"))}</span>'
        "</div>"
        '<div style="margin-top:8px;font-size:12.5px;line-height:1.6;opacity:.95">'
        + _html.escape(str(title))
        + "</div></div>"
    )

    return (
        '<div style="border:1px solid #e5e7eb;border-radius:10px;margin-bottom:16px;background:#fff">'
        + header
        + '<div style="padding:14px 16px">'
        + '<div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:8px">📐 交易计划</div>'
        + plan
        + source_section
        + '<div style="font-size:13px;font-weight:700;color:#111827;margin:14px 0 8px">🔗 决策链 G0-G7</div>'
        + g1 + g2 + g3 + g4 + g5 + g6 + g7
        + snap
        + "</div></div>"
    )


def _plan_cell(label: str, value_html: str, color: str) -> str:
    return (
        '<div style="flex:1;min-width:88px;background:#f9fafb;border-radius:6px;padding:7px 9px">'
        f'<div style="font-size:10.5px;color:#6b7280">{label}</div>'
        f'<div style="font-size:14px;font-weight:700;color:{color};margin-top:2px">{value_html}</div>'
        "</div>"
    )


def _build_token_snapshot(r: dict) -> str:
    """构建「当前代币所有信息」快照区块。"""
    import html as _html

    def _row(label: str, value: str) -> str:
        return (
            '<div style="flex:1;min-width:150px;padding:5px 0">'
            f'<div style="font-size:10.5px;color:#6b7280">{label}</div>'
            f'<div style="font-size:12.5px;font-weight:600;color:#111827;margin-top:2px">{value}</div>'
            "</div>"
        )

    price = _to_float(r.get("current_price"))
    ch24 = _to_float(r.get("change_24h"))
    ch7 = _to_float(r.get("change_7d"))
    vol = _to_float(r.get("volume_24h"))
    mcap = _to_float(r.get("md_market_cap")) or _to_float(r.get("asset_market_cap"))
    circ = _to_float(r.get("circulating_supply"))
    total = _to_float(r.get("total_supply"))
    ath = _to_float(r.get("ath_usd"))
    mc_rank = r.get("market_cap_rank")

    circ_pct = f"（流通率 {circ / total * 100:.1f}%）" if circ and total else ""
    ath_html = "—"
    if ath:
        ath_txt = f"{_fmt_price(ath)}"
        if price:
            gap = price / ath * 100 - 100
            ath_html = (f'{ath_txt} · 距 ATH <span style="color:{_pct_color(gap)}">'
                        f'{gap:+.1f}%</span>')
            # core.asset.ath_usd 由离线同步任务维护，未及时刷新时会出现「现价高于 ATH」
            # 的悖论（ZEC 实测 ATH 737.88 / 现价 1513.6 → 距 ATH +105%）。如实标注，
            # 不要把陈旧基准当成真实回撤幅度。
            if gap > 0:
                ath_html += ('<span style="color:#b45309;font-size:10.5px">'
                             '（现价已高于库内 ATH，快照未及时更新）</span>')
        else:
            ath_html = ath_txt

    cats = r.get("categories") or []
    cats_html = " · ".join(_html.escape(str(c)) for c in cats[:8]) if cats else "—"

    pool_open = r.get("open_cnt") or 0
    pool_watch = r.get("watch_cnt") or 0

    return (
        '<div style="margin-top:14px;border:1px solid #eef2f7;border-left:3px solid #38bdf8;'
        'border-radius:6px;padding:10px 12px;background:#f8fdff">'
        '<div style="font-size:13px;font-weight:700;color:#0369a1;margin-bottom:6px">🪙 代币快照'
        f'<span style="font-size:10.5px;font-weight:400;color:#6b7280;margin-left:6px">'
        f'行情日期 {r.get("md_date") or "—"}</span></div>'
        '<div style="display:flex;flex-wrap:wrap;gap:4px 16px">'
        + _row("现价", _fmt_price(price))
        + _row("24h", f'<span style="color:{_pct_color(ch24)}">{_pct(ch24)}</span>')
        + _row("7d", f'<span style="color:{_pct_color(ch7)}">{_pct(ch7)}</span>')
        + _row("24h 成交量", _fmt_big(vol))
        + _row("市值 / 排名", f'{_fmt_big(mcap)} · #{mc_rank if mc_rank is not None else "—"}')
        + _row("流通 / 总量", f'{_fmt_big(circ, currency=False)} / {_fmt_big(total, currency=False)}{circ_pct}')
        + _row("历史最高", ath_html)
        + _row("上市日期", str(r.get("launch_date") or "—"))
        + _row("资产类型", _html.escape(str(r.get("asset_type") or "—")))
        + _row("主赛道", _html.escape(str(r.get("primary_sector") or "—")))
        + _row("在池信号", f'可动作 {pool_open} 条 · 观察 {pool_watch} 条')
        + "</div>"
        f'<div style="font-size:11.5px;color:#374151;line-height:1.7;margin-top:4px">'
        f'<span style="color:#6b7280">板块标签：</span>{cats_html}</div>'
        "</div>"
    )


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _full_title(r: dict) -> str:
    """取完整标题（修复「内容显示不全」）。"""
    return _complete_text(r.get("title_cn") or r.get("catalyst_title") or "",
                          r.get("catalyst_body") or "")


def _complete_text(value: str, body: str) -> str:
    """补齐被采集层截断的文本（修复「内容显示不全」）。

    采集层把 title / ai_summary 硬截断到 83 字并补 '...'
    （如 'Zcash rose nearly 6% ... Wednesday, t...'、'...Supersta...'），
    而正文 body_text 从同一句完整起头。此处若判定被截断，则从正文取第一个完整句子替代，
    避免邮件在词中间断掉。未截断的原样返回。
    """
    value = (value or "").strip()
    body = (body or "").strip()
    if not value:
        return "—"
    if not value.endswith(("...", "…")):
        return value
    if not body:
        return value

    # 截断标记前的词干应与正文同一起头，才敢用正文替换
    probe = value.rstrip(".…").strip()[:20]
    if probe and not body.startswith(probe):
        return value

    head = body[:400]
    # 取「第一个完整句子」：在所有句末标记中选最短且长度合理的候选
    # （避免正文开头出现短行/换行时切出过短标题）
    best = None
    for sep in ("。", "！", "？", ". ", "! ", "? ", "\n"):
        idx = head.find(sep)
        if idx <= 0:
            continue
        cand = head[: idx + len(sep)].strip()
        if len(cand) < 12:
            continue
        if best is None or len(cand) < len(best):
            best = cand
    return best or head.strip() or value


def _trade_direction(entry, sl, tp) -> tuple[str, str]:
    """从档位结构推导交易方向（signal 表无 direction 列）。

    空头：止损 > 入场 > 止盈；多头：止损 < 入场 < 止盈；其余视为区间/中性。
    """
    if entry is None or sl is None or tp is None:
        return "—", "#6b7280"
    if sl > entry > tp:
        return "做空", "#dc2626"
    if sl < entry < tp:
        return "做多", "#059669"
    return "区间/中性", "#6b7280"


def _pct(v, digits: int = 2) -> str:
    """百分比格式化（输入即百分数，如 7.3857 → +7.39%）。"""
    f = _to_float(v)
    if f is None:
        return "—"
    return f"{'+' if f > 0 else ''}{f:.{digits}f}%"


def _pct_color(v) -> str:
    f = _to_float(v)
    if f is None:
        return "#6b7280"
    return "#059669" if f > 0 else ("#dc2626" if f < 0 else "#6b7280")


def _fmt_big(v, currency: bool = True) -> str:
    """大数格式化（市值/成交量=货币；供应量=纯数量，须 currency=False）。

    审计 2026-09-22 P1：供应量（流通/总量）是**代币枚数**而非美元，模板误用带 `$`
    的格式化（ETH 显示 `$120.68M`、COPPER 显示 `$100000.00T`），易被读成市值。
    """
    f = _to_float(v)
    if f is None:
        return "—"
    sign = "$" if currency else ""
    if f >= 1e12:
        return f"{sign}{f / 1e12:.2f}T"
    if f >= 1e9:
        return f"{sign}{f / 1e9:.2f}B"
    if f >= 1e6:
        return f"{sign}{f / 1e6:.2f}M"
    if f >= 1e3:
        return f"{f / 1e3:.1f}K"
    return f"{f:,.0f}"


def _fmt_ts(v, beijing: bool = True) -> str:
    """时间戳格式化（默认换算北京时间）。"""
    if not v:
        return "—"
    try:
        if beijing and getattr(v, "tzinfo", None) is not None:
            v = v.astimezone(timezone(timedelta(hours=8)))
            return v.strftime("%Y-%m-%d %H:%M") + "（北京）"
        return v.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v)


def _trim_trailing_zeros(s: str) -> str:
    """去掉定点小数无意义的尾零（`0.42830000 → 0.4283`、`620.5000 → 620.5`）。

    复验旧项：现价 `0.42830000` 属显示噪音，读者易误读精度。
    """
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".")


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        if f >= 1000:
            return _trim_trailing_zeros(f"{f:,.1f}")
        if f >= 1:
            return _trim_trailing_zeros(f"{f:,.4f}")
        if f >= 1e-4:
            return _trim_trailing_zeros(f"{f:.8f}")
        # 极小价（meme 常见 1e-12）：固定 8 位小数会被截断成 0.00000000，
        # 让「现价/MA20」看起来像 0（审计 2026-09-22 P0 的显示根因）→ 改科学计数。
        return f"{f:.4e}"
    except (TypeError, ValueError):
        return "—"


def _build_slow_digest_html(a_rows, stats: dict,
                            class_label: str = "加密货币",
                            tier_label: str = "A级") -> str:
    """构建 A 级 Alert 邮件 HTML（OPT-CATALYST-ALERT-001 改版）。

    Args:
        a_rows: 24h 内去重新信号（最多 2 条，未充分定价 + 完整交易档位）
        stats: 慢通道统计（second_order_count / expired_count）
        class_label: 资产类别中文标签（加密货币 / 美股·商品）
        tier_label: 档位标签（A级 / B级；审计 2026-10-02 P0-3 方案 B：无 A 级时
            回退 B 级高置信度，邮件头部/标题随之标注，避免文案与卡片不符）

    每条卡片在邮件内完整展开「交易计划 + 催化剂原文 + 决策链 G0-G7 + 代币快照」，
    不依赖外链网页（用户口径：拿到邮件即看到整个决策过程与代币全部信息）。
    """
    so_count = stats.get("second_order_count", 0)
    expired = stats.get("expired_count", 0)

    cards = "".join(_build_a_alert_card(r) for r in a_rows)
    if not cards:
        cards = ('<div style="padding:16px;text-align:center;color:#9ca3af;background:#f9fafb;'
                 'border-radius:8px">过去 24h 无 {tier_label} 新信号</div>')

    return f"""
    <div style="font-family:sans-serif;max-width:760px;margin:auto;padding:16px;background:#f3f4f6">
      <div style="background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;padding:24px;border-radius:12px">
        <div style="font-size:12px;opacity:.7;text-transform:uppercase;letter-spacing:1px">催化剂决策管道 · {class_label} · {tier_label} Alert</div>
        <div style="font-size:24px;font-weight:700;margin-top:8px">{class_label} {tier_label} 新增 {len(a_rows)} 条可交易信号</div>
        <div style="margin-top:4px;font-size:13px;opacity:.8">24h 窗口 · 未充分定价(weak)可动作池 · 二阶受益 {so_count} 条 · 过期 {expired} 条</div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 12px 12px">
        <h3 style="font-size:15px;margin:0 0 12px;color:#111827">🟣 {tier_label} 信号（过去 24h 新增 · 未充分定价可动作 + 完整交易档位）</h3>
        {cards}

        <div style="margin-top:20px;padding:12px;background:#f0f9ff;border-radius:8px;font-size:12px;color:#0369a1">
          💡 C 级及以下热点已下沉至每日早报「📡 催化剂热点」观察区；本邮件仅保留高置信度 idea。
        </div>

        <div style="margin-top:20px;font-size:11px;color:#9ca3af;text-align:center">
          由催化剂决策管道自动生成 · 24h 去重 · {class_label} 独立发送
        </div>
      </div>
    </div>
    """


# =====================================================================
# 辅助：单条信号详情查询 + 深度评审输入构建
# =====================================================================

def _fetch_signal_row(conn, signal_id: int):
    """查询单条信号的完整详情（用于 AI 深度评审输入）。"""
    row = conn.execute("""
        SELECT s.signal_id, s.tier, s.composite_score, s.kind,
               s.asset_id, a.canonical_name, a.canonical_symbol AS symbol,
               a.asset_type, a.primary_sector, a.categories,
               a.market_cap, a.market_cap_rank,
               a.circulating_supply, a.total_supply,
               a.ath_usd, a.launch_date, a.description_short,
               c.catalyst_id, c.title AS catalyst_title, c.title_cn,
               c.source_code, c.body_text AS catalyst_summary, c.ai_summary,
               c.ai_event_type, c.event_category,
               s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
               s.investment_cycle, s.ai_reason, s.ai_deep_review,
               s.technical_state, s.resonance_state,
               s.invalidation, s.persistence,
               s.base_strength, s.resonance_score,
               s.confidence, s.regime,
               c.published_at,
               -- 风险标签（risk_label 存的是 high/medium/low）
               (SELECT json_build_array(json_build_object('level', arl.risk_label, 'label',
                     CASE arl.risk_label
                       WHEN 'high' THEN '高风险'
                       WHEN 'medium' THEN '中风险'
                       WHEN 'low' THEN '低风险'
                       WHEN 'critical' THEN '极高风险'
                       ELSE '风险等级未知'
                     END))
                  FROM biz.asset_risk_labels arl
                 WHERE arl.asset_id = s.asset_id) AS risk_labels,
               -- 最新日行情 + 7 日均量 + 量比 + 链上池流动性（口径见 _MARKET_COLS_SQL）
""" + _MARKET_COLS_SQL + """
               , -- 衍生品 24h 聚合（OI / 资金费率 / CVD）
               ad.total_oi_usd AS oi_total_24h,
               ad.oi_change_24h_pct,
               ad.funding_rate_pct,
               ad.funding_rate_7d_avg,
               ad.cvd_24h_usd,
               ad.cvd_ratio_24h,
               ad.available_exchanges AS derivative_exchanges,
               -- OI/CVD 时序（1h / 4h 变化率，从 oi_cvd_snapshot 计算）
               oi_snap.oi_1h_chg_pct,
               oi_snap.oi_4h_chg_pct,
               oi_snap.cvd_1h_usd AS cvd_1h_total,
               oi_snap.vol_1h_usd,
               -- 最近 24h 扫描信号
               (SELECT json_agg(json_build_object(
                   'signal_ts', ss.signal_ts,
                   'pool', ss.pool,
                   'scenario', ss.scenario,
                   'timeframe', ss.timeframe,
                   'confidence', ss.confidence,
                   'p_dir', ss.p_dir,
                   'price_chg_pct', ss.price_chg_pct,
                   'vol_state', ss.vol_state,
                   'vol_ratio', ss.vol_ratio,
                   'oi_dir', ss.oi_dir,
                   'oi_chg_pct', ss.oi_chg_pct,
                   'cvd_dir', ss.cvd_dir
                 ) ORDER BY ss.signal_ts DESC)
                  FROM biz.scan_signal ss
                 WHERE ss.symbol = (a.canonical_symbol || 'USDT')
                   AND ss.signal_ts >= NOW() - INTERVAL '24 hours'
                   AND ss.status != 'stale'
                 LIMIT 10) AS recent_scan_signals
        FROM biz.catalyst_signal s
        JOIN core.asset a ON s.asset_id = a.asset_id
        JOIN biz.asset_catalyst c ON s.catalyst_id = c.catalyst_id
""" + _MARKET_LATERAL_SQL + """
        -- 衍生品 24h 快照
        LEFT JOIN biz.asset_derivatives ad ON ad.asset_id = s.asset_id
        -- OI/CVD 时序（从 oi_cvd_snapshot 计算 1h/4h 变化）
        LEFT JOIN LATERAL (
            WITH latest AS (
                SELECT oi_usd, cvd_1h_usd, vol_5m_usd, ts
                  FROM biz.oi_cvd_snapshot
                 WHERE symbol = (a.canonical_symbol || 'USDT')
                   AND exchange = 'binance'
                 ORDER BY ts DESC
                 LIMIT 1
            ),
            one_hour_ago AS (
                SELECT oi_usd
                  FROM biz.oi_cvd_snapshot
                 WHERE symbol = (a.canonical_symbol || 'USDT')
                   AND exchange = 'binance'
                   AND ts <= (SELECT ts FROM latest) - INTERVAL '1 hour'
                 ORDER BY ts DESC
                 LIMIT 1
            ),
            four_hour_ago AS (
                SELECT oi_usd
                  FROM biz.oi_cvd_snapshot
                 WHERE symbol = (a.canonical_symbol || 'USDT')
                   AND exchange = 'binance'
                   AND ts <= (SELECT ts FROM latest) - INTERVAL '4 hours'
                 ORDER BY ts DESC
                 LIMIT 1
            ),
            vol_1h AS (
                SELECT SUM(vol_5m_usd) AS vol_1h_usd
                  FROM biz.oi_cvd_snapshot
                 WHERE symbol = (a.canonical_symbol || 'USDT')
                   AND exchange = 'binance'
                   AND ts > (SELECT ts FROM latest) - INTERVAL '1 hour'
            )
            SELECT
                (SELECT oi_usd FROM latest) AS oi_latest,
                CASE
                    WHEN (SELECT oi_usd FROM one_hour_ago) > 0
                         AND (SELECT oi_usd FROM latest) > 0
                    THEN ROUND(((SELECT oi_usd FROM latest) - (SELECT oi_usd FROM one_hour_ago))
                              / (SELECT oi_usd FROM one_hour_ago) * 100, 2)
                    ELSE NULL
                END AS oi_1h_chg_pct,
                CASE
                    WHEN (SELECT oi_usd FROM four_hour_ago) > 0
                         AND (SELECT oi_usd FROM latest) > 0
                    THEN ROUND(((SELECT oi_usd FROM latest) - (SELECT oi_usd FROM four_hour_ago))
                              / (SELECT oi_usd FROM four_hour_ago) * 100, 2)
                    ELSE NULL
                END AS oi_4h_chg_pct,
                (SELECT cvd_1h_usd FROM latest) AS cvd_1h_usd,
                (SELECT vol_1h_usd FROM vol_1h) AS vol_1h_usd
        ) oi_snap ON true
        WHERE s.signal_id = %s
    """, (signal_id,)).fetchone()
    return dict(row) if row else None


def _signal_row_to_deep_review_input(row: dict) -> dict:
    """将数据库行转换为 AI 深度评审模块需要的输入格式。

    ai_enhance.py 里的 AISignalDeepReviewer 接收一个 dict，里面的字段名
    和数据库行基本一致，这里做必要的兼容映射。
    """
    d = dict(row) if not isinstance(row, dict) else row
    # 兼容字段名
    d.setdefault("symbol", d.get("symbol") or d.get("canonical_symbol"))
    d.setdefault("asset_name", d.get("canonical_name"))
    d.setdefault("event_type", d.get("ai_event_type") or d.get("event_category") or d.get("event_type") or "other")
    # 审计 P1-D3：简介里的过期行情句同样不能进 prompt——否则 LLM 会把 stale 价格
    # （XRP $1.087）写进核心逻辑，与实时价 $1.57 一起出现在同一封邮件里。
    if d.get("description_short"):
        d["description_short"] = _strip_stale_price_sentences(d["description_short"])
    return d


# =====================================================================
# 催化剂周报（每周一 09:00 北京）
# =====================================================================
#
# 背景（2026-09-29 用户需求「我想每周收到催化剂的周报」）：
# 与 24h 的 A 级 Alert（快通道/慢通道）不同，周报是**自然周**维度的固定节奏邮件，
# 面向「回看上周发生了什么重要的事、分别有什么影响」而非「即时可动作信号」。
#
# 内容结构（用户 2026-09-29 二次确认）：
#   ① 本周总述：LLM 概括本周催化剂主线、整体方向与最值得关注的变化
#   ② 主线主题：2~5 条主题（如「ETF 与机构资金」），各带脉络与影响
#   ③ 大事记与影响：逐条列出重要事件 + **影响解读**（作用机制/持续性），
#      而非交易档位。方向（利好/利空/中性）仅作徽章标注。
#   ④ 概览统计（亚行）：信号总数、tier 分布、事件类型分布、本周催化剂入库数
#
# 「重要事件」口径（2026-09-29 用户质疑「这些重要吗、覆盖全了吗」后**重建**）：
# 首版用 tier='A' OR resonance_state='confirmed'，实测无效——
#   ① tier 是**可交易性**强度（event_type_weights 里 listing=95），窗口内 13 条 A 级里
#      11 条是上新公告；而 BTC ETF 创纪录流入、Bitget 被盗 3.516 亿、美联储加息、
#      CLARITY 法案受阻等**全部落选**。
#   ② `OR ... ORDER BY composite_score DESC LIMIT 30` 里，A 级分数(80~88)占满 Top30，
#      confirmed(均分 58) 几乎进不来，OR 形同虚设。
#   ③ 素材取自 biz.catalyst_signal，而窗口内 4698/7049 条原始事件**无 asset_id**
#      （宏观/监管/无标的），从不进入 signal 表 → 采集口径天然漏掉一半要闻。
# 现口径：素材改为 biz.asset_catalyst 原始事件，按「市场显著性」排序，
#   importance = 类型权重 × 资产权重 + 共振/广度加成（见 _WEEKLY_TYPE_WEIGHT 与
#   _weekly_key_events 的 SQL），剔除 market_update 行情噪音，单类型配额防淹没。
#
# 叙事由 LLM（DeepSeek）生成，每周仅 1 次调用；LLM 不可用时回退为
# 「标题 + 已有 ai_summary」的模板拼接（无主题分组、无总述润色），不阻断发信。
#
# 去重：sentinel -4 + ntype 'weekly_report'，按**自然周**去重（本周已发过即跳过），
# 不同于 24h 去重的 Alert 通道。发送频率由调度器约束（每周一 09:00 触发一次）。

NTYPE_WEEKLY_REPORT = "weekly_report"
SENTINEL_WEEKLY_REPORT_SIGNAL_ID = -4   # 负号哨兵，同 -1/-2/-3 约定（NULL 不触发 UNIQUE）

# 送入 LLM 的事件上限（控制 prompt 规模与单次调用成本）
WEEKLY_EVENT_LIMIT = 30

# 单类型配额：防止某一类事件淹没清单（首版 listing 占 11/13 即因此）
WEEKLY_MAX_PER_TYPE = 8

# 周报「事件重要性」权重表 —— 口径 = **市场显著性**，与 grade.py 的
# event_type_weights（**可交易性**）刻意分离，二者不可互相替代：
#   · listing 95→62：上新公告量大（窗口 1737 条），高权重会淹没清单；
#   · regulation 75→80、tech_upgrade 55→68：周报看的是「影响面」；
#   · 新增 security/macro/etf 三类高权（95/88/85）——它们在库里被误分类
#     （黑客事件落 other、ETF/美联储落 market_update），故 SQL 里先做关键词兜底归类。
_WEEKLY_TYPE_WEIGHT = {
    "security": 95,       # 被盗/攻击/漏洞/rug
    "macro": 88,          # 美联储/利率/CPI/非农
    "etf": 85,            # ETF 审批与资金流
    "regulation": 80,     # 监管/法案/合规
    "delisting": 72,      # 下架/退市
    "tech_upgrade": 68,   # 主网/硬分叉/重大升级
    "listing": 62,        # 上新（数量大 → 降权）
    "funding": 55,        # 融资
    "burn": 52,           # 销毁
    "partnership": 45,    # 合作
    "airdrop": 45,
    "staking": 40,
    "governance": 40,
    "market_update": 18,  # 纯行情播报（整类剔除）
    "other": 32,
}

# 无关联标的的宏观/监管类事件，给可读占位标的（避免事件卡渲染为「—」）
_WEEKLY_SYMBOL_FALLBACK = {"macro": "宏观", "regulation": "监管",
                           "security": "安全", "etf": "ETF"}


def _weekly_window(end_ts=None) -> tuple[datetime, datetime, str]:
    """计算自然周窗口（北京时区）：上周一 00:00 ~ 本周一 00:00，返回 UTC 边界。

    Args:
        end_ts: 窗口计算参考时刻（tz-aware）。缺省用当前时刻。

    Returns:
        (start_utc, end_utc, label)，label 形如「2026-09-21 ~ 2026-09-28（北京）」
    """
    tz = ZoneInfo("Asia/Shanghai")
    if end_ts is None:
        end_ts = datetime.now(timezone.utc)
    elif getattr(end_ts, "tzinfo", None) is None:
        end_ts = end_ts.replace(tzinfo=timezone.utc)
    bj = end_ts.astimezone(tz)
    this_monday = (bj - timedelta(days=bj.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_monday = this_monday - timedelta(days=7)
    return (last_monday.astimezone(timezone.utc),
            this_monday.astimezone(timezone.utc),
            f"{last_monday.strftime('%Y-%m-%d')} ~ {this_monday.strftime('%Y-%m-%d')}（北京）")


def _weekly_overview_stats(conn, start_utc, end_utc) -> dict:
    """本周催化剂多维统计（概览）。

    Returns:
        {signals_total, tier_dist, event_type_dist, sentiment_dist, source_dist, catalysts_new}
    """
    total = conn.execute("""
        SELECT COUNT(*) AS cnt FROM biz.catalyst_signal
        WHERE created_at >= %s AND created_at < %s
    """, (start_utc, end_utc)).fetchone()
    tier_rows = conn.execute("""
        SELECT tier, COUNT(*) AS cnt FROM biz.catalyst_signal
        WHERE created_at >= %s AND created_at < %s
        GROUP BY tier ORDER BY cnt DESC, tier
    """, (start_utc, end_utc)).fetchall()
    event_rows = conn.execute("""
        SELECT COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS key, COUNT(*) AS cnt
        FROM biz.catalyst_signal s
        JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
        WHERE s.created_at >= %s AND s.created_at < %s
        GROUP BY 1 ORDER BY cnt DESC
    """, (start_utc, end_utc)).fetchall()
    sentiment_rows = conn.execute("""
        SELECT COALESCE(ac.ai_sentiment, 'neutral') AS key, COUNT(*) AS cnt
        FROM biz.catalyst_signal s
        JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
        WHERE s.created_at >= %s AND s.created_at < %s
        GROUP BY 1 ORDER BY cnt DESC
    """, (start_utc, end_utc)).fetchall()
    source_rows = conn.execute("""
        SELECT ac.source_code AS key, COUNT(*) AS cnt
        FROM biz.catalyst_signal s
        JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
        WHERE s.created_at >= %s AND s.created_at < %s
        GROUP BY 1 ORDER BY cnt DESC
    """, (start_utc, end_utc)).fetchall()
    catalysts_new = conn.execute("""
        SELECT COUNT(*) AS cnt FROM biz.asset_catalyst
        WHERE created_at >= %s AND created_at < %s
    """, (start_utc, end_utc)).fetchone()

    def _cnt(row) -> int:
        return int(row["cnt"]) if row and row.get("cnt") is not None else 0

    def _pairs(rows):
        return [(r["key"] or "?", int(r["cnt"])) for r in rows if r.get("cnt")]

    return {
        "signals_total": _cnt(total),
        "tier_dist": [(r["tier"] or "?", int(r["cnt"])) for r in tier_rows if r.get("cnt")],
        "event_type_dist": _pairs(event_rows),
        "sentiment_dist": _pairs(sentiment_rows),
        "source_dist": _pairs(source_rows),
        "catalysts_new": _cnt(catalysts_new),
    }


# 标题前缀噪音（去重用）：来源署名 +「消息/快讯/发推」等转述词、以及中英日期。
# 同一事件被多家媒体转述时标题只差前缀，剥离后才可能归并（实测 Payy Network
# 被 Specter Investigation 一条监测稿复制到 4 个来源，占掉 4 个 security 名额）。
_WEEKLY_SOURCE_PREFIX = re.compile(
    r"^(?:[\w\s·\-—:：,，.。!！?？\"'“”()（）\[\]【】|/]*?"
    r"(?:消息|报道|快讯|讯|发推表示|公告|披露|监测)[，,：:\s]*"
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[，,。.:：\s]*"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日[，,。.:：\s]*"
    r"|(?:昨日|今日|昨夜今晨)[，,。.:：\s]*"
    r")"
)


def _weekly_story_key(row: dict) -> str:
    """同一事件被多来源/多资产重复入库时的去重键：剥前缀 + 标题归一化。

    先反复剥离来源署名/日期等前缀噪音，再保留字母数字与汉字（含 CJK）截断 40 字符；
    字面量 'null' 等占位标题视为空，交由调用方回退为 catalyst_id
    （避免所有空标题被折叠成一条）。

    局限（已知）：跨来源**改写**（同语言不同措辞、中英双语）无法靠标题归一化归并，
    这类冗余交由 LLM 在叙事阶段合并——prompt 已要求 events 只保留 8~15 条。
    """
    t = str(row.get("title_cn") or row.get("title") or "").lower()
    for _ in range(6):
        stripped = _WEEKLY_SOURCE_PREFIX.sub("", t, count=1)
        if stripped == t:
            break
        t = stripped
    key = "".join(ch for ch in t if ch.isalnum())[:40]
    return "" if key in ("", "null", "none", "nan") else key


def _weekly_key_events(conn, start_utc, end_utc, limit: int | None = None) -> list[dict]:
    """本周「重要事件」清单（周报素材，非交易信号）。

    素材为 biz.asset_catalyst 原始事件（含无 asset_id 的宏观/监管类，它们从不进入
    signal 表），先剔除占位标题（'null'）与「要闻汇总/日报」类聚合帖，再按**标题**
    关键词兜底归类 security/etf/macro（不看 ai_summary，避免无关事件蹭到关键词），
    然后按「市场显著性」打分：importance = 类型权重 × 资产权重 + confirmed 共振加成；
    剔除 market_update 噪音。返回前按标题归一化去重、加广度加成、施加单类型配额，
    按 importance 降序截断。
    """
    lim = limit if limit is not None else WEEKLY_EVENT_LIMIT
    pool = max(lim * 10, 400)
    rows = conn.execute("""
        WITH base AS (
            SELECT ac.catalyst_id, ac.asset_id AS raw_asset_id, ac.created_at,
                   ac.title, ac.title_cn, ac.ai_summary, ac.source_code, ac.published_at,
                   COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS raw_event_type,
                   COALESCE(ac.ai_sentiment, 'neutral') AS sentiment,
                   a.canonical_name, a.canonical_symbol AS symbol, a.primary_sector,
                   a.market_cap_rank, a.market_cap,
                   s.signal_id, s.tier, s.composite_score, s.confidence,
                   s.resonance_state, s.status,
                   LOWER(COALESCE(ac.title_cn, '') || ' ' || COALESCE(ac.title, '')) AS head
            FROM biz.asset_catalyst ac
            LEFT JOIN core.asset a ON ac.asset_id = a.asset_id
            LEFT JOIN LATERAL (
                SELECT si.signal_id, si.tier, si.composite_score, si.confidence,
                       si.resonance_state, si.status
                FROM biz.catalyst_signal si
                WHERE si.catalyst_id = ac.catalyst_id
                ORDER BY si.composite_score DESC NULLS LAST, si.signal_id
                LIMIT 1
            ) s ON TRUE
            WHERE ac.created_at >= %s AND ac.created_at < %s
              AND COALESCE(s.resonance_state, '') <> 'divergent'
              -- 剔除占位标题（LLM 漏译写成的字面量 'null'）与「要闻汇总/日报」类
              -- 无单一事件的聚合帖：其标题不含事件，只会污染关键词归类
              AND LOWER(COALESCE(ac.title_cn, ac.title, ''))
                  NOT IN ('', 'null', 'none', 'nan', 'tl;dr')
              AND LOWER(COALESCE(ac.title_cn, ac.title, ''))
                  !~ '^(今日要闻|要闻预告|要闻提示|一、|二、|热点新闻|行情|盘前|盘后|每日|快讯汇总|市场综述)'
        ),
        categorized AS (
            SELECT *,
                CASE
                    -- 关键词只匹配**标题**（ai_summary 过长，会在无关事件里蹭到关键词，
                    -- 实测把「今日要闻提示：」「标普500指数…」等误判成 security/macro/etf）
                    WHEN head ~ 'hack|exploit|stolen|steal|breach|drain|被盗|被黑|遭攻击|漏洞|rug ?pull'
                        THEN 'security'
                    WHEN head ~ 'etf' THEN 'etf'
                    WHEN head ~ '美联储|federal reserve|rate hike|rate cut|加息|降息|基点|basis point|通胀|非农'
                        THEN 'macro'
                    ELSE raw_event_type
                END AS category
            FROM base
        ),
        scored AS (
            SELECT *,
                CASE category
                    WHEN 'security' THEN 95 WHEN 'macro' THEN 88 WHEN 'etf' THEN 85
                    WHEN 'regulation' THEN 80 WHEN 'delisting' THEN 72
                    WHEN 'tech_upgrade' THEN 68 WHEN 'listing' THEN 62
                    WHEN 'funding' THEN 55 WHEN 'burn' THEN 52 WHEN 'partnership' THEN 45
                    WHEN 'airdrop' THEN 45 WHEN 'staking' THEN 40 WHEN 'governance' THEN 40
                    WHEN 'market_update' THEN 18 ELSE 32
                END::numeric AS type_w,
                (CASE
                    WHEN raw_asset_id IS NULL THEN 0.75          -- 无标的 → 市场级事件
                    WHEN market_cap_rank IS NULL THEN 0.35
                    WHEN market_cap_rank <= 10 THEN 1.00
                    WHEN market_cap_rank <= 30 THEN 0.80
                    WHEN market_cap_rank <= 100 THEN 0.62
                    WHEN market_cap_rank <= 500 THEN 0.45
                    ELSE 0.30
                END)::numeric AS asset_w
            FROM categorized
        )
        SELECT catalyst_id, raw_asset_id, signal_id, tier, composite_score, confidence,
               resonance_state, status, created_at, canonical_name, symbol, primary_sector,
               market_cap_rank, market_cap, title, title_cn, ai_summary, source_code,
               published_at, category AS event_type, sentiment,
               ROUND(
                   type_w * (CASE WHEN category IN ('security','macro','etf','regulation')
                                  THEN GREATEST(asset_w, 0.5) ELSE asset_w END)
                   + CASE WHEN resonance_state = 'confirmed' THEN 6 ELSE 0 END, 1
               ) AS importance
        FROM scored
        WHERE category <> 'market_update'
        ORDER BY importance DESC, composite_score DESC NULLS LAST, catalyst_id
        LIMIT %s
    """, (start_utc, end_utc, pool)).fetchall()

    # 同一事件去重：保留重要性最高的一条，聚合其余行出现的标的
    best: dict[str, dict] = {}
    for r in rows:
        key = _weekly_story_key(r) or f"cid:{r.get('catalyst_id')}"
        cur = best.get(key)
        if cur is None:
            d = dict(r)
            d["symbols"] = [d.get("symbol")] if d.get("symbol") else []
            best[key] = d
        else:
            sym = r.get("symbol")
            if sym and sym not in cur["symbols"]:
                cur["symbols"].append(sym)

    items = list(best.values())
    for d in items:
        # 广度加成：覆盖标的越多 → 越偏「市场级」重要事件（上限 +15）
        d["importance"] = round(float(d.get("importance") or 0)
                                + min(max(len(d["symbols"]) - 1, 0), 5) * 3, 1)
        if not d.get("symbol"):
            d["symbol"] = _WEEKLY_SYMBOL_FALLBACK.get(str(d.get("event_type")))
    items.sort(key=lambda d: (-float(d.get("importance") or 0),
                              -float(d.get("composite_score") or 0)))

    # 单类型配额：防止某一类（如 listing / regulation）淹没清单
    out: list[dict] = []
    used: dict[str, int] = {}
    for d in items:
        et = str(d.get("event_type") or "other")
        if used.get(et, 0) >= WEEKLY_MAX_PER_TYPE:
            continue
        used[et] = used.get(et, 0) + 1
        out.append(d)
        if len(out) >= lim:
            break
    return out


_SENTIMENT_CN = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
_SENTIMENT_COLOR = {"bullish": "#059669", "bearish": "#dc2626", "neutral": "#6b7280"}


_WEEKLY_SYSTEM_PROMPT = """你是加密货币催化剂分析员。用户会给你一份「本周重要催化剂事件」清单（JSON），
你要输出一份中文周报，说明**本周发生了什么重要的事、分别有什么影响**。

严格按以下 JSON 结构输出（不要输出 JSON 以外的任何文字）：
{
  "overview": "本周总述，2~4 句：本周催化剂的主线、整体方向倾向、最值得关注的变化",
  "themes": [
    {"name": "主题名（6~12 字，如「ETF 与机构资金」）",
     "summary": "该主题本周的脉络与市场影响，2~4 句",
     "symbols": ["BTC", "ETH"]}
  ],
  "events": [
    {"symbol": "BTC",
     "headline": "事件一句话标题（25 字内，中文）",
     "impact": "该事件的影响解读，2~3 句：说明对标的本身、所在板块或市场情绪的作用机制与持续性",
     "direction": "bullish | bearish | neutral"}
  ]
}

硬性要求：
1. 只使用清单中出现的事实，**不得编造**价格、数字、时间或清单未提及的事件。
2. `impact` 必须写「影响」而非复述标题——要说明作用机制（如解锁抛压、流动性改善、机构资金流入）与持续性。
3. `themes` 按重要性从高到低，2~5 个；`events` 只保留清单中最重要的 8~15 条，按其重要性排序。
4. `symbol` 必须与清单中的 symbol 完全一致；`direction` 只能取 bullish/bearish/neutral。
5. 全部中文输出（symbol 保持原文大写）。"""


def _weekly_events_brief(events: list[dict]) -> list[dict]:
    """把事件行压缩为 LLM 输入（去噪、限长，只保留叙事所需字段）。"""
    brief = []
    for r in events:
        brief.append({
            "symbol": str(r.get("symbol") or ""),
            "symbols": [str(x) for x in (r.get("symbols") or [])][:6],
            "name": str(r.get("canonical_name") or ""),
            "sector": str(r.get("primary_sector") or ""),
            "event_type": str(r.get("event_type") or ""),
            "sentiment": str(r.get("sentiment") or "neutral"),
            "tier": str(r.get("tier") or ""),
            "resonance_state": str(r.get("resonance_state") or ""),
            "score": round(_to_float(r.get("composite_score")) or 0.0, 1),
            "published_at": _fmt_ts(r.get("published_at") or r.get("created_at")),
            "title": str(r.get("title_cn") or r.get("title") or "")[:120],
            "summary": str(r.get("ai_summary") or "")[:300],
        })
    return brief


def _weekly_llm_narrative(events: list[dict], stats: dict,
                          window_label: str) -> dict | None:
    """调用 LLM 生成本周叙事（总述 + 主题 + 逐条影响）。失败返回 None。"""
    if not events:
        return None
    try:
        import json as _json

        from crypto_research.config import get_settings
        from crypto_research.clients.llm_client import LLMClient
    except Exception as e:
        logger.warning("周报 LLM 依赖导入失败: %s", e)
        return None

    try:
        settings = get_settings(require_database=False)
        llm = LLMClient(settings, rpm=10, timeout=180)
        if not llm.is_available():
            logger.warning("周报 LLM 不可用，回退模板叙事")
            return None
    except Exception as e:
        logger.warning("周报 LLM 构建失败: %s", e)
        return None

    user_prompt = _json.dumps({
        "window": window_label,
        "stats": {
            "signals_total": stats.get("signals_total", 0),
            "catalysts_new": stats.get("catalysts_new", 0),
            "event_type_dist": stats.get("event_type_dist", []),
        },
        "events": _weekly_events_brief(events),
    }, ensure_ascii=False)

    try:
        raw = llm.chat(
            _WEEKLY_SYSTEM_PROMPT, user_prompt,
            temperature=0.3, max_tokens=4096,
            response_format={"type": "json_object"},
            use_cache=False,   # 每周一次，不缓存
        )
    except Exception as e:
        logger.warning("周报 LLM 调用失败: %s", e, exc_info=True)
        return None

    from .ai_enhance import _extract_json
    data = _extract_json(raw)
    if not isinstance(data, dict):
        logger.warning("周报 LLM 返回非 JSON，回退模板叙事")
        return None

    themes = []
    for t in (data.get("themes") or [])[:5]:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        themes.append({
            "name": str(t.get("name"))[:32],
            "summary": str(t.get("summary") or "")[:600],
            "symbols": [str(s)[:16] for s in (t.get("symbols") or [])][:12],
        })

    ev_out = []
    for e in (data.get("events") or [])[:20]:
        if not isinstance(e, dict) or not e.get("headline"):
            continue
        d = str(e.get("direction") or "neutral").lower()
        ev_out.append({
            "symbol": str(e.get("symbol") or "")[:16],
            "headline": str(e.get("headline"))[:80],
            "impact": str(e.get("impact") or "")[:800],
            "direction": d if d in _SENTIMENT_CN else "neutral",
        })

    return {
        "overview": str(data.get("overview") or "")[:900],
        "themes": themes,
        "events": ev_out,
        "source": "llm",
    }


def _weekly_fallback_narrative(events: list[dict]) -> dict:
    """LLM 不可用时的模板叙事：标题 + 已有 ai_summary，无主题分组。"""
    ev_out = []
    for r in events[:15]:
        ev_out.append({
            "symbol": str(r.get("symbol") or ""),
            "headline": str(r.get("title_cn") or r.get("title") or "")[:80],
            "impact": _complete_text(r.get("ai_summary") or "", r.get("title") or "")[:800],
            "direction": str(r.get("sentiment") or "neutral"),
        })
    return {
        "overview": (f"本周共产生 {len(events)} 条重要催化剂事件"
                     "（按事件类型与标的重要性筛选）。"
                     "以下按事件重要性降序列出事件与已有解读"
                     "（AI 摘要暂不可用，此处为事件原始摘要拼接）。"),
        "themes": [],
        "events": ev_out,
        "source": "fallback",
    }


def _weekly_event_card(ev: dict) -> str:
    """周报单条事件卡：标的 + 标题 + 影响解读 + 方向徽章。"""
    import html as _html

    symbol = _html.escape(str(ev.get("symbol") or "—"))
    headline = _html.escape(str(ev.get("headline") or ""))
    impact = _html.escape(str(ev.get("impact") or ""))
    direction = str(ev.get("direction") or "neutral")
    dir_cn = _SENTIMENT_CN.get(direction, "中性")
    dir_color = _SENTIMENT_COLOR.get(direction, "#6b7280")

    return f"""
    <div style="border:1px solid #eef2f7;border-left:3px solid {dir_color};border-radius:8px;
                padding:12px 14px;margin-bottom:10px;background:#fff">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
        <div style="font-size:13px;font-weight:800;color:#111827;line-height:1.5">
          <span style="color:#7c3aed">{symbol}</span>
          <span style="font-weight:600"> · {headline}</span>
        </div>
        <span style="flex:0 0 auto;background:{dir_color};color:#fff;padding:2px 9px;
                     border-radius:20px;font-size:11px;font-weight:700">{dir_cn}</span>
      </div>
      <div style="font-size:12px;color:#374151;margin-top:6px;line-height:1.7">{impact}</div>
    </div>
    """


def _build_weekly_report_html(stats: dict, narrative: dict, window_label: str) -> str:
    """构建催化剂周报邮件 HTML（本周总述 + 主线主题 + 大事记与影响 + 概览附录）。"""
    import html as _html

    total = stats.get("signals_total", 0)
    catalysts_new = stats.get("catalysts_new", 0)
    themes = narrative.get("themes") or []
    events = narrative.get("events") or []
    overview = narrative.get("overview") or ""
    is_llm = narrative.get("source") == "llm"

    theme_blocks = ""
    for t in themes:
        chips = "".join(
            f'<span style="display:inline-block;background:#eef2ff;color:#4338ca;'
            f'border-radius:10px;padding:1px 8px;font-size:11px;margin:2px 4px 2px 0">'
            f'{_html.escape(s)}</span>' for s in (t.get("symbols") or [])
        )
        theme_blocks += f"""
        <div style="border:1px solid #eef2f7;border-radius:8px;padding:12px 14px;
                    margin-bottom:10px;background:#fafaff">
          <div style="font-size:13px;font-weight:800;color:#4338ca">{_html.escape(t.get("name") or "")}</div>
          <div style="font-size:12px;color:#374151;margin-top:5px;line-height:1.7">{_html.escape(t.get("summary") or "")}</div>
          <div style="margin-top:6px">{chips}</div>
        </div>
        """
    if not theme_blocks:
        theme_blocks = '<div style="color:#9ca3af;font-size:12px">本周无显著主线主题</div>'

    event_blocks = "".join(_weekly_event_card(e) for e in events)
    if not event_blocks:
        event_blocks = ('<div style="padding:16px;text-align:center;color:#9ca3af;background:#f9fafb;'
                        'border-radius:8px">本周无重要催化剂事件</div>')

    overview_block = (
        f'<div style="font-size:13px;color:#1f2937;line-height:1.8;background:#f8fafc;'
        f'border-radius:8px;padding:14px 16px">{_html.escape(overview)}</div>'
        if overview else ""
    )

    def _bar_rows(pairs) -> str:
        if not pairs:
            return '<div style="color:#9ca3af;font-size:12px">本周无数据</div>'
        mx = max(c for _, c in pairs) or 1
        rows = []
        for k, c in pairs:
            pct = c / mx * 100
            rows.append(
                '<div style="display:flex;align-items:center;gap:8px;margin:3px 0">'
                f'<div style="flex:0 0 110px;font-size:11px;color:#6b7280;text-align:right">'
                f'{_html.escape(str(k))}</div>'
                '<div style="flex:1;background:#eef2f7;border-radius:4px;height:12px">'
                f'<div style="height:12px;background:linear-gradient(90deg,#7c3aed,#3b82f6);'
                f'border-radius:4px;width:{pct:.0f}%"></div></div>'
                f'<div style="flex:0 0 32px;font-size:11px;color:#111827;font-weight:600">{c}</div>'
                "</div>"
            )
        return "".join(rows)

    src_note = ("叙事由 AI 生成" if is_llm
                else "AI 叙事不可用，已回退为事件原始摘要拼接")

    return f"""
    <div style="font-family:sans-serif;max-width:760px;margin:auto;padding:16px;background:#f3f4f6">
      <div style="background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;padding:24px;border-radius:12px">
        <div style="font-size:12px;opacity:.7;text-transform:uppercase;letter-spacing:1px">催化剂决策管道 · 周报</div>
        <div style="font-size:24px;font-weight:700;margin-top:8px">本周重要催化剂事件与影响</div>
        <div style="margin-top:4px;font-size:13px;opacity:.8">窗口 {_html.escape(window_label)} · 重要事件 {len(events)} 条 · 信号 {total} 条 · 新入库催化剂 {catalysts_new} 条</div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 12px 12px">
        <h3 style="font-size:15px;margin:0 0 10px;color:#111827">📝 本周总述</h3>
        {overview_block}

        <h3 style="font-size:15px;margin:22px 0 10px;color:#111827">🧭 主线主题</h3>
        {theme_blocks}

        <h3 style="font-size:15px;margin:22px 0 10px;color:#111827">📌 大事记与影响</h3>
        {event_blocks}

        <h3 style="font-size:14px;margin:26px 0 8px;color:#6b7280">📊 本周概览（附录）</h3>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px">
          {_plan_cell("信号总数", str(total), "#7c3aed")}
          {_plan_cell("催化剂入库", str(catalysts_new), "#3b82f6")}
          {_plan_cell("重要事件", str(len(events)), "#059669")}
        </div>
        <div style="margin:12px 0 4px;font-size:11px;font-weight:700;color:#6b7280">事件类型分布</div>
        {_bar_rows(stats.get("event_type_dist", []))}
        <div style="margin:12px 0 4px;font-size:11px;font-weight:700;color:#6b7280">Tier 分布</div>
        {_bar_rows(stats.get("tier_dist", []))}

        <div style="margin-top:20px;font-size:11px;color:#9ca3af;text-align:center">
          {src_note} · 由催化剂决策管道自动生成 · 自然周去重 · 每周一 09:00 发送<br>
          本邮件为研究性事件回顾，非投资建议。
        </div>
      </div>
    </div>
    """


def _weekly_report_already_sent(conn, start_utc, end_utc) -> bool:
    """本周（自然周窗口内）是否已成功发送过周报。"""
    row = conn.execute("""
        SELECT 1 FROM biz.catalyst_notification_log
        WHERE notification_type = %s AND status = 'sent'
          AND sent_at >= %s AND sent_at < %s
        LIMIT 1
    """, (NTYPE_WEEKLY_REPORT, start_utc, end_utc)).fetchone()
    return row is not None


def send_catalyst_weekly_report(conn, end_ts=None, dry_run: bool = False) -> dict:
    """发送催化剂周报（本周重要事件总结 + 影响解读）。

    Args:
        conn: 数据库连接
        end_ts: 窗口计算参考时刻（tz-aware），缺省用当前时刻。
        dry_run: 仅查询/生成叙事，不发信、不占去重位。

    Returns:
        dict: {sent, skipped, failed, reason, window, event_count,
               signals_total, narrative_source, body}
    """
    try:
        ensure_notification_table(conn)
    except Exception as e:
        logger.warning("确保通知表存在失败: %s", e)

    def _ret(sent=0, skipped=0, failed=0, reason="", window=None,
             event_count=0, signals_total=0, narrative_source=None, body=None):
        return {"sent": sent, "skipped": skipped, "failed": failed, "reason": reason,
                "window": window, "event_count": event_count,
                "signals_total": signals_total, "narrative_source": narrative_source,
                "body": body}

    try:
        start_utc, end_utc, window_label = _weekly_window(end_ts)
    except Exception as e:
        logger.warning("周报窗口计算失败: %s", e, exc_info=True)
        return _ret(reason=f"窗口计算失败: {e}")

    # 自然周去重：本周已发过即跳过（dry-run 不参与）
    if not dry_run:
        try:
            if _weekly_report_already_sent(conn, start_utc, end_utc):
                return _ret(skipped=1, window=window_label,
                            reason=f"本周（{window_label}）周报已发送过，跳过")
        except Exception as e:
            logger.warning("周报去重预检失败: %s", e, exc_info=True)

    try:
        stats = _weekly_overview_stats(conn, start_utc, end_utc)
        events = _weekly_key_events(conn, start_utc, end_utc)
    except Exception as e:
        logger.warning("周报统计/清单查询失败: %s", e, exc_info=True)
        return _ret(window=window_label, reason=f"查询失败: {e}")

    # 叙事：优先 LLM，失败回退模板拼装（不阻断发信）
    narrative = _weekly_llm_narrative(events, stats, window_label)
    if narrative is None:
        narrative = _weekly_fallback_narrative(events)

    subject = f"📊 催化剂周报 · {window_label} · 重要事件 {len(narrative.get('events') or [])} 条"

    if dry_run:
        try:
            body = _build_weekly_report_html(stats, narrative, window_label)
        except Exception as e:
            logger.warning("周报渲染失败: %s", e, exc_info=True)
            return _ret(failed=1, window=window_label, reason=f"渲染失败: {e}")
        return _ret(window=window_label, event_count=len(events),
                    signals_total=stats.get("signals_total", 0),
                    narrative_source=narrative.get("source"),
                    reason=f"[DRY-RUN] {subject}", body=body)

    # 原子占锁（防并发/重复触发）
    if not _try_acquire_send_lock(conn, SENTINEL_WEEKLY_REPORT_SIGNAL_ID,
                                  NTYPE_WEEKLY_REPORT, None, subject):
        return _ret(skipped=1, window=window_label,
                    reason="发送锁未获取（可能已在发送中），跳过",
                    event_count=len(events),
                    signals_total=stats.get("signals_total", 0))

    try:
        body = _build_weekly_report_html(stats, narrative, window_label)
    except Exception as e:
        logger.warning("周报渲染失败: %s", e, exc_info=True)
        return _ret(failed=1, window=window_label, reason=f"渲染失败: {e}",
                    event_count=len(events),
                    signals_total=stats.get("signals_total", 0))

    ok, msg = _send_email(subject, body)
    _mark_sent(conn, SENTINEL_WEEKLY_REPORT_SIGNAL_ID, NTYPE_WEEKLY_REPORT,
               None, subject, status="sent" if ok else "failed",
               error_msg=None if ok else msg)

    return _ret(sent=1 if ok else 0, failed=0 if ok else 1, reason=msg,
                window=window_label, event_count=len(events),
                signals_total=stats.get("signals_total", 0),
                narrative_source=narrative.get("source"))



