"""Meme 四阶段生命周期判定引擎（纯规则，零 LLM）。

阶段：launch → bloom → diverge → decay → unknown
输入四轴：launch_date(年龄) / liquidity(流动性) / social(社交热度) / holder(持仓变化)

参考: meme_risk.py 范式 + market_rules.yaml [meme_lifecycle] 外置阈值
"""
from __future__ import annotations

from datetime import date
from typing import Any


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _load_lifecycle() -> dict:
    """从 market_rules.yaml 加载 [meme_lifecycle] 段（缺失/解析失败回退内建默认值）。"""
    thresholds = {
        "launch_max_days": 14,
        "bloom_max_days": 60,
        "old_no_social_days": 90,
        "liq_launch_min": 10000,
        "liq_bloom_min": 50000,
        "liq_decay_max": 5000,
        "social_hot_min": 2,
        "holder_bleed_max": -10,
    }
    try:
        import yaml
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_rules.yaml")
        if not os.path.exists(path):
            return {"thresholds": thresholds}
        data = (yaml.safe_load(open(path, encoding="utf-8")) or {}).get("meme_lifecycle") or {}
        if isinstance(data.get("thresholds"), dict):
            for k, v in data["thresholds"].items():
                if k in thresholds:
                    thresholds[k] = type(thresholds[k])(v)
    except Exception:
        pass
    return {"thresholds": thresholds}


def classify_stage(asset: dict, rules: dict) -> tuple[str, dict]:
    """判定资产的四阶段生命周期。

    Args:
        asset: 包含 launch_date, liquidity, social_heat, holder_change_30d, proxy_age_days
        rules: _load_lifecycle() 返回值

    Returns:
        (stage, detail)。stage ∈ {launch, bloom, diverge, decay, unknown}
    """
    th = rules["thresholds"]

    launch = asset.get("launch_date")
    liq = asset.get("liquidity")  # dict or None
    social = _to_float(asset.get("social_heat"))
    holder30 = _to_float(asset.get("holder_change_30d"))

    proxy_used = False
    if launch is None:
        proxy_age = _to_float(asset.get("proxy_age_days"))
        if proxy_age is not None:
            age = int(proxy_age)
            proxy_used = True
        else:
            return "unknown", {"reason": "no_launch_date_and_no_proxy"}
    else:
        try:
            if isinstance(launch, str):
                launch = date.fromisoformat(launch[:10])
            age = (date.today() - launch).days
        except (ValueError, TypeError):
            return "unknown", {"reason": "invalid_launch_date"}

    liq_usd = _to_float(liq.get("total_liquidity_usd")) if isinstance(liq, dict) else None
    social_hot = social is not None and social >= th["social_hot_min"]
    holder_bleed = holder30 is not None and holder30 <= th["holder_bleed_max"]

    # 定性兜底：精确 holder 数据缺失时用 transfer-log 活跃度辅助判断
    qual = asset.get("qualitative_activity")
    if not holder_bleed and holder30 is None and qual is not None:
        level = qual.get("activity_level", "low")
        if level == "low":
            holder_bleed = True  # 低活跃 ≈ 流失信号

    # launch：新发且流动性已起量
    if age < th["launch_max_days"] and liq_usd is not None and liq_usd >= th["liq_launch_min"]:
        return "launch", {"age": age, "liq_usd": liq_usd, "proxy_used": proxy_used}

    # bloom：成长期 + 高流动性 + 高热度
    if (th["launch_max_days"] <= age < th["bloom_max_days"]
            and liq_usd is not None and liq_usd >= th["liq_bloom_min"]
            and social_hot):
        return "bloom", {"age": age, "liq_usd": liq_usd, "social": social}

    # decay：流动性萎缩 / holder 持续流失 / 老币无社交
    if (liq_usd is None or liq_usd < th["liq_decay_max"]
            or holder_bleed
            or (not social_hot and age > th["old_no_social_days"])):
        return "decay", {"age": age, "liq_usd": liq_usd, "holder30": holder30,
                         "social_hot": social_hot, "proxy_used": proxy_used}

    # diverge：其余（分歧期）
    return "diverge", {"age": age, "liq_usd": liq_usd, "social": social}


def compute_lifecycle(asset_id: int, conn) -> dict:
    """计算单资产的四阶段生命周期。返回 UPSERT 兼容 dict。"""
    rules = _load_lifecycle()

    asset: dict[str, Any] = {}

    # launch_date
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT launch_date FROM core.asset WHERE asset_id = %s", (asset_id,))
            row = cur.fetchone()
        if row:
            asset["launch_date"] = row[0]
    except Exception:
        conn.rollback()

    # liquidity（取最新一条）
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_liquidity_usd FROM biz.asset_liquidity WHERE asset_id = %s LIMIT 1",
                (asset_id,),
            )
            row = cur.fetchone()
        if row:
            asset["liquidity"] = {"total_liquidity_usd": row[0]}
    except Exception:
        conn.rollback()

    # social_heat（kol + github 代理）
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM biz.kol_signal "
                "WHERE symbol = (SELECT UPPER(canonical_symbol) FROM core.asset WHERE asset_id = %s) "
                "AND created_at >= NOW() - INTERVAL '30 days'",
                (asset_id,),
            )
            kol_count = (cur.fetchone() or [0])[0]

            cur.execute(
                "SELECT COUNT(*) FROM biz.github_repo_activity "
                "WHERE repo_id IN (SELECT repo_id FROM biz.asset_github_repo WHERE asset_id = %s) "
                "AND activity_date >= CURRENT_DATE - INTERVAL '30 days'",
                (asset_id,),
            )
            gh_count = (cur.fetchone() or [0])[0]

        asset["social_heat"] = max(kol_count, gh_count)
    except Exception:
        conn.rollback()

    # holder_change_30d（精确快照优先，无则 transfer-log 定性兜底）
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT holder_change_30d FROM biz.onchain_holder_snapshot "
                "WHERE asset_id = %s ORDER BY snapshot_date DESC LIMIT 1",
                (asset_id,),
            )
            row = cur.fetchone()
        if row:
            asset["holder_change_30d"] = row[0]
    except Exception:
        conn.rollback()

    # 定性兜底：精确快照无数据时用 transfer-log 活跃度
    if "holder_change_30d" not in asset:
        try:
            from meme_risk import estimate_from_transfers
            qual_list = estimate_from_transfers(asset_id, conn)
            if qual_list:
                best = max(qual_list, key=lambda x: {"high": 3, "mid": 2, "low": 1}.get(x["activity_level"], 0))
                asset["qualitative_activity"] = best
        except Exception:
            pass  # 兜底失败不阻断主流程

    # 判定
    stage, detail = classify_stage(asset, rules)

    return {
        "asset_id": asset_id,
        "stage": stage,
        "age_days": detail.get("age"),
        "liquidity_usd": detail.get("liq_usd"),
        "holder_change_30d": asset.get("holder_change_30d"),
        "social_score": asset.get("social_heat"),
        "proxy_used": detail.get("proxy_used", False),
    }
