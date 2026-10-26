"""解锁事件质量判据（UNLOCK-SYS-002 源头治理 G3.1）。

把「模板占位行识别」与「供应量基准合理性」从消费端脚本抽到公共模块，
让**采集端**（phase_chain_token_unlocks.py）与**消费端**
（sync_unlock_events_from_json.py）共用同一实现，避免判据漂移。

背景（2026-10-26 实测）：
  tokenomist 页面会为「无真实解锁计划」的资产渲染占位行：
      pct=12.0 / value_usd=123460.0 / value_str="$123.46K"
      status="4 months left" / allocations=1 / is_upcoming=True
  该签名在母表 1,383 个资产中跨 108 个资产重复出现（含 BTC 这类无「解锁」概念的币），
  日期集中在 2027-01-20 / 2027-01-21 / 2026-12-25。
  入库后表现为 ratio_total=12 假比例 + 荒谬解锁价值（BNB $8.32 万亿 / 占市值 8524%）。

设计原则：**宁可漏杀不可错杀** —— 漏判的行只是多一条噪声数据，
错杀会丢掉真实解锁事件（SHIB/SOL/YFI 等真实事件中同样存在 pct=12.0 / $123.46K）。
"""
from __future__ import annotations

__all__ = [
    "TEMPLATE_PLACEHOLDER_VALUE_USD",
    "TEMPLATE_PLACEHOLDER_PCT",
    "TEMPLATE_STATUS_PATTERNS",
    "is_template_event",
    "classify_event_quality",
    "sanitize_supply_basis",
    "MAX_SUPPLY_CIRC_MULTIPLE_MAX",
    "_to_float_or_none",
]


# ── 模板占位行特征常量（源站写死的值）──────────────────────────────
TEMPLATE_PLACEHOLDER_VALUE_USD = 123460.0
TEMPLATE_PLACEHOLDER_PCT = 12.0
TEMPLATE_STATUS_PATTERNS = ("months left", "next unlock", "next")


def _to_float_or_none(val) -> float | None:
    """宽松数值转换：字符串/数字/Decimal 皆可，bool 明确拒绝（避免 True → 1.0）。

    ⚠️ 必须支持 Decimal —— psycopg 从 NUMERIC 列取回的全是 Decimal，
    用 `isinstance(val, (int, float))` 会把它们全部误判为「非数值」而返回 None
    （这会让 97.9% 的真实值被护栏误杀，2026-10-26 实测踩过）。
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    # Decimal / numpy 数值等：走通用 float() 转换
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def is_template_event(ev: dict) -> bool:
    """判定 tokenomist 模板占位行。

    三条同时成立才判为模板行：
      ① value_usd 精确等于 123460.0（源站写死的占位金额）
      ② pct 精确等于 12.0（源站写死的占位比例）
      ③ status 属于模板话术集合（"N months left" / "next unlock" / "next"）

    任一不成立 ⇒ 视为真实事件（宁可漏杀）。
    """
    if not isinstance(ev, dict):
        return False
    val_f = _to_float_or_none(ev.get("value_usd"))
    pct_f = _to_float_or_none(ev.get("pct"))
    if val_f is None or pct_f is None:
        return False
    if abs(val_f - TEMPLATE_PLACEHOLDER_VALUE_USD) > 0.01:
        return False
    if abs(pct_f - TEMPLATE_PLACEHOLDER_PCT) > 1e-9:
        return False
    status = str(ev.get("status") or "").lower()
    if not any(p in status for p in TEMPLATE_STATUS_PATTERNS):
        return False
    return True


def classify_event_quality(ev: dict) -> str:
    """事件质量分级，供母表加 event_quality 字段（G3.2）。

    返回：
      "template"    —— 模板占位行，应丢弃
      "implausible" —— 比例越界（>100%），不应据此反推金额
      "real"        —— 正常事件
    """
    if not isinstance(ev, dict):
        return "implausible"
    if is_template_event(ev):
        return "template"
    pct = _to_float_or_none(ev.get("pct") or ev.get("unlock_pct"))
    if pct is not None and pct > 100:
        return "implausible"
    return "real"


# ── 供应量基准合理性（源头 1 治理 G1.1/G1.2 的共用判据）────────────
# max_supply 相对流通量的合理上限：PoW 币的「最大供应量」通常在流通量的
# 1~10 倍内（ZEC 21M / circ 16.9M = 1.24；KAS 27.8B / circ 27.8B = 1.0）。
# 超过 100 倍几乎必然是单位解析错误或源站填错（实测污染样本倍率 388~4.4 亿倍）。
MAX_SUPPLY_CIRC_MULTIPLE_MAX = 100.0


def sanitize_supply_basis(
    max_supply,
    circulating_supply,
    total_supply=None,
    cmc_total_supply=None,
) -> tuple[float | None, str]:
    """校验 max_supply 是否可信，返回 (可用值, 口径标记)。

    规则（按优先级）：
      ① 候选值必须 > 0
      ② 若有 circulating_supply：候选值必须落在 [circ, circ × MAX_SUPPLY_CIRC_MULTIPLE_MAX]
      ③ 若有 cmc_total_supply：候选值与 CMC 差 >10 倍 ⇒ 判污染
      ④ 全部通过才返回候选值；否则返回 (None, 原因标记)

    口径标记取值：
      "ok"                      —— 通过全部校验
      "reject_no_circulating"   —— 无流通量可参照，但有 CMC 且偏差过大
      "reject_cmc_mismatch"     —— 与 CMC 偏差过大
      "reject_circ_range"       —— 超出 [circ, circ×100] 区间
      "reject_nonpositive"      —— 非法值
    """
    cand = _to_float_or_none(max_supply)
    if cand is None or cand <= 0:
        return None, "reject_nonpositive"

    circ = _to_float_or_none(circulating_supply)
    total = _to_float_or_none(total_supply)

    # ① 流通量区间护栏
    #    circ 缺失或为 0（CMC 未提供流通量，实测 2,913 行）时**跳过**——
    #    否则会把 UNIT/NEU/DOGEC/SWIFT 这类「max 远大于 circ 但 circ 本身无意义」的
    #    正常资产全部误杀。改用 total_supply 兜底。
    #    max 略小于 circ 是正常的（流通量可能已超过名义最大供应量，SNX 即此），
    #    故下界取 circ 的 50%（宽松），上界取 circ×MAX_SUPPLY_CIRC_MULTIPLE_MAX。
    if circ is not None and circ > 0:
        lo = circ * 0.5
        hi = circ * MAX_SUPPLY_CIRC_MULTIPLE_MAX
        if not (lo <= cand <= hi):
            return None, "reject_circ_range"
    elif total is not None and total > 0:
        lo = total * 0.5
        hi = total * MAX_SUPPLY_CIRC_MULTIPLE_MAX
        if not (lo <= cand <= hi):
            return None, "reject_total_range"

    # ② CMC 交叉校验
    #    circ 缺失时，CMC 的 total_supply 才是可比口径；
    #    此处 cmc_total 与 total 同源（生产路径传入的就是 CMC 权威 total）。
    cmc = _to_float_or_none(cmc_total_supply)
    if cmc is None:
        cmc = total
    if cmc is not None and cmc > 0:
        ratio = cand / cmc
        if ratio > 10 or ratio < 0.1:
            return None, "reject_cmc_mismatch"

    return cand, "ok"
