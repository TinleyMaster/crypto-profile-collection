#!/usr/bin/env python3
"""轧空池爆仓阈值标定（复验 P1-b / P2-c：让标定可复现）。

为什么需要这个脚本（复验结论）：
  - 上一版标定只留下结论数字（「7 天 / 8018 行 / 211 币」「n=1202 越阈率 43.2%」），
    脚本未入库 ⇒ 复验按同一文字口径独立实现得到 **n=997 / 51.96% / 17.95%**，
    n 差 17%、越阈率差 8.8pp，**无法定位差异来源**（属过程不可审计）。
  - 且「7 天」前提不成立：`liquidation_snapshot` 标定时全表只覆盖约 6 小时，
    `INTERVAL '7 days'` 形同虚设，行数随时间增长（8018 / 10080 / 25296 都出现过）
    ⇒ 「8018 行」是瞬时值，不是稳定样本。
  - 判据本身也偏弱：单一实现的「点越阈率 < 20%」在另一口径下就能翻盘。

本脚本把八件事写死（第 8 条为工单 SQUEEZE-SPAN-001 追加）：
  1. **口径定义**（窗口 / 基准 / 回撤 / 振幅）写进 `variant_*` 函数，可被逐行核对；
  2. **时间边界**：每次运行都打印 min(ts)/max(ts)/实际跨度/行数，避免再拿瞬时值当样本量；
     注意 `span_hours = max(ts)-min(ts)` **有整段空洞时照样显示「连续」**，故另有 ⑥；
  3. **跨口径上界**：至少跑 3 个变体，取越阈率 max 作为判据输入（P2-c）；
  4. **分母自证**（复验 P1-1a / D2 / E5 / F3）：打印每币在分母窗口内的 5m 根数 / 期望
     （`bars / (days×288)`）与实际覆盖小时数；**整体覆盖 < 0.9、或「低于门槛（0.9）的币
     占比 > 5%」、或「覆盖率低于半格（0.5）的币占比 > 2%」时拒绝出结论**（exit 3）——只看均值会被
     少数劣质币蒙混过关（`symbols_below` 只判「< 0.9」，覆盖 0.89 与 0.10 同等对待，
     更深的尾部由「低于半格占比」兜住）。原因：阈值语义是「爆仓额 / 24h 成交额」，若
     `asset_klines` 在该窗口缺小时，分母被系统性少算 ⇒ 同一份爆仓数据算出的越阈率被放大
     **1.66×**（复验 §2；绝对数字随表滚动，一律以本次实跑为准）。
  5. **long 侧双口径**（复验 D3）：`long_liq > 0` 子集（判据所用）与**含 0** 口径
     并列输出——`long_liq = 0` 是「该 1h 无多头爆仓」的合法观测，排除它会系统性
     抬高触发频率（实测 1.34×）。
  6. **分子自证**（复验 E3）：分母自证只验 `asset_klines`，**分子（爆仓表）的时间
     连续性从未被验过**。实测爆仓表 24h 窗口内只有 11 个整点有数据、最长连续空洞
     **14 小时**，而 `span_hours` 照样显示 24.58h ⇒ 必须同时卡整点覆盖率与最长空洞。
  7. **统计判别力**（复验 E2）：判据线附近不能只看单次点估计。B 变体 n≈1.6k 时
     二项 SE ≈1pp、**95% CI 覆盖判据线 20%**，且同一 24h 窗口内各 4h 段的上界能摆
     9pp（单段 25% 必然 FAIL ↔ 单段 16% 轻松 PASS）⇒ 输出每变体 n/越阈数/SE/CI、
     跨时段上界区间，并在 CI 跨线或时段跨线时判 **decisive=False**（不可判）。
  8. **判据聚合方式 + 跨度前置门**（工单 SQUEEZE-SPAN-001 §6-A/B/C，三个 P1 同落）：
     - **A**：判据输入由「合并样本上界」改为**各 4h 段上界的中位数**（`SEGMENT_JUDGE_STAT`）——
       实证**聚合悖论**：段层面 9/15 段 PASS（中位 17.58%）而合并上界 31.47% = FAIL，
       合并让高活跃段获「样本量 × 越阈率」双重权重加成；`decisive` = 段 IQR 不跨判据线。
       ⚠️ **复验 F3（口径更正，勿误读）**：A 只改**聚合方式**、不改判据线的**可分性** ——
       段中位在 1 小时内即可跨 20% 线（实测 19.91% ↔ 22.88%），且 `--days 1` 仅 6 段、
       IQR 由线性插值自 6 点得出 ⇒ 「段中位落在 PASS 侧」**不是稳定读数**，判据仍不可判。
     - **B**：极端段（上界 ≥ `EXTREME_SEG_PCT`）**显式列出**，不并入判据但不得静默丢弃
       （单个 4h 段即可翻转合并结论）。
     - **C**：**跨度充分性前置门**（`span_sufficiency`）——表跨度 < `MIN_SPAN_DAYS`、或极端段
       观测 < `MIN_EXTREME_SEGMENTS` 次 ⇒ 直接判样本不可用（rc=3），**不给出**会被误读成
       「判据不通过」的数字。⚠️ 判据的真正卡点是**时间跨度**（方差分解：时间解释 98.7% 方差、
       抽样仅 1.3%），20~60 天是**量级估计、非承诺** —— 本脚本**不得**写「再等 N 天就能判」。

口径澄清（复验 P1-1b/c）：
  - 窗口参数默认 **1 天**——与「/24h 成交额」语义一致；旧默认 7 天会得 3× 偏差
    （实测同一数据 24.76% ↔ 8.32%）；
  - `asset_klines` 的成交额别名改叫 `vol_win`（不再叫 `vol24`，避免被误当严格 24h）；
  - 窗口高低点/收盘取 `open_time < l.ts`——旧写 `<= l.ts` 会取到**覆盖
    `[l.ts, l.ts+5m)` 的桶**，即判定时刻**之后** 5 分钟的量价（未来函数）。

只读：不写任何表。

口径 A/B 并行（P0-C / §4.3，**必须分表读**）：
  - A（现役，**混合**口径）= coin-list 全交易所**滚动 1h** 爆仓额 / Binance 滚动窗口成交额
    （`vol_win`）——线上判定所用；
  - B（新增，**单所同窗**口径）= `liquidation/history` **exchange=Binance** 的 **4h 分段增量** /
    `asset_klines` 1h 在同一 **4h 墙钟区间**的成交额和——只用于给 A 的**偏高幅度下界**定值，
    **非阈值基准**；
  - ⚠️ 两者**不可换算、不可相加**，比值**严禁**放进同一分布/同一分位（不变量 4）；
  - B 样本不足（`biz.liquidation_history` 无行，或跨所配对数 < `MIN_CROSS_PAIRS`）⇒ 直接 rc=3；
    `--no-b-gate` **仅供诊断**（输出带 `b_gate.enforced=false` 标记，不得据此出结论）。

用法：
    python calib_squeeze_liq_thr.py                  # 默认 1 天窗口 / vol_win ≥ 5e6
    python calib_squeeze_liq_thr.py --days 7 --json  # 拉长窗口 / 输出机器可读

退出码（复验 F2 四码，文本与 --json **同码**；`judge.conclusion` 由它**单一真源派生** —— 复验 G1）：
    0 = PASS（样本可用 + 有判别力 + 上界 < 判据线）
    2 = 有判别力的 FAIL（据此调阈值才有依据）
    3 = 样本不可用（分母/分子自证不过，或**无任何变体可算比率** ⇒ 任何比率都不可比）
    4 = 不可判（样本可用但 CI 或各时段跨判据线 ⇒ PASS/FAIL 只是抽样噪声）
    `judge.conclusion` = PASS / FAIL / SAMPLE_UNUSABLE / INCONCLUSIVE，与上述码位**一一对应**
    （复验 G1：不再由 `pass`/`decisive` 独立派生 —— 那条路径不读 `sample_ok`，样本不合格时
    照样打出「FAIL」，与 rc=3 / `reliable=false` 三处口径互相打架）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.analysis import squeeze as sqz  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

# 标定用的旧值与判据线（写死，便于对照；新值从 squeeze.py 实时读，避免两侧漂移）
OLD_LONG_LIQ_RATIO = 0.00008
JUDGE_UPPER_BOUND_PCT = 20.0   # 判据：条件子集越阈率**跨口径上界** < 20%
WINDOW_MIN = 60                # 条件子集的观察窗（分钟）
SURGE_MIN_PCT = 2.0            # 窗口内拉升幅度下限（%）
RETRACE_MIN_PCT = 2.0          # 距窗口高点回撤幅度下限（%）
BUCKETS_PER_DAY = 288          # 5m 桶 / 天（分母窗口完成度的期望值基准）
MIN_DENOM_COVERAGE = 0.9       # 分母窗口整体覆盖下限，低于此值拒绝出结论（exit 3）
# 复验 D2：只看「整体均值」会被少数劣质币蒙混过关（算术例：249 币里 27 币仅 10% 覆盖
# ⇒ 均值仍 0.902 过 0.9 线，而这 27 币的 vol_win 被少算 10×、越阈率被放大 10× 且照样入样污染分布）
# ⇒ 同时卡「低于门槛的币占比」。
MAX_DENOM_BELOW_PCT = 5.0      # 低于 MIN_DENOM_COVERAGE 的币占比上限（%），超过即拒绝出结论
# 复验 E5/E6：`symbols_below` 只判「< MIN_DENOM_COVERAGE」，覆盖 0.89 与 0.10 同等对待
# ⇒ 单靠它仍拦不住「少数币分母被少算 10×」（5% × 248 = 12 币可在 10% 覆盖下放行）
# ⇒ 需另有一条更严的闸门（见下）。
# 复验 F3：原「每币覆盖率 P10 < 0.5」闸门**逻辑上不可达且对目标场景无感**——
#   ① `pct()` 是线性插值分位：「300 币里 15 币覆盖率 0.49」时 k=299×0.10=29.9 落在
#      「好币」区间 ⇒ P10 仍输出 1.000，**恰好看不见 E5/E6 要抓的那个尾部**；
#   ② `P10 < 0.5` ⇒ 至少 10% 的币 < 0.5 < 0.9 ⇒ 必先触发 `MAX_DENOM_BELOW_PCT`
#      ⇒ 被数学支配、永不成为唯一触发理由（E9 想消除的「重复理由」又回来了）。
# 改为直接卡「覆盖率低于半格」的币占比：与 below 同构（故可并列打印对照），但门槛
# 更严 ⇒ 与 below **不共线**（占比落在 2%~5% 区间时可单独触发，正是目标场景）。
MIN_DENOM_HALF_COVERAGE = 0.5      # 「低于半格」的单币 5m 覆盖率门槛
MAX_DENOM_BELOW_HALF_PCT = 2.0     # 覆盖率 < 半格的币占比上限（%），超过即拒绝出结论
# 复验 E3：分子（爆仓表）时间连续性自证。分母自证只验 asset_klines，而爆仓表在窗口内
# 可有整段空洞（实测 24h 里只有 11 个整点有数据、最长连空 14h），`span_hours` 照样
# 显示「连续」⇒ 样本既不代表 24h、又高度时间聚集。
MIN_MOLECULE_HOUR_COVERAGE = 0.8   # 窗口内有数据的整点小时占比下限
MAX_MOLECULE_HOLE_H = 3            # 最长连续零行小时数上限（小时）
# 复验 E2：判据线附近必须看统计判别力——B 变体 n≈1.6k 时二项 SE ≈1pp，95% CI 覆盖
# 判据线 ⇒ 单次点估计的 PASS/FAIL 等权，不能作为阈值决策依据。
SEGMENT_HOURS = 4                  # 跨时段分桶粒度（小时），用于给出上界的时段漂移区间
MIN_SEGMENT_N = 30                 # 分桶样本量下限，低于此值不进漂移区间

# ── 工单 SQUEEZE-SPAN-001（2026-09-24）：判据从「合并样本上界」改为「段层面稳健统计」，
#    并加「跨度充分性」前置门。A/B/C 三个 P1 必须**同落**（A 单独上会把极端 regime 在语义上
#    降级为噪声，见工单 §6 反面提醒）。阈值本身一律不动。
# A（§6）：实证**聚合悖论**——段层面 9/15 段落在 PASS 侧（中位 17.58%），而合并样本上界
#    31.47% = FAIL：合并让高活跃段获得「样本量 × 越阈率」双重权重加成。⇒ 判据输入改为
#    各 4h 段上界的**中位数**（对单个极端段稳健）。
SEGMENT_JUDGE_STAT = "median"      # 判据输入：段上界的稳健统计（median / p75）
# B（§6）：极端段（段上界 ≥ 该值）**必须显式列出**，不并入判据但不得静默丢弃
#    （实证：单个 4h 段即可把合并结论从 FAIL 翻成 PASS）。
EXTREME_SEG_PCT = 40.0
# C（§6）：跨度充分性前置门 —— 表跨度 < `MIN_SPAN_DAYS`、或极端段观测 < `MIN_EXTREME_SEGMENTS`
#    次 ⇒ 直接判样本不可用（rc=3），**不给出**会被误读成「判据不通过」的数字（如 44%）。
#    依据：判据卡点是**时间跨度**不是样本量（方差分解：时间解释 98.7% 方差、抽样仅 1.3%），
#    且现有数据不足以外推（跨度扩到 48h 仍跨线，min 每 +12h 仅 +1pp）。
#    ⚠️ 20~60 天是**量级估计**（两条独立路径：稀释 / 频率估计），非「再等 N 天就能判」的承诺。
MIN_SPAN_DAYS = 30
MIN_EXTREME_SEGMENTS = 3

SQL_SAMPLE = """
WITH v AS (
    SELECT symbol, SUM(quote_vol) AS vol_win
    FROM biz.asset_klines
    WHERE interval = '5m' AND open_time >= NOW() - make_interval(days => %(days)s)
    GROUP BY symbol
    HAVING SUM(quote_vol) >= %(vol_min)s
)
SELECT l.symbol,
       l.ts,
       l.long_liq_usd_1h::float8  AS long_liq,
       l.short_liq_usd_1h::float8 AS short_liq,
       l.liq_usd_4h::float8       AS liq_4h,
       l.liq_usd_24h::float8      AS liq_24h,
       v.vol_win::float8          AS vol_win,
       w.peak_hi::float8  AS peak_hi,
       w.trough_lo::float8 AS trough_lo,
       w.peak_c::float8   AS peak_c,
       w.trough_c::float8 AS trough_c,
       cn.close_now::float8 AS close_now
