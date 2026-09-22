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

────────────────────────────────────────────────────────────────
缺失数据的口径（2026-09-21 审计 P1-3）
────────────────────────────────────────────────────────────────
「缺失」**绝不等于「为 0」**。三个关键输入（`d_oi_pct` / `cvd_ratio` /
`long_liq_ratio`）任一为 `None` 时：

  - 不得静默补 0 后照常判定（补 0 会让「无爆仓数据」无条件满足
    `not big_long_liq`，从而把缺数据误判成 `profit_take` / `long_win`）；
  - 依赖该维度的分支一律不成立（`long_liq_ratio is None` 时禁止
    `profit_take` / `long_win`，因为这两条的前提是「**确知**无多单踩踏」）；
  - 缺失维度写进 `metrics['data_missing']` 并在文案中显式标注，降级为
    `churn`（无方向）而非给出高置信度方向。
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
# 判定阈值**独立标定**（与入队阈值 SQZ_SHORT_LIQ_RATIO_MIN 语义相反、分布不同，勿联动；
# 两者恰好同值纯属巧合，此前被误当同一常量）。
# 稳定结论（不随样本量/分位数值漂移，可长期引用）：
#   ① 旧值 8e-5 落在无条件分布**约 P75**、越阈率 ≈20%；
#   ② 「冲高回撤」条件子集（判定实际发生的场景）越阈率 ≈43%~52%（跨口径上界）⇒
#      近半数回撤期采样被标成 big_long_liq，**系统性屏蔽 profit_take / long_win**，
#      结论被推向 churn/short_win；
#   ③ 故上调至无条件 P95 量级（0.00040709）：条件子集越阈率降到 ≈15%~18%
#      （跨口径上界 <20%，满足工单 SQZ-02 判据）；P90（2.27e-4）复验口径下越阈率
#      ≈29.8% ⇒ 不采用。
# ⚠️ 分位数值**随采样时刻漂移**，此处不再写死（复验 P3-e）；每次标定跑
#   `workbench/calib_squeeze_liq_thr.py`，其输出自带 min/max(ts) 与跨口径上界。
# ⚠️ 标定前提（复验 P1-a）：`liquidation_snapshot` 现表历史仅数小时（非 7 天），
#   样本时间代表性弱 ⇒ 该阈值为**临时值**，待判定样本积累后按判定窗口直接标定定稿。
LONG_LIQ_RATIO_THR = 0.00040709
MIN_WINDOW_COVERAGE = 0.6     # 判定窗口 5m OI 桶覆盖率下限（低于此值放弃判定，见审计 P1-2）
# 判定窗口内最长连续缺桶（含左端起）≥ 该值 → 拒判（复验 P2-d）。覆盖率只看「数量」、
# 尾部判据只看「右端」⇒「前段齐、中段缺 1~2 桶、尾部齐」两项都不拦（13 桶窗口缺 2 个
# 中间桶 → 11/13=0.846 仍通过）；而重启 / `os._exit(1)` 杀掉飞行中一轮时，缺的恰恰是
# **中间某桶**且快照不可回补 ⇒ 窗口指标被悄悄污染。判定区间 `[first_bucket, last_bucket)`，
# 右端正在采集的桶不计（由尾部判据负责）。
MAX_MID_GAP_BUCKETS = 2
# metrics 版本位（复验 D5）：v1 = `mid_gap_buckets` **含左端起**；
# v2 = 左端游程单列 `head_gap_buckets`、`mid_gap_buckets` 不含左端。
# 判据行为等价（`max(head, mid)` ≡ v1 的 `mid`），但**字段语义变了** ⇒ 跨版本回看
# `biz.squeeze_track.metrics->'mid_gap_buckets'` 必须先看这个版本位，否则会误读历史行。
GAP_METRIC_VER = 2

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


_DIM_LABELS = {
    "d_oi_pct": "OI变化",
    "cvd_ratio": "净主动成交占比",
    "long_liq_ratio": "多单爆仓额",
}


def _pct_str(v: float | None, digits: int = 1) -> str:
    """比例值（0.1234）→ '+12.34%'；None → '缺失'（禁止把缺失写成 0）。"""
    if v is None:
        return "缺失"
    return f"{v * 100:+.{digits}f}%"


def _raw_pct_str(v: float | None, digits: int = 2) -> str:
    """已是百分数的值（-2.786）→ '-2.79%'；None → '缺失'。"""
    if v is None:
        return "缺失"
    return f"{v:+.{digits}f}%"


