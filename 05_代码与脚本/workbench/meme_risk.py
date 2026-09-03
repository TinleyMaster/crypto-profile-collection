"""Meme 五维风险标签评分引擎（纯规则，零 LLM）。

五轴：contract(合约安全) / liquidity(流动性) / holder(筹码集中度) / lifecycle(生命周期) / social(社交热度)
每轴: score(0-100) + label(red/yellow/green/unknown)
加权合成 total_score + risk_label
一票否决: is_honeypot / mint_authority / freeze_authority 非空 → block

参考: db_stats._compute_pressure_score 范式 + market_rules.yaml 外置阈值
"""
from __future__ import annotations

from typing import Any


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _load_meme_risk() -> dict:
    """从 market_rules.yaml 加载 [meme_risk] 段（缺失/解析失败回退内建默认值）。"""
    weights = {"contract": 0.30, "liquidity": 0.25, "holder": 0.20, "lifecycle": 0.15, "social": 0.10}
    thresholds: dict[str, dict] = {}
    block_flags = ["is_honeypot", "mint_authority", "freeze_authority"]
    try:
        import yaml
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_rules.yaml")
        if not os.path.exists(path):
            return {"weights": weights, "thresholds": thresholds, "block_flags": block_flags}
        data = (yaml.safe_load(open(path, encoding="utf-8")) or {}).get("meme_risk") or {}
        if isinstance(data.get("weights"), dict) and data["weights"]:
            weights.update({k: float(v) for k, v in data["weights"].items() if k in weights})
        if isinstance(data.get("block_flags"), list) and data["block_flags"]:
            block_flags = [str(x) for x in data["block_flags"]]
        for axis in ("contract", "liquidity", "holder", "lifecycle", "social"):
            if isinstance(data.get(axis), dict):
                thresholds[axis] = data[axis]
    except Exception:
        pass
    return {"weights": weights, "thresholds": thresholds, "block_flags": block_flags}


def _label(score: float | None, thresholds: dict) -> str:
    """根据分数给 red/yellow/green/unknown 标签。"""
    if score is None:
        return "unknown"
    red = thresholds.get("red", 70)
    yellow = thresholds.get("yellow", 40)
    if score >= red:
        return "red"
    if score >= yellow:
        return "yellow"
    return "green"


# ── 轴评分函数 ──

def score_contract(data: dict, thresholds: dict) -> tuple[float | None, str, list[str]]:
    """合约安全轴: risk_score 直读 + 布尔红旗叠加。"""
    flags: list[str] = []
    base = _to_float(data.get("risk_score"))

    # 布尔红旗 → 扣分（每条 +15 分）
    red_flags = {
        "is_honeypot": ("honeypot", 40),
        "can_take_back_ownership": ("take_back_ownership", 20),
        "is_blacklisted": ("blacklisted", 15),
    }
    for key, (name, weight) in red_flags.items():
        if data.get(key) is True:
            flags.append(name)
            base = (base or 30) + weight  # 无 risk_score 时以 30 为基线

    # mint/freeze authority 非空 → 红旗
    if data.get("mint_authority"):
        flags.append("mint_authority")
        base = (base or 30) + 15
    if data.get("freeze_authority"):
        flags.append("freeze_authority")
        base = (base or 30) + 10

    score = min(100, max(0, base)) if base is not None else None
    return score, _label(score, thresholds), flags


def score_liquidity(data: dict, thresholds: dict) -> tuple[float | None, str, list[str]]:
    """流动性轴: total_liquidity_usd + pool_count + top_pool_share_pct。"""
    flags: list[str] = []
    liq = _to_float(data.get("total_liquidity_usd"))
    top_share = _to_float(data.get("top_pool_share_pct"))

    if liq is None and data.get("source_status") == "na":
        return None, "unknown", ["no_liquidity_data"]

    score = 50  # 基线
    if liq is not None:
        min_liq = thresholds.get("min_usd", 100_000)
        if liq < min_liq:
            score = 80
            flags.append("low_liquidity")
        elif liq < min_liq * 10:
            score = 55
        else:
            score = 20

    if top_share is not None and top_share > thresholds.get("top_pool_pct", 80):
        score = max(score, 75)
        flags.append("concentrated_pool")

    return min(100, score), _label(score, thresholds), flags


