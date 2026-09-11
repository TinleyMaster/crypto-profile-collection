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

信号生命周期：
    expires_at = published_at + expiry_days（按 kind 差异化）
    慢通道巡检：expires_at < NOW() AND status='open' → status='expired'
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


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

        # R:R 不够的降级（降一档 tier）
        if rr_ratio is not None and rr_ratio < self.min_rr and tier and tier != 'C':
            if tier == 'A':
                tier = 'B'
            elif tier == 'B':
                tier = 'C'

        # 过期时间
        expiry_days = self.expiry_days.get(kind, 3)
        expires_at = published_at + timedelta(days=expiry_days)

        # 失效条件描述（P0 简单生成）
        invalidation = self._build_invalidation(resonance_state, kind, stop_loss)

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
            composite_score=composite_score,
            tier=tier,
            confidence=confidence,
            regime=regime,
            invalidation=invalidation,
            expires_at=expires_at,
            status="open",
        )

    def upsert_to_db(self, conn, signal: CatalystSignalResult) -> Optional[int]:
        """写入 catalyst_signal 表。
        如果 tier 为 None（分数太低），不写入（返回 None）。

        Returns:
            int: 写入/更新的 signal_id，失败返回 None
        """
        if signal.tier is None:
            return None

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
                %(expires_at)s, %(status)s
            )
            ON CONFLICT (catalyst_id, asset_id) DO UPDATE SET
                kind = EXCLUDED.kind,
                base_strength = EXCLUDED.base_strength,
                resonance_score = EXCLUDED.resonance_score,
                resonance_state = EXCLUDED.resonance_state,
                persistence = EXCLUDED.persistence,
                persistence_verified = EXCLUDED.persistence_verified,
                fundamental_pass = EXCLUDED.fundamental_pass,
                technical_state = EXCLUDED.technical_state,
                entry_price = COALESCE(EXCLUDED.entry_price, biz.catalyst_signal.entry_price),
                stop_loss = COALESCE(EXCLUDED.stop_loss, biz.catalyst_signal.stop_loss),
                take_profit = COALESCE(EXCLUDED.take_profit, biz.catalyst_signal.take_profit),
                rr_ratio = COALESCE(EXCLUDED.rr_ratio, biz.catalyst_signal.rr_ratio),
                composite_score = EXCLUDED.composite_score,
                tier = EXCLUDED.tier,
                confidence = EXCLUDED.confidence,
                regime = COALESCE(EXCLUDED.regime, biz.catalyst_signal.regime),
                invalidation = COALESCE(EXCLUDED.invalidation, biz.catalyst_signal.invalidation),
                expires_at = EXCLUDED.expires_at,
                status = CASE
                    WHEN biz.catalyst_signal.status = 'open' THEN EXCLUDED.status
                    ELSE biz.catalyst_signal.status
                END,
                updated_at = NOW()
            RETURNING signal_id
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
                "expires_at": signal.expires_at,
                "status": signal.status,
            },
        ).fetchone()
        return row["signal_id"] if row else None

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
    """巡检过期信号：expires_at < NOW() 且 status='open' → 'expired'。

    Returns:
        int: 过期处理数量
    """
    cur = conn.execute(
        """
        UPDATE biz.catalyst_signal
        SET status = 'expired', updated_at = NOW()
        WHERE status = 'open' AND expires_at < NOW()
        """
    )
    return cur.rowcount