def evaluate_battle(*, d_oi_pct: float | None, cvd_ratio: float | None,
                    long_liq_ratio: float | None, top_ratio_chg: float | None,
                    taker_ratio: float | None) -> dict:
    """回撤窗口胜负判定（窗口固定为 [peak_ts, now]）。

    Args:
        d_oi_pct: 窗口内 OI 变化率（%，负=持仓离场）
        cvd_ratio: 窗口净主动成交 / 窗口成交额（∈[-1,1]）
        long_liq_ratio: 最近 1h 多单爆仓额 / 24h 成交额
        top_ratio_chg: 大户持仓多空比相对高点变化（倍数差，正=多头增仓）
        taker_ratio: 最新主动买卖量比（>1 主动买占优），仅作辅助 corroboration

    判定顺序（互斥，修正了原稿分支 1/2 条件重复的缺陷）：
        1. short_win   —— 主动卖压主导 + 多单爆仓放大
        2. profit_take —— OI 快速离场（≤ OI_EXIT_PCT）且无多单踩踏（多头止盈，非「平局」）
        3. long_win    —— OI 未快速离场 + 确有主动买承接（cvd ≥ CVD_BUY_MILD）
                          + 无多单踩踏（+ 大户持仓多空比未降）
        4. churn       —— 其余

    缺失数据策略（审计 P1-3）：任一关键维度为 `None` 时，依赖该维度的分支一律
    不成立，并降级为 `churn`（无方向）+ 在 `data_missing` / 文案中显式标注。
    """
    signals = {
        "d_oi_pct": None if d_oi_pct is None else round(d_oi_pct, 3),
        "cvd_ratio": None if cvd_ratio is None else round(cvd_ratio, 4),
        "long_liq_ratio": None if long_liq_ratio is None else round(long_liq_ratio, 6),
        "top_ratio_chg": None if top_ratio_chg is None else round(top_ratio_chg, 4),
        "taker_ratio": None if taker_ratio is None else round(taker_ratio, 4),
    }
    missing = [name for name, val in (
        ("d_oi_pct", d_oi_pct), ("cvd_ratio", cvd_ratio),
        ("long_liq_ratio", long_liq_ratio)) if val is None]
    signals["data_missing"] = missing

    oi_ok = d_oi_pct is not None
    cvd_ok = cvd_ratio is not None
    liq_ok = long_liq_ratio is not None
    # 缺失 ≠ 无踩踏：只有确知爆仓额时才允许判定「大额多单爆仓」
    big_long_liq = liq_ok and long_liq_ratio >= LONG_LIQ_RATIO_THR
    lliq_txt = _pct_str(long_liq_ratio, 3)

    # 1) 空头胜：主动卖主导 + 多单爆仓放大（两个维度都必须已知）
    if cvd_ok and liq_ok and cvd_ratio <= CVD_SELL_STRONG and big_long_liq:
        extra = "，大户持仓多空比下降" if (top_ratio_chg is not None and top_ratio_chg < 0) else ""
        taker = f"，主动买卖比 {taker_ratio:.2f}" if taker_ratio else ""
        return {"conclusion": SHORT_WIN,
                "reason": (f"回撤窗口净主动卖占比 {_pct_str(cvd_ratio)}，"
                           f"最近1h多单爆仓占24h成交额 {lliq_txt}，"
                           f"高位空单反击/多头踩踏{extra}{taker}"),
                "metrics": signals, "confidence": "high"}

    # 2) 多头止盈离场（趋势衰竭）：OI 快速下降且**确知**无多单踩踏
    if oi_ok and liq_ok and d_oi_pct <= OI_EXIT_PCT and not big_long_liq:
        return {"conclusion": PROFIT_TAKE,
                "reason": (f"冲高后持仓快速下降 {_raw_pct_str(d_oi_pct)}（非爆仓去化，"
                           f"最近1h多单爆仓仅占24h成交额 {lliq_txt}），"
                           f"多头止盈离场、无新资金进场"),
                "metrics": signals, "confidence": "medium"}

    # 3) 多头胜：OI 未快速离场 + 确有主动买承接 + 无多单踩踏（三维度都需已知）
    if (oi_ok and cvd_ok and liq_ok and d_oi_pct > OI_EXIT_PCT
            and cvd_ratio >= CVD_BUY_MILD and not big_long_liq):
        if top_ratio_chg is None or top_ratio_chg >= 0:
            extra = "，大户持仓多空比未降" if top_ratio_chg is not None else ""
            return {"conclusion": LONG_WIN,
                    "reason": (f"回踩阶段持仓维持 {_raw_pct_str(d_oi_pct)}，"
                               f"净主动成交占比 {_pct_str(cvd_ratio)}"
                               f"（仍有主动买承接），无大规模多单爆仓{extra}，新多头承接"),
                    "metrics": signals, "confidence": "high"}
        # 大户在减仓则视为换手，回落到 churn
        return {"conclusion": CHURN,
                "reason": (f"持仓维持 {_raw_pct_str(d_oi_pct)}、净主动买 {_pct_str(cvd_ratio)}，"
                           f"但大户持仓多空比下降 {top_ratio_chg:+.3f}，疑似换手而非新多承接"),
                "metrics": signals, "confidence": "low"}

    lack = (f"（数据不足：{'、'.join(_DIM_LABELS[m] for m in missing)}缺失）"
            if missing else "")
    return {"conclusion": CHURN,
            "reason": (f"OI {_raw_pct_str(d_oi_pct)}、净主动成交占比 {_pct_str(cvd_ratio)}、"
                       f"最近1h多单爆仓占24h成交额 {lliq_txt}，"
                       f"多空换手博弈，无明确胜负信号{lack}"),
            "metrics": signals, "confidence": "low"}