def score_holder(data: dict, thresholds: dict) -> tuple[float | None, str, list[str]]:
    """筹码集中度轴: whale_balance_change_7d_pct + holder_change_7d。"""
    flags: list[str] = []
    whale_chg = _to_float(data.get("whale_balance_change_7d_pct"))
    holder_chg = _to_float(data.get("holder_change_7d"))
    qual = data.get("qualitative_activity")  # transfer-log 降级

    # 若精确快照全无且定性也无 → 显式声明不可用（非 silent unknown）
    if whale_chg is None and holder_chg is None and qual is None:
        return None, "unknown", ["qualitative_unavailable"]

    score = 50
    if whale_chg is not None:
        if whale_chg > thresholds.get("whale_inflow_pct", 3.0):
            score = 80
            flags.append("whale_inflow")
        elif whale_chg < -thresholds.get("whale_inflow_pct", 3.0):
            score = 30

    if holder_chg is not None:
        if holder_chg < 0:
            score = max(score, 65)
            flags.append("holder_decline")

    # 定性兜底：精确数据缺失时用 transfer-log 活跃度调制
    if qual is not None and whale_chg is None and holder_chg is None:
        level = qual.get("activity_level", "low")
        if level == "high":
            score = 40  # 高活跃 → 偏正面
            flags.append("qual_high_activity")
        elif level == "mid":
            score = 55
            flags.append("qual_mid_activity")
        else:
            score = 75  # 低活跃 → 偏负面
            flags.append("qual_low_activity")

    return min(100, score), _label(score, thresholds), flags


def score_lifecycle(data: dict, thresholds: dict) -> tuple[float | None, str, list[str]]:
    """生命周期轴: launch_date 年龄分桶。"""
    launch = data.get("launch_date")
    if launch is None:
        return None, "unknown", ["no_launch_date"]

    from datetime import date, timedelta
    try:
        if isinstance(launch, str):
            launch = date.fromisoformat(launch[:10])
        age_days = (date.today() - launch).days
    except (ValueError, TypeError):
        return None, "unknown", ["invalid_launch_date"]

    buckets = thresholds.get("age_buckets", {
        "red_max": 7, "yellow_max": 30, "green_min": 90,
    })
    if age_days < buckets.get("red_max", 7):
        return 85, "red", [f"new_{age_days}d"]
    if age_days < buckets.get("yellow_max", 30):
        return 60, "yellow", [f"young_{age_days}d"]
    if age_days >= buckets.get("green_min", 90):
        return 20, "green", [f"mature_{age_days}d"]
    return 40, "yellow", [f"mid_{age_days}d"]


def score_social(data: dict, thresholds: dict) -> tuple[float | None, str, list[str]]:
    """社交热度轴（方案 A）：死盘/零社交=风险；高热度=关注度（正面，低分）。取各渠道最活跃值。"""
    kol = _to_float(data.get("kol_signal_count_30d"))
    gh = _to_float(data.get("github_activity_30d"))
    if kol is None and gh is None:
        return None, "unknown", ["no_social_data"]

    flags: list[str] = []
    parts: list[float] = []
    if kol is not None:
        max_kol = thresholds.get("kol_max_count", 5)
        if kol >= max_kol:
            parts.append(25); flags.append("high_kol_attention")
        elif kol > 0:
            parts.append(45); flags.append("some_kol_signal")
        else:
            parts.append(85); flags.append("dead_kol")
    if gh is not None:
        min_active = thresholds.get("gh_min_activity", 5)
        if gh >= min_active:
            parts.append(25); flags.append("active_github")
        elif gh > 0:
            parts.append(50); flags.append("low_github")
        else:
            parts.append(85); flags.append("dead_github")

    score = min(parts)  # 任一渠道活跃即不算死盘
    return min(100, score), _label(score, thresholds), flags


