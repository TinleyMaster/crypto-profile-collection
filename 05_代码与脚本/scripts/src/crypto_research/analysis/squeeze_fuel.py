"""拉升期结构分解与轧空衰竭判定（设计方案 §10.10；纯函数，脱离 DB / 网络，可直接单测）。

与 `analysis/squeeze.py` 的分工（**两套窗口不同，勿混用**）：

  - `squeeze.evaluate_battle`：「**冲高回撤后**多空谁赢了」，窗口 `[peak_ts, now]`；
  - 本模块：「**拉升期**这波由什么驱动、轧空的弹药还剩多少」，窗口 `W = [surge_start_ts, now]`。

两者**并存**：本模块挂在阶段 2 的跟踪循环里，**不替换** `evaluate_battle`。同一 track 的
`metrics` 里两套指标并存，键名已分开（本模块一律落在 `metrics['fuel']` 下）。

────────────────────────────────────────────────────────────────
数据口径（§10.10.1 / §10.10.2）：两个量**无真值源**，只能走代理
────────────────────────────────────────────────────────────────
① 大户多/空仓分解：Binance 免费端点 `/futures/data/topLongShortPositionRatio` 只返回
   **占比**（`longAccount`/`shortAccount`，和为 1），CoinGlass 多空比全系列在 HOBBYIST
   下 404 ⇒ 没有任何可用源给出绝对名义额。故由已落库的比值反推（**无需改表**）：

       longShare = r / (1 + r)          （r = `biz.long_short_ratio.top_position_ratio`）
       L_proxy   = oi_usd × longShare   （大户多仓**代理量**）

   ⚠️ 该量等于「OI × 占比」，只有在「大户占 OI 的份额在窗口内稳定」这一**尚未验证**的
   假设下，其相对变化才可读；它**不是**大户真实绝对仓位、跨币不可比 ⇒ 只做**同币同窗口内
   `dL`/`dS` 的符号与相对幅度**判断。

② 主动平仓/开仓：`aggTrades` 的 `isBuyerMaker` 只标主动方、**不区分开/平** ⇒ 用
   `OI × CVD` 四象限反推（免费数据下唯一路径）：

       OI↓ & CVD>0 → 买平（空头回补，轧空特征）    OI↓ & CVD<0 → 卖平（多头离场）
       OI↑ & CVD>0 → 新多开仓（假轧空）            OI↑ & CVD<0 → 新空开仓

⇒ 上述两项均**非**交易所开/平标记与真实仓位，故本模块结论的证据强度**低于**真值源：
`confidence` 上限 `medium`，邮件/展示层必须显式标注口径（§10.10.6）。

────────────────────────────────────────────────────────────────
缺失数据：缺失 ≠ 0（沿用 §10.6 纪律）
────────────────────────────────────────────────────────────────
- 结构量 `d_oi_pct` / `d_s_pct` / `d_l_pct` 任一缺失 ⇒ 依赖它的分支不成立 ⇒ `mixed`；
- 辅助量 `liq_*` 缺失 ⇒ 既不得判 `sqz_fuel_exhausting`、也**不得**判 `sqz_fuel_active`
  （后者断言「空头爆仓仍在高位」）⇒ 一律 `mixed`（缺失不等于「爆仓从未发生」）；
- 辅助量 `cvd_divergence` 缺失 ⇒ 只挡 `sqz_fuel_exhausting`（它断言「CVD 背离」），
  结构成立时归 `sqz_fuel_active`。
- **「衰减必须先有峰值」（§10.10.5，本节最易写错的一条）**：`liq_peak` 未越阈时
  `liq_decay` 是一个无意义的比值（分子分母都极小），照它判会得出「爆仓萎缩 ⇒ 弹药耗尽」
  的假结论 —— 实际是「**爆仓从未发生**」⇒ 一律不得判 `sqz_fuel_exhausting`。

⚠️ 阈值常量**全为经验初值、未标定**（§10.10.4 / §10.10.8：`biz.long_short_ratio` 只对
tracking 币采集且历史极短，没有可回测的样本，只能前向积累）。
"""
from __future__ import annotations

from datetime import datetime

from .squeeze import (GAP_METRIC_VER, MAX_MID_GAP_BUCKETS, MIN_WINDOW_COVERAGE,
                      SQZ_SHORT_LIQ_RATIO_MIN, window_gate)

