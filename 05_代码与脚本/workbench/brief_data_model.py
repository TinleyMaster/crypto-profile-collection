"""
早报数据契约层 (Brief Data Model)
=================================

问题背景：
  数据层（macro_market.py 的 fetch_* 函数）和渲染层（send_daily_brief.py）之间
  没有统一的字段契约，导致大量字段名不匹配 Bug（如 whale_change_pct vs
  whale_balance_change_7d_pct、amount_usd vs value_usd 等）。

设计目标：
  1. 统一字段命名：一套规范的字段名，所有老字段在适配层映射
  2. 类型明确：TypedDict 定义每个模块的字段和类型
  3. 增量接入：作为适配器存在，不强制，可逐步迁移
  4. 自检能力：检查关键字段完整性，发现数据质量问题

使用方式：
  from brief_data_model import normalize_brief, BriefData

  raw_brief = generate_morning_brief(...)
  brief = normalize_brief(raw_brief)  # 标准化后传给渲染层
  health = check_brief_health(brief)  # 数据健康度检查

模块命名：
  顶层 BriefData 对应原来的 brief dict
  每个子模块用 xxx_data 命名（如 tldr_data, whale_moves_data）
"""

from __future__ import annotations

from typing import TypedDict, Optional, List, Dict, Any


# ════════════════════════════════════════════════════════════
# 一、TypedDict 类型定义
# ════════════════════════════════════════════════════════════

class TldrData(TypedDict, total=False):
    """M0 头部摘要：大盘核心指标。"""
    date: str                       # 日期 YYYY-MM-DD
    btc_price: float                # BTC 价格 (USD)
    btc_change_24h_pct: float       # BTC 24h 涨跌幅 (%)
    btc_change_7d_pct: Optional[float]  # BTC 7日涨跌幅 (%)
    btc_volatility_7d: float        # BTC 7日波动率 (%)
    eth_price: float                # ETH 价格 (USD)
    eth_change_24h_pct: float       # ETH 24h 涨跌幅 (%)
    total_market_cap: float         # 总市值 (USD)
    fear_greed: int                 # 恐贪指数 (0-100)
    fear_greed_label: str           # 恐贪标签 (Greed/Fear 等)
    btc_cycle_phase: str            # BTC 周期阶段
    summary: str                    # 一句话摘要
    tldr: str                       # TLDR 文本


class TransferItem(TypedDict, total=False):
    """单笔大额转账。"""
    symbol: str                     # 币种符号
    chain: str                      # 链
    value_usd: float                # 转账金额 (USD) — 统一字段名
    from_address: str               # 发起地址
    to_address: str                 # 接收地址
    from_label: str                 # 发起地址标签（单值，粗糙）
    to_label: str                   # 接收地址标签（单值，粗糙）
    from_labels: List[str]          # 发起地址标签数组（类型）
    to_labels: List[str]            # 接收地址标签数组（类型）
    from_label_names: List[str]     # 发起地址标签名称（具体，如 "Binance 14"）
    to_label_names: List[str]       # 接收地址标签名称
    from_exchange: Optional[str]    # 发起交易所名称（如是）
    to_exchange: Optional[str]      # 接收交易所名称（如是）
    direction: str                  # 方向描述（inflow/outflow 等）
    tx_hash: str                    # 交易哈希
    block_timestamp: str            # 区块时间


class WhaleMovesData(TypedDict, total=False):
    """链上大额转账异动。"""
    status: str                     # ok / empty / error
    transfers: List[TransferItem]   # 转账列表
    total_count: int                # 总笔数
    total_usd: float                # 总金额 (USD)
    exchange_in_count: int          # 充值交易所笔数
    exchange_in_usd: float          # 充值金额 (USD)
    exchange_out_count: int         # 提现交易所笔数
    exchange_out_usd: float         # 提现金额 (USD)
    net_exchange_usd: float         # 净流入 (USD，正=流入)
    whale_to_whale_count: int       # 巨鲸互转笔数
    hours: int                      # 时间窗口 (小时)


