#!/usr/bin/env python3
"""投研结论前向跟踪（P4）离线回归护栏。

方案：04_架构与代码方案/投研页三档确定性可落地方案_2026-09-27.md §1.3 / §5
运行：python workbench/test_thesis_forward_track_20260927.py（纯离线，不连库、不连网）

覆盖：
  1  建表 DDL 双份齐备（迁移 SQL + _ensure_research_tables 幂等建表），且 DDL 后立即 commit
  2  唯一键 (asset_id, as_of) + 到期扫描 partial index 存在
  3  _thesis_forward_payload 从三档结果正确抽取字段（含 L 档不可评估 → False）
  4  _forward_return_pct 计算正确，且缺失/非正价一律返回 None（不写假数）
  5  生成侧写入钩子：按 (asset_id, as_of) upsert，且不回退已填 ret_*
  6  回填侧：到期闸门（as_of+N）、已填不重算、price_at 空跳过、三期满→filled_at
  7  脚本 + 调度注册齐备（每日任务，07:50）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import db_stats as ds  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f"  → {detail}" if detail else ""))


_DB_SRC = open(os.path.join(_HERE, "db_stats.py"), encoding="utf-8").read()
_SCHED_SRC = open(os.path.join(_HERE, "scheduler.py"), encoding="utf-8").read()
_MIG_PATH = os.path.join(_HERE, "..", "scripts", "migrations",
                         "fix_072_thesis_forward_track.sql")
_MIG_SRC = open(_MIG_PATH, encoding="utf-8").read() if os.path.exists(_MIG_PATH) else ""
_BIN_PATH = os.path.join(_HERE, "..", "scripts", "bin",
                         "backfill_thesis_forward_track.py")


def _src_of(func_name):
    """取函数源码片段（从 def 行到下一个顶层 def），用于源码级断言。"""
    marker = f"\ndef {func_name}("
    i = _DB_SRC.find(marker)
    if i < 0:
        return ""
    j = _DB_SRC.find("\ndef ", i + len(marker))
    return _DB_SRC[i:j if j > 0 else len(_DB_SRC)]


# ── 1/2 建表 DDL 与索引 ──
print("\n[1/2] 建表 DDL 与索引")
check("CREATE TABLE IF NOT EXISTS biz.thesis_forward_track" in _MIG_SRC,
      "迁移 SQL 含建表语句")
check("CREATE TABLE IF NOT EXISTS biz.thesis_forward_track" in _DB_SRC,
      "_ensure_research_tables 含幂等建表（容器内自愈）")
check("CONSTRAINT uq_thesis_forward UNIQUE (asset_id, as_of)" in _MIG_SRC
      and "CONSTRAINT uq_thesis_forward UNIQUE (asset_id, as_of)" in _DB_SRC,
      "唯一键 (asset_id, as_of) 双份一致")
check("idx_thesis_forward_due" in _MIG_SRC and "idx_thesis_forward_due" in _DB_SRC,
      "到期扫描 partial index 存在（filled_at IS NULL）")
check("WHERE filled_at IS NULL" in _MIG_SRC, "index 为 partial（只覆盖未完成行）")
# DDL 后立即 commit（项目教训：AccessExclusiveLock 阻塞表读）
_ensure_src = _src_of("_ensure_research_tables")
check("conn.commit()" in _ensure_src, "_ensure_research_tables 在 DDL 后立即 commit")
check(_ensure_src.rstrip().endswith("conn.commit()"),
      "commit 位于函数末尾（DDL 块之后）")
for col in ("tier_s_score", "tier_m_score", "tier_l_evaluable", "gate_s_open",
            "gate_m_open", "price_at", "ret_t7_pct", "ret_t30_pct", "ret_t90_pct",
            "filled_at", "thesis_id", "as_of"):
    check(col in _MIG_SRC, f"迁移含列 {col}")

# ── 3 字段抽取 ──
print("\n[3] _thesis_forward_payload 字段抽取")
_p = ds._thesis_forward_payload({
    "s": {"score": 92.0, "gate": {"open": True}},
    "m": {"score": 32.5, "gate": {"open": False}},
    "l": {"score": None, "tier": "not_evaluable"},
})
check(_p["tier_s_score"] == 92.0 and _p["tier_m_score"] == 32.5,
      "s/m 档分数抽取正确", str(_p))
check(_p["tier_l_evaluable"] is False, "L 档 not_evaluable → tier_l_evaluable=False")
check(_p["gate_s_open"] is True and _p["gate_m_open"] is False,
      "闸门开合抽取正确")
_p2 = ds._thesis_forward_payload({
    "s": {"score": 60.0, "gate": {"open": False}},
    "m": {"score": 55.0, "gate": {"open": True}},
    "l": {"score": 70.0, "tier": "actionable"},
})
check(_p2["tier_l_evaluable"] is True, "L 档可评估 → tier_l_evaluable=True")
_p3 = ds._thesis_forward_payload(None)
check(all(v is None for v in _p3.values()), "三档缺失 → 全 None（不臆造）", str(_p3))
_p4 = ds._thesis_forward_payload({"s": {"score": "88"}, "m": {"score": None}})
check(_p4["tier_s_score"] == 88.0 and _p4["tier_m_score"] is None,
      "字符串分数可转，None 保持 None")

# 真实三档函数 → payload 打通
_sm = {"derivatives": {"cvd_ratio_24h": 0.068, "oi_change_24h_pct": -3.12,
                       "funding_rate_pct": 0.005},
       "pressure": {"pressure_score": 26.96, "risk_level": "low"},
       "unlock": {"data_available": True, "next_unlock_date": "2027-01-05",
                  "next_unlock_pct": 12.0},
       "data_freshness": {"derivatives": {"age_hours": 2}}}
_th = {"thesis": [{"point": "P", "citations": []}], "dimensions": {}}
_t3 = ds._compute_determinism_3tier(_th, _sm, [])
_pl = ds._thesis_forward_payload(_t3)
check(_pl["tier_s_score"] == _t3["s"]["score"] and _pl["tier_m_score"] == _t3["m"]["score"],
      "与 _compute_determinism_3tier 输出同源（S/M 分数一致）")
check(_pl["tier_l_evaluable"] == (_t3["l"]["tier"] != "not_evaluable"),
      "L 可评估判定与 tier 一致")

# ── 4 前向收益计算 ──
print("\n[4] _forward_return_pct")
check(ds._forward_return_pct(1.0, 1.1) == 10.0, "1.0 → 1.1 = +10%")
check(ds._forward_return_pct(2.0, 1.5) == -25.0, "2.0 → 1.5 = -25%")
check(ds._forward_return_pct("1.0", "0.8") == -20.0, "字符串价格可转")
check(ds._forward_return_pct(0.3333, 1.0) == 200.03, "四舍五入保留 2 位")
for bad in ((None, 1.0), (1.0, None), (0, 1.0), (1.0, 0), (-1.0, 1.0), (1.0, -1.0),
            ("", 1.0), ("x", 1.0)):
    check(ds._forward_return_pct(*bad) is None, f"非法价格 {bad} → None")

# ── 5 写入钩子 ──
print("\n[5] 生成侧写入钩子")
check("INSERT INTO biz.thesis_forward_track" in _DB_SRC, "生成侧含 INSERT")
check("ON CONFLICT (asset_id, as_of) DO UPDATE" in _DB_SRC, "按 (asset_id, as_of) upsert")
_gen_src = _src_of("generate_research_thesis")
check("_thesis_forward_payload" in _gen_src and "_fetch_as_of_price" in _gen_src,
      "钩子消费 payload + 基准价函数")
_fw_src = _DB_SRC[_DB_SRC.find("INSERT INTO biz.thesis_forward_track"):
                  _DB_SRC.find("INSERT INTO biz.thesis_forward_track") + 1400]
check("ret_t7_pct" not in _fw_src.split("ON CONFLICT")[1].split("WHERE")[0],
      "upsert 分支不触碰 ret_*（不回退已填收益）")
check("COALESCE(EXCLUDED.price_at" in _fw_src,
      "price_at 用 COALESCE 保护（写入空值不覆盖已有基准价）")
check("(CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date" in _fw_src,
      "as_of 按北京时间取当日")
# 基准价取「最近可得」而非严格当日，避免永久无法回填
_fetch_src = _src_of("_fetch_as_of_price")
check("market_date <= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date" in _fetch_src,
      "基准价取 as_of 及之前最近收盘价（避免永久空价）")
check("source_code IN ('cmc', 'cmc_historical')" in _fetch_src,
      "基准价按 cmc 优先口径取源")

# ── 6 回填侧 ──
print("\n[6] 回填侧（backfill_thesis_forward_track）")
_bf_src = _src_of("backfill_thesis_forward_track")
check(_bf_src != "", "回填函数存在")
check("WHERE filled_at IS NULL" in _bf_src, "只扫描未完成行")
for days in (7, 30, 90):
    check(f"as_of + {days}" in _bf_src, f"含 T+{days} 到期判定")
check("if not r[f\"due_{days}\"] or r[col] is not None:" in _bf_src
      or 'if not r[f"due_{days}"] or r[col] is not None:' in _bf_src,
      "未到期 或 已填 → 跳过（幂等）")
check("stats[\"skipped_no_price\"] += 1" in _bf_src, "price_at 空 → 跳过并计数")
check("if not price_at:" in _bf_src, "price_at 空提前 continue（不算不写）")
check("filled_at = NOW()" in _bf_src, "三期满 → 置 filled_at")
check("all(v is not None for v in merged.values())" in _bf_src,
      "完成判定要求三期全非空")
check("_THESIS_FORWARD_HORIZONS" in _bf_src, "期次表驱动（7/30/90 单一来源）")
check(ds._THESIS_FORWARD_HORIZONS == ((7, "ret_t7_pct"), (30, "ret_t30_pct"),
                                      (90, "ret_t90_pct")),
      "期次表定义正确")

# ── 7 脚本 + 调度 ──
print("\n[7] 脚本与调度注册")
check(os.path.exists(_BIN_PATH), "回填脚本存在")
check("backfill_thesis_forward_track" in _SCHED_SRC, "调度表含 thesis_forward_backfill")
check('"thesis_forward_backfill", "50 7 * * *"' in _SCHED_SRC,
      "每日 07:50 触发（asset_market_daily 06:15 ETL 之后）")
check("backfill_thesis_forward_track.py" in _SCHED_SRC, "调度脚本名一致")

print("\n" + "=" * 60)
print(f"通过 {passed} / 失败 {failed}")
print("=" * 60)
sys.exit(1 if failed else 0)