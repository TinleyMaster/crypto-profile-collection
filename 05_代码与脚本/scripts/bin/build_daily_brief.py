#!/usr/bin/env python3
"""每日早报快照落库 + 趋势 diff（P1-4 第二刀）。

scheduler.py 注册：daily_brief_snapshot（Asia/Shanghai，早于邮件发送）。
流程：数据源新鲜度自检 → 实时拉 overview（force_refresh=1 绕过缓存）→
读昨日快照 → 生成早报 → 落库今日供明日 diff。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

# 将 workbench（macro_market.py）所在目录加入 sys.path：
#   prod: /app/scripts/bin → /app（macro_market.py 在 /app）
#   local: scripts/bin → 05_代码与脚本（macro_market.py 在 05_代码与脚本/workbench）
_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
for cand in (os.path.join(_code_root, "workbench"), "/app", _code_root):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

# scripts/src 加入 path（crypto_research 包）。
# 快照生成会调用 macro_market._build_mvrv_universe / fetch_*，它们内部
# `from crypto_research.config import get_settings`；缺此路径会以
# "No module named 'crypto_research'" 失败，导致 mvrv_universe 落库为 error
#（2026-09-16/17/18 连续出现，早报 MVRV 维度持续缺失）。
_scripts_src = os.path.join(_code_root, "scripts", "src")
if os.path.isdir(_scripts_src) and _scripts_src not in sys.path:
    sys.path.insert(0, _scripts_src)

from macro_market import (  # noqa: E402
    generate_morning_brief,
    get_market_overview,
    load_snapshot,
    save_snapshot,
)


def _freshness_verdict(latest, max_lag_days: int, today: date | None = None) -> tuple[bool, int | None, str]:
    """纯函数：判定单个数据源是否滞后。返回 (is_stale, days, label)。

    - latest 为 None（表空 / 查询不到）→ 视为**滞后**（不是「不告警」）；
    - 否则 days = today - latest，`days >= max_lag_days` 即判滞后
      （阈值 1 表示「隔天就算滞后」）。
    """
    if latest is None:
        return True, None, "无数据"
    days = ((today or date.today()) - latest).days
    return days >= max_lag_days, days, str(latest)


def _check_data_freshness() -> list[str]:
    """早报依赖数据源新鲜度自检：返回滞后（days >= max_lag_days）或无数据的数据源列表。

    在生成早报前调用，滞后项打印告警并附加到 brief.degraded（邮件降级标注）。
    逐源配置容忍周期：日更源=1（隔天即滞后）；ETF 交易日更、跨周末至多 4 天。
    旧实现用统一 `days > 2`：恐贪指数 09-25 vs 09-27 = 2 天不告警，采集停更被静默放过。
    """
    stale: list[str] = []
    checks = [
        ("恐贪指数",   "biz.fear_greed_daily",       "metric_date",   1),  # 日更
        ("BTC OI",    "biz.btc_oi_daily",           "metric_date",   1),  # 日更
        ("CEFI 指数",  "biz.cefi_index_daily",       "metric_date",   1),  # 日更
        ("赛道TVL",    "biz.category_tvl_daily",     "snapshot_date", 1),  # 日更
        ("ETF资金流",  "biz.etf_flow_daily",         "flow_date",     4),  # 交易日更，跨周末至多 4 天
        ("赛道市值",   "biz.sector_flow_daily",      "metric_date",   1),  # 日更
        ("大盘快照",   "biz.market_snapshot_daily",  "snapshot_date", 1),  # 日更
    ]
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                for name, tbl, col, max_lag in checks:
                    try:
                        cur.execute(f"SELECT MAX({col}) FROM {tbl}")
                        row = cur.fetchone()
                        latest = row[0] if row else None
                        is_stale, days, label = _freshness_verdict(latest, max_lag)
                        if is_stale:
                            _dtxt = f"滞后{days}天" if days is not None else "无数据"
                            print(f"[freshness] ⚠️ {name}: {label}（{_dtxt}，阈值{max_lag}天）")
                            stale.append(f"{name}({label}, {_dtxt}, 阈值{max_lag}天)")
                        else:
                            print(f"[freshness] ✓ {name}: {label}（滞后{days}天，阈值{max_lag}天）")
                    except Exception as e:
                        print(f"[freshness] ⚠️ {name}: 查询失败 {e}")
                        stale.append(f"{name}(查询失败, 阈值{max_lag}天)")
    except Exception as e:
        print(f"[freshness] ⚠️ 自检失败: {e}")
    return stale


def _read_app_commit() -> str | None:
    """W-06：读取生成时的代码版本（env 优先，其次 /app/.git_head / 仓库 .git_head）。

    读不到返回 None（写 NULL，不阻断早报主流程）。
    """
    for key in ("APP_COMMIT", "GIT_COMMIT", "GIT_SHA"):
        v = os.environ.get(key)
        if v and v.strip():
            return v.strip()[:64]
    for path in ("/app/.git_head", os.path.join(_code_root, ".git_head")):
        try:
            with open(path, encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v[:64]
        except Exception:
            pass
    return None


def _save_brief_snapshot(brief_date: str, brief: dict) -> None:
    """W-06：把**完整 brief** 落库（含 M0_ai_summary / data_quality），供 P2 变更日志与事后复盘。

    - 幂等：`ON CONFLICT (brief_date) DO UPDATE`（同日重跑只覆盖，不产生重复行）；
    - payload 不裁剪；为便于按工单口径查询（`payload ? 'data_quality'`），
      若 brief 顶层无 data_quality，则从 M0_ai_summary.data_quality 提升一份到顶层；
    - 落库失败**不阻断**早报主流程（早报本身比留痕重要）。
    """
    import json

    payload_obj = dict(brief or {})
    if "data_quality" not in payload_obj:
        payload_obj["data_quality"] = (brief.get("M0_ai_summary") or {}).get("data_quality") or []
    app_commit = _read_app_commit()
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        payload = json.dumps(payload_obj, ensure_ascii=False, default=str)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO biz.daily_brief_snapshot (brief_date, payload, app_commit)
                    VALUES (%s, %s::jsonb, %s)
                    ON CONFLICT (brief_date) DO UPDATE
                      SET payload = EXCLUDED.payload,
                          app_commit = EXCLUDED.app_commit,
                          created_at = NOW()
                    """,
                    (brief_date, payload, app_commit),
                )
            conn.commit()
        print(f"[brief_snapshot] 已落库 {brief_date}（app_commit={app_commit}）")
    except Exception as e:
        print(f"[brief_snapshot] 落库失败: {e}")