FROM biz.liquidation_snapshot l
JOIN v USING (symbol)
JOIN LATERAL (
    SELECT MAX(k.high_px) AS peak_hi, MIN(k.low_px) AS trough_lo,
           MAX(k.close_px) AS peak_c, MIN(k.close_px) AS trough_c
    FROM biz.asset_klines k
    WHERE k.symbol = l.symbol AND k.interval = '5m'
      AND k.open_time > l.ts - make_interval(mins => %(win)s)
      AND k.open_time < l.ts
) w ON TRUE
JOIN LATERAL (
    SELECT k2.close_px AS close_now
    FROM biz.asset_klines k2
    WHERE k2.symbol = l.symbol AND k2.interval = '5m'
      AND k2.open_time > l.ts - make_interval(mins => %(win)s)
      AND k2.open_time < l.ts
    ORDER BY k2.open_time DESC LIMIT 1
) cn ON TRUE
WHERE l.ts >= NOW() - make_interval(days => %(days)s)
  AND l.long_liq_usd_1h IS NOT NULL AND l.long_liq_usd_1h > 0
  AND v.vol_win > 0
"""

# 复验 D3：long 侧「含 0」口径（去掉上方 `long_liq > 0` 过滤）。
# `long_liq = 0` 是「该 1h 无多头爆仓」的**合法观测**，且永远不可能越阈 ⇒ 只报过滤后子集
# 等于系统性抬高触发频率（实测两口径差 **1.34×**，绝对数字随表滚动，以实跑为准）。
# 这里只需要无条件分布，不取窗口高低点 ⇒ 省掉两个 LATERAL，开销小。
SQL_LONG_INCL0 = """
WITH v AS (
    SELECT symbol, SUM(quote_vol) AS vol_win
    FROM biz.asset_klines
    WHERE interval = '5m' AND open_time >= NOW() - make_interval(days => %(days)s)
    GROUP BY symbol
    HAVING SUM(quote_vol) >= %(vol_min)s
)
SELECT l.long_liq_usd_1h::float8 / v.vol_win::float8 AS ratio
FROM biz.liquidation_snapshot l
JOIN v USING (symbol)
WHERE l.ts >= NOW() - make_interval(days => %(days)s)
  AND l.long_liq_usd_1h IS NOT NULL
  AND v.vol_win > 0
"""


# ══ 口径 B（§4.3 P0-C）：Binance 单所 4h **分段增量** / 同一 4h 墙钟区间的成交额 ══
# 目标降级（2026-09-24 评审）：不声称「把口径拉齐」——分子端可同源，分母端窗口无法对齐
# ⇒ 口径 B 只回答一个问题：**混合口径的偏高幅度至少是多少**（下界）。
# ⚠️ 口径 B 与口径 A（滚动 1h / 24h 成交额）数值**不可换算、不可相加**，任何比较必须在
#    同一口径内做；两者的比值**不得**放进同一个分布/同一个分位计算（不变量 4）。
B_INTERVAL_HOURS = {"4h": 4, "6h": 6, "8h": 8, "12h": 12, "1d": 24}
# 跨所放大（全所 4h 增量 / Binance 同 ts 4h 增量）的配对数下限：低于此值不给下界（rc=3）。
# 依据：分子端不等式 `全所 ≥ 单所` 是**逐点**可验的结构事实，但点数太少时中位数无法代表
# 常规 regime ⇒ 与既有 `MIN_SEGMENT_N=30` 同量级取值，不另造新标准。
MIN_CROSS_PAIRS = 30

SQL_B_4H = """
SELECT l.symbol,
       l.ts,
       -- `interval` / `exchange_scope` 必须取回：`assert_single_scope()` 靠它们证明 B 侧样本
       -- 同属单一口径（不选则两列恒为 None ⇒ 不变量 4 的校验形同虚设）。
       l.interval              AS interval,
       l.exchange_scope        AS exchange_scope,
       l.long_liq_usd::float8  AS long_liq,
       l.short_liq_usd::float8 AS short_liq,
       d.vol_win::float8       AS vol_win
FROM biz.liquidation_history l
JOIN LATERAL (
    SELECT SUM(k.quote_vol) AS vol_win
    FROM biz.asset_klines k
    WHERE k.symbol = l.symbol AND k.interval = '1h'
      AND k.open_time >= l.ts
      AND k.open_time < l.ts + make_interval(hours => %(iv_hours)s)
) d ON TRUE
WHERE l.interval = %(interval)s
  AND l.exchange_scope = %(scope)s
  AND l.ts >= NOW() - make_interval(days => %(days)s)
  AND l.long_liq_usd IS NOT NULL
  AND d.vol_win > 0
"""

# 同一 (symbol, interval, ts) 上的**全所 vs Binance**配对（分子端严格不等式 `全所 ≥ 单所`）。
SQL_CROSS_EXCHANGE = """
SELECT a.symbol, a.ts,
       a.long_liq_usd::float8  AS all_long,
       b.long_liq_usd::float8  AS bin_long
FROM biz.liquidation_history a
JOIN biz.liquidation_history b
  ON b.symbol = a.symbol AND b.interval = a.interval AND b.ts = a.ts
 AND b.exchange_scope = 'binance'
WHERE a.exchange_scope = 'all'
  AND a.interval = %(interval)s
  AND a.ts >= NOW() - make_interval(days => %(days)s)
  AND a.long_liq_usd IS NOT NULL AND a.long_liq_usd > 0
  AND b.long_liq_usd IS NOT NULL AND b.long_liq_usd > 0
