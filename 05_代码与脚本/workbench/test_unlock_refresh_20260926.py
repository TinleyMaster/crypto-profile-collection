#!/usr/bin/env python3
"""复验 #5 处置（陈旧刷新路径）· 离线回归护栏。

来源：核验_一键投研页P0修复_2cc1e55_2026-09-26.md 的 #5
     （data_freshness.unlock.age_hours=533.2，要求 <168）
运行：python workbench/test_unlock_refresh_20260926.py（纯离线，不连库、不连网）

根因：phase_chain_token_unlocks_batch 的候选查询用 _PENDING_EXCLUDE 永久排除
      crawl_status='ok'，已抓过的资产再无刷新路径（PONS/11114 停在 09-04）。

覆盖：
  ① 候选查询并入「ok 且 updated_at 超 refresh_days 天」的刷新组（含参数装配）
  ② refresh_days=0 关闭该路径（回到旧行为，SQL 中不出现刷新分支）
  ③ 刷新组沿用主查询门槛（active / 非稳定币 / 非 meme / 市值门槛）
  ④ 排序：新候选优先；刷新组按 updated_at 升序（最旧优先，防长尾饿死）
  ⑤ save_to_db 降级护栏：原行 ok 时只接受仍为 ok 的写入（防刷新抹掉已有时间表）
  ⑥ 调度默认门槛 DEFAULT_REFRESH_DAYS 明显小于稳态周期 N/B
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
_BIN = os.path.join(_SCRIPTS, "bin")
for p in (_BIN, os.path.join(_SCRIPTS, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import phase_chain_token_unlocks_batch as B  # noqa: E402

passed = failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"    {detail}")


class _Cur:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return []

    def fetchone(self):
        return (0, 0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.cur = _Cur()

    def cursor(self, **_kw):
        return self.cur


def _sql_of(fn, refresh_days, limit=10):
    conn = _Conn()
    fn(conn, limit, refresh_days) if fn is B.get_pending_assets else fn(conn, refresh_days)
    return conn.cur.sql, conn.cur.params


_MIN = B.MIN_UNLOCK_MCAP

print("[① 刷新组并入 + 参数装配]")
sql5, p5 = _sql_of(B.get_pending_assets, 5, 10)
check("crawl_status = 'ok'" in sql5, "刷新组要求 crawl_status='ok'")
check("INTERVAL '1 day'" in sql5 and "u.updated_at < NOW() -" in sql5,
      "刷新组按 updated_at 超 N 天筛选")
check(p5 == (_MIN, 5, _MIN, 10), f"参数装配=市值门槛,刷新天数,市值门槛,limit（实得 {p5}）")
check("UNION ALL" in sql5, "刷新组以 UNION ALL 并入候选池")

print("[② refresh_days=0 关闭（旧行为）]")
sql0, p0 = _sql_of(B.get_pending_assets, 0, 10)
# 注意：主查询自身的 _PENDING_EXCLUDE 里本就含 u.crawl_status = 'ok'（NOT EXISTS 反查），
# 故不能用该串判「是否关闭」；改判刷新组专属特征（时间窗比较与 UNION）。
check("u.updated_at < NOW() -" not in sql0, "关闭后无刷新组的时间窗条件")
check("UNION ALL" not in sql0, "关闭后无 UNION")
check(p0 == (_MIN, 10), f"关闭后参数=市值门槛,limit（实得 {p0}）")
check(B.DEFAULT_REFRESH_DAYS > 0, "默认开启陈旧刷新")

print("[③ 刷新组沿用主查询门槛]")
_ref = B._REFRESH_SELECT
for cond, name in (
    ("a.status = 'active'", "active"),
    ("a.asset_type != 'stablecoin'", "非稳定币"),
    ("a.primary_sector != 'meme'", "非 meme"),
    ("COALESCE(a.market_cap, 0) >= %s", "市值门槛"),
    ("source_asset_key IS NOT NULL", "需有 CG 映射"),
):
    check(cond in _ref, f"刷新组含门槛：{name}")

print("[④ 排序：新候选优先 + 刷新组最旧优先]")
check(re.search(r"ORDER BY\s+is_refresh ASC", sql5) is not None, "新候选（is_refresh=0）排前")
check("COALESCE(last_updated, 'epoch'::timestamptz) ASC" in sql5,
      "刷新组按 updated_at 升序（最旧优先，防按市值排序饿死长尾）")
check("mcap DESC" in sql5, "新候选组仍按市值降序（行为不变）")
check("is_refresh, last_updated" in sql5, "is_refresh 作为排序键随候选行返回")

print("[⑤ save_to_db 降级护栏]")
_TU_SRC = open(os.path.join(_BIN, "phase_chain_token_unlocks.py"), encoding="utf-8").read()
_BATCH_SRC = open(os.path.join(_BIN, "phase_chain_token_unlocks_batch.py"), encoding="utf-8").read()
_save = _TU_SRC.split("def save_to_db(")[1].split("\ndef ")[0]
check("ON CONFLICT (asset_id) DO UPDATE" in _save, "仍走 ON CONFLICT 幂等更新")
check("WHERE biz.asset_token_unlocks.crawl_status IS DISTINCT FROM 'ok'" in _save
      and "EXCLUDED.crawl_status = 'ok'" in _save,
      "仅「原行非 ok」或「本次仍 ok」才覆盖（防刷新抹掉已有时间表）")

print("[⑥ 默认门槛与稳态时效]")
# 稳态刷新周期 T = max(D, N/B)：到龄(D)才进队列，队列积压时为 N/B，否则为 D。
# 实测可通过门槛的 ok 行 N=269，调度日预算 B=100 ⇒ N/B≈2.7 天≈65h。
# 复验要求 T < 168h ⇒ D 与 N/B 都必须 < 7 天。
_N_OBS, _BUDGET = 269, 100
_T_CYCLE_DAYS = max(B.DEFAULT_REFRESH_DAYS, _N_OBS / _BUDGET)
check(_T_CYCLE_DAYS * 24 < 168,
      f"稳态时效 {_T_CYCLE_DAYS*24:.0f}h < 168h"
      f"（max(门槛 {B.DEFAULT_REFRESH_DAYS} 天, N/B {_N_OBS/_BUDGET:.2f} 天)）")
check(B.DEFAULT_REFRESH_DAYS < 7,
      f"默认门槛 {B.DEFAULT_REFRESH_DAYS} 天 < 7 天（D=7 会恰好卡在 168h 边界）")
check("--refresh-days" in _BATCH_SRC and "args.refresh_days" in _BATCH_SRC,
      "--refresh-days 已注册并接入主流程")
_SCHED = open(os.path.join(_HERE, "scheduler.py"), encoding="utf-8").read()
check("token_unlocks_batch" in _SCHED, "调度条目存在（默认门槛自动生效）")

print("\n汇总: PASS=%d FAIL=%d" % (passed, failed))
sys.exit(1 if failed else 0)