def compute_meme_risk(asset_id: int, conn) -> dict:
    """计算单资产的五维风险标签。返回 UPSERT 兼容 dict。"""
    from crypto_research.db.conn import get_connection

    # 加载阈值/权重（yaml 覆盖内建默认值）
    rules = _load_meme_risk()
    weights = rules["weights"]
    block_flags = rules["block_flags"]

    # 从各表取数据（每个查询独立 try/except，一个失败不阻断其余）
    data: dict[str, Any] = {}

    # contract 轴
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM biz.asset_contract_security WHERE asset_id = %s", (asset_id,))
            row = cur.fetchone()
        if row:
            data.update(dict(row))
    except Exception:
        conn.rollback()

    # liquidity 轴
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM biz.asset_liquidity WHERE asset_id = %s LIMIT 1", (asset_id,))
            row = cur.fetchone()
        if row:
            data.update(dict(row))
    except Exception:
        conn.rollback()

    # holder 轴（精确快照优先，无则 transfer-log 定性兜底）
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT whale_balance_change_7d_pct, holder_change_7d "
                "FROM biz.onchain_holder_snapshot "
                "WHERE asset_id = %s ORDER BY snapshot_date DESC LIMIT 1",
                (asset_id,),
            )
            row = cur.fetchone()
        if row:
            data.update(dict(row))
        else:
            # 精确快照无数据 → 尝试 transfer-log 定性兜底
            qual = estimate_from_transfers(asset_id, conn)
            if qual:
                data["qualitative_activity"] = qual
    except Exception:
        conn.rollback()

    # lifecycle 轴
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT launch_date FROM core.asset WHERE asset_id = %s", (asset_id,))
            row = cur.fetchone()
        if row:
            data["launch_date"] = row[0]
    except Exception:
        conn.rollback()

    # social 轴（双代理：kol_signal + github_repo_activity）
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM biz.kol_signal "
                "WHERE symbol = (SELECT UPPER(canonical_symbol) FROM core.asset WHERE asset_id = %s) "
                "AND created_at >= NOW() - INTERVAL '30 days'",
                (asset_id,),
            )
            row = cur.fetchone()
            data["kol_signal_count_30d"] = row[0] if row else 0

            cur.execute(
                "SELECT COUNT(*) FROM biz.github_repo_activity "
                "WHERE repo_id IN (SELECT repo_id FROM biz.asset_github_repo WHERE asset_id = %s) "
                "AND activity_date >= CURRENT_DATE - INTERVAL '30 days'",
                (asset_id,),
            )
            row = cur.fetchone()
            data["github_activity_30d"] = row[0] if row else 0
    except Exception:
        conn.rollback()

    # 五轴评分
    contract_score, contract_label, contract_flags = score_contract(data, rules["thresholds"].get("contract", {}))
    liquidity_score, liquidity_label, liquidity_flags = score_liquidity(data, rules["thresholds"].get("liquidity", {}))
    holder_score, holder_label, holder_flags = score_holder(data, rules["thresholds"].get("holder", {}))
    lifecycle_score, lifecycle_label, lifecycle_flags = score_lifecycle(data, rules["thresholds"].get("lifecycle", {}))
    social_score, social_label, social_flags = score_social(data, rules["thresholds"].get("social", {}))

    all_flags = contract_flags + liquidity_flags + holder_flags + lifecycle_flags + social_flags

    # 一票否决
    risk_label = "unknown"
    total_score = 0
    block = False
    for flag in block_flags:
        v = data.get(flag)
        if v is True or (v is not None and not isinstance(v, bool) and v != "" and v != []):
            block = True
            all_flags.insert(0, f"BLOCK:{flag}")

    # 加权合成
    axes = [
        ("contract", contract_score), ("liquidity", liquidity_score),
        ("holder", holder_score), ("lifecycle", lifecycle_score),
        ("social", social_score),
    ]
    known = [(name, score) for name, score in axes if score is not None]
    axes_computed = len(known)

    if axes_computed == 0:
        risk_label = "unknown"
        total_score = 0
    elif block:
        risk_label = "block"
        total_score = 100
    else:
        w_sum = sum(weights[name] for name, _ in known)
        total_score = round(sum(score * weights[name] for name, score in known) / w_sum, 1) if w_sum > 0 else 0
        if total_score >= 70:
            risk_label = "high"
        elif total_score >= 45:
            risk_label = "medium"
        else:
            risk_label = "low"

    return {
        "asset_id": asset_id,
        "contract_score": contract_score, "contract_label": contract_label,
        "liquidity_score": liquidity_score, "liquidity_label": liquidity_label,
        "holder_score": holder_score, "holder_label": holder_label,
        "lifecycle_score": lifecycle_score, "lifecycle_label": lifecycle_label,
        "social_score": social_score, "social_label": social_label,
        "axes_computed": axes_computed,
        "total_score": total_score,
        "risk_label": risk_label,
        "flags": all_flags if all_flags else None,
    }
