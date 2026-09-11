"""
催化剂 G3 二阶受益 + 持续性验证。

G3-1 二阶受益映射：
    - 一阶：catalyst_asset_link（直连，置信度 0.9 / 0.6）
    - 二阶：同 primary_sector 的其他资产（板块传导，置信度打折 0.4-0.6）

G3-2 持续性（两阶段）：
    - 预判：发布时基于事件类型（structural / one_off / decaying）
    - 验证：72h 后用共振四窗口序列 + 同类事件衔接验证

设计原则：
- 二阶受益只补"板块级"传导，不做生态/基础设施三阶（P2 再扩展）
- 持续性预判快通道可算，验证只在慢通道跑
- 所有阈值外置 yaml
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


# =====================================================================
# G3-1 二阶受益映射
# =====================================================================

@dataclass
class SecondOrderResult:
    catalyst_id: int
    asset_id: int
    order_level: int = 2       # 1=直连 2=板块
    confidence: float = 0.4    # 0-1
    sector_name: Optional[str] = None
    link_basis: str = "sector"  # sector / ecosystem / infra


class SecondOrderMapper:
    """二阶受益资产映射器。

    从已有直连资产出发，按 sector 找同板块资产，作为二阶受益标的。
    为避免噪声爆炸，严格限制：
    - 只取 primary_sector 且 confidence >= sector_conf_threshold 的
    - 每个 catalyst 最多 max_second_order 个二阶标的
    - 只对 structural / event 级催化剂做二阶（sentiment/noise 不做）
    """

    def __init__(self, config: dict):
        g3 = config.get("g3_second_order", {})
        self.sector_conf_threshold = float(g3.get("sector_conf_threshold", 0.6))
        self.max_second_order = int(g3.get("max_second_order", 10))
        self.order2_confidence = float(g3.get("order2_base_confidence", 0.45))
        self.min_kind_for_order2 = g3.get("min_kind", "event")  # structural > event > sentiment
        # 只对这些 kind 做二阶
        self.kind_allow_order2 = {"structural", "event"}

    def should_do_second_order(self, kind: str, base_strength: int) -> bool:
        """判断是否值得做二阶受益展开。"""
        if kind not in self.kind_allow_order2:
            return False
        # structural 无条件展开；event 需要 strength >= 50
        if kind == "structural":
            return True
        return base_strength >= 50

    def build_order2_from_sector(self,
                                  catalyst_id: int,
                                  direct_asset_ids: list[int],
                                  direct_sectors: list[str],
                                  sector_assets: dict[str, list[tuple[int, float]]],
                                  base_strength: int = 50) -> list[SecondOrderResult]:
        """从直连资产的 sector 出发，找同板块其他资产作为二阶受益。

        Args:
            catalyst_id: 催化剂 ID
            direct_asset_ids: 已直连的 asset_id 列表（排除用）
            direct_sectors: 直连资产的 primary_sector 列表
            sector_assets: {sector_name: [(asset_id, market_cap_rank), ...]} 按市值排序
            base_strength: 催化剂基础强度，影响置信度

        Returns:
            二阶受益列表（不包含直连资产）
        """
        direct_set = set(direct_asset_ids)
        results: list[SecondOrderResult] = []
        seen_assets = set()

        # 按 sector 遍历，每个 sector 取前几名
        sectors = [s for s in direct_sectors if s and s in sector_assets]
        # 每个 sector 分配名额
        if not sectors:
            return results

        per_sector_quota = max(1, self.max_second_order // len(sectors))

        for sector in sectors:
            candidates = sector_assets.get(sector, [])
            added = 0
            for asset_id, _mcap_rank in candidates:
                if asset_id in direct_set or asset_id in seen_assets:
                    continue
                # 置信度 = base * (1 + 0.1 * (strength-50)/50)，上限 0.65
                strength_bonus = max(0, min(0.2, (base_strength - 50) / 250))
                conf = min(0.65, round(self.order2_confidence + strength_bonus, 3))

                results.append(SecondOrderResult(
                    catalyst_id=catalyst_id,
                    asset_id=asset_id,
                    order_level=2,
                    confidence=conf,
                    sector_name=sector,
                    link_basis="sector",
                ))
                seen_assets.add(asset_id)
                added += 1
                if added >= per_sector_quota:
                    break

        return results

    @staticmethod
    def batch_upsert(conn, results: list["SecondOrderResult"]) -> int:
        """批量写入二阶受益映射（幂等）。

        Returns:
            写入数量
        """
        if not results:
            return 0

        conn.execute("""
            CREATE TEMP TABLE tmp_second_order (
                catalyst_id BIGINT,
                asset_id BIGINT,
                order_level SMALLINT,
                confidence NUMERIC(4,3),
                sector_name VARCHAR,
                link_basis TEXT
            ) ON COMMIT DROP
        """)

        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO tmp_second_order VALUES (
                    %(catalyst_id)s, %(asset_id)s, %(order_level)s,
                    %(confidence)s, %(sector_name)s, %(link_basis)s
                )
            """, [
                {
                    "catalyst_id": r.catalyst_id,
                    "asset_id": r.asset_id,
                    "order_level": r.order_level,
                    "confidence": r.confidence,
                    "sector_name": r.sector_name,
                    "link_basis": r.link_basis,
                }
                for r in results
            ])

        conn.execute("""
            INSERT INTO biz.catalyst_second_order (
                catalyst_id, asset_id, order_level,
                confidence, sector_name, derived_from
            )
            SELECT t.catalyst_id, t.asset_id, t.order_level,
                   t.confidence, t.sector_name, t.link_basis
            FROM tmp_second_order t
            ON CONFLICT (catalyst_id, asset_id, order_level) DO UPDATE SET
                confidence = EXCLUDED.confidence,
                sector_name = COALESCE(EXCLUDED.sector_name, biz.catalyst_second_order.sector_name),
                derived_from = COALESCE(EXCLUDED.derived_from, biz.catalyst_second_order.derived_from)
        """)

        return len(results)


