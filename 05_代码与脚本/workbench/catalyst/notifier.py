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
from datetime import datetime, timedelta, timezone
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
        if v >= 1000:
            return f"${v:,.2f}"
        if v >= 1:
            return f"${v:.2f}"
        if v >= 0.01:
            return f"${v:.4f}"
        if v >= 1e-4:
            return f"${v:.6f}"
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
    - 无 A 级信号 → 发极简「空窗 note」（决策①：女王接受空窗，写明原因，避免通道静默死掉）
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

    if not rows:
        # 无 A 级信号 → 静默跳过，不发空窗邮件
        return {"sent": 0, "skipped": 1, "failed": 0,
                "reason": f"{label} 24h 内无 A 级高置信度信号，静默跳过",
                "new_signals_24h": 0}
    else:
        subject = f"🎯 催化剂 Alert·{label}·A级 {new_count} 条"
        body = _build_slow_digest_html(rows, stats, class_label=label)

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


def _recent_new_a_signals(conn, hours: int = 24, asset_class: str = "crypto") -> list[dict]:
    """过去 N 小时内 A 级新信号（按资产类别，按资产去重留最高分，取前 2）。

    入选条件（OPT-CATALYST-ALERT-001 P0-1/P0-2 语义闸门，d3 修订）：
    - tier = 'A'（composite_score → tier 单点真源不变，DB tier 不改）
    - status = 'open'（d3：open 已表示「价格未充分定价」，即真正的可动作集合。
      原先额外要求 resonance_state='confirmed' 是反向的——实测 confirmed 的
      72h 前瞻超额 -2.16%（n=47）远弱于 weak +1.58%（n=231），
      即「等价格确认再开单」等于追高；confirmed 现已归入观察池 status='watch'）
    - entry/stop/tp 齐全（可交易性）
    - 近 DEDUP_WINDOW_HOURS 内**未被快讯发过**（pre_alert_sent_at 判据）——
      跨通道去重，digest 仅作快讯的兜底（诊断_催化剂A级邮件延迟链路_XRP_BCH_2026-09-24）
    composite_score DESC 取前 2 条（每日 1~2 idea）。

    返回字段覆盖「决策链 G0-G7 + 代币快照」全量：signal 全维度 + catalyst 原文
    + grade/impact/resonance 分项 + core.asset 基本面 + 最新日线 + 在池信号计数，
    供 _build_a_alert_card 一次渲染，邮件不依赖外链网页。
    """
    filter_sql = ASSET_NAME_FILTER_SQL if asset_class == "crypto" else IS_STOCK_SQL
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
              AND s.tier = 'A'
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
    """, (hours, DEDUP_WINDOW_HOURS)).fetchall()


# =====================================================================
# 重大事件通道（重要性闸门，独立于 tier 的可交易性闸门）
# =====================================================================
#
# 背景（2026-09-23 排查）：A 级 Alert 的入选口径是 `tier='A' AND status='open'`，
# 而 tier 同时承担了「重要性」与「可交易性」两件事——价格档位不齐、RR 不足、
# 方向不符都会把 A/B 封顶到 C。于是「CME 将于 10/19 上线 BCH 与 UNI 期货」这类
# 重大利好因为给不出可交易档位而完全静默（实测 tier='A' AND status='open' 全表 0 行）。
#
# 本通道把「重要性」单独拎出来：判据只读 catalyst_grade 的原始字段 + 事件前的
# 价格异动，不要求 entry/stop/tp，因此不会因为「给不出交易计划」而漏报。
# 反向约束同样重要——该判据近 6 天实测命中 9 条（≈1.3 条/天），既捞得到 BCH 那条，
# 也不会把「24h 涨幅播报」这类负 alpha 内容捞进来。
#
# 邮件刻意不出现任何交易档位，并显式标注「非交易建议」，避免被读成开单指令。

NTYPE_MAJOR_EVENT = "major_event"     # 重大事件通道（与 A 级 Alert 分开渲染/去重）

MAJOR_EVENT_MIN_PRELAUNCH_RET = 5.0   # 事件前 24h 已异动 ≥5%：市场已确认事件有效
MAJOR_EVENT_COOLDOWN_HOURS = 24       # 同一资产 24h 内只发一次（事件级去重）
MAJOR_EVENT_KINDS = ("structural", "event")
MAJOR_EVENT_MAX_PER_RUN = 3           # 单轮上限，配合「日均 ≤3 条」目标


def _recent_major_events(conn, hours: int = 24,
                         limit: int = MAJOR_EVENT_MAX_PER_RUN) -> list[dict]:
    """过去 N 小时发布的「重大事件」候选（每资产留最高分一条）。

    入选条件（缺一不可）：
    - `tier IN ('A','B')` 且 `status='open'`：合成分 ≥60，且未被方向闸门
      /价格档位闸门压到 C（即方向为多头、未被判为已充分定价）
    - `catalyst_kind IN ('structural','event')`：排除情绪稿与噪音
    - `prelaunch_ret_24h >= 5` 且 `prelaunch_penalty = 0`：事件前 24h 市场已异动，
      且该异动未被计入降权（避免通报已经涨完的事件）
    - `ai_event_type <> 'market_update'`：排除纯行情播报（负 alpha 类别）
    - 事件发布时间在 N 小时内：只通报新鲜事件，避免长期停摆后补发陈旧事件
    - 同一资产 N 小时内已发过 major_event 则跳过：一条新闻常被多家媒体重复采集
      （实测 BCH 那条来自 4 家媒体、5 条 catalyst），事件级去重后只发一封
    - 额外 LEFT JOIN LATERAL 取 `biz.catalyst_second_order` 的二阶标的（只消费、不生成），
      供邮件「板块联动」模块渲染
    """
    return conn.execute(f"""
        SELECT * FROM (
            SELECT DISTINCT ON (s.asset_id)
                   s.signal_id, s.catalyst_id, s.asset_id,
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
                   so.second_order_confidence, so.second_order_count
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
            WHERE s.tier IN ('A', 'B')
              AND s.status = 'open'
              AND cg.catalyst_kind = ANY(%s::TEXT[])
              AND cg.prelaunch_ret_24h >= %s
              AND cg.prelaunch_penalty = 0
              AND COALESCE(ac.ai_event_type, '') <> 'market_update'
              AND ac.published_at > NOW() - (%s::int * INTERVAL '1 hour')
              AND {ASSET_NAME_FILTER_SQL}
              AND NOT EXISTS (
                  SELECT 1
                  FROM biz.catalyst_notification_log nl
                  JOIN biz.catalyst_signal ns ON ns.signal_id = nl.signal_id
                  WHERE ns.asset_id = s.asset_id
                    AND nl.notification_type = %s
                    AND nl.status = 'sent'
                    AND nl.sent_at > NOW() - (%s::int * INTERVAL '1 hour')
              )
            ORDER BY s.asset_id, s.composite_score DESC
        ) t
        ORDER BY t.composite_score DESC
        LIMIT %s
    """, (list(MAJOR_EVENT_KINDS), MAJOR_EVENT_MIN_PRELAUNCH_RET, hours,
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

    Returns:
        (level, label, confidence_cn)，level ∈ {'direct','indirect'}
    """
    text = " ".join(
        str(x) for x in (
            r.get("catalyst_title"), r.get("title_cn"), r.get("ai_summary"),
        ) if x
    )
    sym_hit, sym_carrier = _mentions_token(text, (r.get("symbol") or "").strip())
    name_hit, name_carrier = _mentions_token(text, (r.get("canonical_name") or "").strip())
    self_mentioned = (sym_hit and not sym_carrier) or (name_hit and not name_carrier)
    has_action = any(k in text for k in _DIRECT_ACTION_KEYWORDS)
    if self_mentioned and has_action:
        return "direct", "直接利好标的", "高"
    return "indirect", "生态间接受益", "中"