class HolderItem(TypedDict, total=False):
    """持仓集中度单条记录。"""
    symbol: str                     # 币种
    name: str                       # 名称
    chain: str                      # 链
    top10_concentration: float      # Top10 持仓集中度 (%)
    top50_concentration: float      # Top50 持仓集中度 (%)
    whale_balance_change_7d_pct: float  # 巨鲸7日余额变化 (%) — 统一字段名
    total_holders: int              # 总持仓地址数
    exchange_wallet_pct: float      # 交易所钱包占比 (%)
    smart_money_pct: float          # 聪明钱占比 (%)
    market_cap_rank: int            # 市值排名


class HolderConcentrationData(TypedDict, total=False):
    """持仓集中度摘要。"""
    status: str                     # ok / empty / error
    snapshot_date: str              # 快照日期
    most_concentrated: List[HolderItem]   # 最集中 Top N
    whale_buying: List[HolderItem]        # 巨鲸增仓 Top N
    whale_selling: List[HolderItem]       # 巨鲸减仓 Top N


class StablecoinData(TypedDict, total=False):
    """稳定币供应趋势。"""
    status: str                     # ok / error
    total_usd: float                # 总供应量 (USD)
    change_1d_pct: float            # 1日变化率 (%)
    change_7d_pct: float            # 7日变化率 (%)
    change_7d_usd: float            # 7日变化金额 (USD，近似估算)
    top_assets: List[dict]          # Top N 稳定币明细（可选）


class SectorFlowData(TypedDict, total=False):
    """赛道资金流向。"""
    status: str                     # ok / error
    total_volume_24h: float         # 24h 总成交量 (USD)
    sectors: List[dict]             # 赛道列表


class EtfFlowData(TypedDict, total=False):
    """ETF 资金流。"""
    status: str                     # ok / error
    assets: List[dict]              # ETF 资产列表


class UnlockItem(TypedDict, total=False):
    """单条解锁事件。"""
    symbol: str                     # 币种
    name: str                       # 名称
    unlock_date: str                # 解锁日期 YYYY-MM-DD
    unlock_type: str                # 解锁类型
    unlock_amount: float            # 解锁数量（枚）
    unlock_value_usd: float         # 解锁价值 (USD) — 统一字段名
    unlock_ratio_circulating: float      # 占流通量百分比 (%) — 统一使用
    unlock_ratio_circulating_src: str    # circulating 来源: source/computed
    unlock_ratio_total: float            # 占总供给百分比 (%)
    unlock_ratio_mcap: float             # 占市值百分比 (%)
    risk_level: str                 # 风险等级
    beneficiary_type: str           # 受益方类型
    market_cap_rank: int            # 市值排名


class UpcomingUnlocksData(TypedDict, total=False):
    """即将解锁事件。"""
    status: str                     # ok / empty / error
    unlocks: List[UnlockItem]       # 解锁事件列表
    days: int                       # 时间窗口 (天)


class KolSignalItem(TypedDict, total=False):
    """单条 KOL 链上信号。"""
    signal_id: int                  # 信号ID
    signal_category: str            # 分类 (onchain)
    signal_subtype: str             # 子类型 (exchange_flow/smart_money 等)
    symbol: str                     # 币种符号（可能有噪声）
    event_token: str                # 链上事件对应的代币（更可靠）
    event_direction: str            # 事件方向 (in/out)
    event_amount: float             # 事件金额
    event_usd_value: float          # 事件价值 (USD)
    kol_name: str                   # KOL 名称
    confidence: float               # 置信度
    created_at: str                 # 创建时间


class KolOnchainData(TypedDict, total=False):
    """KOL 链上信号。"""
    status: str                     # ok / error
    signals: List[KolSignalItem]    # 信号列表
    stats: List[dict]               # 各子类型统计
    kols: List[str]                 # 涉及的KOL
    hours: int                      # 时间窗口 (小时)


class AiSummaryData(TypedDict, total=False):
    """AI 今日定调。"""
    status: str                     # ok / error
    headline: str                   # 头条标题
    market_regime: str              # 市场状态
    bias: str                       # 多空倾向
    conviction: str                 # 置信度
    key_drivers: List[str]          # 核心驱动因素
    trade_suggestions: List[str]    # 交易建议
    risk_warnings: List[str]        # 风险提示
    sector_rotation: List[dict]     # 赛道轮动建议
    watchlist: List[dict]           # 观察列表


