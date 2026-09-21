"""轧空行情扫描·判定逻辑（纯函数，脱离 DB / 网络，可直接单测）。

业务链路：① 轮询扫描疑似轧空启动 → ② 入队持续跟踪峰值 → ③ 冲高回撤后判定多空胜负。
**只做数据监控与告警，不含任何下单逻辑。**

────────────────────────────────────────────────────────────────
对原始业务伪代码的 6 处修正（原稿存在无法直接落地的缺陷）
────────────────────────────────────────────────────────────────
1. 原分支 1/2 引用了从未定义的 `CVD_peak` → 本实现显式区分两个增量：
   「拉升阶段 CVD 增量」（入队确认用）与「回撤窗口 CVD 增量」（胜负判定用），
   绝不使用累计绝对量。
2. CVD 是单调累计量，拿绝对累计值当比例基准（`0.3 * CVD_peak`）会随币的生命周期
   漂移、跨币不可比 → 统一归一化为 `cvd_ratio = 窗口净主动成交 / 窗口成交额`，
   取值 ∈ [-1, 1]，含义为「主动买卖力量占比」。
3. 原分支 1/2 的 OI 条件完全相同（都是 ΔOI > +1%），无法区分多空 →
   引入大户持仓多空比（`top_position_ratio`）与主动买卖比（`taker_ratio`）作方向判据；
   OI 只承担「持仓是维持还是离场」这一单一语义。
4. `MIN_LIQ` / `MIN_LIQ_THRESHOLD` / 24h 成交额阈值均无定义，且绝对金额跨币不可比 →
   爆仓一律用「窗口爆仓额 / 24h 成交额」的比例阈值。
5. 原分支 3（OI 快速下降）被归为「平局」，语义错误 → 单列为
   `profit_take`（多头止盈离场 / 趋势衰竭），是独立的风险提示而非无方向。
6. 原判定只在「首次回撤 2%」触发一次，易被噪音高点误触发 →
   增加「高点后最小观察时长 MIN_OBSERVE_MIN」与「跟踪期上限 TRACK_EXPIRE_MIN」，
   且判定窗口固定为 [peak_ts, now]。

另一处口径澄清：拉升阶段 CVD 上涨来自空头止损被动买入，**不能**用来判多空胜负；
胜负只看冲高后的回撤窗口指标（原稿核心约束，本实现严格遵守）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

# ── 阶段 1：扫描（拉升初筛 + 轧空确认）────────────────────────────
SURGE_THR_5M = 2.0            # 5 分钟内涨幅 ≥ 该值（%）进入初筛
SURGE_THR_15M = 3.5           # 15 分钟内涨幅 ≥ 该值（%）
SURGE_VOL_RATIO_MIN = 1.5     # 拉升需伴随放量（相对近 20 根均量）
# 轧空确认（满足其一即可，再叠加主动买盘要求）：
#   a. 窗口内空头爆仓占 24h 成交额 ≥ SQZ_SHORT_LIQ_RATIO_MIN（空头被强平）
#   b. 价升而 OI 降（OI 降幅 ≤ OI_SHORT_COVER_PCT）→ 空头回补特征
SQZ_SHORT_LIQ_RATIO_MIN = 0.00008
OI_SHORT_COVER_PCT = -0.3
SQZ_CVD_RATIO_MIN = 0.02      # 拉升窗口净主动买占比 > 该值（0.02 = 2%）

# ── 阶段 2：跟踪 ──────────────────────────────────────────────
RETRACE_THR = 2.0             # 距高点回撤 ≥ 该值（%）触发判定
MIN_OBSERVE_MIN = 15          # 高点后至少观察这么久（分钟）才允许判定
TRACK_EXPIRE_MIN = 180        # 跟踪期上限（分钟），超时标记 expired
TRACK_QUEUE_MAX = 12          # 队列上限（控制 API 用量）
SQUEEZE_COOLDOWN_H = 6        # 同一币判定后的冷却期（小时）

# ── 阶段 3：胜负判定（回撤窗口指标阈值）──────────────────────────
OI_EXIT_PCT = -1.0            # 窗口 OI 变化 ≤ 该值 → 持仓快速离场（否则视为维持）
CVD_SELL_STRONG = -0.15       # 窗口净主动卖占比 ≤ 该值 → 主动卖压主导
CVD_SELL_MILD = -0.08         # 温和卖压线（多单踩踏 vs 正常回踩的分界）
CVD_BUY_MILD = 0.02           # 窗口净主动买占比 ≥ 该值 → 确有主动买承接（多头胜的必要条件）
LONG_LIQ_RATIO_THR = 0.00008  # 窗口多单爆仓 / 24h 成交额 ≥ 该值 → 多头被强平

# 结论枚举
LONG_WIN = "long_win"        # 多头胜：持仓维持/再增，主动买未崩，无多单踩踏
SHORT_WIN = "short_win"      # 空头胜：主动卖主导 + 多单爆仓放大
PROFIT_TAKE = "profit_take"  # 多头止盈离场 / 趋势衰竭（OI 快速下降，无踩踏）
CHURN = "churn"              # 多空换手博弈，方向不明

CONCLUSION_LABEL = {
    LONG_WIN: "多头胜（新多承接）",
    SHORT_WIN: "空头胜（多头踩踏）",
    PROFIT_TAKE: "多头止盈离场（趋势衰竭）",
    CHURN: "多空平局（震荡消化）",
}


def base_symbol(symbol: str) -> str:
    """合约符号 → 币种基码（CoinGlass coin-list 口径）：BTCUSDT → BTC。"""
    s = (symbol or "").upper()
    for suffix in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def alias_bases(symbol: str) -> list[str]:
    """基码的候选别名（CoinGlass 对 1000 倍面值币可能用带/不带前缀两种写法）。

    如 1000PEPEUSDT → ["1000PEPE", "PEPE"]；PEPE1000USDT → ["PEPE1000", "PEPE"]。
    """
    base = base_symbol(symbol)
    out = [base]
    if base.startswith("1000") and len(base) > 4:
        out.append(base[4:])
    elif base.endswith("1000") and len(base) > 4:
        out.append(base[:-4])
    return out


def screen_surge(chg_5m: float | None, chg_15m: float | None,
                 vol_ratio: float | None) -> dict | None:
    """拉升初筛：短窗口大涨 + 放量。返回 {timeframe, chg_pct} 或 None。"""
    if vol_ratio is None or vol_ratio < SURGE_VOL_RATIO_MIN:
        return None
    if chg_5m is not None and chg_5m >= SURGE_THR_5M:
        return {"timeframe": "5m", "chg_pct": chg_5m}
    if chg_15m is not None and chg_15m >= SURGE_THR_15M:
        return {"timeframe": "15m", "chg_pct": chg_15m}
    return None


def confirm_squeeze(oi_chg_pct: float | None, short_liq_ratio: float | None,
                    cvd_ratio: float | None) -> tuple[bool, str]:
    """轧空确认：主动买盘主导，且出现「空头被强平」或「价升 OI 降」特征之一。

    返回 (是否确认, 确认依据说明)。
    """
    if cvd_ratio is None or cvd_ratio < SQZ_CVD_RATIO_MIN:
        return False, "主动买盘不足（CVD 占比未达阈值）"
    reasons: list[str] = []
    if short_liq_ratio is not None and short_liq_ratio >= SQZ_SHORT_LIQ_RATIO_MIN:
        reasons.append(f"空头爆仓占24h成交额 {short_liq_ratio * 100:.3f}%")
    if oi_chg_pct is not None and oi_chg_pct <= OI_SHORT_COVER_PCT:
        reasons.append(f"价升 OI 降 {oi_chg_pct:+.2f}%（空头回补）")
    if not reasons:
        return False, "无空头强平 / 无空头回补特征"
    return True, " + ".join(reasons)


def evaluate_battle(*, d_oi_pct: float | None, cvd_ratio: float | None,
                    long_liq_ratio: float | None, top_ratio_chg: float | None,
                    taker_ratio: float | None) -> dict:
    """回撤窗口胜负判定（窗口固定为 [peak_ts, now]）。

    Args:
        d_oi_pct: 窗口内 OI 变化率（%，负=持仓离场）
        cvd_ratio: 窗口净主动成交 / 窗口成交额（∈[-1,1]）
        long_liq_ratio: 窗口多单爆仓额 / 24h 成交额
        top_ratio_chg: 大户持仓多空比相对高点变化（倍数差，正=多头增仓）
        taker_ratio: 最新主动买卖量比（>1 主动买占优），仅作辅助 corroboration

    判定顺序（互斥，修正了原稿分支 1/2 条件重复的缺陷）：
        1. short_win   —— 主动卖压主导 + 多单爆仓放大
        2. profit_take —— OI 快速离场（≤ OI_EXIT_PCT）且无多单踩踏（多头止盈，非「平局」）
        3. long_win    —— OI 未快速离场 + 确有主动买承接（cvd ≥ CVD_BUY_MILD）
                          + 无多单踩踏（+ 大户持仓多空比未降）
        4. churn       —— 其余
    """
    cvd = cvd_ratio if cvd_ratio is not None else 0.0
    oi = d_oi_pct if d_oi_pct is not None else 0.0
    lliq = long_liq_ratio if long_liq_ratio is not None else 0.0
    big_long_liq = lliq >= LONG_LIQ_RATIO_THR

    signals = {
        "d_oi_pct": None if d_oi_pct is None else round(d_oi_pct, 3),
        "cvd_ratio": None if cvd_ratio is None else round(cvd_ratio, 4),
        "long_liq_ratio": None if long_liq_ratio is None else round(long_liq_ratio, 6),
        "top_ratio_chg": None if top_ratio_chg is None else round(top_ratio_chg, 4),
        "taker_ratio": None if taker_ratio is None else round(taker_ratio, 4),
    }

    # 1) 空头胜：主动卖主导 + 多单爆仓放大
    if cvd <= CVD_SELL_STRONG and big_long_liq:
        extra = "，大户持仓多空比下降" if (top_ratio_chg is not None and top_ratio_chg < 0) else ""
        taker = f"，主动买卖比 {taker_ratio:.2f}" if taker_ratio else ""
        return {"conclusion": SHORT_WIN,
                "reason": (f"回撤窗口净主动卖占比 {cvd * 100:.1f}%，"
                           f"多单爆仓占24h成交额 {lliq * 100:.3f}%，"
                           f"高位空单反击/多头踩踏{extra}{taker}"),
                "metrics": signals, "confidence": "high"}

    # 2) 多头止盈离场（趋势衰竭）：OI 快速下降且无多单踩踏
    if oi <= OI_EXIT_PCT and not big_long_liq:
        return {"conclusion": PROFIT_TAKE,
                "reason": (f"冲高后持仓快速下降 {oi:+.2f}%（非爆仓去化，多单爆仓仅占"
                           f"24h成交额 {lliq * 100:.3f}%），多头止盈离场、无新资金进场"),
                "metrics": signals, "confidence": "medium"}

    # 3) 多头胜：OI 未快速离场 + 确有主动买承接 + 无多单踩踏
    if oi > OI_EXIT_PCT and cvd >= CVD_BUY_MILD and not big_long_liq:
        if top_ratio_chg is None or top_ratio_chg >= 0:
            extra = "，大户持仓多空比未降" if top_ratio_chg is not None else ""
            return {"conclusion": LONG_WIN,
                    "reason": (f"回踩阶段持仓维持 {oi:+.2f}%，净主动成交占比 {cvd * 100:+.1f}%"
                               f"（仍有主动买承接），无大规模多单爆仓{extra}，新多头承接"),
                    "metrics": signals, "confidence": "high"}
        # 大户在减仓则视为换手，回落到 churn
        return {"conclusion": CHURN,
                "reason": (f"持仓维持 {oi:+.2f}%、净主动买 {cvd * 100:+.1f}%，但大户持仓多空比下降 "
                           f"{top_ratio_chg:+.3f}，疑似换手而非新多承接"),
                "metrics": signals, "confidence": "low"}

    return {"conclusion": CHURN,
            "reason": (f"OI {oi:+.2f}%、净主动成交占比 {cvd * 100:+.1f}%、"
                       f"多单爆仓占24h成交额 {lliq * 100:.3f}%，"
                       f"多空换手博弈，无明确胜负信号"),
            "metrics": signals, "confidence": "low"}


def latest_ratio(merged: dict, key: str, at: datetime | None = None):
    """取多空比序列中某个 key 的最近值。

    各端点时间戳互不相同（takerlongshortRatio 通常比 top/global 晚/早一个周期），
    因此必须逐 key 独立取「最近值」，不能先按 timestamp 对齐再取整条记录。
    `at` 非空时只取 ≤ at 的值（用于取「高点时刻」的基准）。
    """
    best_ts = None
    best_val = None
    for ts, vals in merged.items():
        if key not in vals:
            continue
        if at is not None and ts > at:
            continue
        if best_ts is None or ts > best_ts:
            best_ts, best_val = ts, vals[key]
    return best_val


def should_judge(retrace_pct: float | None, peak_ts: datetime | None,
                 now: datetime) -> tuple[bool, str]:
    """是否达到判定触发点：回撤达标且已过最小观察时长。"""
    if peak_ts is None:
        return False, "无峰值时间"
    if retrace_pct is None or retrace_pct < RETRACE_THR:
        return False, "回撤未达阈值"
    observe_min = (now - peak_ts).total_seconds() / 60
    if observe_min < MIN_OBSERVE_MIN:
        return False, f"高点后仅 {observe_min:.0f} 分钟（<{MIN_OBSERVE_MIN}）"
    return True, f"回撤 {retrace_pct:.2f}%，高点后 {observe_min:.0f} 分钟"


def is_expired(started_at: datetime, now: datetime,
               expire_min: int = TRACK_EXPIRE_MIN) -> bool:
    """跟踪超时（无法判定）。"""
    return (now - started_at) > timedelta(minutes=expire_min)


__all__ = [
    "base_symbol", "alias_bases", "screen_surge", "confirm_squeeze",
    "evaluate_battle", "should_judge", "is_expired", "latest_ratio",
    "CONCLUSION_LABEL", "LONG_WIN", "SHORT_WIN", "PROFIT_TAKE", "CHURN",
]