BUCKET_SECONDS = 300   # 5m OI/CVD 桶（必须与 scan_daemon.BUCKET_SECONDS 一致，单测守卫）

# ── 阈值常量（§10.10.4，**全为经验初值、未标定**）────────────────────
# ⚠️ 与 §10.5 同纪律：取值不在设计方案留数字结论，一律以标定脚本当次实跑为准
#   （`workbench/calib_squeeze_fuel_thr.py` 待立，见 §10.10.8-①）。
# 分界一律以**正数幅度**存储（判据自行取负），避免 ±0.3 双写导致符号漂移。
OI_UP_THR = 0.3            # % 窗口 OI 净增分界（dOI ≥ +0.3% ⇒ 有新增持仓）
OI_DOWN_THR = 0.3          # % 窗口 OI 净减分界（判据 dOI ≤ -0.3%）
SHORT_ADD_THR = 3.0        # % 大户空仓代理量「显著增加」分界（dS ≥ +3%）
SHORT_CUT_THR = 3.0        # % 大户空仓代理量「显著减少」分界（判据 dS ≤ -3%）
LONG_ADD_THR = 3.0         # % 大户多仓代理量「显著加仓」分界（dL ≥ +3%）
LONG_FLAT_THR = 1.0        # % 大户多仓代理量「基本持平」分界（判据 dL ≥ -1%）
LIQ_DECAY_THR = 0.5        # 空头爆仓衰减比上限（当前 / 窗口峰值 ≤ 0.5 ⇒ 视为萎缩）
MIN_LSR_POINTS = 6         # W 内 LSR 有效点数下限（≈30min），不足拒判（冷启动约束）
MIN_FUEL_BUCKETS = 3       # W 内有效 5m 桶数下限
# metrics 版本位：`metrics['fuel']` 的结构变更时递增；跨版本回看历史行必须先看它
# （同 §10.5 的 `gap_metric_ver` 纪律）。
# v2（2026-09-24，P0-A §4.1）：新增 `liq_bg_*`（滚动 24h **规模背景值**，只展示不参与判定）
#   与 `liq_bg_scope`。v1 的历史行没有这 4 个键 ⇒ NULL 而非 0，勿当「无爆仓」读。
FUEL_METRIC_VER = 2

# P0-A（§4.1 消费点②）：滚动 24h 背景值的**口径标签**（与判定侧的 1h 口径并存，不得混读）。
# ⚠️ 故意不叫 `liq_scope`：顶层 `metrics.liq_scope` 已被判定侧占用（`coinglass_rolling_1h`），
#    同一 metrics 对象里出现两个不同口径却同名的键会造成「一词多义」误读。
LIQ_BG_SCOPE = "coinglass_rolling_24h"

# ── 结论枚举 ──────────────────────────────────────────────────
FUEL_EXHAUSTING = "sqz_fuel_exhausting"   # 轧空弹药耗尽
FUEL_ACTIVE = "sqz_fuel_active"           # 轧空进行中（弹药尚存）
LONG_PUMP = "long_pump"                   # 多头主动开仓拉盘（假轧空）
SHORT_REBUILD = "short_rebuild"           # 新增空头进场
MIXED = "mixed"                           # 结构不明（含数据缺失降级）

VERDICT_LABEL = {
    FUEL_EXHAUSTING: "轧空弹药耗尽",
    FUEL_ACTIVE: "轧空进行中（弹药尚存）",
    LONG_PUMP: "多头主动开仓拉盘",
    SHORT_REBUILD: "新增空头进场",
    MIXED: "结构不明",
}

# ── 四象限（代理口径，非交易所开/平标记）──────────────────────────
BUY_CLOSE = "buy_close"       # OI↓ & CVD>0：买平（空头回补，轧空特征）
SELL_CLOSE = "sell_close"     # OI↓ & CVD<0：卖平（多头离场）
LONG_OPEN = "long_open"       # OI↑ & CVD>0：新多开仓
SHORT_OPEN = "short_open"     # OI↑ & CVD<0：新空开仓

_MISS_LABEL = {
    "d_oi_pct": "窗口OI变化",
    "d_s_pct": "大户空仓代理量变化",
    "d_l_pct": "大户多仓代理量变化",
    "liq": "空头爆仓额",
}