class NarrativeItem(TypedDict, total=False):
    """单条叙事/赛道。"""
    name: str                       # 叙事/赛道名称
    change_7d: float                # 7日涨跌幅 (%)
    net_flow_usd: float             # 净流入 (USD)
    confidence: float               # 置信度
    direction: str                  # 方向 (up/down)
    top_coins: List[dict]           # 领涨币


class NarrativeFlowData(TypedDict, total=False):
    """叙事榜/赛道轮动。"""
    status: str                     # ok / error
    ranked: List[NarrativeItem]     # 排名列表
    degraded: List[str]             # 降级项


class CatalystData(TypedDict, total=False):
    """催化剂（宏观+代币事件）。"""
    hardcoded: List[dict]           # 宏观硬日程
    token_events: List[dict]        # 代币级事件


class BriefData(TypedDict, total=False):
    """早报完整数据契约（标准格式）。

    所有经过 normalize_brief() 处理的数据都应遵循此结构。
    渲染层只依赖此结构中的标准字段名。
    """
    # ── 核心模块 ──
    M0_tldr: TldrData               # 头部摘要
    M0_ai_summary: AiSummaryData    # AI 定调
    M2_flow: Dict[str, Any]         # 资金流（兼容原结构）
    M2_sector_flow: SectorFlowData  # 赛道资金流
    M2_etf_flow: EtfFlowData        # ETF 资金流
    M2_whale_moves: WhaleMovesData  # 大额转账
    M2_holder_concentration: HolderConcentrationData  # 持仓集中度
    M2_stablecoin: StablecoinData   # 稳定币供应
    M6_catalyst: CatalystData       # 催化剂
    M6_upcoming_unlocks: UpcomingUnlocksData  # 即将解锁
    kol_onchain: KolOnchainData     # KOL 链上信号
    narrative_flow: NarrativeFlowData  # 叙事榜/赛道轮动

    # ── 其他模块（原样保留，逐步标准化） ──
    M2_exchange_flow: Dict[str, Any]
    M7_divergence: Dict[str, Any]
    M8_opportunities: List[dict]
    M8_watchlist: List[dict]
    M8_resonance: Dict[str, Any]
    M8_meme: Dict[str, Any]
    M8_chimney: Dict[str, Any]
    M8_smart_money: Dict[str, Any]
    M9_degraded: List[str]          # 数据层降级项
    degraded: List[str]             # 渲染层降级项（保留兼容）
    DIFF: Dict[str, Any]            # 日度差异
    status: str                     # 整体状态


# ════════════════════════════════════════════════════════════
# 二、字段映射表（旧字段名 → 标准字段名）
# ════════════════════════════════════════════════════════════

# 巨鲸动向/持仓集中度：余额变化字段
_WHALE_CHANGE_FIELDS = (
    "whale_balance_change_7d_pct",  # 标准字段（数据源正确字段）
    "whale_change_pct",             # 渲染端旧字段（错误，已废弃）
)

# 大额转账：金额字段
_TRANSFER_AMOUNT_FIELDS = (
    "value_usd",                    # 标准字段（SQL 实际查的字段）
    "amount_usd",                   # 渲染端旧字段（错误，已废弃）
)

# 解锁：价值字段
_UNLOCK_VALUE_FIELDS = (
    "unlock_value_usd",             # 标准字段
    "value_usd",                    # 别名
)

# 解锁：解锁数量字段
_UNLOCK_AMOUNT_FIELDS = (
    "unlock_amount",                # 标准字段
    "amount",                       # 别名
)

# 解锁：日期字段
_UNLOCK_DATE_FIELDS = (
    "unlock_date",                  # 标准字段
    "date",                         # 别名
)

# 解锁：占比字段（按优先级回退）
_UNLOCK_RATIO_FIELDS = (
    "unlock_ratio_circulating",     # 占流通（最有参考价值）
    "unlock_ratio_total",           # 占总供给
    "unlock_ratio_mcap",            # 占市值
    "pct_of_supply",                # 旧字段
    "unlock_pct",                   # 旧字段
)


# ════════════════════════════════════════════════════════════
# 三、normalize_brief 适配器
# ════════════════════════════════════════════════════════════

def _first_non_none(data: dict, keys: tuple) -> Optional[Any]:
    """按顺序尝试多个键，返回第一个非 None 值。"""
    for k in keys:
        v = data.get(k)
        if v is not None:
            return v
    return None


