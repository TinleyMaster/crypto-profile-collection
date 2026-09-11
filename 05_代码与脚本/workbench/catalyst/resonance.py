"""
催化剂 G2 价格共振打分（快通道，15min 级别）。

核心三指标：
1. excess_ret: 资产区间收益 - BTC 同期收益（扣 beta，避免混入大盘）
2. vol_zscore: 24h 量能 z-score = volume_24h / avg(volume_24h, 20d)
3. direction_match: 超额收益方向 是否与 catalyst_impact.impact_direction 一致

板块共振：
- peer 池：同 primary_sector + 市值前 50（或流动性前 30）
- peer_median_ret: 同期收益中位数

数据源（P0 阶段）：
- ≤24h: cmc_asset_quote_snapshot（用 percent_change_1h/24h 近似）
  ⚠️ 精度近似声明：快照时间点与 published_at 不对齐，t+1h 是近似值
- >24h: biz.asset_market_daily（日收盘回看 t+1d / t+3d / t+7d）

设计原则：
- 共振确认 ≠ 入场信号（入场位由 G5 给）
- resonance_score 是唯一数值输出，下游只读
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class ResonanceResult:
    catalyst_id: int
    asset_id: int
    excess_ret_1h: Optional[float] = None
    excess_ret_4h: Optional[float] = None
    excess_ret_24h: Optional[float] = None
    excess_ret_72h: Optional[float] = None
    vol_zscore_24h: Optional[float] = None
    peer_median_ret_24h: Optional[float] = None
    direction_match: Optional[bool] = None
    resonance_score: int = 0      # 0-100
    resonance_state: str = "pending"  # confirmed / weak / divergent / pending
    ret_source: str = "cmc_snapshot"


class ResonanceScorer:
    """G2 共振打分器。"""

    def __init__(self, config: dict):
        th = config.get("resonance_thresholds", {})
        self.excess_confirmed = th.get("excess_confirmed", 5.0)
        self.excess_weak = th.get("excess_weak", 1.0)
        self.vol_confirmed = th.get("vol_confirmed", 1.5)
        self.vol_weak = th.get("vol_weak", 1.0)
        self.peer_bonus = th.get("peer_bonus_points", 5)
        self.divergent_penalty = th.get("divergent_penalty", 20)

        w = config.get("resonance_weights", {})
        self.w_excess = w.get("excess_ret", 0.5)
        self.w_vol = w.get("vol_zscore", 0.3)
        self.w_direction = w.get("direction_match", 0.2)

    # ---- 公开入口 ----

    def compute(self, asset_ret_pct: float, btc_ret_pct: float,
                vol_zscore: float = 0.0,
                impact_direction: str = "bullish",
                peer_median_ret: float | None = None,
                window: str = "24h") -> ResonanceResult:
        """计算单个资产在单个窗口的共振。

        Args:
            asset_ret_pct: 资产区间涨跌幅（%）
            btc_ret_pct: BTC 同期涨跌幅（%）
            vol_zscore: 24h 量能 z-score
            impact_direction: bullish / bearish / neutral
            peer_median_ret: 同板块 peer 中位数收益（%）
            window: 1h / 4h / 24h / 72h

        Returns:
            ResonanceResult（用 24h 窗口作为主打分依据，其他窗口仅作参考存表）
        """
        # 数据库可能返回 Decimal，统一转 float 避免与 float 阈值混算崩溃
        asset_ret_pct = float(asset_ret_pct or 0.0)
        btc_ret_pct = float(btc_ret_pct or 0.0)
        vol_zscore = float(vol_zscore) if vol_zscore is not None else 0.0
        peer_median_ret = float(peer_median_ret) if peer_median_ret is not None else None
        excess = asset_ret_pct - btc_ret_pct

        # 方向一致性
        if impact_direction == "neutral":
            direction_match = True  # 中性事件不看方向
        elif impact_direction == "bullish":
            direction_match = excess > 0
        else:  # bearish
            direction_match = excess < 0

        # 各维度得分（归一化到 0-100）
        excess_score = self._score_excess(excess)
        vol_score = self._score_vol(vol_zscore)
        direction_score = 100 if direction_match else 0

        # 加权综合分
        raw_score = (
            excess_score * self.w_excess
            + vol_score * self.w_vol
            + direction_score * self.w_direction
        )

        # peer 加成（跑赢板块中位数加分）
        if peer_median_ret is not None and excess > peer_median_ret:
            raw_score += self.peer_bonus

        # 背离惩罚（方向相反时额外扣分）
        if not direction_match and abs(excess) > self.excess_weak:
            raw_score -= self.divergent_penalty

        # 钳位 0-100
        score = max(0, min(100, round(raw_score)))

        # 共振状态
        state = self._determine_state(excess, vol_zscore, direction_match)

        result = ResonanceResult(
            catalyst_id=0,  # 调用方填
            asset_id=0,     # 调用方填
            vol_zscore_24h=round(vol_zscore, 4) if vol_zscore is not None else None,
            peer_median_ret_24h=round(peer_median_ret, 4) if peer_median_ret is not None else None,
            direction_match=direction_match,
            resonance_score=score,
            resonance_state=state,
        )

        # 根据窗口把 excess 放到对应字段
        setattr(result, f"excess_ret_{window}", round(excess, 4))
        return result

    # ---- 数据库操作 ----

    def upsert_to_db(self, conn, result: ResonanceResult) -> None:
        """写入 catalyst_resonance 表。
        如果已存在，用 MAX 合并各窗口字段（保留已有窗口数据，更新新窗口）。
        """
        # 先查已有记录
        existing = conn.execute(
            """
            SELECT * FROM biz.catalyst_resonance
            WHERE catalyst_id = %s AND asset_id = %s
            """,
            (result.catalyst_id, result.asset_id),
        ).fetchone()

        if existing:
            # 合并：已有窗口保留，新窗口覆盖，score/state 取最新窗口的
            merged = self._merge_results(existing, result)
            conn.execute(
                """
                UPDATE biz.catalyst_resonance SET
                    excess_ret_1h = %s,
                    excess_ret_4h = %s,
                    excess_ret_24h = %s,
                    excess_ret_72h = %s,
                    vol_zscore_24h = %s,
                    peer_median_ret_24h = %s,
                    direction_match = %s,
                    resonance_score = %s,
                    resonance_state = %s,
                    ret_source = %s,
                    updated_at = NOW()
                WHERE catalyst_id = %s AND asset_id = %s
                """,
                (
                    merged.excess_ret_1h,
                    merged.excess_ret_4h,
                    merged.excess_ret_24h,
                    merged.excess_ret_72h,
                    merged.vol_zscore_24h,
                    merged.peer_median_ret_24h,
                    merged.direction_match,
                    merged.resonance_score,
                    merged.resonance_state,
                    merged.ret_source,
                    result.catalyst_id,
                    result.asset_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO biz.catalyst_resonance (
                    catalyst_id, asset_id,
                    excess_ret_1h, excess_ret_4h, excess_ret_24h, excess_ret_72h,
                    vol_zscore_24h, peer_median_ret_24h, direction_match,
                    resonance_score, resonance_state, ret_source
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    result.catalyst_id, result.asset_id,
                    result.excess_ret_1h, result.excess_ret_4h,
                    result.excess_ret_24h, result.excess_ret_72h,
                    result.vol_zscore_24h, result.peer_median_ret_24h,
                    result.direction_match,
                    result.resonance_score, result.resonance_state,
                    result.ret_source,
                ),
            )

    # ---- 内部方法 ----

    def _score_excess(self, excess: float) -> float:
        """超额收益 → 0-100 分。"""
        abs_excess = abs(excess)
        if abs_excess >= self.excess_confirmed:
            return 100
        if abs_excess >= self.excess_weak:
            # 线性插值：weak(50) → confirmed(100)
            ratio = (abs_excess - self.excess_weak) / (self.excess_confirmed - self.excess_weak)
            return 50 + 50 * min(1.0, max(0.0, ratio))
        # 弱于 weak
        ratio = abs_excess / self.excess_weak if self.excess_weak > 0 else 0
        return 50 * min(1.0, max(0.0, ratio))

    def _score_vol(self, vol_z: float) -> float:
        """量能 z-score → 0-100 分。"""
        if vol_z is None:
            return 50  # 无数据给中性分
        if vol_z >= self.vol_confirmed:
            return 100
        if vol_z >= self.vol_weak:
            ratio = (vol_z - self.vol_weak) / (self.vol_confirmed - self.vol_weak)
            return 60 + 40 * min(1.0, max(0.0, ratio))
        # 低于 weak
        ratio = vol_z / self.vol_weak if self.vol_weak > 0 else 0
        return 60 * min(1.0, max(0.0, ratio))

    def _determine_state(self, excess: float, vol_z: float, direction_match: bool) -> str:
        """判定共振状态。"""
        abs_excess = abs(excess)

        if abs_excess >= self.excess_confirmed and vol_z >= self.vol_confirmed and direction_match:
            return "confirmed"

        if not direction_match and abs_excess >= self.excess_weak:
            return "divergent"

        if abs_excess >= self.excess_weak or vol_z >= self.vol_weak:
            return "weak"

        return "pending"

    def _merge_results(self, existing: dict, new: ResonanceResult) -> ResonanceResult:
        """合并已有记录和新结果（各窗口取最新值，score/state 用新的）。"""
        merged = ResonanceResult(
            catalyst_id=new.catalyst_id,
            asset_id=new.asset_id,
            excess_ret_1h=new.excess_ret_1h if new.excess_ret_1h is not None else existing.get("excess_ret_1h"),
            excess_ret_4h=new.excess_ret_4h if new.excess_ret_4h is not None else existing.get("excess_ret_4h"),
            excess_ret_24h=new.excess_ret_24h if new.excess_ret_24h is not None else existing.get("excess_ret_24h"),
            excess_ret_72h=new.excess_ret_72h if new.excess_ret_72h is not None else existing.get("excess_ret_72h"),
            vol_zscore_24h=new.vol_zscore_24h if new.vol_zscore_24h is not None else existing.get("vol_zscore_24h"),
            peer_median_ret_24h=new.peer_median_ret_24h if new.peer_median_ret_24h is not None else existing.get("peer_median_ret_24h"),
            direction_match=new.direction_match if new.direction_match is not None else existing.get("direction_match"),
            resonance_score=new.resonance_score,
            resonance_state=new.resonance_state,
            ret_source=new.ret_source,
        )
        return merged


# =====================================================================
# 行情数据获取辅助函数
# =====================================================================

def get_cmc_snapshot_ret(conn, asset_id: int) -> dict:
    """从 CMC 快照表获取最新 1h/24h/7d 涨跌幅和 24h 量能。

    Returns:
        dict: {percent_change_1h, percent_change_24h, percent_change_7d, volume_24h}
    """
    # 先从 asset_source_map 拿 cmc_id（source_code = 'cmc'）
    row = conn.execute(
        """
        SELECT source_asset_key
        FROM core.asset_source_map
        WHERE asset_id = %s AND source_code = 'cmc'
          AND is_primary = true
        LIMIT 1
        """,
        (asset_id,),
    ).fetchone()

    # 兜底：用 asset 表 canonical_symbol 直接查 cmc_asset_map
    cmc_id = None
    if row and row["source_asset_key"]:
        try:
            cmc_id = int(row["source_asset_key"])
        except (ValueError, TypeError):
            cmc_id = None

    if cmc_id is None:
        asset_row = conn.execute(
            "SELECT canonical_symbol FROM core.asset WHERE asset_id = %s",
            (asset_id,),
        ).fetchone()
        if not asset_row:
            return {}
        cmc_row = conn.execute(
            """
            SELECT cmc_id FROM src_cmc.cmc_asset_map
            WHERE symbol = %s
            ORDER BY rank_num NULLS LAST
            LIMIT 1
            """,
            (asset_row["canonical_symbol"].upper(),),
        ).fetchone()
        if not cmc_row:
            return {}
        cmc_id = cmc_row["cmc_id"]

    # 取最新快照
    snap = conn.execute(
        """
        SELECT percent_change_1h, percent_change_24h, percent_change_7d, volume_24h
        FROM src_cmc.cmc_asset_quote_snapshot
        WHERE cmc_id = %s
        ORDER BY quote_time DESC
        LIMIT 1
        """,
        (cmc_id,),
    ).fetchone()

    return dict(snap) if snap else {}


def get_btc_cmc_snapshot_ret(conn) -> dict:
    """获取 BTC 的 CMC 最新快照收益。"""
    # BTC cmc_id 通过 source_map 或 symbol 查询
    snap = conn.execute(
        """
        SELECT percent_change_1h, percent_change_24h, percent_change_7d, volume_24h
        FROM src_cmc.cmc_asset_quote_snapshot
        WHERE cmc_id = (
            SELECT cmc_id FROM src_cmc.cmc_asset_map
            WHERE symbol = 'BTC'
            ORDER BY rank_num NULLS LAST
            LIMIT 1
        )
        ORDER BY quote_time DESC
        LIMIT 1
        """,
    ).fetchone()
    return dict(snap) if snap else {}


def get_market_daily_ret(conn, asset_id: int, start_date, end_date) -> float | None:
    """从 asset_market_daily 算区间涨跌幅。

    Returns:
        涨跌幅百分比，或 None（无数据）
    """
    rows = conn.execute(
        """
        SELECT market_date, price_usd
        FROM biz.asset_market_daily
        WHERE asset_id = %s AND market_date BETWEEN %s AND %s
        ORDER BY market_date ASC
        """,
        (asset_id, start_date, end_date),
    ).fetchall()

    if len(rows) < 2:
        return None

    first_price = rows[0]["price_usd"]
    last_price = rows[-1]["price_usd"]
    if not first_price or not last_price:
        return None

    return ((last_price - first_price) / first_price) * 100


def calc_vol_zscore(conn, asset_id: int, current_volume_24h: float, lookback_days: int = 20) -> float | None:
    """计算 24h 量能 z-score（用日成交量 20 日均值做分母）。

    P0 简化版：z = current / avg(lookback)，不做标准差。
    """
    if not current_volume_24h or current_volume_24h <= 0:
        return None

    # 从 asset_market_daily 取 20 日平均成交量
    row = conn.execute(
        """
        SELECT AVG(volume_24h) as avg_vol
        FROM (
            SELECT volume_24h
            FROM biz.asset_market_daily
            WHERE asset_id = %s AND volume_24h > 0
            ORDER BY market_date DESC
            LIMIT %s
        ) sub
        """,
        (asset_id, lookback_days),
    ).fetchone()

    if not row or not row["avg_vol"] or float(row["avg_vol"]) <= 0:
        return None

    return float(current_volume_24h) / float(row["avg_vol"])


def get_peer_median_ret(conn, asset_id: int, window_days: int = 1,
                        peer_top_n: int = 50) -> float | None:
    """获取同板块 peer 的中位数收益。

    peer 池：同 primary_sector + 市值前 peer_top_n。
    """
    # 先查该资产的 primary_sector（core.asset 上有冗余字段，直接取）
    sector_row = conn.execute(
        """
        SELECT primary_sector as sector_name
        FROM core.asset
        WHERE asset_id = %s
        LIMIT 1
        """,
        (asset_id,),
    ).fetchone()

    if not sector_row or not sector_row["sector_name"]:
        return None

    sector_name = sector_row["sector_name"]

    # 取同板块市值前 N 的资产的 24h 收益（CMC 快照）
    rows = conn.execute(
        """
        SELECT m.percent_change_24h
        FROM core.asset a
        JOIN src_cmc.cmc_asset_quote_snapshot m ON m.cmc_id = (
            SELECT cam.cmc_id
            FROM core.asset_source_map asm
            JOIN src_cmc.cmc_asset_map cam ON cam.symbol = a.canonical_symbol
            WHERE asm.asset_id = a.asset_id AND asm.source_code = 'cmc'
              AND asm.is_primary = true
            LIMIT 1
        )
        WHERE a.primary_sector = %s
          AND a.market_cap IS NOT NULL
          AND a.status = 'active'
        ORDER BY a.market_cap DESC NULLS LAST
        LIMIT %s
        """,
        (sector_name, peer_top_n),
    ).fetchall()

    if not rows:
        return None

    rets = [float(r["percent_change_24h"]) for r in rows if r["percent_change_24h"] is not None]
    if not rets:
        return None

    rets.sort()
    n = len(rets)
    if n % 2 == 0:
        return (rets[n // 2 - 1] + rets[n // 2]) / 2
    return rets[n // 2]