def _check_snapshot_gap(days: int = 7) -> list[str]:
    """W-11：校验最近 N 天 biz.market_overview_snapshot 是否连续，返回缺失日期列表。

    缺口会让次日 `load_snapshot(yesterday)` 取不到基准 → `DIFF` 为空（早报「没有与
    昨日变化」的真正根因）。此处只检测 + 告警 + 记 M9_degraded，**不做历史全量回填**。
    """
    missing: list[str] = []
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        today = date.today()
        want = [(today - timedelta(days=i)).isoformat() for i in range(1, days + 1)]
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT snap_date FROM biz.market_overview_snapshot WHERE snap_date >= %s",
                    ((today - timedelta(days=days)).isoformat(),),
                )
                have = {str(r[0]) for r in cur.fetchall()}
        missing = [d for d in want if d not in have]
        if missing:
            print(f"[snapshot_gap] ⚠️ 最近 {days} 天缺 {len(missing)} 天：{', '.join(missing)}")
        else:
            print(f"[snapshot_gap] ✓ 最近 {days} 天快照连续")
    except Exception as e:
        print(f"[snapshot_gap] ⚠️ 检测失败: {e}")
    return missing


# W-12：变更日志需追踪的关键指标 → 越阈阈值（> 上阈 或 < 下阈 即记「越阈」）
_M0_DELTA_METRIC_THRESHOLDS = {"fear_greed": [25, 75]}