def _transmission_path(r: dict) -> str:
    for key in (r.get("ai_event_type"), r.get("rule_event_type"), r.get("catalyst_kind")):
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
    dir_color = "#b45309" if _dir_level == "direct" else "#4b5563"
    path_txt = _transmission_path(r)
    tl_immediate, tl_short, tl_mid = _transmission_timeline(r)
    res_note = _RESONANCE_NOTE.get(r.get("resonance_state"), "")

    if pre is not None:
        consume_note = (
            f'公告前 24h 已涨 {pre_txt} —— 说明部分预期已被市场提前消化，非“零成本”；'
            f'{res_note}。' if res_note else
            f'公告前 24h 已涨 {pre_txt} —— 说明部分预期已被市场提前消化，非“零成本”。'
        )
    else:
        consume_note = "公告前 24h 无异动数据，预期消化度暂无法判定。"

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
    so_sector = (r.get("second_order_sector") or "").strip()
    so_block = f'''<div style="background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:14px">
    <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:6px">板块联动</div>
    <div style="font-size:13px;line-height:1.7;color:#374151">
      同「{so_sector or "相关"}」赛道的 {'、'.join(so_syms)} 或受带动（置信度中等）；
      此为二阶传导映射，非直接建议。
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
      发布：{_fmt_ts(r.get('published_at'))} · 来源 {r.get('source_code') or '—'}
      · 事件类别 {r.get('ai_event_type') or r.get('rule_event_type') or '—'}
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
      {_kv('市场确认', f'事件前 24h 已异动 {pre_txt}', _pct_color(pre))}
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
           + _kv("信息源", _html.escape(str(r.get("source_code") or "—")))
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


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        if f >= 1000:
            return f"{f:,.1f}"
        if f >= 1:
            return f"{f:,.4f}"
        if f >= 1e-4:
            return f"{f:.8f}"
        # 极小价（meme 常见 1e-12）：固定 8 位小数会被截断成 0.00000000，
        # 让「现价/MA20」看起来像 0（审计 2026-09-22 P0 的显示根因）→ 改科学计数。
        return f"{f:.4e}"
    except (TypeError, ValueError):
        return "—"


def _build_slow_digest_html(a_rows, stats: dict,
                            class_label: str = "加密货币") -> str:
    """构建 A 级 Alert 邮件 HTML（OPT-CATALYST-ALERT-001 改版）。

    Args:
        a_rows: 24h 内 A 级去重新信号（最多 2 条，未充分定价 + 完整交易档位）
        stats: 慢通道统计（second_order_count / expired_count）
        class_label: 资产类别中文标签（加密货币 / 美股·商品）

    每条卡片在邮件内完整展开「交易计划 + 催化剂原文 + 决策链 G0-G7 + 代币快照」，
    不依赖外链网页（用户口径：拿到邮件即看到整个决策过程与代币全部信息）。
    """
    so_count = stats.get("second_order_count", 0)
    expired = stats.get("expired_count", 0)

    cards = "".join(_build_a_alert_card(r) for r in a_rows)
    if not cards:
        cards = ('<div style="padding:16px;text-align:center;color:#9ca3af;background:#f9fafb;'
                 'border-radius:8px">过去 24h 无 A 级新信号</div>')

    return f"""
    <div style="font-family:sans-serif;max-width:760px;margin:auto;padding:16px;background:#f3f4f6">
      <div style="background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;padding:24px;border-radius:12px">
        <div style="font-size:12px;opacity:.7;text-transform:uppercase;letter-spacing:1px">催化剂决策管道 · {class_label} · A 级 Alert</div>
        <div style="font-size:24px;font-weight:700;margin-top:8px">{class_label} A级 新增 {len(a_rows)} 条可交易信号</div>
        <div style="margin-top:4px;font-size:13px;opacity:.8">24h 窗口 · 未充分定价(weak)可动作池 · 二阶受益 {so_count} 条 · 过期 {expired} 条</div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 12px 12px">
        <h3 style="font-size:15px;margin:0 0 12px;color:#111827">🟣 A 级信号（过去 24h 新增 · 未充分定价可动作 + 完整交易档位）</h3>
        {cards}

        <div style="margin-top:20px;padding:12px;background:#f0f9ff;border-radius:8px;font-size:12px;color:#0369a1">
          💡 B/C 级热点已下沉至每日早报「📡 催化剂热点」观察区；本邮件仅保留高置信度 A 级 idea。
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



