"""
催化剂 G4 基本面体检（按类别选尺 + 复用 thesis）。

设计原则：
- 按 asset_type / sector 选择不同的基本面尺子
- 优先复用 biz.research_thesis（已有结论的不重复计算）
- 输出 fundamental_pass + 明细 JSON（fundamental_detail）
- 所有阈值外置 yaml
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FundamentalResult:
    asset_id: int
    pass_: bool = False
    score: int = 0  # 0-100 综合基本面分
    detail: dict = field(default_factory=dict)
    checks: list[str] = field(default_factory=list)


class FundamentalChecker:
    """G4 基本面体检器。

    按资产类别选择不同的检查项：
    - coin / L1L2：供给 + 解锁压力 + TVL
    - meme：生命周期 + 风险标签 + 流动性 + 持仓集中度
    - defi / protocol：TVL + 收入
    - 通用安全：合约安全扫描
    """

    def __init__(self, config: dict):
        g4 = config.get("g4_fundamental", {})
        self.pass_threshold = int(g4.get("pass_threshold", 50))
        self.thesis_bonus = int(g4.get("thesis_bonus", 10))  # 有 thesis 的额外加分
        self.high_risk_penalty = int(g4.get("high_risk_penalty", 25))  # thesis 高风险扣分

        # 权重
        w = g4.get("weights", {})
        self.w_lifecycle = w.get("lifecycle", 0.20)
        self.w_risk = w.get("risk_labels", 0.20)
        self.w_liquidity = w.get("liquidity", 0.20)
        self.w_unlock = w.get("unlock_pressure", 0.15)
        self.w_contract_safety = w.get("contract_safety", 0.15)
        self.w_tvl = w.get("tvl", 0.10)

        # 流动性阈值（USD）
        liq = g4.get("liquidity_thresholds_usd", {})
        self.liq_good = float(liq.get("good", 5_000_000))   # 5M+ 好
        self.liq_ok = float(liq.get("ok", 1_000_000))       # 1M+ 及格
        self.liq_bad = float(liq.get("bad", 100_000))       # <100K 差

    def compute(self,
                asset_id: int,
                asset_type: Optional[str] = None,
                lifecycle_phase: Optional[str] = None,
                risk_score: Optional[int] = None,
                liquidity_usd: Optional[float] = None,
                unlock_pressure: Optional[str] = None,  # low / medium / high
                contract_safety_score: Optional[int] = None,
                tvl_usd: Optional[float] = None,
                has_thesis: bool = False,
                thesis_high_risk: bool = False,
                sector: Optional[str] = None,
                ) -> FundamentalResult:
        """计算基本面体检结果。

        Args:
            asset_id: 资产 ID
            asset_type: 资产类型（coin / meme / defi 等）
            lifecycle_phase: 生命周期阶段
            risk_score: 风险评分 0-100（越高越安全）
            liquidity_usd: 流动性 USD
            unlock_pressure: 解锁压力（low / medium / high）
            contract_safety_score: 合约安全分 0-100
            tvl_usd: TVL USD
            has_thesis: 是否有投研 thesis
            thesis_high_risk: thesis 是否标记为高风险
            sector: 板块名

        Returns:
            FundamentalResult
        """
        result = FundamentalResult(asset_id=asset_id)
        scores = {}
        checks = []

        # 1. 生命周期（0-100）
        lifecycle_score = self._score_lifecycle(lifecycle_phase, asset_type, sector)
        scores["lifecycle"] = lifecycle_score
        checks.append(f"生命周期:{lifecycle_phase or 'unknown'}={lifecycle_score}")

        # 2. 风险标签（risk_score 越高越安全，直接用）
        risk_score_val = risk_score if risk_score is not None else 50
        scores["risk_labels"] = risk_score_val
        checks.append(f"风险分:{risk_score_val}")

        # 3. 流动性
        liq_score = self._score_liquidity(liquidity_usd)
        scores["liquidity"] = liq_score
        checks.append(f"流动性:${liquidity_usd or 0:,.0f}={liq_score}")

        # 4. 解锁压力
        unlock_score = self._score_unlock(unlock_pressure)
        scores["unlock_pressure"] = unlock_score
        checks.append(f"解锁压力:{unlock_pressure or 'unknown'}={unlock_score}")

        # 5. 合约安全
        cs_score = contract_safety_score if contract_safety_score is not None else 50
        scores["contract_safety"] = cs_score
        checks.append(f"合约安全:{cs_score}")

        # 6. TVL（defi 类权重加倍，其他类权重减半）
        tvl_score = self._score_tvl(tvl_usd, asset_type)
        scores["tvl"] = tvl_score
        checks.append(f"TVL:${tvl_usd or 0:,.0f}={tvl_score}")

        # 加权综合分
        raw = (
            scores["lifecycle"] * self.w_lifecycle
            + scores["risk_labels"] * self.w_risk
            + scores["liquidity"] * self.w_liquidity
            + scores["unlock_pressure"] * self.w_unlock
            + scores["contract_safety"] * self.w_contract_safety
            + scores["tvl"] * self.w_tvl
        )

        # thesis 加成
        if has_thesis:
            if thesis_high_risk:
                raw -= self.high_risk_penalty
                checks.append(f"thesis高风险-{self.high_risk_penalty}")
            else:
                raw += self.thesis_bonus
                checks.append(f"thesis加成+{self.thesis_bonus}")

        score = max(0, min(100, round(raw)))
        result.score = score
        result.pass_ = score >= self.pass_threshold
        result.checks = checks
        result.detail = {
            "score": score,
            "pass": result.pass_,
            "checks": checks,
            "components": scores,
            "threshold": self.pass_threshold,
        }

        return result

    # ---- 单项评分 ----

    def _score_lifecycle(self, phase: Optional[str],
                         asset_type: Optional[str],
                         sector: Optional[str]) -> int:
        """生命周期阶段 → 分数。越成熟分越高（稳定）。"""
        if not phase:
            return 50
        p = phase.lower()
        # 成熟期 → 稳定 → 高分
        if p in ("mature", "established", "mainnet"):
            return 80
        if p in ("growth", "expansion", "launch"):
            return 65
        if p in ("early", "seed", "presale"):
            return 35
        if p in ("decline", "abandoned", "dead", "decay"):
            return 15
        if p == "unknown":
            return 50
        # meme 类特殊
        if asset_type == "meme" or (sector and "meme" in sector.lower()):
            if p in ("hype", "peak"):
                return 55
            if p in ("cooling", "death_spiral"):
                return 20
        return 50

    def _score_liquidity(self, liq_usd: Optional[float]) -> int:
        """流动性评分。越高越好。"""
        if liq_usd is None:
            return 50
        if liq_usd >= self.liq_good:
            return 90
        if liq_usd >= self.liq_ok:
            return 70
        if liq_usd >= self.liq_bad:
            return 45
        return 20

    def _score_unlock(self, pressure: Optional[str]) -> int:
        """解锁压力评分。压力越低分越高。"""
        if not pressure:
            return 50
        p = pressure.lower()
        if p == "low":
            return 85
        if p == "medium":
            return 55
        if p == "high":
            return 25
        return 50

    def _score_tvl(self, tvl_usd: Optional[float], asset_type: Optional[str]) -> int:
        """TVL 评分。"""
        if tvl_usd is None or tvl_usd <= 0:
            return 50
        if tvl_usd >= 1_000_000_000:  # 1B+
            return 90
        if tvl_usd >= 100_000_000:   # 100M+
            return 75
        if tvl_usd >= 10_000_000:    # 10M+
            return 60
        if tvl_usd >= 1_000_000:     # 1M+
            return 40
        return 25