def proxy_shares(r: float | None) -> tuple[float | None, float | None]:
    """大户持仓多空比 → `(多仓占比, 空仓占比)`（占比可由已存比值反推，**无需改表**）。

    `top_position_ratio` = 多/空 **占比之比**，故 `longShare = r/(1+r)`、
    `shortShare = 1/(1+r)`（两者和恒为 1，即 Binance 的 `longAccount`/`shortAccount`）。
    `r` 为 `None` 或 ≤ 0 时返回 `(None, None)` —— 负/零比值无占比语义，**绝不补 0 冒充**。
    """
    if r is None:
        return None, None
    r = float(r)
    if r <= 0:
        return None, None
    return r / (1.0 + r), 1.0 / (1.0 + r)


def quadrant(oi_delta: float | None, cvd: float | None) -> str | None:
    """`OI 变化 × CVD 方向` 四象限分类（**代理口径**，非交易所开/平标记）。

    任一输入缺失或恰为 0（方向无意义）→ `None`，调用方须按「无效桶」剔除而非补 0。
    ⚠️ 两个 0 都必须**显式**判掉：`cvd == 0` 若只写 `cvd > 0 ... else ...`，会被静默
    归入**卖压**侧（`short_open`/`sell_close`），把「无方向」当成「有方向」。
    """
    if oi_delta is None or cvd is None or oi_delta == 0 or cvd == 0:
        return None
    if oi_delta < 0:
        return BUY_CLOSE if cvd > 0 else SELL_CLOSE
    return LONG_OPEN if cvd > 0 else SHORT_OPEN