def _extract_delta_view(brief: dict) -> dict:
    """W-12：从 brief 抽取逐日对比视图（机会方向 / 风险标的 / 关键指标值）。"""
    opps: dict = {}
    for o in (brief.get("M8_opportunities") or []) + (brief.get("M8_watchlist") or []):
        o = o or {}
        t = str(o.get("target") or o.get("asset") or "").strip()
        if t and t not in opps:
            opps[t] = {
                "direction": str(o.get("direction") or ""),
                "reason": str(o.get("trigger_logic") or o.get("reason") or "")[:80],
                "score": o.get("conviction_score"),
            }
    risks: dict = {}
    for r in (brief.get("M4_risks") or []):
        t = str((r or {}).get("target") or "").strip()
        if t:
            risks[t] = {"reason": str((r or {}).get("trigger_logic") or "")[:80]}
    m0 = brief.get("M0_tldr") or {}
    metrics: dict = {}
    if m0.get("fear_greed") is not None:
        try:
            metrics["fear_greed"] = float(m0["fear_greed"])
        except (TypeError, ValueError):
            pass
    return {"opps": opps, "risks": risks, "metrics": metrics}


def _build_m0_delta(brief: dict, prev_payload: dict | None) -> dict | None:
    """W-12：与 T-1 早报 diff，返回五类字段齐全的 M0_delta。

    `prev_payload` 为空（无 T-1 行）→ 返回 None，由调用方落 no_baseline 标记。
    「维持」天数从昨日 M0_delta 的 维持/新增 条目续期（缺省 1 天）。
    """
    if not prev_payload:
        return None
    cur = _extract_delta_view(brief)
    prev = _extract_delta_view(prev_payload)

    _keep_days: dict = {}
    _pd = prev_payload.get("M0_delta") or {}
    if isinstance(_pd, dict):
        for _e in (_pd.get("维持") or []) + (_pd.get("新增") or []):
            _e = _e or {}
            _t = str(_e.get("target") or "")
            if _t:
                try:
                    _keep_days[_t] = int(_e.get("days") or 1)
                except (TypeError, ValueError):
                    _keep_days[_t] = 1

    keep, added, flipped = [], [], []
    for t, c in cur["opps"].items():
        p = prev["opps"].get(t)
        if p is None:
            added.append({"target": t, "direction": c["direction"]})
        elif p.get("direction") == c.get("direction"):
            keep.append({"target": t, "direction": c["direction"],
                         "days": _keep_days.get(t, 1) + 1, "reason": c.get("reason") or ""})
        else:
            flipped.append({"target": t, "from": p.get("direction") or "—",
                            "to": c.get("direction") or "—", "reason": c.get("reason") or ""})
    new_risk = [{"target": t} for t in cur["risks"] if t not in prev["risks"]]
    crossed = []
    for m, cv in cur["metrics"].items():
        pv = prev["metrics"].get(m)
        if pv is None:
            continue
        for th in _M0_DELTA_METRIC_THRESHOLDS.get(m, []):
            if (pv < th <= cv) or (pv > th >= cv):
                crossed.append({"metric": m, "prev": pv, "curr": cv, "threshold": th})
    return {"维持": keep, "新增": added, "方向反转": flipped,
            "新增风险": new_risk, "越阈": crossed}


def _read_prev_brief_payload(brief_date: str) -> dict | None:
    """W-12：读 T-1 早报完整 payload；无行 / 失败返回 None（不阻断主流程）。"""
    try:
        import json

        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM biz.daily_brief_snapshot WHERE brief_date = %s",
                    (brief_date,),
                )
                row = cur.fetchone()
        if not row or row[0] is None:
            return None
        p = row[0]
        return p if isinstance(p, dict) else json.loads(p)
    except Exception as e:
        print(f"[brief_delta] 读取 T-1 brief 失败: {e}")
        return None