"""


def assert_single_scope(rows: list[dict], label: str = "") -> tuple:
    """不变量 4 的代码化：一个分布/分位只能来自**单一** (interval, exchange_scope)。

    口径 A（滚动 1h 快照）与口径 B（4h 分段增量）的比值若混进同一个分布/分位，读数即无意义
    （§4.3：两者不可换算、不可相加）⇒ 任何分布入口先过这道门：混入多组时**抛错**
    （fail-loud），而不是静默合并成一锅。
    """
    seen = {(r.get("interval"), r.get("exchange_scope")) for r in rows}
    if len(seen) > 1:
        raise ValueError(
            f"{label} 混入了多个分量口径 {sorted(seen)} ⇒ 口径 A/B 不得进同一分布（不变量 4）")
    return next(iter(seen)) if seen else (None, None)


def distribution(vals: list[float]) -> dict:
    """单口径分布（p50/p75/p90/p95 + n）。入口前置：只接受**已按口径过滤**的样本。"""
    return {"n": len(vals), "p50": pct(vals, 0.50), "p75": pct(vals, 0.75),
            "p90": pct(vals, 0.90), "p95": pct(vals, 0.95)}


def cross_exchange_lower_bound(pairs: list[dict]) -> dict:
    """跨所放大下界（分子端）：`全所 4h 增量 / Binance 同 ts 4h 增量`。

    为什么这是**下界**且方向严格（§4.3）：口径 A 的分子 = **全交易所**爆仓额 ⊇ 口径 B 的
    分子 = **Binance 单所** ⇒ 逐点 `all ≥ binance`（本函数用 `share_ge_1` 显式校验）。
    但**分母端**（Binance **24h** 成交额 vs 同一 **4h** 区间成交额）无法在同一窗口对齐
    ⇒「跨所放大」与「分母窗口错配」两个来源**不可分离** ⇒ 本倍数**只能读作下界**，
    **不得**据此直推「阈值应平移多少」。

    `ok=False` 时（配对数不足 / 出现 `all < binance` 的反例）不给数字结论，调用侧据此 rc=3。
    """
    ratios = [p["all_long"] / p["bin_long"] for p in pairs if p["bin_long"]]
    reasons: list[str] = []
    if len(ratios) < MIN_CROSS_PAIRS:
        reasons.append(f"跨所配对数 {len(ratios)} < {MIN_CROSS_PAIRS}"
                       f"（口径 B 样本不足 ⇒ 无法给出偏高幅度下界）")
    bad = [r for r in ratios if r < 1.0]
    if bad:
        # `all ≥ binance` 是结构必然（全所 ⊇ 单所）⇒ 反例说明取数/口径出问题，不能只当噪声
        reasons.append(f"出现 {len(bad)} 个 `全所 < Binance` 的反例（结构性不可能）"
                       f"⇒ 口径或取数有误，先排查，不得据此定阈值")
    return {"ok": not reasons, "reasons": reasons, "n_pairs": len(ratios),
            "min_pairs": MIN_CROSS_PAIRS,
            "ratio_median": pct(ratios, 0.50), "ratio_p25": pct(ratios, 0.25),
            "ratio_p75": pct(ratios, 0.75),
            "share_ge_1": (sum(1 for r in ratios if r >= 1.0) / len(ratios)) if ratios else None,
            "note": "下界：分子端同源可比（全所 ⊇ 单所），分母端窗口（24h vs 4h）错配不可分离"}


def rolling_scale_groups(rows: list[dict], key: str, thr: float) -> dict:
    """P0-A 只读分组维度：按已落库的**滚动窗口**列（`liq_usd_4h` / `liq_usd_24h`）分桶。

    仅用于标定/诊断（§4.1「允许④」），**不得**作为 5m 窗口判定的输入（N2）。
    分桶按**三分位**切（P33/P66），每桶给 n 与越阈率；缺失值单列一组（缺失≠0）。
    """
    vals_all = sorted(r[key] for r in rows if r.get(key) is not None)
    if len(vals_all) < 3:
        return {"key": key, "n": 0, "buckets": [], "threshold": thr}
    q1, q2 = pct(vals_all, 1 / 3), pct(vals_all, 2 / 3)
    buckets = [
        {"label": f"< P33（< {q1:.4g}）", "n": 0, "rate_pct": None, "vals": []},
        {"label": f"P33~P66（{q1:.4g}~{q2:.4g}）", "n": 0, "rate_pct": None, "vals": []},
        {"label": f"> P66（> {q2:.4g}）", "n": 0, "rate_pct": None, "vals": []},
        {"label": "缺失（NULL）", "n": 0, "rate_pct": None, "vals": []},
    ]
    for r in rows:
        v = r.get(key)
        if v is None:
            idx = 3
        elif v < q1:
            idx = 0
        elif v <= q2:
            idx = 1
        else:
            idx = 2
        buckets[idx]["n"] += 1
        buckets[idx]["vals"].append(r["long_liq"] / r["vol_win"])
    for b in buckets:
        b["rate_pct"] = rate(b["vals"], thr)
        b["p50_ratio"] = pct(b["vals"], 0.50)
        del b["vals"]
    return {"key": key, "n": len(rows), "buckets": buckets, "threshold": thr,
            "note": "滚动窗口列仅作**分组维度**（跨币横截面可比）；不进 5m 判定、禁跨桶差分"}


def pct(vals: list[float], q: float) -> float | None:
    """线性插值分位（等价 `percentile_cont`）。"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rate(vals: list[float], thr: float) -> float | None:
    """越阈率（%）。"""
    if not vals:
        return None
    return sum(1 for x in vals if x >= thr) / len(vals) * 100


def _f(v, spec: str = ".3e", dash: str = "n/a") -> str:
    """None 安全的数值格式化（分位在空样本上是 None，不能直接 f-string）。"""
    return dash if v is None else format(v, spec)


def _pct_s(v) -> str:
    """百分比格式化（None 安全）。"""
    return "n/a" if v is None else f"{v:.2f}%"


def exit_code(pass_: bool, sample_ok: bool, decisive: bool, measurable: bool = True) -> int:
    """四码退出码（复验 F2）：把「不可判」从「有判别力的 FAIL」里拆出来。

    E1 修好了「退出码与结论相反」，但把矛盾推进了一层：旧码 `rc=2` **同时**表示
    「有判别力的 FAIL」与「无判别力的不可判」，而 E2 的全部论点恰恰是这两者在当前样本量下
    **等权**。只看 rc 的下游（cron / CI / 看板）仍会把「不可判」读成「判据明确不通过」，
    进而得出「该调阈值」的错误推论 ⇒ 必须有独立码位：

        0 = PASS（样本可用 + 有判别力 + 上界 < 判据线）
        2 = 有判别力的 FAIL（据此调阈值才有依据）
        3 = 样本不可用（分母/分子自证不过，或**无任何变体可算比率**）
        4 = 不可判（样本可用但 CI 或各时段跨判据线 ⇒ PASS/FAIL 只是抽样噪声）

    复验 G3：`measurable=False`（无任何变体命中 ⇒ 上界根本算不出来）旧码落到 **rc=4**，
    而 rc=4 的语义是「样本可用、只是判据不具判别力」—— 两者不同源 ⇒ 状态机未穷尽。
    并入 3（「不可比」），因为它同样是「任何比率都不成立」而非「判据不可判」。
    """
    if not sample_ok or not measurable:
        return 3
    # 复验 F1：`decisive` 必须**早于** `pass_` 判定 —— 旧序 `if pass_: return 0` 会把
    # 「PASS 侧但段 IQR 跨判据线」判成 rc=0，与本文档「0 = 样本可用 + **有判别力** +
    # 上界 < 线」自相矛盾（且 `judge.decisive` 会由码表假报 True）。当下跨度门先拦、
    # 不可达，但跨度门一放行即自动激活 —— 恰是本工单的目标状态。
    if not decisive:
        return 4
    return 0 if pass_ else 2


# 复验 G1：`judge.conclusion` 与退出码的**唯一映射表**（由 `exit_code()` 派生，
# 不再各自独立计算）。旧码的 `conclusion` 由 `pass`/`decisive` 另算一遍、完全不读
# `sample_ok` ⇒ 实跑 `--days 7` 出现 `sample_ok=false` + `conclusion="FAIL"`（rc=3）。
# 复验 I1/I3：**所有**结论类字段一律走映射表 —— 同一缺陷类已五度复发（D1 `pass` →
# F2 `decisive` → G1 `conclusion` → H1 `reliable` → 本轮 `decisive`/`ci_decisive`），
# 每次都是「改好一个、兄弟字段仍是各自派生」。映射表对码表扩展会 **KeyError 炸响**
# （fail-loud），而 `rc != 3` / `rc in (0,2)` 这类写法会**静默误报** ⇒ 统一用表。
CONCLUSION_BY_CODE = {0: "PASS", 2: "FAIL", 3: "SAMPLE_UNUSABLE", 4: "INCONCLUSIVE"}
RELIABLE_BY_CODE = {0: True, 2: True, 3: False, 4: True}      # judge.reliable
DECISIVE_BY_CODE = {0: True, 2: True, 3: False, 4: False}     # judge.decisive（具判别力）


def ci_margin_pp(ci: tuple[float, float] | None, line: float) -> float | None:
    """CI 两界距判据线的**带符号**裕度（百分点，复验 G4 + H4）。

    为什么需要：`ci_decisive` 是布尔化判定，贴线时的边界性被完全掩盖 —— 实测
    `--days 7` 的 CI = [20.01, 23.54] 对判据线 20.00% 只差 **0.01pp** 即判「有判别力」，
    而 0.01pp 远在任何统计误差之内 ⇒ 该 `ci_decisive=true` 实质是掷硬币。

    为什么必须**带符号**（复验 H4）：判据本身是有向的（上界须 `< 20%`），而旧版取绝对值
    ⇒ `[10,15]` 与 `[25,30]` 对线 20 都返回 `5.0`，**裕度同值但结论相反**（前者在 PASS 侧、
    后者在 FAIL 侧）。故定为：**正 = CI 整段在判据线下方（PASS 侧）**、**负 = 整段在上方
    （FAIL 侧）**、**0.0 = 已跨线（两侧皆有，谈不上裕度）**。
    """
    if not ci:
        return None
    lo, hi = ci
    if hi < line:
        return round(line - hi, 2)      # 整段在线下方 ⇒ PASS 侧 ⇒ 正（越大越稳）
    if lo > line:
        return round(line - lo, 2)      # 整段在线上方 ⇒ FAIL 侧 ⇒ 负（越负越差）
    return 0.0                          # 跨线 ⇒ 裕度为零