def _normalize_transfer(t: dict) -> dict:
    """标准化单笔转账：统一字段名。"""
    if not isinstance(t, dict):
        return t

    # 金额：统一为 value_usd（同时保留旧字段兼容）
    amount = _first_non_none(t, _TRANSFER_AMOUNT_FIELDS)
    if amount is not None:
        t["value_usd"] = amount

    return t


def _normalize_holder_item(h: dict) -> dict:
    """标准化持仓集中度条目：统一巨鲸变化字段。"""
    if not isinstance(h, dict):
        return h

    # 巨鲸7日变化：统一为 whale_balance_change_7d_pct
    val = _first_non_none(h, _WHALE_CHANGE_FIELDS)
    if val is not None:
        h["whale_balance_change_7d_pct"] = val

    return h


def _normalize_unlock_item(u: dict) -> dict:
    """标准化解锁事件条目：统一字段名。"""
    if not isinstance(u, dict):
        return u

    # 解锁价值：统一为 unlock_value_usd
    val = _first_non_none(u, _UNLOCK_VALUE_FIELDS)
    if val is not None:
        u["unlock_value_usd"] = val

    # 解锁数量：统一为 unlock_amount
    amt = _first_non_none(u, _UNLOCK_AMOUNT_FIELDS)
    if amt is not None:
        u["unlock_amount"] = amt

    # 解锁日期：统一为 unlock_date
    dt = _first_non_none(u, _UNLOCK_DATE_FIELDS)
    if dt is not None:
        u["unlock_date"] = dt

    return u


def _normalize_whale_moves(data: dict) -> dict:
    """标准化大额转账模块。"""
    if not isinstance(data, dict):
        return data

    transfers = data.get("transfers") or []
    if transfers:
        data["transfers"] = [_normalize_transfer(t) for t in transfers]

    return data


def _normalize_holder_concentration(data: dict) -> dict:
    """标准化持仓集中度模块。"""
    if not isinstance(data, dict):
        return data

    for key in ("most_concentrated", "whale_buying", "whale_selling"):
        items = data.get(key) or []
        if items:
            data[key] = [_normalize_holder_item(h) for h in items]

    return data


def _normalize_stablecoin(data: dict) -> dict:
    """标准化稳定币模块：补充计算 7日变化金额。"""
    if not isinstance(data, dict):
        return data

    total = data.get("total_usd")
    change_7d = data.get("change_7d_pct")
    if total and change_7d is not None and data.get("change_7d_usd") is None:
        try:
            data["change_7d_usd"] = round(float(total) * float(change_7d) / 100, 2)
        except (ValueError, TypeError):
            pass

    return data


def _normalize_upcoming_unlocks(data: dict) -> dict:
    """标准化解锁模块。"""
    if not isinstance(data, dict):
        return data

    unlocks = data.get("unlocks") or []
    if unlocks:
        data["unlocks"] = [_normalize_unlock_item(u) for u in unlocks]

    return data


def _normalize_narrative_flow(data: dict) -> dict:
    """标准化叙事榜/赛道轮动。"""
    if not isinstance(data, dict):
        return data

    ranked = data.get("ranked") or []
    if ranked:
        normalized = []
        for item in ranked:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            # 巨鲸变化字段归一化
            val = _first_non_none(item, _WHALE_CHANGE_FIELDS)
            if val is not None:
                item["whale_balance_change_7d_pct"] = val
            normalized.append(item)
        data["ranked"] = normalized

    return data


