"""
催化剂 G6 信号层（P0 MVP 骨架）。

核心设计原则：
- composite_score 是 tier 的唯一来源（口径单点真源）
- 下游（API/前端/早报/看板）只能读 tier 和 composite_score，禁止自己算
- P0 阶段 G4（基本面）和 G5（技术面）用占位值，P1 填充

综合分计算：
    composite_score = kind_strength * 0.25
                    + resonance   * 0.30
                    + persistence * 0.15
                    + fundamental * 0.15
                    + technical   * 0.15

tier 阈值：A≥80 / B≥60 / C≥40
<40 不写入 signal 表（仅 grade/resonance 留底供回测）

信号生命周期（d3 分层，2026-09-18）：
    status = 价格定价程度（由 resonance_state 映射，见 signal_actionability 配置）
        confirmed（价格已同向反应≥5% 且放量）→ watch  观察池，不推送
        weak     （价格温和/未充分反应）      → open   可动作
        divergent（价格方向与催化剂背离）      → invalid 剔除
        pending  （未反应）                   → watch  观察
    实测依据（biz.catalyst_outcome，收益自信号生成时刻起算）：
        confirmed 72h 超额 -2.16%（n=47）远弱于 weak +1.58%（n=231）
        —— 事件一旦被价格确认即已定价，此时开单等于追高为负期望。
    状态迁移：expired/done 为终态冻结；其余行每轮重算跟随当前 resonance_state
        （watch→open 晋升 = 价格行为确认；open→watch 回退 = 已定价不再可动作）
    expires_at = published_at + expiry_days（按 kind 差异化）
    慢通道巡检：expires_at < NOW() AND status IN ('open','watch') → status='expired'

方向闸门（d6，2026-09-21）：
    本系统档位是「做多」口径，催化方向直接决定可否作为做多机会：
        bearish → status='invalid'（利空不产做多机会；tier/composite 保留供回测）
        其余（neutral / 方向缺失 / 未知值）→ tier 封顶 C
    即「只有显式 bullish 才保留 A/B」——方向未知时同样不占 A/B 推送位。
    口径依据：近 14 天实测 bearish 有 9 条 A / 101 条 B、neutral 有 41 条 A /
        412 条 B 被公式合成进入 A 级 Alert——方向未参与档位判定。
    方向缺失一并封顶的依据：实测非终态 45 条无方向行中 8 条进 B，样本集中在
        regulation / other /「24h 涨幅播报」等实测负 alpha 类别；这类行方向永缺
        （catalyst_impact 与 ai_sentiment 均为 NULL），不会被后续 AI 结果修正。
        注：「创建时无方向、AI 方向后到」的路径已由 run_slow_g3g5 覆盖——它的
        候选集是「G3-G5 缺失」，快通道产出的信号必然被重算一次并带入当时的方向。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


def _prices_valid(entry, stop, tp) -> bool:
    """entry/stop/tp 三者是否均为**正数**（None / 0 / 负数 / 非数值都视为无效）。

    审计 2026-09-22 P0：COPPER 三个档位全 0，rr 无法计算（`if entry and ...` 对 0 短路），
    RR 闸门因此不触发，信号仍以 86 分进 A 级「可交易」。价格缺失必须硬拦截。
    """
    for v in (entry, stop, tp):
        try:
            if v is None or float(v) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


@dataclass
class CatalystSignalResult:
    catalyst_id: int
    asset_id: int
    # G1
    kind: Optional[str] = None
    base_strength: int = 0
    # G2
    resonance_score: int = 0
    resonance_state: Optional[str] = None
    # G3（P0 仅预判值）
    persistence: Optional[str] = None
    persistence_verified: bool = False
    # G4（P0 占位）
    fundamental_pass: Optional[bool] = None
    # G5（P0 占位）
    technical_state: Optional[str] = None
    # G6
    composite_score: int = 0
    tier: Optional[str] = None        # A / B / C / None（<40）
    confidence: float = 0.0
    regime: Optional[str] = None
    invalidation: Optional[str] = None
    expires_at: Optional[datetime] = None
    status: str = "open"

    # 入场/止损/止盈（P0 占位，P1 G5 填充）
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    rr_ratio: Optional[float] = None

    # G7 AI 决策（慢通道 LLM 增强）
    ai_reason: Optional[str] = None
    investment_cycle: Optional[str] = None


class CatalystSignalBuilder:
    """G6 信号构建器。

    把 G1-G5 的结果加权汇总，产出 composite_score 和 tier。
    composite_score 是 tier 的唯一来源（口径单点真源）。
    """

    def __init__(self, config: dict):
        cw = config.get("composite_score_weights", {})
        self.w_kind = cw.get("kind_strength", 0.25)
        self.w_resonance = cw.get("resonance", 0.30)
        self.w_persistence = cw.get("persistence", 0.15)
        self.w_fundamental = cw.get("fundamental", 0.15)
        self.w_technical = cw.get("technical", 0.15)

        # 各维度得分映射表
        self.persistence_scores = config.get("persistence_scores", {
            "structural": 90, "one_off": 60, "decaying": 30,
        })
        self.fundamental_scores = config.get("fundamental_scores", {
            "pass": 85, "fail": 20, "unknown": 50,
        })
        self.technical_scores = config.get("technical_scores", {
            "up": 85, "range": 55, "down": 25, "unknown": 50,
        })

        # tier 阈值
        tt = config.get("tier_thresholds", {})
        self.tier_a = tt.get("A", 80)
        self.tier_b = tt.get("B", 60)
        self.tier_c = tt.get("C", 40)

        # 过期天数
        self.expiry_days = config.get("signal_expiry_days", {
            "structural": 7, "event": 3, "sentiment": 1, "noise": 1,
        })

        self.min_rr = config.get("min_rr_ratio", 2.0)

        # 动作闸门：resonance_state → 生命周期状态（d3 信号分层，依据见模块 docstring）
        self.actionability = config.get("signal_actionability", {
            "confirmed": "watch",
            "weak": "open",
            "divergent": "invalid",
            "pending": "watch",
        })

    # ---- 公开入口 ----

    def build(self,
              catalyst_id: int,
              asset_id: int,
              kind: str,
              base_strength: int,
              resonance_score: int,
              resonance_state: str,
              published_at: datetime,
              persistence: Optional[str] = None,
              fundamental_pass: Optional[bool] = None,
              technical_state: Optional[str] = None,
              regime: Optional[str] = None,
              entry_price: Optional[float] = None,
              stop_loss: Optional[float] = None,
              take_profit: Optional[float] = None,
              ai_reason: Optional[str] = None,
              investment_cycle: Optional[str] = None,
              impact_direction: Optional[str] = None,
              ) -> CatalystSignalResult:
        """构建一条信号。

        Returns:
            CatalystSignalResult，如果 composite_score < tier_c 则 tier=None
        """
        # 各维度得分（缺失的给中性分）
        kind_score = base_strength  # G1 的 base_strength 已经是 0-100
        res_score = resonance_score
        pers_score = self._persistence_score(persistence) if persistence else 50  # P0 未算给 50
        fund_score = self._fundamental_score(fundamental_pass)
        tech_score = self._technical_score(technical_state)

        # 加权综合分
        raw = (
            kind_score * self.w_kind
            + res_score * self.w_resonance
            + pers_score * self.w_persistence
            + fund_score * self.w_fundamental
            + tech_score * self.w_technical
        )
        composite_score = max(0, min(100, round(raw)))

        # tier（唯一入口，禁止在其他地方复算）
        tier = self._score_to_tier(composite_score)

        # 置信度（简化：composite_score / 100，P0 粗略）
        confidence = round(composite_score / 100, 3)

        # R:R（如果有价格数据）
        rr_ratio = None
        if entry_price and stop_loss and take_profit and entry_price != stop_loss:
            rr_ratio = round(abs((take_profit - entry_price) / (entry_price - stop_loss)), 2)

        # R:R 不达标 → 不入库（tier=None，upsert_to_db 会跳过）
        # 依据工单 §5 G6 / §10：composite_score 是 tier 唯一来源（单点真源）。
        # 不做"只降 tier 不改分"的二次判定，避免产生 composite_score/tier 不一致行。
        if rr_ratio is not None and rr_ratio < self.min_rr:
            tier = None

        # 过期时间
        expiry_days = self.expiry_days.get(kind, 3)
        expires_at = published_at + timedelta(days=expiry_days)

        # 失效条件描述（P0 简单生成）
        invalidation = self._build_invalidation(resonance_state, kind, stop_loss)

        # 生命周期状态：由价格定价程度决定（d3 分层），而非一律 open
        status = self._initial_status(resonance_state)

        # 方向闸门（d6）：本系统为「做多」口径，方向决定可否作为做多机会
        #   bearish → invalid（利空不产做多机会；tier/composite 保留供回测）
        #   其余（neutral / 方向缺失 / 未知值）→ tier 封顶 C
        # 只有显式 bullish 保留 A/B；方向未知时不占 A/B 推送位。
        # 与 RR 闸门同属「只降级不改分」的显式例外，口径见模块 docstring。
        direction = (impact_direction or "").strip().lower()
        if direction == "bearish":
            status = "invalid"
        elif direction != "bullish" and tier in ("A", "B"):
            tier = "C"

        # 价格/档位完整性闸门（审计 2026-09-22 P0）：entry/stop/tp 缺失或非正 ⇒
        # 无有效交易档位，**不得**作为 A/B 可交易信号（COPPER 三档全 0 仍评 A 的根因）。
        # 与 RR / 方向闸门同属「只降级不改分」的显式例外：tier 封顶 C、动作降为观察。
        if not _prices_valid(entry_price, stop_loss, take_profit):
            if tier in ("A", "B"):
                tier = "C"
            if status == "open":
                status = "watch"

        return CatalystSignalResult(
            catalyst_id=catalyst_id,
            asset_id=asset_id,
            kind=kind,
            base_strength=base_strength,
            resonance_score=resonance_score,
            resonance_state=resonance_state,
            persistence=persistence,
            persistence_verified=False,
            fundamental_pass=fundamental_pass,
            technical_state=technical_state,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rr_ratio=rr_ratio,
            ai_reason=ai_reason,
            investment_cycle=investment_cycle,
            composite_score=composite_score,
            tier=tier,
            confidence=confidence,
            regime=regime,
            invalidation=invalidation,
            expires_at=expires_at,
            status=status,
        )

    def upsert_to_db(self, conn, signal: CatalystSignalResult) -> Optional[tuple[int, bool, bool]]:
        """写入 catalyst_signal 表。
        如果 tier 为 None（分数太低），不写入（返回 None）。

        状态迁移（d3 分层）：
            expired / done 为终态，冻结不改；
            其余行一律跟随本轮由 resonance_state 推出的 status，
            因此 watch→open（价格行为确认）可自动晋升，open→watch（已定价）可自动回退。

        Returns:
            tuple[int, bool, bool] | None: (signal_id, is_new_insert, became_open)
                is_new_insert=True 表示本次是新插入，False 表示是更新已有记录
                became_open=True 表示本轮该信号的 status 由非 open 变为 open
                （新插入即 open，或 watch→open 晋升），是本轮唯一需要推送/告警的集合
                失败返回 None
        """
        if signal.tier is None:
            return None

        # 先取旧状态：状态迁移基线（读不加锁，唯一键 (catalyst_id, asset_id) 命中索引）
        prev = conn.execute(
            """
            SELECT status FROM biz.catalyst_signal
            WHERE catalyst_id = %s AND asset_id = %s
            """,
            (signal.catalyst_id, signal.asset_id),
        ).fetchone()
        prev_status = prev["status"] if prev else None

        row = conn.execute(
            """
            INSERT INTO biz.catalyst_signal (
                catalyst_id, asset_id,
                kind, base_strength,
                resonance_score, resonance_state,
                persistence, persistence_verified,
                fundamental_pass,
                technical_state,
                entry_price, stop_loss, take_profit, rr_ratio,
                composite_score, tier, confidence,
                regime, invalidation,
                ai_reason, investment_cycle,
                expires_at, status
            ) VALUES (
                %(catalyst_id)s, %(asset_id)s,
                %(kind)s, %(base_strength)s,
                %(resonance_score)s, %(resonance_state)s,
                %(persistence)s, %(persistence_verified)s,
                %(fundamental_pass)s,
                %(technical_state)s,
                %(entry_price)s, %(stop_loss)s, %(take_profit)s, %(rr_ratio)s,
                %(composite_score)s, %(tier)s, %(confidence)s,
                %(regime)s, %(invalidation)s,
                %(ai_reason)s, %(investment_cycle)s,
                %(expires_at)s, %(status)s
            )
            ON CONFLICT (catalyst_id, asset_id) DO UPDATE SET
                kind = EXCLUDED.kind,
                base_strength = EXCLUDED.base_strength,
                resonance_score = EXCLUDED.resonance_score,
                resonance_state = EXCLUDED.resonance_state,
                persistence = EXCLUDED.persistence,
                persistence_verified = EXCLUDED.persistence_verified,
                -- G4/G5 由慢通道（run_slow_g3g5）计算并落库；快通道全量刷新时不带这两项
                -- （传 None 占位）。必须 COALESCE 保留库内值，否则每轮快扫都会把
                -- 慢通道刚算出的基本面/技术面结果抹成 NULL，composite_score 永久回落到
                -- 占位分（fundamental/technical 各 50），G4/G5 全链路沦为死代码。
                fundamental_pass = COALESCE(EXCLUDED.fundamental_pass,
                                            biz.catalyst_signal.fundamental_pass),
                technical_state = COALESCE(EXCLUDED.technical_state,
                                           biz.catalyst_signal.technical_state),
                entry_price = COALESCE(EXCLUDED.entry_price, biz.catalyst_signal.entry_price),
                stop_loss = COALESCE(EXCLUDED.stop_loss, biz.catalyst_signal.stop_loss),
                take_profit = COALESCE(EXCLUDED.take_profit, biz.catalyst_signal.take_profit),
                rr_ratio = COALESCE(EXCLUDED.rr_ratio, biz.catalyst_signal.rr_ratio),
                composite_score = EXCLUDED.composite_score,
                tier = EXCLUDED.tier,
                confidence = EXCLUDED.confidence,
                regime = COALESCE(EXCLUDED.regime, biz.catalyst_signal.regime),
                invalidation = COALESCE(EXCLUDED.invalidation, biz.catalyst_signal.invalidation),
                ai_reason = COALESCE(EXCLUDED.ai_reason, biz.catalyst_signal.ai_reason),
                investment_cycle = COALESCE(
                    EXCLUDED.investment_cycle, biz.catalyst_signal.investment_cycle),
                expires_at = EXCLUDED.expires_at,
                -- d3：expired/done 终态冻结，其余行跟随本轮的定价闸门
                -- （这样才能 watch↔open 双向迁移；旧规则 open 单向锁定会让
                --   「已定价」信号永远滞留在可动作集合里）
                status = CASE
                    WHEN biz.catalyst_signal.status IN ('expired', 'done')
                        THEN biz.catalyst_signal.status
                    ELSE EXCLUDED.status
                END,
                updated_at = NOW()
            RETURNING signal_id, status, (xmax = 0) AS is_new_insert
            """,
            {
                "catalyst_id": signal.catalyst_id,
                "asset_id": signal.asset_id,
                "kind": signal.kind,
                "base_strength": signal.base_strength,
                "resonance_score": signal.resonance_score,
                "resonance_state": signal.resonance_state,
                "persistence": signal.persistence,
                "persistence_verified": signal.persistence_verified,
                "fundamental_pass": signal.fundamental_pass,
                "technical_state": signal.technical_state,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "rr_ratio": signal.rr_ratio,
                "composite_score": signal.composite_score,
                "tier": signal.tier,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "invalidation": signal.invalidation,
                "ai_reason": signal.ai_reason,
                "investment_cycle": signal.investment_cycle,
                "expires_at": signal.expires_at,
                "status": signal.status,
            },
        ).fetchone()
        if not row:
            return None
        new_status = row["status"]
        became_open = new_status == "open" and prev_status != "open"
        return row["signal_id"], bool(row["is_new_insert"]), became_open

    # ---- 内部方法 ----

    def _score_to_tier(self, score: int) -> Optional[str]:
        """综合分 → tier。唯一入口，禁止在他处复算。"""
        if score >= self.tier_a:
            return 'A'
        if score >= self.tier_b:
            return 'B'
        if score >= self.tier_c:
            return 'C'
        return None

    def _initial_status(self, resonance_state: str) -> str:
        """价格定价程度 → 初始生命周期状态（d3 分层）。

        口径：只在「价格尚未充分定价」时才可动作。
        未配置的 resonance_state 保守归入观察池，避免误推送。
        """
        return self.actionability.get(resonance_state, "watch")

    def _persistence_score(self, persistence: str) -> int:
        return self.persistence_scores.get(persistence, 50)

    def _fundamental_score(self, fundamental_pass: Optional[bool]) -> int:
        if fundamental_pass is None:
            return self.fundamental_scores.get("unknown", 50)
        if fundamental_pass:
            return self.fundamental_scores.get("pass", 85)
        return self.fundamental_scores.get("fail", 20)

    def _technical_score(self, technical_state: Optional[str]) -> int:
        if technical_state is None:
            return self.technical_scores.get("unknown", 50)
        return self.technical_scores.get(technical_state, 50)

    def _build_invalidation(self, resonance_state: str, kind: str,
                            stop_loss: Optional[float]) -> str:
        """构建失效条件描述（P0 简化版）。"""
        parts = []
        if stop_loss:
            parts.append(f"跌破止损价 {stop_loss}")
        if resonance_state == "divergent":
            parts.append("价格方向与催化剂背离持续扩大")
        if kind == "event":
            parts.append("事件落地后 3 天内无后续催化")
        if not parts:
            parts.append("催化剂逻辑证伪或事件过期")
        return "；".join(parts)


# =====================================================================
# 信号生命周期管理
# =====================================================================

def expire_signals(conn) -> int:
    """巡检过期信号：expires_at < NOW() 且未终结 → 'expired'。

    观察池（watch）同样需要过期，否则未晋升的观察信号会永久滞留。

    Returns:
        int: 过期处理数量
    """
    cur = conn.execute(
        """
        UPDATE biz.catalyst_signal
        SET status = 'expired', updated_at = NOW()
        WHERE status IN ('open', 'watch') AND expires_at < NOW()
        """
    )
    return cur.rowcount