def buy_to_close_share(oi_rows: list[dict]) -> float | None:
    """W 内「买平（空头回补）」桶占比 = 买平桶数 / 有效桶数。

    `oi_rows` 为窗口内 OI 桶（按 ts 升序，含 `oi_usd` / `cvd_5m_usd`）。
    有效桶 = 相邻两桶 OI 变化与当桶 CVD 都非 0/非缺失（否则方向无意义，不进分母）；
    有效桶为 0 → `None`（缺失 ≠ 0）。
    """
    if not oi_rows or len(oi_rows) < 2:
        return None
    quads: list[str] = []
    for prev, cur in zip(oi_rows, oi_rows[1:]):
        try:
            d_oi = float(cur["oi_usd"]) - float(prev["oi_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        cvd = cur.get("cvd_5m_usd")
        q = quadrant(d_oi, None if cvd is None else float(cvd))
        if q is not None:
            quads.append(q)
    if not quads:
        return None
    return sum(1 for q in quads if q == BUY_CLOSE) / len(quads)


def price_cvd_divergence(prices: list[float], cum_cvd: list[float]) -> bool | None:
    """价格在 W 内创新高、但累计 CVD 未创新高 ⇒ `True`（「无新增买盘承接」）。

    口径在本模块**明确定义**（避免各处各写一套），以**当次评估时刻**为准：

      - 末点价格 < 窗口最高价 ⇒ 价格本身未创新高，**不构成背离**（`False`）；
      - 末点价格 = 窗口最高价且末点累计 CVD < 窗口累计 CVD 最大值 ⇒ `True`。

    序列不足 2 点或长度不等 → `None`（缺失 ≠ `False`）。
    """
    if len(prices) < 2 or len(prices) != len(cum_cvd):
        return None
    if prices[-1] < max(prices):
        return False
    return cum_cvd[-1] < max(cum_cvd)


def _ts_of(row: dict) -> datetime | None:
    """行时间戳：OI/爆仓行是 `ts`，K 线行是 `open_time`（两者同为 5m 墙钟对齐）。"""
    v = row.get("ts")
    if v is None:
        v = row.get("open_time")
    return v if isinstance(v, datetime) else None


def _in_win(row: dict, start: datetime, end: datetime) -> bool:
    ts = _ts_of(row)
    return ts is not None and start <= ts <= end


def _at_or_before(rows: list[dict], when: datetime) -> dict | None:
    """取 ts ≤ when 的最后一条（`rows` 按 ts 升序）；无则 `None`。"""
    hit = None
    for r in rows:
        ts = _ts_of(r)
        if ts is None:
            continue
        if ts <= when:
            hit = r
        else:
            break
    return hit


def proxy_extremes(oi_rows: list[dict], lsr_points: list[tuple]) -> dict:
    """窗口首/末可用 LSR 点对应的大户多/空仓**代理量**与窗口变化率。

    `lsr_points`：`[(ts, top_position_ratio)]`（升序）。每个 LSR 点配「**ts ≤ 该点**的最近
    一条 OI 桶」——这正是「基准桶取错问题」的解：不能拿窗口首桶去配窗口末点的比值，否则
    `dL`/`dS` 会混入 OI 自身的漂移（§10.10.7-①）。
    `oi_rows` 传**未裁剪**的完整序列，以便首个 LSR 点的基准桶能落到窗口左端之外。

    返回 `{'r_first','r_last','d_r','d_l_pct','d_s_pct','l_first','l_last','s_first','s_last'}`；
    任一端不可得或可用点不足 2 个 → 对应字段 `None`（缺失 ≠ 0）。
    """
    out = {k: None for k in ("r_first", "r_last", "d_r", "d_l_pct", "d_s_pct",
                             "l_first", "l_last", "s_first", "s_last")}
    pairs: list[tuple] = []
    for ts, r in lsr_points:
        share_l, share_s = proxy_shares(r)
        if share_l is None:
            continue
        oi = _at_or_before(oi_rows, ts)
        if not oi or oi.get("oi_usd") is None:
            continue
        oi_usd = float(oi["oi_usd"])
        pairs.append((float(r), oi_usd * share_l, oi_usd * share_s))
    if not pairs:
        return out
    first, last = pairs[0], pairs[-1]
    out["r_first"], out["r_last"] = first[0], last[0]
    out["l_first"], out["l_last"] = first[1], last[1]
    out["s_first"], out["s_last"] = first[2], last[2]
    out["d_r"] = last[0] - first[0]
    if len(pairs) >= 2:
        if first[1]:
            out["d_l_pct"] = (last[1] - first[1]) / first[1] * 100
        if first[2]:
            out["d_s_pct"] = (last[2] - first[2]) / first[2] * 100
    return out


def liq_extremes(liq_rows: list[dict], vol24_usd: float | None) -> dict:
    """窗口内空头爆仓的峰值 / 当前值与衰减比（均为「占 24h 成交额」的比例口径）。

    ⚠️ 源数据是 CoinGlass **滚动 1h 窗口绝对值**，严禁跨桶差分（滚动窗口相减 ≠ 窗口内新增，
    见 `scan_daemon._latest_liq_snapshot`），故此处只取绝对值与 max。
    ⚠️ `liq_decay` 只有在峰值**越阈**时才有意义（§10.10.5）——本函数照算，把门交给判定侧。
    """
    out = {"liq_peak_ratio": None, "liq_now_ratio": None, "liq_decay": None,
           "liq_peak_usd": None, "liq_now_usd": None}
    if not liq_rows or not vol24_usd:
        return out
    vals = [float(r["short_liq_usd_1h"]) for r in liq_rows
            if r.get("short_liq_usd_1h") is not None]
    if not vals:
        return out
    peak, now_v = max(vals), vals[-1]        # liq_rows 按 ts 升序
    out["liq_peak_usd"], out["liq_now_usd"] = peak, now_v
    out["liq_peak_ratio"] = peak / float(vol24_usd)
    out["liq_now_ratio"] = now_v / float(vol24_usd)
    out["liq_decay"] = (now_v / peak) if peak else None
    return out


def liq_background(liq_rows: list[dict]) -> dict:
    """**规模背景值**（P0-A §4.1 消费点②③）：滚动 24h 窗口的爆仓累计额，只作背景展示。

    与 `liq_extremes()` 的分工（**不得混用**）：
      - `liq_extremes()` 读 `*_liq_usd_1h`（判定侧口径，参与 `classify_fuel` 的衰减判据）；
      - 本函数读 `*_liq_usd_24h`，**不参与任何判定**，只回答「该币 24h 内爆仓规模多大」。

    ⚠️ 同为**滚动窗口**列（§3.3-2 / AGENTS.md P1-1）：只取**绝对值**——严禁跨桶差分、
    严禁与 `*_liq_usd_1h` 混算比值（§4.1 禁止项①②）、严禁 `÷24` 当 1h（禁止项③）。
    取**调用方传入的 `liq_rows` 中最新一条**（不按燃料窗口 `W` 过滤）：它是背景值、与窗口
    长短无关，`W` 内无行时照样给得出。缺失保持 `None`（缺失≠0）——旧行无该列、接口未返回
    时**不得补 0**（§4.2：`12h` 分列未补，`24h` 分列旧行为 NULL）。
    """
    out = {"liq_bg_24h_usd": None, "liq_bg_24h_long_usd": None,
           "liq_bg_ts": None, "liq_bg_scope": LIQ_BG_SCOPE}
    live = [r for r in liq_rows
            if r.get("short_liq_usd_24h") is not None
            or r.get("long_liq_usd_24h") is not None]
    if not live:
        return out
    newest = max(live, key=lambda r: _ts_of(r) or datetime.min)
    ts = _ts_of(newest)
    out["liq_bg_24h_usd"] = (None if newest.get("short_liq_usd_24h") is None
                             else float(newest["short_liq_usd_24h"]))
    out["liq_bg_24h_long_usd"] = (None if newest.get("long_liq_usd_24h") is None
                                  else float(newest["long_liq_usd_24h"]))
    out["liq_bg_ts"] = ts.isoformat() if ts else None
    return out


def divergence_inputs(k_rows: list[dict], oi_rows: list[dict]) -> tuple[list[float], list[float]]:
    """构造背离判定用的「价格序列」与「累计 CVD 序列」（严格等长、按时间升序）。

    以 **5m K 线桶**为时间轴，逐桶按「ts ≤ 该桶」取最近一条 OI 桶的 `cvd_5m_usd` 累加
    （**增量**口径，绝不用累计绝对量当基准）。两侧同为 5m 墙钟对齐，用 at-or-before 连接
    可避免「相位差 1 桶时整窗错位」；缺价或缺 CVD 的桶**整桶剔除**（两序列不得错位）。
    """
    prices: list[float] = []
    cum: list[float] = []
    acc = 0.0
    for k in k_rows:
        if k.get("close_px") is None:
            continue
        oi = _at_or_before(oi_rows, _ts_of(k))
        if not oi or oi.get("cvd_5m_usd") is None:
            continue
        acc += float(oi["cvd_5m_usd"])
        prices.append(float(k["close_px"]))
        cum.append(acc)
    return prices, cum


def fuel_gate(*, oi_rows: list[dict], lsr_points: list[tuple],
              surge_start_ts: datetime, now: datetime,
              bucket_seconds: int = BUCKET_SECONDS) -> dict:
    """§10.10.5 闸门（**复用 §10.5 既有口径，不新造**）。

    逐条：① 窗口覆盖率 ≥ `MIN_WINDOW_COVERAGE`；② 尾部 `oi_lag_sec ≤ 2×桶`（SQZ-03 口径）；
    ③ 连续缺桶 < `MAX_MID_GAP_BUCKETS`（`squeeze.window_gate`）；④ W 内 LSR 点数
    ≥ `MIN_LSR_POINTS`（本节新增，冷启动约束）；⑤ W 内有效 5m 桶数 ≥ `MIN_FUEL_BUCKETS`。

    返回 `{'gate_ok','gate_reason','oi_cover':{'have','expect'},'oi_lag_sec',
    'head_gap_buckets','mid_gap_buckets','gap_metric_ver'}`。
    ⚠️ 缺桶文案**不**复用 `squeeze.gap_reason()`：其「判定窗口」前缀是
    `check_scan_freshness.py` 的 `reason LIKE '判定窗口%'` 耦合点（统计 track 拒判数），
    燃料拒判只落 `metrics.fuel`、不属于该统计口径，复用会把两个口径混在一起。
    """
    first_bucket = -(-int(surge_start_ts.timestamp()) // bucket_seconds)
    last_bucket = int(now.timestamp()) // bucket_seconds
    expect = max(1, last_bucket - first_bucket + 1)
    have = len(oi_rows)
    present = {int(_ts_of(r).timestamp()) // bucket_seconds
               for r in oi_rows if _ts_of(r) is not None}
    oi_ts_max = max((_ts_of(r) for r in oi_rows if _ts_of(r) is not None), default=None)
    oi_lag_sec = None if oi_ts_max is None else (now - oi_ts_max).total_seconds()
    tail_gap = oi_lag_sec is not None and oi_lag_sec > 2 * bucket_seconds
    gap_ok, head_gap, mid_gap = window_gate(present, first_bucket, last_bucket)

    reason = None
    if have / expect < MIN_WINDOW_COVERAGE:
        reason = f"燃料窗口数据覆盖不足 {have}/{expect} 桶，暂不评估"
    elif tail_gap:
        reason = f"燃料窗口尾部 OI 桶缺失（最新桶滞后 {oi_lag_sec:.0f}s），暂不评估"
    elif not gap_ok:
        reason = (f"燃料窗口内连续缺桶 {max(head_gap, mid_gap)} 个"
                  f"（左端起 {head_gap} 个 / 中段 {mid_gap} 个，{have}/{expect} 桶），暂不评估")
    elif len(lsr_points) < MIN_LSR_POINTS:
        reason = (f"燃料窗口内多空比有效点仅 {len(lsr_points)} 个"
                  f"（<{MIN_LSR_POINTS}，冷启动期），暂不评估")
    elif have < MIN_FUEL_BUCKETS:
        reason = f"燃料窗口内有效 5m 桶仅 {have} 个（<{MIN_FUEL_BUCKETS}），暂不评估"

    return {
        "gate_ok": reason is None,
        "gate_reason": reason,
        "oi_cover": {"have": have, "expect": expect},
        "oi_lag_sec": None if oi_lag_sec is None else round(oi_lag_sec),
        "head_gap_buckets": head_gap,
        "mid_gap_buckets": mid_gap,
        "gap_metric_ver": GAP_METRIC_VER,
    }


def _mk(verdict: str, reason: str, confidence: str, missing: list[str]) -> dict:
    return {"verdict": verdict, "label": VERDICT_LABEL[verdict], "reason": reason,
            "confidence": confidence, "data_missing": list(missing)}


def classify_fuel(*, d_oi_pct: float | None, d_s_pct: float | None,
                  d_l_pct: float | None, liq_peak_ratio: float | None,
                  liq_decay: float | None,
                  cvd_divergence: bool | None) -> dict:
    """§10.10.3 判定（**优先级 = 书写顺序**：先判 OI 升的两类，再判 OI 降的两类，兜底 `mixed`）。

    这个顺序本身就是判据：「多头主动开仓拉盘」这个假信号由 `long_pump` 单列承接，靠优先级
    保证它在结构上**不可能**落进 `sqz_fuel_*`（OI 升时先被前两个分支截住）。这正是
    「必须同时看多仓/空仓两个序列、不能只看比值」的落地点。
    """
    oi_known = d_oi_pct is not None
    s_known = d_s_pct is not None
    l_known = d_l_pct is not None
    liq_known = liq_peak_ratio is not None
    missing = [k for k, ok in (("d_oi_pct", oi_known), ("d_s_pct", s_known),
                               ("d_l_pct", l_known), ("liq", liq_known)) if not ok]

    # 1) 新增空头进场：OI 升 + 大户空仓代理升
    if oi_known and s_known and d_oi_pct >= OI_UP_THR and d_s_pct >= SHORT_ADD_THR:
        return _mk(SHORT_REBUILD,
                   f"窗口 OI 净增 {d_oi_pct:+.2f}%，大户空仓代理量增加 {d_s_pct:+.2f}% "
                   f"⇒ 新增空头进场承接，轧空被对冲（非空头平仓推高比值）",
                   "medium", missing)

    # 2) 多头主动开仓拉盘（需求点名的假信号：比值涨 ≠ 轧空）
    if oi_known and l_known and d_oi_pct >= OI_UP_THR and d_l_pct >= LONG_ADD_THR:
        return _mk(LONG_PUMP,
                   f"窗口 OI 净增 {d_oi_pct:+.2f}%，大户多仓代理量增加 {d_l_pct:+.2f}% "
                   f"⇒ 比值上升由多头主动开仓推动，空头并未平仓，不是轧空",
                   "medium", missing)

    # 3) 平仓驱动：OI 净降 + 空仓代理显著减少 + 多仓代理未显著加仓
    if (oi_known and s_known and l_known
            and d_oi_pct <= -OI_DOWN_THR and d_s_pct <= -SHORT_CUT_THR
            and d_l_pct >= -LONG_FLAT_THR):
        head = (f"窗口 OI 净降 {d_oi_pct:+.2f}%、大户空仓代理量减少 {d_s_pct:+.2f}%"
                f"（空头平仓止损），多仓代理量 {d_l_pct:+.2f}% 未显著加仓")
        if not liq_known:
            return _mk(MIXED, f"{head}；但空头爆仓数据缺失，无法判「弹药是否耗尽」",
                       "low", missing)
        peak_ok = liq_peak_ratio >= SQZ_SHORT_LIQ_RATIO_MIN
        decay_ok = liq_decay is not None and liq_decay <= LIQ_DECAY_THR
        if peak_ok and decay_ok and cvd_divergence is True:
            return _mk(FUEL_EXHAUSTING,
                       f"{head}；空头爆仓由窗口峰值衰减至 {liq_decay:.2f}，"
                       f"价格新高但累计 CVD 未新高（无新增买盘承接）⇒ 轧空弹药正在耗尽",
                       "medium", missing)
        if not peak_ok:
            sub = "窗口内空头爆仓从未越过阈值（爆仓未成规模，谈不上「萎缩」）"
        elif liq_decay is None:
            sub = "空头爆仓当前值缺失，衰减比不可算"
        elif not decay_ok:
            sub = f"空头爆仓仍处高位（衰减比 {liq_decay:.2f}）"
        elif cvd_divergence is None:
            sub = "CVD 背离不可判定（窗口内价格/CVD 数据不足）"
        else:
            sub = "价格与 CVD 未现背离（仍有主动买盘承接）"
        return _mk(FUEL_ACTIVE, f"{head}；但{sub} ⇒ 轧空进行中、弹药尚未确认耗尽",
                   "medium", missing)

    # 4) 兜底
    if missing:
        lack = "、".join(_MISS_LABEL[m] for m in missing)
        return _mk(MIXED, f"结构不明：{lack}缺失，不出方向", "low", missing)
    return _mk(MIXED,
               f"窗口 OI {d_oi_pct:+.2f}%、大户空仓代理量 {d_s_pct:+.2f}%、"
               f"多仓代理量 {d_l_pct:+.2f}% 未构成明确结构"
               f"（既非 OI 升的空头重建/多头拉盘，也非 OI 降的平仓驱动）",
               "low", missing)


def evaluate_fuel(*, oi_rows: list[dict], lsr_points: list[tuple],
                  liq_rows: list[dict], k_rows: list[dict],
                  vol24_usd: float | None, surge_start_ts: datetime,
                  now: datetime, bucket_seconds: int = BUCKET_SECONDS) -> dict:
    """窗口 `W = [surge_start_ts, now]` 的完整评估：闸门 → 指标 → 判定 → 文案。

    调用方（`scan_daemon.task_scan_squeeze` 阶段 2）只需把返回值整体塞进
    `metrics['fuel']`（**合并**语义，勿覆盖入场指标），并按影子模式处理（不发信）。

    返回 `{'verdict','label','reason','confidence','gate_ok','gate_reason',
    'data_missing','metrics'}`；`verdict=None` 表示闸门未过（**拒判**，与 `mixed`
    的「结构不明」是两回事，用 `gate_ok` 区分）。
    `metrics` 另含 `liq_bg_*`（滚动 24h **规模背景值** + `liq_bg_scope`，P0-A §4.1）——
    **只展示、不参与判定**，且**闸门未过时同样存在**（缺口存在性标注）。
    """
    w_oi = [r for r in oi_rows if _in_win(r, surge_start_ts, now)]
    w_liq = [r for r in liq_rows if _in_win(r, surge_start_ts, now)]
    w_k = [r for r in k_rows if _in_win(r, surge_start_ts, now)]
    w_lsr = sorted(((ts, r) for ts, r in lsr_points
                    if ts is not None and surge_start_ts <= ts <= now),
                   key=lambda p: p[0])

    gate = fuel_gate(oi_rows=w_oi, lsr_points=w_lsr, surge_start_ts=surge_start_ts,
                     now=now, bucket_seconds=bucket_seconds)
    # P0-A（§4.1 消费点②，**缺口存在性标注**）：24h 规模背景值取自**未过滤**的 `liq_rows`，
    # 故闸门未过（含 1h 快照超龄 ⇒ 判定侧走 `missing`）时**照样落库** ⇒ 排查时能区分
    # 「确实没有爆仓」与「1h 快照断了但 24h 背景还在」。判定行为不受其影响（不进 classify_fuel）。
    bg = liq_background(liq_rows)
    metrics = {
        "fuel_metric_ver": FUEL_METRIC_VER,
        "window_start": surge_start_ts.isoformat(),
        "window_end": now.isoformat(),
        "buckets": len(w_oi),
        "lsr_points": len(w_lsr),
        "gate_ok": gate["gate_ok"],
        "oi_cover": gate["oi_cover"],
        "oi_lag_sec": gate["oi_lag_sec"],
        "gap_metric_ver": gate["gap_metric_ver"],
        "head_gap_buckets": gate["head_gap_buckets"],
        "mid_gap_buckets": gate["mid_gap_buckets"],
        **bg,
    }
    if not gate["gate_ok"]:
        return {"verdict": None, "label": "暂不评估（闸门未过）",
                "reason": gate["gate_reason"], "confidence": "low",
                "gate_ok": False, "gate_reason": gate["gate_reason"],
                "data_missing": [], "metrics": metrics}

    d_oi = None
    if len(w_oi) >= 2 and w_oi[0].get("oi_usd") and w_oi[-1].get("oi_usd"):
        oi_first, oi_last = float(w_oi[0]["oi_usd"]), float(w_oi[-1]["oi_usd"])
        d_oi = (oi_last - oi_first) / oi_first * 100
    cvd_ratio_w = None
    if w_oi:
        vol_w = sum(float(r["vol_5m_usd"] or 0) for r in w_oi)
        if vol_w:
            cvd_ratio_w = sum(float(r["cvd_5m_usd"] or 0) for r in w_oi) / vol_w

    prox = proxy_extremes(oi_rows, w_lsr)
    liqx = liq_extremes(w_liq, vol24_usd)
    prices, cum_cvd = divergence_inputs(w_k, w_oi)
    divergence = price_cvd_divergence(prices, cum_cvd)
    btc_share = buy_to_close_share(w_oi)

    out = classify_fuel(d_oi_pct=d_oi, d_s_pct=prox["d_s_pct"], d_l_pct=prox["d_l_pct"],
                        liq_peak_ratio=liqx["liq_peak_ratio"],
                        liq_decay=liqx["liq_decay"], cvd_divergence=divergence)
    metrics.update({
        "d_oi_pct": None if d_oi is None else round(d_oi, 3),
        "d_l_pct": None if prox["d_l_pct"] is None else round(prox["d_l_pct"], 3),
        "d_s_pct": None if prox["d_s_pct"] is None else round(prox["d_s_pct"], 3),
        "d_r": None if prox["d_r"] is None else round(prox["d_r"], 4),
        "r_first": prox["r_first"], "r_last": prox["r_last"],
        "l_first": prox["l_first"], "l_last": prox["l_last"],
        "s_first": prox["s_first"], "s_last": prox["s_last"],
        "liq_peak_ratio": None if liqx["liq_peak_ratio"] is None
        else round(liqx["liq_peak_ratio"], 6),
        "liq_now_ratio": None if liqx["liq_now_ratio"] is None
        else round(liqx["liq_now_ratio"], 6),
        "liq_decay": None if liqx["liq_decay"] is None else round(liqx["liq_decay"], 4),
        "cvd_divergence": divergence,
        "cvd_ratio_w": None if cvd_ratio_w is None else round(cvd_ratio_w, 4),
        "btc_share": None if btc_share is None else round(btc_share, 4),
        # 口径标注：本模块结论的证据强度低于真值源（代理量 + 四象限反推），邮件须披露
        "proxy_scope": "oi_x_share / oi_x_cvd_quadrant",
        "data_missing": out["data_missing"],
    })
    return {"verdict": out["verdict"], "label": out["label"], "reason": out["reason"],
            "confidence": out["confidence"], "gate_ok": True, "gate_reason": None,
            "data_missing": out["data_missing"], "metrics": metrics}


__all__ = [
    "proxy_shares", "quadrant", "buy_to_close_share", "price_cvd_divergence",
    "proxy_extremes", "liq_extremes", "liq_background", "divergence_inputs", "fuel_gate",
    "classify_fuel", "evaluate_fuel", "VERDICT_LABEL", "LIQ_BG_SCOPE",
    "FUEL_EXHAUSTING", "FUEL_ACTIVE", "LONG_PUMP", "SHORT_REBUILD", "MIXED",
    "BUY_CLOSE", "SELL_CLOSE", "LONG_OPEN", "SHORT_OPEN",
]