def normalize_brief(raw_brief: dict) -> dict:
    """将原始 brief 数据标准化为 BriefData 格式。

    主要工作：
    1. 统一字段命名（旧字段 → 标准字段）
    2. 补充派生字段（如稳定币 change_7d_usd）
    3. 数组元素逐条标准化
    4. 保留所有原始字段（向后兼容）

    Args:
        raw_brief: generate_morning_brief() 返回的原始 brief dict

    Returns:
        标准化后的 brief dict（同时保留原字段，确保兼容）
    """
    if not isinstance(raw_brief, dict):
        return raw_brief

    brief = raw_brief  # 原地修改（所有字段都是新增或覆盖修正，不删除）

    # M2 模块标准化
    if "M2_whale_moves" in brief:
        brief["M2_whale_moves"] = _normalize_whale_moves(brief["M2_whale_moves"])
    if "M2_holder_concentration" in brief:
        brief["M2_holder_concentration"] = _normalize_holder_concentration(
            brief["M2_holder_concentration"])
    if "M2_stablecoin" in brief:
        brief["M2_stablecoin"] = _normalize_stablecoin(brief["M2_stablecoin"])
    if "M6_upcoming_unlocks" in brief:
        brief["M6_upcoming_unlocks"] = _normalize_upcoming_unlocks(
            brief["M6_upcoming_unlocks"])

    # 其他模块的标准化（逐步补充）
    if "narrative_flow" in brief:
        brief["narrative_flow"] = _normalize_narrative_flow(brief["narrative_flow"])

    # kol_onchain 内的 signal 标准化（暂无字段映射，预留）
    # 如果以后发现 KOL 信号也有字段不一致，在这里加

    return brief


# ════════════════════════════════════════════════════════════
# 四、数据健康度检查
# ════════════════════════════════════════════════════════════

# 核心模块的关键字段（缺失 = 严重降级）
_CRITICAL_FIELDS = {
    "M0_tldr": ["btc_price", "total_market_cap", "fear_greed"],
    "M2_whale_moves": ["transfers"],
    "M2_stablecoin": ["total_usd"],
    "M6_upcoming_unlocks": ["unlocks"],
}

# 辅助模块的关键字段（缺失 = 一般降级）
_WARNING_FIELDS = {
    "M2_holder_concentration": ["most_concentrated"],
    "M2_sector_flow": ["sectors", "total_volume_24h"],
    "M2_etf_flow": ["assets"],
    "kol_onchain": ["signals"],
    "narrative_flow": ["ranked"],
}


def check_brief_health(brief: dict) -> dict:
    """检查早报数据健康度。

    Args:
        brief: 标准化后的 brief dict

    Returns:
        {
            "score": 0-100 健康度评分,
            "critical": [缺失的核心字段列表],
            "warning": [缺失的辅助字段列表],
            "modules_ok": [正常的模块列表],
            "details": {模块名: {字段: 状态}}
        }
    """
    critical_missing = []
    warning_missing = []
    modules_ok = []
    details = {}

    def _check_module(mod_name: str, required_fields: list, level: str):
        mod = brief.get(mod_name)
        if not isinstance(mod, dict):
            missing = [f"{mod_name}.{f}" for f in required_fields]
            if level == "critical":
                critical_missing.extend(missing)
            else:
                warning_missing.extend(missing)
            details[mod_name] = {"status": "missing", "missing": required_fields}
            return

        missing_fields = []
        for f in required_fields:
            v = mod.get(f)
            if v is None or (isinstance(v, (list, str)) and len(v) == 0):
                missing_fields.append(f)

        if missing_fields:
            full_missing = [f"{mod_name}.{f}" for f in missing_fields]
            if level == "critical":
                critical_missing.extend(full_missing)
            else:
                warning_missing.extend(full_missing)
            details[mod_name] = {"status": "partial", "missing": missing_fields}
        else:
            modules_ok.append(mod_name)
            details[mod_name] = {"status": "ok"}

    for mod, fields in _CRITICAL_FIELDS.items():
        _check_module(mod, fields, "critical")

    for mod, fields in _WARNING_FIELDS.items():
        _check_module(mod, fields, "warning")

    # 评分：核心模块每个 15 分，辅助模块每个 8 分，满分 100
    critical_total = len(_CRITICAL_FIELDS) * 15
    warning_total = len(_WARNING_FIELDS) * 8
    max_score = critical_total + warning_total

    critical_lost = 0
    for mod in _CRITICAL_FIELDS:
        if details.get(mod, {}).get("status") != "ok":
            critical_lost += 15

    warning_lost = 0
    for mod in _WARNING_FIELDS:
        if details.get(mod, {}).get("status") != "ok":
            warning_lost += 8

    score = round((max_score - critical_lost - warning_lost) / max_score * 100)

    return {
        "score": score,
        "critical": critical_missing,
        "warning": warning_missing,
        "modules_ok": modules_ok,
        "details": details,
    }
