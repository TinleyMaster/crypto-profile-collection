"""
催化剂 G0 市场环境 + G1 分级（快通道核心，纯规则零 LLM）。

G0: 计算当日市场环境（risk_on / neutral / risk_off）
G1: 催化剂分级（authority × event_weight × scope → kind + base_strength）

设计原则：
- 所有阈值/权重从 catalyst_rules.yaml 读取，代码不硬编码
- base_strength 是分级的唯一数值输出，下游只读
- catalyst_kind 由 event_type + strength 共同决定，规则集中在这里

用法：
    from catalyst.grade import CatalystGrader, MarketRegime
    grader = CatalystGrader(config)
    grade = grader.grade_catalyst(catalyst_row, asset_links)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional


# =====================================================================
# G0: 市场环境
# =====================================================================

@dataclass
class MarketRegimeResult:
    regime_date: date
    regime: str           # risk_on / neutral / risk_off
    btc_trend_7d: float
    total_mcap_7d: float = 0.0
    derived_from: str = "rule"


class MarketRegime:
    """日度市场环境判定。

    规则：
    - BTC 7日涨跌幅 ≥ btc_7d_risk_on → risk_on
    - BTC 7日涨跌幅 ≤ btc_7d_risk_off → risk_off
    - 中间 → neutral
    - total_mcap_7d 作为辅助验证（同向才确认，反向取中性）
    """

    def __init__(self, config: dict):
        rules = config.get("regime_rules", {})
        self.btc_risk_on = rules.get("btc_7d_risk_on", 5.0)
        self.btc_risk_off = rules.get("btc_7d_risk_off", -5.0)
        self.mcap_risk_on = rules.get("total_mcap_7d_risk_on", 8.0)
        self.mcap_risk_off = rules.get("total_mcap_7d_risk_off", -8.0)

    def determine(self, btc_7d: float, total_mcap_7d: float = 0.0,
                  regime_date: date | None = None) -> MarketRegimeResult:
        """判定市场环境。

        Args:
            btc_7d: BTC 7 日涨跌幅（%）
            total_mcap_7d: 全市场市值 7 日涨跌幅（%），可选
            regime_date: 判定日期，默认今天

        Returns:
            MarketRegimeResult
        """
        if regime_date is None:
            regime_date = date.today()

        # BTC 主判定
        if btc_7d >= self.btc_risk_on:
            btc_regime = "risk_on"
        elif btc_7d <= self.btc_risk_off:
            btc_regime = "risk_off"
        else:
            btc_regime = "neutral"

        # 全市场辅助（同向确认，反向取中性）
        if total_mcap_7d:
            if total_mcap_7d >= self.mcap_risk_on:
                mcap_regime = "risk_on"
            elif total_mcap_7d <= self.mcap_risk_off:
                mcap_regime = "risk_off"
            else:
                mcap_regime = "neutral"

            # 同向 → 确认；反向 → 降为 neutral
            if btc_regime == mcap_regime:
                final = btc_regime
            else:
                final = "neutral"
        else:
            final = btc_regime

        return MarketRegimeResult(
            regime_date=regime_date,
            regime=final,
            btc_trend_7d=round(btc_7d, 4),
            total_mcap_7d=round(total_mcap_7d, 4),
        )

    def upsert_to_db(self, conn, result: MarketRegimeResult) -> None:
        """写入 market_regime_daily 表。"""
        conn.execute(
            """
            INSERT INTO biz.market_regime_daily (
                regime_date, regime, btc_trend_7d, alt_index_7d,
                total_mcap_7d, derived_from
            ) VALUES (
                %(regime_date)s, %(regime)s, %(btc_trend_7d)s, NULL,
                %(total_mcap_7d)s, %(derived_from)s
            )
            ON CONFLICT (regime_date) DO UPDATE SET
                regime = EXCLUDED.regime,
                btc_trend_7d = EXCLUDED.btc_trend_7d,
                total_mcap_7d = EXCLUDED.total_mcap_7d,
                updated_at = NOW()
            """,
            {
                "regime_date": result.regime_date,
                "regime": result.regime,
                "btc_trend_7d": result.btc_trend_7d,
                "total_mcap_7d": result.total_mcap_7d,
                "derived_from": result.derived_from,
            },
        )


# =====================================================================
# G1: 催化剂分级
# =====================================================================

@dataclass
class CatalystGradeResult:
    catalyst_id: int
    authority_score: int
    event_weight: int
    scope_score: int
    tradable: bool
    catalyst_kind: str     # structural / event / sentiment / noise
    base_strength: int     # 0-100
    event_type_src: str    # rule / ai / hybrid
    graded_by: str = "rule"


class CatalystGrader:
    """催化剂 G1 分级器。

    三维加权：
        base_strength = authority * w_authority + event_weight * w_event + scope * w_scope

    kind 判定：
        structural: 事件在 structural_event_types 中 + strength ≥ structural_min
        event:      strength ≥ event_min （非 structural、非 sentiment）
        sentiment:  事件在 sentiment_event_types 中
        noise:      other 类事件 且 无关联资产
    """

    def __init__(self, config: dict):
        gw = config.get("grade_weights", {})
        self.w_authority = gw.get("authority", 0.4)
        self.w_event = gw.get("event", 0.4)
        self.w_scope = gw.get("scope", 0.2)

        self.event_type_weights = config.get("event_type_weights", {})
        self.authority_scores = config.get("authority_scores", {})
        self.scope_rules = config.get("scope_score_rules", {})

        kt = config.get("kind_thresholds", {})
        self.structural_min = kt.get("structural_min_strength", 70)
        self.event_min = kt.get("event_min_strength", 50)

        self.structural_event_types = set(
            config.get("structural_event_types",
                       ["listing", "delisting", "burn", "regulation", "tech_upgrade"])
        )
        self.sentiment_event_types = set(
            config.get("sentiment_event_types", ["market_update"])
        )

    # ---- 公开入口 ----

    def grade(self, catalyst: dict, linked_assets: list[dict] | None = None) -> CatalystGradeResult:
        """分级一条催化剂。

        Args:
            catalyst: asset_catalyst 表的一行 dict
            linked_assets: catalyst_asset_link 关联的资产列表（可选）
        """
        catalyst_id = catalyst["catalyst_id"]
        source_code = catalyst.get("source_code", "")

        # 1) 事件类型：优先用 ai_event_type（如果有），兜底 rule_event_type
        event_type = catalyst.get("ai_event_type") or catalyst.get("rule_event_type") or "other"
        event_type_src = "ai" if catalyst.get("ai_event_type") else "rule"

        # 2) 权威度
        authority = self._authority_score(source_code)

        # 3) 事件权重
        event_weight = self.event_type_weights.get(event_type, 15)

        # 4) 影响聚焦度
        pairs = catalyst.get("related_pairs") or []
        scope = self._scope_score(pairs)

        # 5) 是否有可交易标的
        tradable = bool(linked_assets) if linked_assets is not None else self._has_asset_link(catalyst)

        # 6) 加权计算 base_strength
        base_strength = round(
            authority * self.w_authority
            + event_weight * self.w_event
            + scope * self.w_scope
        )
        base_strength = max(0, min(100, base_strength))  # 钳位 0-100

        # 7) 判定 kind
        kind = self._determine_kind(event_type, base_strength, tradable)

        return CatalystGradeResult(
            catalyst_id=catalyst_id,
            authority_score=authority,
            event_weight=event_weight,
            scope_score=scope,
            tradable=tradable,
            catalyst_kind=kind,
            base_strength=base_strength,
            event_type_src=event_type_src,
        )

    def upsert_to_db(self, conn, result: CatalystGradeResult) -> None:
        """写入 catalyst_grade 表。"""
        conn.execute(
            """
            INSERT INTO biz.catalyst_grade (
                catalyst_id, authority_score, event_weight, scope_score,
                tradable, catalyst_kind, base_strength, event_type_src, graded_by
            ) VALUES (
                %(catalyst_id)s, %(authority_score)s, %(event_weight)s, %(scope_score)s,
                %(tradable)s, %(catalyst_kind)s, %(base_strength)s, %(event_type_src)s, %(graded_by)s
            )
            ON CONFLICT (catalyst_id) DO UPDATE SET
                authority_score = EXCLUDED.authority_score,
                event_weight = EXCLUDED.event_weight,
                scope_score = EXCLUDED.scope_score,
                tradable = EXCLUDED.tradable,
                catalyst_kind = EXCLUDED.catalyst_kind,
                base_strength = EXCLUDED.base_strength,
                event_type_src = EXCLUDED.event_type_src,
                graded_by = EXCLUDED.graded_by,
                updated_at = NOW()
            """,
            {
                "catalyst_id": result.catalyst_id,
                "authority_score": result.authority_score,
                "event_weight": result.event_weight,
                "scope_score": result.scope_score,
                "tradable": result.tradable,
                "catalyst_kind": result.catalyst_kind,
                "base_strength": result.base_strength,
                "event_type_src": result.event_type_src,
                "graded_by": result.graded_by,
            },
        )

    # ---- 内部方法 ----

    def _authority_score(self, source_code: str) -> int:
        """来源权威度评分。"""
        # 精确匹配
        if source_code in self.authority_scores:
            return self.authority_scores[source_code]

        # 模糊匹配：binance_square_* 分官方/非官方
        if source_code.startswith("kol_catalyst_binance_square"):
            # 暂时统一给 85，后续可以从 kol_profile.kol_type 区分
            return self.authority_scores.get("binance_square_official", 85)

        # 默认
        return self.authority_scores.get("default", 40)

    def _scope_score(self, pairs: list[str]) -> int:
        """影响聚焦度评分。"""
        n = len(pairs) if pairs else 0
        if n == 0:
            # 无具体交易对 → 泛市场/板块，暂时给 broad
            return self.scope_rules.get("broad", 30)
        if n == 1:
            return self.scope_rules.get("single_pair", 90)
        if n <= 3:
            return self.scope_rules.get("few_pairs", 65)
        return self.scope_rules.get("many_pairs", 45)

    def _has_asset_link(self, catalyst: dict) -> bool:
        """简单判断：asset_id 非空 或 related_pairs 非空。
        准确判断应该查 catalyst_asset_link 表，这里给个快速判断。"""
        if catalyst.get("asset_id"):
            return True
        pairs = catalyst.get("related_pairs")
        return bool(pairs)

    def _determine_kind(self, event_type: str, base_strength: int, tradable: bool) -> str:
        """判定催化剂类型。"""
        # 噪声：other 类 且 无关联资产
        if event_type == "other" and not tradable:
            return "noise"

        # 情绪类事件（无论强度）
        if event_type in self.sentiment_event_types:
            return "sentiment"

        # 结构性：结构类事件 + 强度够
        if event_type in self.structural_event_types and base_strength >= self.structural_min:
            return "structural"

        # 事件型：强度在 event_min 以上
        if base_strength >= self.event_min:
            return "event"

        # 弱事件 → 情绪类
        return "sentiment"