def denominator_coverage(cur, syms: list[str], days: int) -> dict:
    """分母窗口自证（复验 P1-1a）：每币 5m 根数 / 期望 + 实际覆盖小时数。

    `vol_win` = `SUM(quote_vol)` over `days` 天，其可用性完全取决于 `asset_klines`
    在该窗口内的完成度：表缺小时 ⇒ 分母被少算 ⇒ 越阈率整体被放大
    （实测同一份爆仓数据：完整分母 24.58% ↔ 缺 14h 分母 40.21%，差 1.66×）。
    只评估「本次入样币集合」——被少算的正是这些币，越阈率被放大的也是它们。
    """
    expect_bars = max(1, days * BUCKETS_PER_DAY)
    expect_hours = max(1, days * 24)
    out = {"days": days, "symbols": len(syms), "expect_bars_per_symbol": expect_bars,
           "expect_hours": expect_hours, "bars_min": None, "bars_median": None,
           "bars_max": None, "hours_median": None, "coverage": None,
           "bars_ratio_p10": None, "symbols_below": 0, "symbols_below_pct": 0.0,
           "symbols_below_half": 0, "symbols_below_half_pct": 0.0,
           "worst": [], "per_symbol": []}
    if not syms:
        return out
    cur.execute(
        "SELECT symbol, count(*) AS bars, "
        "       count(DISTINCT date_trunc('hour', open_time)) AS hours "
        "FROM biz.asset_klines "
        "WHERE interval = '5m' "
        "  AND open_time >= NOW() - make_interval(days => %(days)s) "
        "  AND symbol = ANY(%(syms)s) "
        "GROUP BY symbol", {"days": days, "syms": syms})
    cnt = {r["symbol"]: (int(r["bars"]), int(r["hours"])) for r in cur.fetchall()}
    per = [{"symbol": s, "bars": cnt.get(s, (0, 0))[0], "hours": cnt.get(s, (0, 0))[1],
            "bars_ratio": round(cnt.get(s, (0, 0))[0] / expect_bars, 3)} for s in syms]
    bars = sorted(d["bars"] for d in per)
    hours = sorted(d["hours"] for d in per)
    out["per_symbol"] = per
    out["bars_min"], out["bars_max"] = bars[0], bars[-1]
    out["bars_median"] = bars[len(bars) // 2]
    out["hours_median"] = hours[len(hours) // 2]
    out["coverage"] = sum(bars) / (len(syms) * expect_bars)
    out["symbols_below"] = sum(1 for b in bars if b < expect_bars * MIN_DENOM_COVERAGE)
    out["symbols_below_pct"] = round(out["symbols_below"] / len(syms) * 100, 2)
    # 复验 F3：「低于半格」的币 —— 真正参与闸门的那条（P10 降级为纯观察项，见常量注释）。
    out["symbols_below_half"] = sum(
        1 for b in bars if b < expect_bars * MIN_DENOM_HALF_COVERAGE)
    out["symbols_below_half_pct"] = round(
        out["symbols_below_half"] / len(syms) * 100, 2)
    out["bars_ratio_p10"] = pct([b / expect_bars for b in bars], 0.10)
    out["worst"] = sorted(per, key=lambda d: d["bars_ratio"])[:5]
    return out


def molecule_coverage(cur, days: int) -> dict:
    """分子窗口自证（复验 E3）：爆仓表在窗口内的**整点覆盖**与**最长连续空洞**。

    为什么需要：`denominator_coverage()` 只验 `asset_klines`，而 `table_bound.span_hours`
    是 `max(ts)-min(ts)` —— **有整段空洞时照样显示「24h 连续」**。实测爆仓表 24h 窗口内
    只有 11 个整点有数据、最长连续空洞 14 小时，用它算「/24h 越阈率」既不代表性、样本
    又高度时间聚集（正是复验 E2 统计判别力不足的一半成因）。

    只统计**已走完的整点**（不含当前正在累积的小时），故分母恰为 `days×24`。

    复验 I5：`hours_present_ratio` 的分母**必须用 `expect_hours`**（= 本 docstring 的口径），
    曾用 `len(hist)`（`generate_series` 的实际项数）—— 二者当前恒等（死耦合），但 H3 起
    `missing_hours`（`expect_hours - hours_present`）与展示的 `ratio` 被**同一条折叠判据**绑定，
    一旦网格定义变更（如改为含当前小时）就会印出「分母不一致的一对数字」⇒ 统一同源取值。

    复验 J3：`present` 还可以 **> `expect_hours`**（网格成为 expect 的超集时）⇒ `ratio` 须
    夹到 `1.0`，否则会印出「覆盖率 120.8% 而缺失 0h」的自相矛盾 —— 那正是 I5 要消除的
    那类不一致的**镜像版**。prod 当前不可达（网格由 `generate_series(expect_hours)` 生成
    ⇒ `grid ≡ expect`），但 I5 的动机场景（网格定义变更）本身就会命中。
    """
    expect_hours = max(1, days * 24)
    cur.execute(
        "SELECT count(l.ts) AS n "
        "FROM generate_series(date_trunc('hour', NOW()) - make_interval(hours => %(hours)s), "
        "                     date_trunc('hour', NOW()) - interval '1 hour', "
        "                     interval '1 hour') gs "
        "LEFT JOIN biz.liquidation_snapshot l "
        "       ON date_trunc('hour', l.ts) = gs "
        "GROUP BY gs ORDER BY gs", {"hours": expect_hours})
    hist = [int(r["n"]) for r in cur.fetchall()]
    present = sum(1 for n in hist if n > 0)
    hole = run = 0
    for n in hist:
        run = 0 if n > 0 else run + 1
        hole = max(hole, run)
    return {"expect_hours": expect_hours, "grid_hours": len(hist),
            "hours_present": present,
            # 复验 J3：上夹到 1.0（超集网格下 `present` 可 > `expect_hours`）。
            "hours_present_ratio": round(min(1.0, present / expect_hours), 4),
            "max_hole_hours": hole, "hourly_rows": hist}


def molecule_fail_reasons(mol: dict) -> list[str]:
    """分子自证的不合格理由（整点覆盖 / 最长连续空洞），**同源折叠为一条**（复验 E9 + G5 + H3）。

    为什么折叠：单段长空洞会**同时**压低整点覆盖率 ⇒ 旧码一次给出两条理由（① 覆盖 ② 空洞），
    读者会读成「两个问题」而实际只有一个 —— 正是 E9 想消除的「同一份数据多条重复理由」，
    只是分母组当时收敛了、分子组没有。

    折叠判据（复验 H3 更正）：必须用**观测相对**而非**门槛相对** ——
    「空洞自身**足以使**覆盖不达标」（`hole > expect × (1 - 门槛)`）只是**充分条件**，
    它**不等于**「空洞自身**解释了实测**覆盖率」。prod `--days 7` 的反例：
    `hole=139h`、`missing=168-15=153h` ⇒ 空洞最多只能把覆盖压到 `1-139/168 = 17.3%`，
    而实测 8.9% —— 另有 14h 散点缺失，**是两个独立缺陷**；门槛相对判据却判「同源」，
    既在文案里印出**事实错误的因果**，又把「散点缺失」吞掉（G5 的反向错误：实际两个问题
    被读成一个）。⇒ 判据收紧为「该空洞**即窗口内的全部缺失**」（`hole ≥ missing`）：
    此时折叠才是真的同源；否则散点缺失与局部长洞是彼此独立的缺陷，照报两条。
    """
    fails: list[str] = []
    ratio = mol["hours_present_ratio"] or 0.0
    missing_hours = max(0, mol["expect_hours"] - mol["hours_present"])
    cov_fail = ratio < MIN_MOLECULE_HOUR_COVERAGE
    hole_fail = mol["max_hole_hours"] > MAX_MOLECULE_HOLE_H
    hole_is_all_missing = mol["max_hole_hours"] >= missing_hours
    if cov_fail and hole_fail and hole_is_all_missing:
        fails.append(
            f"分子最长连续空洞 {mol['max_hole_hours']}h > {MAX_MOLECULE_HOLE_H}h，"
            f"且它**即窗口内的全部缺失**（{missing_hours}h）⇒ 整点覆盖 {ratio:.1%} < "
            f"{MIN_MOLECULE_HOUR_COVERAGE:.0%} 只是它的后果（同源 ⇒ 折叠为一条）")
    else:
        if cov_fail:
            fails.append(f"分子整点覆盖 {mol['hours_present']}/{mol['expect_hours']}"
                         f" = {ratio:.1%} < {MIN_MOLECULE_HOUR_COVERAGE:.0%}"
                         f"（缺失 {missing_hours}h"
                         + (f"，其中空洞仅占 {mol['max_hole_hours']}h ⇒ 另有散点缺失"
                            if hole_fail else "，无超限长空洞 ⇒ 呈分散形态") + "）")
        if hole_fail:
            fails.append(f"分子最长连续空洞 {mol['max_hole_hours']}h > {MAX_MOLECULE_HOLE_H}h")
    return fails


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """二项比例的 Wilson 95% 置信区间（%）。比正态近似在小 n / 极端比例下更稳。"""
    if n <= 0:
        return None
    # 复验 F6b：`k > n` 时 `p(1-p) < 0` ⇒ `math.sqrt` 抛 ValueError，无入参守卫。
    # 脚本内 `k ≤ n` 恒成立，但纯函数被复用/注入时不该崩 ⇒ 夹取到 [0, n]。
    k = max(0, min(k, n))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    hw = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return ((c - hw) * 100, (c + hw) * 100)


def segment_upper_bounds(rows: list[dict], new_thr: float) -> dict:
    """跨时段上界的漂移区间（复验 E2）：按 `SEGMENT_HOURS` 分桶，各段独立算跨口径上界。

    同一 24h 窗口内各段上界实测能摆 9pp（某段必然 FAIL ↔ 另一段轻松 PASS）⇒ 单次点估计
    的 PASS/FAIL 完全取决于「窗口里恰好装了哪几段」。样本量低于 `MIN_SEGMENT_N` 的段跳过。
    """
    out = {"segment_hours": SEGMENT_HOURS, "n_segments": 0, "segments": [],
           "min_pct": None, "median_pct": None, "max_pct": None, "skipped": 0}
    if not rows:
        return out
    t_max = max(r["ts"] for r in rows)
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        idx = int((t_max - r["ts"]).total_seconds() // (SEGMENT_HOURS * 3600))
        buckets.setdefault(idx, []).append(r)
    segs = []
    for idx in sorted(buckets, reverse=True):
        seg_rows = buckets[idx]
        if len(seg_rows) < MIN_SEGMENT_N:
            out["skipped"] += 1
            continue
        # 复验 F4：守卫必须判**变体过滤后**的样本量。`rate()` 的分母是
        # `[r for r in seg_rows if fn(r)]`，可远小于 `len(seg_rows)`；旧码只判段内总行数
        # ⇒ 段内塞 1 行「仅某变体命中且越阈」即可把该变体的 rate 拉到 100%，**1 行（占
        # 样本 0.3%）就能把「不跨线」翻成「跨线」** ⇒ decisive 被单条观测操纵（反向也
        # 成立：1 行不越阈可掩盖真实跨线）。⇒ 逐变体判 n，不足者不进该段，并把各变体
        # 的 n 一并输出（`n_by_variant`），便于人核对 straddle 究竟由谁决定。
        rates, n_by = [], {}
        for vname, fn in VARIANTS.items():
            sub = [r["long_liq"] / r["vol_win"] for r in seg_rows if fn(r)]
            n_by[vname] = len(sub)
            if len(sub) < MIN_SEGMENT_N:
                continue
            x = rate(sub, new_thr)
            if x is not None:
                rates.append(x)
        if not rates:
            out["skipped"] += 1
            continue
        segs.append({"start_ts": min(r["ts"] for r in seg_rows).isoformat(),
                     "rows": len(seg_rows), "n_by_variant": n_by,
                     "upper_bound_pct": max(rates)})
    vals = sorted(s["upper_bound_pct"] for s in segs)
    out.update({"n_segments": len(segs), "segments": segs,
                "min_pct": vals[0] if vals else None,
                "median_pct": pct(vals, 0.5),
                "max_pct": vals[-1] if vals else None})
    return out


def segment_judge(segs: list[dict], line: float) -> dict:
    """段层面稳健统计（工单 SQUEEZE-SPAN-001 §6-A/B）。

    为什么改（§6-A）：实证**聚合悖论**——段层面 9/15 段落在 PASS 侧（中位 17.58%），而合并
    样本上界 31.47% = FAIL：合并让高活跃段获得「样本量 × 越阈率」双重权重加成，与段层面多数
    结论相悖。⇒ 判据输入改为各段上界的**中位数**（对单个极端段稳健）。

    B（§6）：极端段（段上界 ≥ `EXTREME_SEG_PCT`）**必须显式列出**，不并入判据但不得静默丢弃
    （实证：单个 4h 段——占总时长 5.6%、占行数 9.6%——即可把合并结论从 FAIL 翻成 PASS）。

    `decisive`：段 IQR 不跨判据线（P75 < 线 或 P25 > 线）才算「有判别力」——比旧的
    「各段 min/max 跨线」对极端段稳健（旧判据被单个极端段直接判 INCONCLUSIVE）。
    """
    out = {"stat": SEGMENT_JUDGE_STAT, "n_segments": 0, "value_pct": None,
           "min_pct": None, "p25_pct": None, "median_pct": None, "p75_pct": None,
           "max_pct": None, "decisive": False,
           "extreme_pct": EXTREME_SEG_PCT, "extreme_segments": [], "extreme_count": 0}
    if not segs:
        return out
    bounds = [s["upper_bound_pct"] for s in segs]
    s = sorted(bounds)
    out["n_segments"] = len(s)
    out["min_pct"], out["max_pct"] = s[0], s[-1]
    out["p25_pct"] = pct(s, 0.25)
    out["median_pct"] = pct(s, 0.5)
    out["p75_pct"] = pct(s, 0.75)
    out["value_pct"] = (out["median_pct"] if SEGMENT_JUDGE_STAT == "median"
                        else out["p75_pct"])
    out["extreme_segments"] = [
        {"start_ts": x["start_ts"], "upper_bound_pct": x["upper_bound_pct"]}
        for x in segs if x["upper_bound_pct"] >= EXTREME_SEG_PCT]
    out["extreme_count"] = len(out["extreme_segments"])
    out["decisive"] = bool(out["p75_pct"] < line or out["p25_pct"] > line)
    return out


def span_sufficiency(span_hours: float, extreme_count: int) -> dict:
    """跨度充分性前置门（工单 SQUEEZE-SPAN-001 §6-C）。

    为什么需要：判据卡点是**时间跨度**不是样本量（方差分解：时间解释 98.7% 方差、抽样仅
    1.3%），且现有 71h 数据不足以外推（跨度扩到 48h 仍跨线，min 每 +12h 仅 +1pp）。
    ⇒ 表跨度 < `MIN_SPAN_DAYS`、或极端段观测 < `MIN_EXTREME_SEGMENTS` 次时，**直接判样本
    不可用（rc=3）**，不给出会被误读成「判据不通过」的数字（如 44%）。

    ⚠️ 这是**量级估计**不是点估计：极端段在 71h 内只观测到 1 次，泊松 95% CI 是 0.025~5.57 倍；
    20~60 天的量级来自两条独立路径（稀释 / 频率估计），**不构成「再等 N 天就能判」的承诺**。
    """
    reasons: list[str] = []
    if span_hours < MIN_SPAN_DAYS * 24:
        reasons.append(f"表跨度 {span_hours:.1f}h < {MIN_SPAN_DAYS} 天（判据需跨 regime，"
                       f"当前仅覆盖单一「暴动→平复」周期）")
    if extreme_count < MIN_EXTREME_SEGMENTS:
        reasons.append(f"极端段（段上界 ≥{EXTREME_SEG_PCT:.0f}%）仅观测到 {extreme_count} 次 "
                       f"< {MIN_EXTREME_SEGMENTS} 次（无法估计其出现频率）")
    return {"ok": not reasons, "reasons": reasons, "span_hours": round(span_hours, 2),
            "extreme_count": extreme_count, "min_span_days": MIN_SPAN_DAYS,
            "min_extreme_segments": MIN_EXTREME_SEGMENTS}


def primary_gate(denom_ok: bool, molecule_ok: bool, span_ok: bool,
                 b_ok: bool = True) -> str | None:
    """拒绝出结论时的**唯一充分因**（优先级：分母 > 分子 > 跨度 > 口径 B）。

    复验 F4：三道门并列进 `sample_ok`，输出只说「拒绝出结论」⇒ 默认参数下三处并发失败时，
    读者会误以为「是跨度门拦下的」（实测 `--days 1` 分母门亦不过）。按优先级标出归因，
    其余门只作附注。

    P0-C（§4.3）：新增第四道门「口径 B 样本门」——口径 B（4h 分段增量）配对数不足时
    「偏高幅度下界」根本算不出来 ⇒ 同样属「任何比率都不成立」，排在最末（它的样本来自
    P1 回填，与 A 侧的自证互相独立）。
    """
    if not denom_ok:
        return "分母门"
    if not molecule_ok:
        return "分子门"
    if not span_ok:
        return "跨度门"
    if not b_ok:
        return "口径B门"
    return None


def variant_a(r) -> bool:
    """A：窗口内拉升（高低点振幅）≥2% 且 已回撤 ≥2%。"""
    if not (r["peak_hi"] and r["trough_lo"] and r["close_now"] and r["trough_lo"] > 0):
        return False
    amp = (r["peak_hi"] - r["trough_lo"]) / r["trough_lo"] * 100
    retrace = (r["peak_hi"] - r["close_now"]) / r["peak_hi"] * 100
    return amp >= SURGE_MIN_PCT and retrace >= RETRACE_MIN_PCT


def variant_b(r) -> bool:
    """B：同 A，但高点/低点改用收盘价（只有 close 时才代表「已实现」涨跌）。"""
    if not (r["peak_c"] and r["trough_c"] and r["close_now"] and r["trough_c"] > 0):
        return False
    amp = (r["peak_c"] - r["trough_c"]) / r["trough_c"] * 100
    retrace = (r["peak_c"] - r["close_now"]) / r["peak_c"] * 100
    return amp >= SURGE_MIN_PCT and retrace >= RETRACE_MIN_PCT


def variant_c(r) -> bool:
    """C：只要求已回撤 ≥2%（不加拉升条件 —— 回撤本身必然蕴含振幅）。"""
    if not (r["peak_hi"] and r["close_now"]):
        return False
    retrace = (r["peak_hi"] - r["close_now"]) / r["peak_hi"] * 100
    return retrace >= RETRACE_MIN_PCT


VARIANTS = {"A 振幅(hi/lo)+回撤": variant_a,
            "B 振幅(close)+回撤": variant_b,
            "C 仅回撤": variant_c}


def main() -> int:
    ap = argparse.ArgumentParser(description="轧空池爆仓阈值标定（只读）")
    ap.add_argument("--days", type=int, default=1,
                    help="样本窗口天数（默认 1，与「/24h 成交额」语义一致；旧默认 7 会得 3× 偏差）")
    ap.add_argument("--vol-win-min", "--vol24-min", dest="vol_win_min", type=float,
                    default=5e6, help="vol_win 下限（USDT，别名 --vol24-min）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--b-interval", default="4h", choices=sorted(B_INTERVAL_HOURS),
                    help="口径 B 的粒度（默认 4h，= P1 回填粒度）")
    ap.add_argument("--b-scope", default="binance", choices=["binance", "all"],
                    help="口径 B 的交易所口径（默认 binance=单所）")
    ap.add_argument("--no-b-gate", action="store_true",
                    help="诊断用：不因口径 B 样本不足而 rc=3（**不得**用于出结论）")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
        conn.readonly = True
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT min(ts) AS mn, max(ts) AS mx, count(*) AS n, "
                "       count(DISTINCT symbol) AS syms "
                "FROM biz.liquidation_snapshot")
            bound = cur.fetchone()
            cur.execute(SQL_SAMPLE, {"days": args.days, "vol_min": args.vol_win_min,
                                     "win": WINDOW_MIN})
            rows = cur.fetchall()
            # 分母自证（复验 P1-1a）：入样币集合上的 5m 根数 / 覆盖小时
            denom = denominator_coverage(cur, sorted({r["symbol"] for r in rows}), args.days)
            # 分子自证（复验 E3）：爆仓表整点覆盖 / 最长连续空洞
            mol = molecule_coverage(cur, args.days)
            # long 侧「含 0」口径（复验 D3）
            cur.execute(SQL_LONG_INCL0, {"days": args.days, "vol_min": args.vol_win_min})
            ratios_incl0 = [r["ratio"] for r in cur.fetchall()]
            # ── 口径 B（P0-C）：Binance 单所 4h 分段增量 / 同一 4h 区间成交额 ──
            # 独立查询、独立分布（§4.3）：与口径 A 的比值**不得**进同一分位（不变量 4）。
            cur.execute(SQL_B_4H, {"days": args.days, "interval": args.b_interval,
                                   "scope": args.b_scope,
                                   "iv_hours": B_INTERVAL_HOURS[args.b_interval]})
            b_rows = [dict(r) for r in cur.fetchall()]
            # 跨所放大（分子端）：全所 vs Binance 同 ts 配对
            cur.execute(SQL_CROSS_EXCHANGE, {"days": args.days, "interval": args.b_interval})
            cross_pairs = [dict(r) for r in cur.fetchall()]

    span_h = ((bound["mx"] - bound["mn"]).total_seconds() / 3600) if bound["mn"] else 0.0
    ratios = [r["long_liq"] / r["vol_win"] for r in rows]
    short_all = [r["short_liq"] / r["vol_win"] for r in rows
                 if r["short_liq"] is not None]
    short_gt0 = [r["short_liq"] / r["vol_win"] for r in rows
                 if r["short_liq"] is not None and r["short_liq"] > 0]

    p50, p75, p90, p95 = (pct(ratios, q) for q in (0.50, 0.75, 0.90, 0.95))
    new_thr = sqz.LONG_LIQ_RATIO_THR
    seg = segment_upper_bounds(rows, new_thr)

    # ── 口径 B（P0-C，§4.3）：独立分布 + 跨所放大**下界** ───────────────────
    # 不变量 4：B 侧样本必须同属单一 (interval, exchange_scope)（混入即抛错，不静默合并）。
    b_scope_seen = assert_single_scope(b_rows, "口径 B")
    b_ratios = [r["long_liq"] / r["vol_win"] for r in b_rows if r["vol_win"]]
    b_dist = distribution(b_ratios)
    cross = cross_exchange_lower_bound(cross_pairs)
    b_reasons = list(cross["reasons"])
    if not b_rows:
        b_reasons.append(f"口径 B（{args.b_interval} / {args.b_scope}）无任何样本 "
                         f"⇒ 偏高幅度下界不可算（先在 P1 回填 biz.liquidation_history）")
    b_gate = {"ok": not b_reasons, "reasons": b_reasons}
    # 诊断逃生门：--no-b-gate 只用于排查，**不得**据此出结论（输出中带 warned 标记）
    b_gate_enforced = b_gate["ok"] or not args.no_b_gate

    # ── P0-A：滚动窗口列的**只读分组维度**（交叉截面可比；不进 5m 判定）────────
    rolling_groups = {
        "liq_usd_4h": rolling_scale_groups(rows, "liq_4h", new_thr),
        "liq_usd_24h": rolling_scale_groups(rows, "liq_24h", new_thr),
    }

    subset: dict[str, list[float]] = {}
    for name, fn in VARIANTS.items():
        subset[name] = [r["long_liq"] / r["vol_win"] for r in rows if fn(r)]

    # ── 样本可用性闸门（复验 P1-1a / D2 / E3 / E5 / E6）────────────────
    cov = denom["coverage"] or 0.0
    below_pct = denom["symbols_below_pct"] or 0.0
    half_pct = denom["symbols_below_half_pct"] or 0.0
    denom_fail: list[str] = []
    if cov < MIN_DENOM_COVERAGE:
        # 复验 E9：整体覆盖不达标时另两条必然同真（同一数据下共线）⇒ 只报它，避免同一份
        # 数据给出三条互相重复的理由（半格币数 / 低于门槛币数仍打印在上方「分母自证」节）。
        denom_fail.append(f"整体覆盖 {cov:.3f} < {MIN_DENOM_COVERAGE}")
    else:
        # 复验 F3：「低于门槛（0.9）」与「低于半格（0.5）」满足包含关系（<0.5 ⇒ <0.9），
        # 但阈值不同 ⇒ **不共线**（占比落在 2%~5% 区间时半格可单独触发，正是 E5/E6 的
        # 目标场景）；同时沿用 E9 的原则「同一份数据只报最重一条」——半格不合格时不再
        # 重复打印门槛那条（两条理由指向同一批币）。
        if half_pct > MAX_DENOM_BELOW_HALF_PCT:
            denom_fail.append(
                f"覆盖率低于半格的币 {denom['symbols_below_half']}/{denom['symbols']}"
                f" = {half_pct:.1f}% > {MAX_DENOM_BELOW_HALF_PCT}%"
                "（这些币分母被少算数倍 ⇒ 比率被放大且照样入样污染分布）")
        elif below_pct > MAX_DENOM_BELOW_PCT:
            denom_fail.append(f"低于门槛的币 {denom['symbols_below']}/{denom['symbols']}"
                              f" = {below_pct:.1f}% > {MAX_DENOM_BELOW_PCT}%")
    # 复验 E9 / G5：不合格理由由纯函数组装（同源折叠，见 `molecule_fail_reasons`）。
    molecule_fail: list[str] = molecule_fail_reasons(mol)
    denom_ok = not denom_fail
    molecule_ok = not molecule_fail
    # 工单 SQUEEZE-SPAN-001 §6-A/B：判据输入 = 各 4h 段上界的**中位数**（对单个极端段稳健）；
    # 极端段（≥ EXTREME_SEG_PCT）显式列出（不并入判据，但不得静默丢弃）。
    sj = segment_judge(seg["segments"], JUDGE_UPPER_BOUND_PCT)
    # §6-C：跨度充分性前置门（表跨度 / 极端段观测次数不足 ⇒ 样本不可用，不给判定数字）。
    span_suf = span_sufficiency(span_h, sj["extreme_count"])
    span_ok = span_suf["ok"]
    # 复验 E3：分母与分子**都要**自证通过，样本才可用（旧码只看分母，而爆仓表实测
    # 24h 里只有 11 个整点有数据、最长连空 14h ⇒ 分子不合格时算出的越阈率同样无意义）。
    # 工单 §6-C：再加「跨度充分性」——它才是判据的真正卡点（时间解释 98.7% 方差）。
    # P0-C（§4.3）：再加「口径 B 样本门」——口径 B 无样本 / 跨所配对不足时「偏高幅度下界」
    # 根本算不出来 ⇒ 同属「任何比率都不成立」。`--no-b-gate` 仅供诊断（见 `b_gate.enforced`）。
    sample_ok = denom_ok and molecule_ok and span_ok and b_gate_enforced
    out = {
        "table_bound": {
            "min_ts": bound["mn"].isoformat() if bound["mn"] else None,
            "max_ts": bound["mx"].isoformat() if bound["mx"] else None,
            "span_hours": round(span_h, 2),
            "rows": bound["n"], "symbols": bound["syms"],
        },
        "denominator": denom,
        "denominator_ok": denom_ok,
        "molecule": mol,
        "molecule_ok": molecule_ok,
        "sample_ok": sample_ok,
        "sample": {"rows": len(rows), "symbols": len({r["symbol"] for r in rows}),
                   "window_days_arg": args.days, "vol_win_min": args.vol_win_min},
        "unconditional": {
            "p50": p50, "p75": p75, "p90": p90, "p95": p95,
            "old_thr": OLD_LONG_LIQ_RATIO, "new_thr": new_thr,
            "rate_old_pct": rate(ratios, OLD_LONG_LIQ_RATIO),
            "rate_new_pct": rate(ratios, new_thr),
        },
        # 复验 D3：long 侧「含 0」口径（long_liq=0 是合法观测，永不可能越阈；
        # 只报过滤后子集会系统性抬高触发频率）
        "unconditional_incl0": {
            "n": len(ratios_incl0),
            "p50": pct(ratios_incl0, 0.50), "p75": pct(ratios_incl0, 0.75),
            "p90": pct(ratios_incl0, 0.90), "p95": pct(ratios_incl0, 0.95),
            "rate_old_pct": rate(ratios_incl0, OLD_LONG_LIQ_RATIO),
            "rate_new_pct": rate(ratios_incl0, new_thr),
        },
        "short_side": {
            "p90_gt0": pct(short_gt0, 0.90),
            "n_incl0": len(short_all), "n_gt0": len(short_gt0),
            "rate_incl0_pct": rate(short_all, sqz.SQZ_SHORT_LIQ_RATIO_MIN),
            "rate_gt0_pct": rate(short_gt0, sqz.SQZ_SHORT_LIQ_RATIO_MIN),
            "threshold": sqz.SQZ_SHORT_LIQ_RATIO_MIN,
        },
        "subsets": {
            name: {"n": len(vals),
                   "n_over_new": sum(1 for x in vals if x >= new_thr),
                   "rate_p90_pct": rate(vals, p90) if p90 else None,
                   "rate_old_pct": rate(vals, OLD_LONG_LIQ_RATIO),
                   "rate_new_pct": rate(vals, new_thr)}
            for name, vals in subset.items()
        },
        "segments": seg,
        "segment_judge": sj,
        "span_sufficiency": span_suf,
        # ── P0-C（§4.3）：口径 A / B **分表输出**（不变量 4：两者的比值**严禁**进同一分布/
        #    同一分位）。B 侧只给「偏高幅度下界」，**不给**阈值基准，也**不得**与 A 换算/相加。
        "scope_split": {
            "a_mixed": {
                "scope": "coinglass 全所滚动 1h / binance 滚动成交额(vol_win)",
                "note": "现役混合口径 = 线上判定所用；分子分母**不同源不同窗**（§10.5 已披露）",
                "n": len(ratios),
                "dist": distribution(ratios),
                "rate_new_pct": rate(ratios, new_thr),
            },
            "b_single_exchange": {
                "scope": f"{args.b_scope} / {args.b_interval} 分段增量 ÷ 同 {args.b_interval} 墙钟区间",
                "note": "单所同窗口径；仅用于给 A 的偏高幅度定**下界**，非阈值基准（§4.3 目标降级）",
                "interval": args.b_interval,
                "exchange_scope": args.b_scope,
                "n": b_dist["n"],
                "dist": {k: v for k, v in b_dist.items() if k != "n"},
                "rate_new_pct": rate(b_ratios, new_thr),
            },
            "separate_scopes_seen": {"a": ["coinglass_rolling_1h", "binance_rolling_24h"],
                                     "b": list(b_scope_seen)},
            "ratio_note": "A/B 数值不可换算、不可相加 ⇒ 两行分布**不得**合并求分位（不变量 4）",
        },
        # 跨所放大（分子端）**下界**：全所 4h 增量 / Binance 同 ts 4h 增量。
        "cross_exchange_lower_bound": cross,
        "b_gate": {**b_gate, "enforced": b_gate_enforced, "no_b_gate_flag": args.no_b_gate},
        # ── P0-A（§4.1「允许①②④」）：滚动窗口列的**只读分组维度**（跨币横截面可比；
        #    不进 5m 判定、禁跨桶差分、禁与 1h 混算比值）。
        "rolling_groups": rolling_groups,
    }
    # ── 判据（工单 SQUEEZE-SPAN-001 §6-A）：段层面稳健统计（中位数）──────────────
    # 合并样本上界 / CI / 分段跨线**降级为诊断**（旧判据；实证「聚合悖论」——段层面 9/15
    # PASS、合并 31.47% FAIL ⇒ 合并让高活跃段获「样本量 × 越阈率」双重加成）。
    ub_name, ub_sub = max(
        ((n, v) for n, v in out["subsets"].items() if v["rate_new_pct"] is not None),
        key=lambda kv: kv[1]["rate_new_pct"], default=(None, None))
    ci = wilson_ci(ub_sub["n_over_new"], ub_sub["n"]) if ub_sub else None
    # CI 必须整体落在判据线某一侧；跨线 ⇒ 无法区分 PASS/FAIL（仅诊断，见 `*_raw`）。
    ci_decisive = bool(ci) and (ci[1] < JUDGE_UPPER_BOUND_PCT or ci[0] > JUDGE_UPPER_BOUND_PCT)
    # 各时段 min/max 跨线（旧判据的「漂移」口径，仅诊断）。
    seg_straddle = (seg["min_pct"] is not None
                    and seg["min_pct"] < JUDGE_UPPER_BOUND_PCT <= seg["max_pct"])
    # 新判据：判据输入 = 段中位数（`sj["value_pct"]`），`decisive` = 段 IQR 不跨线。
    judge_val = sj["value_pct"]
    measurable = judge_val is not None
    decisive = sj["decisive"]
    # 复验 F1：`decisive` 是判据的合取项之一（旧码误删）—— 段 IQR 跨判据线时不得判 PASS
    # （否则 rc=0 与「无判别力」并存，违反 `exit_code` 自身契约）。
    judge_pass = bool(sample_ok and decisive and measurable
                      and judge_val < JUDGE_UPPER_BOUND_PCT)
    # 复验 G1：`conclusion` 必须与退出码**同源**——旧码由 `pass`/`decisive` 另算一遍、完全不读
    # `sample_ok` ⇒ 真码实跑 `--days 7` 打出 `sample_ok=false` + `conclusion="FAIL"`，与 rc=3、
    # `reliable=false` 三处口径互相打架。这是同一缺陷类的第三次复发（D1 `pass` → F2 `decisive`
    # → 本轮 `conclusion`）⇒ 改为由 `exit_code()` 单一真源映射，杜绝再有第四个字段各自为政。
    # 复验 G3：`measurable`（无任何分段可算 ⇒ 判据输入算不出来）并入 3，不再落到语义不符的 4。
    rc = exit_code(judge_pass, sample_ok, decisive, measurable)
    # 复验 G4 + H4 + F5：合并上界 CI 的带符号裕度（诊断字段，改名 `merged_ci_distance_pp`）。
    _ci_margin = ci_margin_pp(ci, JUDGE_UPPER_BOUND_PCT)
    out["judge"] = {
        "criterion": f"各 {SEGMENT_HOURS}h 段上界的{SEGMENT_JUDGE_STAT} < "
                     f"{JUDGE_UPPER_BOUND_PCT}%，且段 IQR 不跨判据线（P75<线 或 P25>线）",
        # 工单 §6-A：判据输入由「合并样本上界」改为**段层面稳健统计**（此处 = 段中位数）。
        "upper_bound_pct": judge_val,
        "upper_bound_variant": f"段{SEGMENT_JUDGE_STAT}({SEGMENT_HOURS}h)",
        # 复验 F5：语义由「样本量 n」漂移为「段数」⇒ 改名（旧名保留一轮为别名，无消费方）。
        "upper_bound_n_segments": sj["n_segments"],
        "upper_bound_n": sj["n_segments"],
        # 合并上界 / CI：诊断字段（旧判据），不再参与判定。
        "merged_upper_bound_pct": ub_sub["rate_new_pct"] if ub_sub else None,
        "merged_variant": ub_name,
        "merged_n": ub_sub["n"] if ub_sub else None,
        "merged_ci95_pct": [round(ci[0], 2), round(ci[1], 2)] if ci else None,
        "seg_p25_pct": sj["p25_pct"], "seg_p75_pct": sj["p75_pct"],
        "seg_min_pct": sj["min_pct"], "seg_max_pct": sj["max_pct"],
        # 复验 F1（渲染源错配）：段 IQR 跨线是**统计事实**，与 `decisive`（码表派生、
        # 描述结论）分开存 —— 渲染层必须用本字段，否则 rc=0 时会把「跨线」印成「不跨」。
        "segment_iqr_straddle_raw": (not sj["decisive"]),
        # 复验 G4 + H4：布尔化的 `ci_decisive` 会掩盖贴线的边界性（实测只差 0.01pp 即判
        # 「有判别力」）⇒ 给出**带符号**裕度（正 = CI 在判据线下方即 PASS 侧，负 = 上方
        # 即 FAIL 侧，0 = 已跨线），既暴露贴线又保住方向。
        # 复验 F5：改名 `merged_ci_distance_pp`（它描述的是**合并上界**的裕度），旧名保留一轮。
        "merged_ci_distance_pp": _ci_margin,
        "ci_distance_pp": _ci_margin,
        # 复验 E1：pass 必须与 sample_ok 同向——分母/分子不合格时算出的上界是纯噪音，
        # 否则 `--json` 会输出「pass=true」而进程 rc=3，下游读 JSON 必然误判。
        # 复验 I1：`decisive` 也曾由 `ci`+`seg` 各自计算、**完全不读 `sample_ok`** ⇒ prod
        # `--days 7 --json` 打出 `sample_ok=false` + `reliable=false` + `decisive=true`
        # （强判定词与「样本不可用」并列，下游读 JSON 会把样本不可用上的 CI 读数当成
        # 「已有有判别力的判断」）。⇒ 收口到 `DECISIVE_BY_CODE[rc]`；原始诊断另存 `*_raw`。
        "decisive": DECISIVE_BY_CODE[rc],
        # 复验 I1（保留原始诊断，避免为「收口」而丢信息）：这两个是 CI / 分段的**原始性质**
        # **不读 `sample_ok`** ⇒ 只可用于解释 rc=3/4 的成因，**判据一律读 conclusion / reliable /
        # exit_code / decisive**（字段名带 `_raw` 即为此警示）。
        "ci_decisive_raw": ci_decisive, "segment_straddle_raw": seg_straddle,
        "raw_note": "`*_raw` 为**合并上界** CI / 分段 min-max 跨线的原始性质（已降级为诊断、"
                    "未与 sample_ok 联动），仅供诊断；判据请读 conclusion / reliable / "
                    "decisive / exit_code。",
        "pass": judge_pass,
        # 复验 E2：三态结论。旧码只输出 PASS/FAIL 两极，而「判据不可判」时把它印成
        # FAIL 与报告结论自相矛盾（该样本量下 PASS 与 FAIL 等权）⇒ 不可判必须单列，
        # 否则读者会把「抽样噪声」当成「判据不通过」并据此调阈值。
        # 复验 G1：改由 `exit_code()` 派生（四态，含「样本不可用」），与 rc 恒一致。
        "exit_code": rc,
        "conclusion": CONCLUSION_BY_CODE[rc],
        # 复验 H1 + I3：`reliable` 曾独立派生（`sample_ok`），G1 之后 `exit_code()` 入参多了
        # `measurable` 而它没跟上 ⇒ `conclusion="SAMPLE_UNUSABLE"` 却 `reliable=true`。
        # I3：`rc != 3` 虽是单源却是「非 3」式写法 —— 第 5 码出现时会**静默误报**，
        # 而隔壁 `CONCLUSION_BY_CODE[rc]` 会炸（两处鲁棒性不对称）⇒ 一律改用同构映射表。
        "reliable": RELIABLE_BY_CODE[rc],
        # 工单 §6-B：极端段显式列出（不并入判据，但不得静默丢弃）。
        "extreme_segments": sj["extreme_segments"],
        "extreme_count": sj["extreme_count"],
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        # 复验 E1：JSON 模式的退出码由 judge 结论决定（旧码 `0 if denom_ok else 3` ⇒
        # `judge.pass=False` 时仍返回 0，**退出码与结论相反**）。
        # 复验 F2：改用 exit_code() 四码 —— 不可判（4）必须与有判别力的 FAIL（2）分开。
        # 复验 G3：与 `judge.exit_code` 同一个值（单一真源），不再各算各的。
        return rc

    b = out["table_bound"]
    print("【样本时间边界】← 每次标定必须先看这里（复验 P1-a：别再把瞬时值当「7 天样本」）")
    print(f"  liquidation_snapshot: {b['min_ts']} ~ {b['max_ts']}")
    print(f"  实际跨度 {b['span_hours']} 小时 / 全表 {b['rows']} 行 / {b['symbols']} 币")
    print(f"  本次入样本：{out['sample']['rows']} 行 / {out['sample']['symbols']} 币"
          f"（窗口参数 {args.days} 天，vol_win ≥ {args.vol_win_min:g}）")

    d = denom
    print("\n【分母自证】← 复验 P1-1a：分母窗口不完整会把越阈率整体放大（实测 1.66×）")
    print(f"  asset_klines(5m) 窗口 {d['days']} 天：期望每币 {d['expect_bars_per_symbol']} 根"
          f" = 288×{d['days']}（{d['expect_hours']} 小时）")
    print(f"  实测每币根数 min={_f(d['bars_min'], 'd')} 中位={_f(d['bars_median'], 'd')}"
          f" max={_f(d['bars_max'], 'd')}"
          f"  | 覆盖小时中位 {_f(d['hours_median'], 'd')}/{d['expect_hours']}")
    print(f"  整体覆盖 = {cov:.3f}（Σ根数 / 期望总数，门槛 {MIN_DENOM_COVERAGE}）")
    print(f"  低于门槛的币 {d['symbols_below']}/{d['symbols']} = {below_pct:.2f}%"
          f"（门槛 {MAX_DENOM_BELOW_PCT}%）"
          f"  |  低于半格的币 {d['symbols_below_half']}/{d['symbols']}"
          f" = {half_pct:.2f}%（门槛 {MAX_DENOM_BELOW_HALF_PCT}%；真正参与闸门的那条）")
    print(f"  每币覆盖率 P10 = {_f(d['bars_ratio_p10'], '.3f')}"
          f"（**纯观察项**：线性插值分位看不见 5% 尾部，且被 above 门槛数学支配 —— 复验 F3）")
    if d["worst"]:
        print("  最差 5 币：" + " ".join(
            f"{w['symbol']}={w['bars']}({w['bars_ratio']:.2f}×288×{d['days']})"
            for w in d["worst"]))

    m = mol
    print("\n【分子自证】← 复验 E3：爆仓表的时间连续性（分母自证查不到这里）")
    print(f"  liquidation_snapshot 窗口 {args.days} 天：期望 {m['expect_hours']} 个整点"
          f"（已走完的小时；`span_hours` 有整段空洞时照样显示「连续」）")
    print(f"  实际有数据的整点 {m['hours_present']}/{m['expect_hours']}"
          f" = {m['hours_present_ratio']:.1%}（门槛 {MIN_MOLECULE_HOUR_COVERAGE:.0%}）"
          f"  |  最长连续空洞 {m['max_hole_hours']}h（上限 {MAX_MOLECULE_HOLE_H}h）")
    if m["hourly_rows"]:
        print("  逐小时行数（旧→新）：" + " ".join(str(n) for n in m["hourly_rows"]))

    # ── P0-C（§4.3）：口径 A / B **分表打印**（严禁合并分位）────────────────────
    # 放在前置门之前：B 门是并列的独立门（样本来自 P1 回填），被拒时读者必须能看到它自己的读数。
    # ⚠️ 此前置块**只印结构性信息**（口径定义 / 样本条数 / 配对对数）——比率与分位属
    # 「结论性读数」，样本可用性未定时印出来会被误读成判据（同 §6-C 对判据输入的处置：
    # 段清单可以印、中位摘要不可以印）。数值统一在【口径 A / B 读数】节（样本可用后）给出。
    sp = out["scope_split"]
    cb = out["cross_exchange_lower_bound"]
    print("\n【口径 A / B 分表】← P0-C：两种爆仓口径**不可换算、不可相加**（§4.3）"
          "⇒ 两行的比值**不得**合并求分位（不变量 4）")
    print(f"  口径 A（现役，混合）：{sp['a_mixed']['scope']}"
          f"  样本 {sp['a_mixed']['n']} 条")
    print(f"  口径 B（新增，单所同窗）：{sp['b_single_exchange']['scope']}"
          f"  样本 {sp['b_single_exchange']['n']} 条"
          f"  |  跨所配对（下界口径）{cb['n_pairs']} 对（下限 {cb['min_pairs']}）")
    print("   ⚠️ B = **4h 分段增量**、A = **滚动窗口** ⇒ 二者比值不可换算；"
          "B 只回答「A 的偏高幅度**至少**是多少」，**不得**据此直推阈值平移量。")
    if not cb["ok"]:
        print("   ⚠️ " + "；".join(cb["reasons"]))
    if out["b_gate"]["no_b_gate_flag"]:
        print(f"   ⭐ --no-b-gate：口径 B 门 {'通过' if out['b_gate']['ok'] else '**未**并入闸门'}"
              "（仅供诊断，**不得**据此出结论）")

    # 工单 SQUEEZE-SPAN-001 §6-E：段上界纳入**标准输出**（原先只在通过 sample_ok 后才打印，
    # 而它恰是诊断「聚合悖论」的唯一入口）⇒ 移到前置门之前，任何路径都能看到。
    sg = out["segments"]
    print(f"\n【各 {sg['segment_hours']}h 段上界】← 工单 §6-E：段间漂移（聚合悖论的诊断入口）")
    if sg["min_pct"] is not None:
        # 复验 F2（甲）：只印**逐段值**，不印 min/中位/max 摘要行 —— A 之后「中位数」同时是
        # 「判据输入」，摘要行会在前置门之前把它印出来（与 §6-C「不打印判据输入」冲突）。
        # 摘要统计改由【统计判别力】节在样本可用时给出。
        # 复验 G4（选甲，已知残留）：逐段值仍可**手算**出中位数 ⇒ C「不打印判据输入」只是字面
        # 成立。这是 C 与 §6-E（段上界必须输出为诊断入口）的固有冲突；取甲是因为 E 的诊断价值
        # 高于「数不出中位」（乙 = rc=3 连逐段值也不印，会废掉聚合悖论的唯一诊断入口）。
        print(f"  共 {sg['n_segments']} 段（跳过 n<{MIN_SEGMENT_N} 的 {sg['skipped']} 段），逐段：")
        for x in sg["segments"]:
            print(f"    {x['start_ts'][:16]}  rows={x['rows']:<6} 上界 {x['upper_bound_pct']:.2f}%")
    else:
        print(f"  无满足 n≥{MIN_SEGMENT_N} 的分段")

    if not sample_ok:
        fails = denom_fail + molecule_fail + span_suf["reasons"] + b_gate["reasons"]
        print("\n🛑 拒绝出结论：" + "；".join(fails))
        # 复验 F4：三处并发失败时标出**唯一充分因**，避免被误读成「跨度门拦下」。
        # 复验 G1：判据必须是**失败门数**（`_failed_gates`），**不能**用「理由条数」（`len(fails)`）——
        # 单门可贡献多条理由（跨度门 = 跨度 + 极端段两条；分子门 = 覆盖 + 空洞两条）⇒ 旧写法会在
        # 「只有跨度门不过」时印出「其余门亦不过」这一与事实相反的句子（prod `--vol-win-min 1e7`
        # 必然命中：分母 1.74% / 半格 0.58% 均达标）。
        _pg = primary_gate(denom_ok, molecule_ok, span_ok, b_gate["ok"])
        _failed_gates = sum(1 for ok in (denom_ok, molecule_ok, span_ok, b_gate["ok"]) if not ok)
        if _pg and _failed_gates > 1:
            print(f"   归因以{_pg}为准（另有 {_failed_gates - 1} 道门亦不过，见上）。")
        if not denom_ok:
            print("   分母不合格 ⇒ `vol_win` 被系统性少算，任何越阈率都不可比。"
                  "先补齐 biz.asset_klines(5m)，或改用落在有数据时段的 --days，再重跑。")
        if not molecule_ok:
            print("   分子不合格 ⇒ 样本既不代表整个窗口、又高度时间聚集（复验 E2 统计判别力"
                  "不足的一半成因）。先在只有这些小时活跃的样本上算「/24h」越阈率没有意义，"
                  "须先补齐爆仓采集或改用落在活跃时段的 --days。")
        if not span_ok:
            # 工单 §6-C：判据的真正卡点是**时间跨度**，不是样本量（方差分解：时间解释 98.7%
            # 方差、抽样仅 1.3%）⇒ 加币/加数据量对判据几乎无益，且现有数据不足以外推。
            print("   跨度不足 ⇒ 判据需跨 regime 的时间跨度（量级估计 20~60 天，**非承诺**）："
                  "「等」只能稀释极端段、不能消除它（极端 regime 出现时判据结构性必然 FAIL）。"
                  "期间维持影子模式，并继续积累判定记录。")
        if not b_gate["ok"]:
            print("   口径 B 不合格 ⇒ 「混合口径偏高幅度**下界**」算不出来（P0-C / §4.3）"
                  "：先在 P1 回填 `biz.liquidation_history`（`phase_backfill_liq_history.py`）"
                  "再重跑。⚠️ A 侧线上判定链不受影响（B **不进**实时判定，N2）。")
        # 复验 G3：与其它出口共用同一真源（旧码此处是硬编码字面量 3，改码表时会与之分叉）。
        return rc

    print("\n【无条件分布】long_liq / vol_win")
    print("  ——主口径：仅 long_liq > 0（下方全部输出与判据均基于此子集）——")
    print(f"  P50={p50:.3e} P75={p75:.3e} P90={p90:.3e} P95={p95:.3e}")
    print(f"  旧值 {OLD_LONG_LIQ_RATIO:.0e} 越阈率 {out['unconditional']['rate_old_pct']:.2f}%"
          f"  |  新值 {new_thr:.8f} 越阈率 "
          f"{out['unconditional']['rate_new_pct']:.2f}%")
    z = out["unconditional_incl0"]
    print("  ——含 0 口径（复验 D3：long_liq=0 是合法观测，永不可能越阈）——")
    print(f"  n={z['n']:<7} P50={_f(z['p50'])} P75={_f(z['p75'])}"
          f" P90={_f(z['p90'])} P95={_f(z['p95'])}")
    print(f"  旧值越阈率 {_f(z['rate_old_pct'], '.2f')}%"
          f"  |  新值越阈率 {_f(z['rate_new_pct'], '.2f')}%"
          f"  ← 与主口径之差即「排除零爆仓样本」带来的触发频率虚高")
    s = out["short_side"]
    print(f"\n【入队侧对照】short_liq / vol_win：阈值 {s['threshold']:.0e}"
          f"（位置随数据集变化，以本次实跑双率为准；勿引用历史分位）")
    print(f"  含 0 ：n={s['n_incl0']:<6} 越阈率 {_f(s['rate_incl0_pct'], '.2f')}%")
    print(f"  >0   ：n={s['n_gt0']:<6} 越阈率 {_f(s['rate_gt0_pct'], '.2f')}%"
          f"（P90={_f(s['p90_gt0'])}）")
    print(f"\n【条件子集（窗口 {WINDOW_MIN}min，拉升 ≥{SURGE_MIN_PCT}% / 回撤 ≥{RETRACE_MIN_PCT}%）】")
    for name, v in out["subsets"].items():
        r_new = f"{v['rate_new_pct']:.2f}%" if v["rate_new_pct"] is not None else "n/a"
        r_old = f"{v['rate_old_pct']:.2f}%" if v["rate_old_pct"] is not None else "n/a"
        print(f"  {name:<18} n={v['n']:<6} 越阈数 {v['n_over_new']:<5}"
              f"（{v['n_over_new']}/{v['n']} = {r_new}）  旧值越阈 {r_old:>8}")
    # ── P0-C（§4.3）：口径 A / B **读数分表**（数字只在样本可用后给）──────────────
    # 上游【口径 A / B 分表】只印结构性信息（定义 / 条数 / 配对数）——比率与分位属
    # 「结论性读数」，样本可用性未定时印出来会被误读成判据（同 §6-C 对判据输入的处置）
    # ⇒ 数值统一收在这道门之后。
    a, b = sp["a_mixed"], sp["b_single_exchange"]
    print("\n【口径 A / B 读数】← P0-C：**两行禁止合并求分位**（不变量 4；§4.3）")
    print(f"  口径 A（现役混合）n={a['n']}  P50={_f(a['dist']['p50'])}"
          f" P90={_f(a['dist']['p90'])}"
          f"  越阈率（新值 {new_thr:.8f}）= {_f(a['rate_new_pct'], '.2f')}%")
    print(f"  口径 B（{b['interval']} / {b['exchange_scope']}）n={b['n']}"
          f"  P50={_f(b['dist']['p50'])} P90={_f(b['dist']['p90'])}"
          f"  越阈率（同一新值）= {_f(b['rate_new_pct'], '.2f')}%"
          "  ← 同一阈值下 B 明显更低 = A 的偏高幅度**至少**这么大")
    print("   ⚠️ 两行**不可换算、不可相加**；B 只回答「A 的偏高幅度**至少**是多少」"
          "⇒ 不得据 B 直推阈值平移量（分母端窗口 24h vs 4h 错配不可分离）。")
    if cb["ok"] and cb["ratio_median"] is not None:
        print(f"  跨所放大下界（分子端）：配对数 {cb['n_pairs']}"
              f"  中位 {cb['ratio_median']:.3f}"
              f"（P25~P75 {cb['ratio_p25']:.3f}~{cb['ratio_p75']:.3f}）")
        print(f"  逐点「全所 ≥ Binance」占比 {cb['share_ge_1']:.2%}"
              "（分子端结构必然 ⇒ 仅作口径自洽校验，不作成果）")
    else:
        print("  跨所放大下界（分子端）：**未给出**（配对数不足或出现反例）"
              " ⇒ 本行不构成结论。")

    # ── P0-A（§4.1「允许④」）：滚动窗口列的**只读分组维度** ─────────────────────
    print("\n【滚动窗口列只读分组】← P0-A：仅作**分组维度**"
          "（跨币横截面可比）；不进 5m 判定、禁跨桶差分、禁与 1h 混算比值")
    for _key, _g in out["rolling_groups"].items():
        print(f"  {_key}（按三分位切桶，共 {_g['n']} 条）")
        for _bk in _g["buckets"]:
            _r = f"{_bk['rate_pct']:.2f}%" if _bk["rate_pct"] is not None else "n/a"
            print(f"    {_bk['label']:<26} n={_bk['n']:<6} 越阈率 {_r:>7}"
                  f"  P50(ratio)={_f(_bk['p50_ratio'])}")

    j = out["judge"]
    ub = _pct_s(j["upper_bound_pct"])
    print("\n【统计判别力】← 工单 SQUEEZE-SPAN-001 §6-A：判据输入 = 段层面稳健统计"
          "（对单个极端段稳健）")
    # 复验 H5：无任何分段可算 ⇒ 判据输入根本算不出来 ⇒ 单列该状态（不再借用 CI 措辞）。
    if j["upper_bound_pct"] is None:
        # 复验 I6：码位由 `{rc}` 动态引用（旧码在此 prose 里写死「退出码 3」，
        # 与紧邻判据行的 `j['exit_code']` 动态取值不对称 ⇒ 码表变动时 prose 静默漂移）。
        print("  无任何满足 n≥MIN_SEGMENT_N 的分段 ⇒ 上界**不可算**（measurable=False）"
              f"⇒ 判据无输入 ⇒ 退出码 {rc}（样本不可比；与 rc=4「样本可用但判据不可判」不同源）")
    else:
        print(f"  判据输入 = 各 {sg['segment_hours']}h 段上界的{SEGMENT_JUDGE_STAT}"
              f"（共 {j['upper_bound_n_segments']} 段）= {ub}"
              f"  |  P25={_pct_s(j['seg_p25_pct'])} / P75={_pct_s(j['seg_p75_pct'])}"
              # 复验 F1：用**统计事实**字段（segment_iqr_straddle_raw），不用码表派生的
              # `decisive`（后者描述结论，rc=0 时会把「跨线」印成「不跨」）。
              f"  ⇒ 段 IQR {'跨' if j['segment_iqr_straddle_raw'] else '不跨'}判据线"
              f"（{'无' if j['segment_iqr_straddle_raw'] else '有'}判别力）")
        # 合并上界 / CI：诊断字段（旧判据），不再参与判定。
        _mci = j["merged_ci95_pct"]
        _mci_txt = f"[{_mci[0]:.2f}%, {_mci[1]:.2f}%]" if _mci else "n/a"
        _margin = j["merged_ci_distance_pp"]
        _margin_txt = "n/a" if _margin is None else f"{_margin:+.2f}pp"
        print(f"  （诊断，不参与判定）合并样本上界 = {_pct_s(j['merged_upper_bound_pct'])}"
              f"（变体 {j['merged_variant']}，n={j['merged_n']}）95% CI = {_mci_txt}"
              f"；CI 距线带符号裕度 = {_margin_txt}")
    if j["extreme_count"]:
        print(f"  ⚠️ 极端段（上界 ≥{EXTREME_SEG_PCT:.0f}%）{j['extreme_count']} 个"
              "（不并入判据，但不得静默丢弃 —— 工单 §6-B）：")
        for x in j["extreme_segments"]:
            print(f"    {x['start_ts'][:16]}  上界 {x['upper_bound_pct']:.2f}%")
    print(f"\n【判定】{j['criterion']}")
    # 复验 E2：三态输出。不可判时**不能**印 FAIL——该样本量下 PASS 与 FAIL 等权，
    # 印成 FAIL 会被读成「判据不通过」并据此动阈值（与报告结论自相矛盾）。
    print(f"  判据输入（段{SEGMENT_JUDGE_STAT}）= {ub}  →  {j['conclusion']}"
          f"（退出码 {j['exit_code']}："
          "0=PASS / 2=有判别力 FAIL / 3=样本不可用 / 4=不可判）")
    if j["conclusion"] == "INCONCLUSIVE":
        print("     段 IQR 跨判据线 ⇒ 此判据输入的 PASS/FAIL 只是抽样噪声，"
              "**不构成阈值决策依据**（勿据此调阈值）。")
        print("     替代读法：看上方 P25/P75 与段上界清单。")
    elif j["conclusion"] == "FAIL":
        print("     判据不通过且具备判别力 ⇒ 先核对分母/分子自证与未来函数口径，再谈调阈值。")
    elif j["conclusion"] == "SAMPLE_UNUSABLE":
        # 复验 H2：按 `conclusion` 精确分派（不再用 `pass` 这种粗粒度布尔兜）——
        # 该态可能是「分母/分子/跨度自证不过」或「判据输入不可算」，两者都谈不上判别力。
        print("     样本不可用（分母/分子/跨度自证不过，或判据输入不可算）"
              "⇒ 这不构成「判据不通过」（也谈不上判别力）⇒ 先满足样本可用性，再谈阈值。")
    print("\n⚠️ 样本时间代表性弱（表历史见上）⇒ 结论仅供临时定稿，"
          "待 squeeze_track 判定样本积累后改用判定窗口直接标定。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())