def _to_num(v):
    """尽力转 float；不可转 → None（不写假数）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    """尽力转 int；不可转 → None。"""
    f = _to_num(v)
    return int(f) if f is not None else None


# ── 非单品信号（W-13-R1）─────────────────────────────────────────────
# 这些 signal_type 的 target 是**聚合口径**（多币 / 板块 / 链 / 叙事情绪），payload 里带的
# asset_id 只是组装时的「代表币」（如「2 币 MVRV 极度高估」→ ADA、「恐贪指数极度贪婪」→ BTC）。
# 若照抄进 opportunity_snapshot，回填会把聚合结论的错误归因到单币，污染按 gate 分组的效果判据。
# 故这些类型一律置 asset_id/ref_price 为 NULL（该行如实记「不可按单币追踪」）。
_NON_ASSET_SIGNAL_TYPES = frozenset({
    "narrative", "chain_inflow", "chain_outflow", "stablecoin_inflow",
    "fng_extreme", "leverage_extreme",
})
_NON_ASSET_SIGNAL_PREFIXES = ("mvrv_", "sector_")


def _is_asset_level_signal(sig) -> bool:
    """该信号是否可归因到单个资产（决定 asset_id/ref_price 是否落库）。"""
    s = str(sig or "").strip().lower()
    if not s or s in _NON_ASSET_SIGNAL_TYPES:
        return False
    return not s.startswith(_NON_ASSET_SIGNAL_PREFIXES)


def _collect_opportunity_rows(brief: dict) -> list[dict]:
    """W-13：从 brief 抽取机会清单行（M8_opportunities + M8_watchlist）。

    两键并集即 `opportunity_list.opportunities` 全量（HIGH 进 M8_opportunities，
    其余进 M8_watchlist）。按 (target, signal_type) 去重——与表主键一致，
    避免同一 INSERT 内二次命中同一行（ON CONFLICT 会报 "cannot affect row a second time"）；
    同键取分数更高者。

    非单品信号（见 `_NON_ASSET_SIGNAL_TYPES`）的 asset_id 强制为 None，避免前向收益归因错位。
    """
    seen: dict = {}
    for o in (brief.get("M8_opportunities") or []) + (brief.get("M8_watchlist") or []):
        o = o or {}
        target = str(o.get("target") or o.get("asset") or "").strip()
        if not target:
            continue
        sig = str(o.get("signal_type") or "")
        cal = o.get("calibration_status") if isinstance(o.get("calibration_status"), dict) else {}
        row = {
            "target": target,
            "signal_type": sig,
            "direction": o.get("direction"),
            "conviction_score": _to_num(o.get("conviction_score")),
            "conviction_tier": o.get("conviction_tier"),
            "calibration_gate": cal.get("gate"),
            "sample_count": _to_int(cal.get("sample_count")),
            "hit_rate": _to_num(cal.get("hit_rate")),
            # 非单品信号（聚合口径）不落 asset_id → 顺带 ref_price 也为空
            "asset_id": _to_int(o.get("asset_id")) if _is_asset_level_signal(sig) else None,
        }
        prev = seen.get((target, sig))
        if prev is None or (row["conviction_score"] or 0) > (prev["conviction_score"] or 0):
            seen[(target, sig)] = row
    return list(seen.values())


def _resolve_ref_prices(asset_ids: list) -> dict:
    """W-13：批量取各 asset_id 的建仓基准价及**该价的真实日期**。

    - 价格口径与 db_stats._fetch_as_of_price 一致（cmc 优先、cmc_historical 次之）：结论多为
      盘中生成，当日 ETL 可能尚未写入该资产日行，取「最近可得」而非严格当日，避免 ref_price
      永久为空。
    - 返回 `{asset_id: (price, price_date)}`；`price_date` 为实际取价对应的 market_date
      （W-13-R2：window 可审计，避免「名义窗口 ≠ 实际窗口」）。
    """
    if not asset_ids:
        return {}
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (asset_id) asset_id, price_usd, market_date
                    FROM biz.asset_market_daily
                    WHERE asset_id = ANY(%s)
                      AND source_code IN ('cmc', 'cmc_historical')
                      AND market_date <= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
                      AND price_usd IS NOT NULL AND price_usd > 0
                    ORDER BY asset_id,
                             market_date DESC,
                             CASE source_code WHEN 'cmc' THEN 0 ELSE 1 END
                    """,
                    (list(asset_ids),),
                )
                return {int(r[0]): (float(r[1]), r[2]) for r in cur.fetchall() if r[1] is not None}
    except Exception as e:
        print(f"[opp_snapshot] 基准价解析失败（ref_price 记 NULL）: {e}")
        return {}