# =====================================================================
# G3-2 持续性（预判 + 验证）
# =====================================================================

class PersistenceScorer:
    """持续性打分器。

    两阶段：
    1. 预判（predict）：发布即刻，基于事件类型 + 基础强度
    2. 验证（verify）：72h 后，用共振窗口序列 + 同类事件衔接判定
    """

    def __init__(self, config: dict):
        g3 = config.get("g3_persistence", {})
        # 预判规则
        self.structural_event_types = set(g3.get("structural_event_types", [
            "listing", "burn", "regulation", "tech_upgrade",
        ]))
        self.one_off_event_types = set(g3.get("one_off_event_types", [
            "partnership", "airdrop", "funding",
        ]))
        self.decaying_event_types = set(g3.get("decaying_event_types", [
            "market_update", "other",
        ]))
        self.structural_min_strength = int(g3.get("structural_min_strength", 70))
        self.funding_min_strength = int(g3.get("funding_min_strength", 65))

        # 验证参数
        self.verify_window_hours = int(g3.get("verify_window_hours", 72))
        self.decay_threshold_pct = float(g3.get("decay_threshold_pct", 1.0))
        self.followup_min_count = int(g3.get("followup_min_count", 2))

    # ---- 预判（快通道可算） ----

    def predict(self, event_type: str, kind: str, base_strength: int) -> str:
        """基于事件类型的持续性预判。

        Returns:
            'structural' | 'one_off' | 'decaying'
        """
        # kind 为 noise 的，直接 decaying
        if kind == "noise":
            return "decaying"

        # kind 已经是 structural 的，维持
        if kind == "structural":
            return "structural"

        # 按事件类型判断
        et = (event_type or "").lower()

        if et in self.structural_event_types:
            if base_strength >= self.structural_min_strength:
                return "structural"
            return "one_off"

        # funding 大额的算 structural
        if et == "funding" and base_strength >= self.funding_min_strength:
            return "structural"

        if et in self.one_off_event_types:
            return "one_off"

        # 默认：sentiment / market_update 类 → decaying
        if kind == "sentiment":
            return "decaying"

        return "one_off"

    # ---- 验证（慢通道 72h 后） ----

    def verify(self,
               event_type: str,
               published_at: datetime,
               excess_ret_1h: Optional[float],
               excess_ret_4h: Optional[float],
               excess_ret_24h: Optional[float],
               excess_ret_72h: Optional[float],
               followup_count: int = 0) -> tuple[str, bool]:
        """持续性验证。

        Args:
            event_type: 事件类型
            published_at: 发布时间
            excess_ret_*: 各窗口超额收益（%）
            followup_count: 同资产同类事件后续 72h 内出现次数

        Returns:
            (persistence, verified) - 验证后的持续性 + 是否经过验证
        """
        # 时间不够 72h 的，不验证
        if datetime.utcnow() - published_at < timedelta(hours=self.verify_window_hours):
            return (self.predict(event_type, "event", 50), False)

        # 有 72h 数据，验证衰减模式
        rets = []
        for r in [excess_ret_1h, excess_ret_4h, excess_ret_24h, excess_ret_72h]:
            if r is not None:
                rets.append(abs(r))

        if len(rets) >= 3:
            # 单调递减且末值 < 阈值 → decaying
            is_decaying = True
            for i in range(1, len(rets)):
                if rets[i] > rets[i - 1] * 1.2:  # 允许 20% 误差
                    is_decaying = False
                    break
            if is_decaying and rets[-1] < self.decay_threshold_pct:
                return ("decaying", True)

        # 有后续同类事件衔接 → structural（说明叙事在持续）
        if followup_count >= self.followup_min_count:
            # 前提是初始类型本身偏 structural
            et = (event_type or "").lower()
            if et in self.structural_event_types or et == "funding":
                return ("structural", True)

        # 既不满足衰减也不满足持续 → one_off
        return ("one_off", True)