def latest_ratio_ts(merged: dict, key: str,
                    at: datetime | None = None) -> tuple[datetime | None, float | None]:
    """取多空比序列中某个 key 的最近 (ts, value)。

    各端点时间戳互不相同（takerlongshortRatio 通常比 top/global 晚/早一个周期），
    因此必须逐 key 独立取「最近值」，不能先按 timestamp 对齐再取整条记录。
    `at` 非空时只取 ≤ at 的值（用于取「高点时刻」的基准）。
    返回 `(None, None)` 表示该 key 无任何可用值。
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
    return best_ts, best_val


def latest_ratio(merged: dict, key: str, at: datetime | None = None):
    """取多空比序列中某个 key 的最近值（无值返回 None）。"""
    return latest_ratio_ts(merged, key, at)[1]


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


def window_gate(present_buckets, first_bucket: int, last_bucket: int,
                max_gap: int = MAX_MID_GAP_BUCKETS) -> tuple[bool, int, int]:
    """判定窗口连续性闸门（复验 P2-d / P3）：返回 `(ok, head_gap, mid_gap)`。

    - 统计区间 `[first_bucket, last_bucket)`：右端 `last_bucket` 是**正在采集**的桶，
      基线本就落后约 1 桶，由尾部判据（`oi_lag_sec > 2×桶`）负责，故不计入。
    - `head_gap`：**自 `first_bucket` 起**的连续缺桶数。左端缺桶语义更重——
      `base_oi = _last_at_or_before(oi_sym, peak_ts)` 会落到**窗口之外**，
      回撤窗口的 ΔOI 基准直接失真，故单列口径（复验 P3）。
    - `mid_gap`：去掉左端游程后的最长连续缺桶数（右端贴近 `last_bucket` 的缺桶也算）。
    - 判据：`max(head_gap, mid_gap) >= max_gap` → `ok=False`（拒判）。
    - ⚠️ `max_gap` 必须 ≥ 1（复验 D7）：判据是 `max(...) < max_gap`，传 0 会让
      **任何窗口都拒判**，静默停摆整条轧空判定；此处显式夹到 ≥1 兜底。
    """
    max_gap = max(1, int(max_gap))
    head_gap = mid_gap = run = 0
    n = max(0, last_bucket - first_bucket)
    for i in range(n):
        if (first_bucket + i) in present_buckets:
            if run:
                if i - run == 0:
                    head_gap = run
                else:
                    mid_gap = max(mid_gap, run)
                run = 0
        else:
            run += 1
    if run:   # 收尾：游程一直延续到 last_bucket-1
        if n - run == 0:
            head_gap = run
        else:
            mid_gap = max(mid_gap, run)
    return max(head_gap, mid_gap) < max_gap, head_gap, mid_gap


def gap_reason(head_gap: int, mid_gap: int, have: int, expect: int) -> str:
    """窗口缺桶拒判的统一文案（复验 D6：抽成纯函数以便单测守卫）。

    ⚠️ **必须以「判定窗口」开头**——`check_scan_freshness.py` 用
    `reason LIKE '判定窗口%'` 统计「拒判中 N 条 track（同一 track 多轮被拒只计 1）」，
    改前缀会让该观测**静默失联**（`test_squeeze_battle.py` 已加断言守卫）。
    """
    return (f"判定窗口内连续缺桶 {max(head_gap, mid_gap)} 个"
            f"（左端起 {head_gap} 个 / 中段 {mid_gap} 个，{have}/{expect} 桶），暂不判定")


__all__ = [
    "base_symbol", "alias_bases", "screen_surge", "confirm_squeeze",
    "evaluate_battle", "should_judge", "is_expired", "latest_ratio",
    "latest_ratio_ts", "window_gate", "gap_reason",
    "CONCLUSION_LABEL", "LONG_WIN", "SHORT_WIN", "PROFIT_TAKE", "CHURN",
]