def _save_opportunity_snapshot(brief_date: str, brief: dict) -> int:
    """W-13：把当日机会清单落库 `biz.opportunity_snapshot`（幂等 upsert）。

    - 幂等：`ON CONFLICT (snapshot_date, target, signal_type) DO UPDATE`（同日重跑只覆盖）；
    - ref_price 取不到 → 写 NULL（回填脚本跳过无基准价的行，不写假数）；
    - 落库失败**不阻断**早报主流程。
    """
    rows = _collect_opportunity_rows(brief)
    if not rows:
        print("[opp_snapshot] 无机会条目，跳过")
        return 0
    prices = _resolve_ref_prices(sorted({r["asset_id"] for r in rows if r["asset_id"] is not None}))
    values = [
        (
            brief_date, r["target"], r["signal_type"], r["direction"],
            r["conviction_score"], r["conviction_tier"], r["calibration_gate"],
            r["sample_count"], r["hit_rate"],
            prices[r["asset_id"]][0] if r["asset_id"] in prices else None,
            prices[r["asset_id"]][1] if r["asset_id"] in prices else None,
            r["asset_id"],
        )
        for r in rows
    ]
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO biz.opportunity_snapshot (
                        snapshot_date, target, signal_type, direction, conviction_score,
                        conviction_tier, calibration_gate, sample_count, hit_rate,
                        ref_price, ref_price_date, asset_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (snapshot_date, target, signal_type) DO UPDATE
                      SET direction        = EXCLUDED.direction,
                          conviction_score = EXCLUDED.conviction_score,
                          conviction_tier  = EXCLUDED.conviction_tier,
                          calibration_gate = EXCLUDED.calibration_gate,
                          sample_count     = EXCLUDED.sample_count,
                          hit_rate         = EXCLUDED.hit_rate,
                          ref_price        = EXCLUDED.ref_price,
                          ref_price_date   = EXCLUDED.ref_price_date,
                          asset_id         = EXCLUDED.asset_id,
                          updated_at       = NOW()
                    """,
                    values,
                )
            conn.commit()
        _n_ref = sum(1 for v in values if v[9] is not None)
        print(f"[opp_snapshot] 已落库 {brief_date}：{len(values)} 条（有基准价 {_n_ref} 条）")
        return len(values)
    except Exception as e:
        print(f"[opp_snapshot] 落库失败: {e}")
        return 0


def main() -> dict:
    stale = _check_data_freshness()
    today = get_market_overview(force_refresh="1")  # 快照必须最新，绕过 CACHE_TTL
    y_date = (date.today() - timedelta(days=1)).isoformat()
    yesterday = load_snapshot(y_date)
    brief = generate_morning_brief(today, yesterday)
    if stale:
        brief.setdefault("M9_degraded", []).extend(stale)
    # W-11-1：昨日基准缺失 → DIFF 显式标注 no_baseline（不静默留空，渲染层据此提示）
    if not brief.get("DIFF"):
        brief["DIFF"] = {"status": "no_baseline", "baseline_date": y_date}
        print(f"[brief] 无昨日基准（{y_date} 快照缺失），本期无变化对比")
    # W-11-2：快照缺日检测（最近 7 天）
    _gap = _check_snapshot_gap(7)
    if _gap:
        brief.setdefault("M9_degraded", []).append(
            f"market_overview_snapshot 缺日（最近7天缺 {len(_gap)} 天：{', '.join(_gap)}）")
    # W-12：变更日志（与 T-1 早报 diff）；无 T-1 行 → no_baseline（渲染层输出 W-11 提示，不出空块）
    _prev_payload = _read_prev_brief_payload(y_date)
    _delta = _build_m0_delta(brief, _prev_payload)
    if _delta is None:
        brief["M0_delta"] = {"status": "no_baseline", "baseline_date": y_date}
        print(f"[brief_delta] 无 T-1 brief（{y_date}），变更日志输出「无昨日基准」")
    else:
        brief["M0_delta"] = _delta
        print("[brief_delta] 与 {} 对比：维持{} / 新增{} / 反转{} / 新增风险{} / 越阈{}".format(
            y_date, len(_delta["维持"]), len(_delta["新增"]), len(_delta["方向反转"]),
            len(_delta["新增风险"]), len(_delta["越阈"])))
    save_snapshot(date.today().isoformat(), today)  # 落库供明日 diff
    _save_brief_snapshot(date.today().isoformat(), brief)  # W-06：完整 brief 落库（P2 前置）
    _save_opportunity_snapshot(date.today().isoformat(), brief)  # W-13：机会清单落表（效果追踪）

    print("M0:", brief.get("M0_tldr"))
    print("DIFF:", brief.get("DIFF"))
    print("M0_delta:", brief.get("M0_delta"))
    return brief


if __name__ == "__main__":
    main()
