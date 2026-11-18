#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 biz.asset_factor_daily 的未来日期行（unlock_event_usd 因子）。

背景（2026-11-18）
------------------
`bin/build_factors.py::build_unlock_factors` 同时写两个因子：

  ① unlock_90d_usd   factor_date = today          ← 日频聚合，正确
  ② unlock_event_usd factor_date = 解锁事件日期 d  ← 事件级数据，错误

②把「未来 90 天内的解锁事件日期」当作因子观测日写进日频因子表 →
  产生未来日期行。实测：
    · unlock_event_usd 共 334 行，其中 207 行 factor_date > today
    · 日期范围 2026-11-19 ~ 2027-01-12，涉及 121 个资产
    · 全表仅此一个因子有未来日期（其余 factor 最新为 2026-10-13/14）

为什么这是硬伤（不只是"数据好看"）
------------------------------------
本表索引为 (asset_id, factor_date DESC) 与 (factor_code, factor_date DESC)，
即专为「取最新因子」设计的 DESC 排序。任何
    ORDER BY factor_date DESC LIMIT 1
的查询都会优先命中 2027-01-12 那行 → 下游因子面板取到未来值。

冗余性
------
事件明细已在 biz.asset_unlock_event（4126 行，含 unlock_date / unlock_value_usd）
存在且为权威源；因子表这份是冗余副本，且值与事件表不一致
（因子值是 build_factors 运行当时的快照，事件表后来被更新）。

下游影响面（已核实）
--------------------
· 代码引用 unlock_event_usd：仅 build_factors.py 自己（写入侧）
· API 层 / 前端：0 处消费 biz.asset_factor_daily
· tmp_grass_persist.py（一次性未跟踪脚本）：只读打印，不影响业务
→ 删除无下游依赖

处置
----
1) 代码侧：已删除 build_factors.py 中的事件级写入（停止继续产生）
2) 数据侧：本脚本删除 unlock_event_usd 全部 334 行

安全设计
--------
· 默认干跑（不加 --apply 不写库）
· 删除前备份到 biz.asset_factor_daily_repair_backup（可 --rollback 还原）
· 幂等：已删净则跳过
· 写后断言校验

用法
----
    "C:/python/python311/python.exe" -u bin/repair_factor_future_dates.py            # 干跑
    "C:/python/python311/python.exe" -u bin/repair_factor_future_dates.py --apply    # 执行
    "C:/python/python311/python.exe" -u bin/repair_factor_future_dates.py --rollback # 回滚
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

TARGET_CODE = "unlock_event_usd"
BACKUP_TABLE = "biz.asset_factor_daily_repair_backup"
REPAIR_TAG = "unlock_event_usd_purge_20261118"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行删除（默认干跑）")
    ap.add_argument("--rollback", action="store_true", help="从备份表还原")
    args = ap.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    s = get_settings(require_database=True)
    with get_connection(s.database_url) as conn:
        return _run(conn, args)


def _run(conn, args) -> int:
    print("=" * 76)
    print("修复 biz.asset_factor_daily 未来日期行（unlock_event_usd）")
    print("=" * 76)

    st = report(conn)
    print("\n[盘点] 全表未来日期行按因子分布：")
    if not st["future_by_code"]:
        print("    （无未来日期行）")
    for code, n, assets, mn, mx in st["future_by_code"]:
        print(f"    {code:<24s} n={n:<5d} 资产={assets:<5d} {mn} ~ {mx}")

    t_n, t_assets, t_min, t_max, t_future = st["target"]
    print(f"\n[目标] factor_code={TARGET_CODE}")
    print(f"    总行数 {t_n}｜涉及资产 {t_assets}｜日期 {t_min} ~ {t_max}｜其中未来行 {t_future}")
    print(f"    权威事件源 biz.asset_unlock_event 有效行数：{st['event_rows']}")

    if args.rollback:
        n = do_rollback(conn)
        conn.commit()
        print(f"\n[回滚] 已还原 {n} 行")
        left, future = verify(conn)
        print(f"[校验] 目标因子剩余 {left} 行｜全表未来日期 {future} 行")
        return 0

    if not args.apply:
        print("\n[干跑] 未做任何写入。确认无误后加 --apply 执行。")
        print(f"       将删除 {t_n} 行（其中 {t_future} 行未来日期），"
              f"涉及 {t_assets} 个资产。")
        print("       删除前自动备份到 biz.asset_factor_daily_repair_backup。")
        return 0

    if t_n == 0:
        print("\n[跳过] 目标因子已无行，无需处理（幂等）。")
        left, future = verify(conn)
        print(f"[校验] 全表未来日期 {future} 行")
        return 0

    bk = do_backup(conn)
    conn.commit()
    print(f"\n[备份] 已写入 {BACKUP_TABLE}，repair_tag={REPAIR_TAG}（{bk} 行）")
    n = do_purge(conn)
    conn.commit()
    print(f"[删除] 已删除 {n} 行 factor_code={TARGET_CODE}")

    left, future = verify(conn)
    print(f"[校验] 目标因子剩余 {left} 行｜全表未来日期 {future} 行")
    assert left == 0, "目标因子未删净"
    assert future == 0, f"全表仍有 {future} 行未来日期"
    print("[校验] ✅ 全部断言通过")
    print("[回滚] 如需还原：python bin/repair_factor_future_dates.py --rollback")
    return 0


def report(conn) -> dict:
    """只读盘点：未来日期行 + 目标因子行。"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT factor_code, count(*) n, count(DISTINCT asset_id) assets,
                      min(factor_date), max(factor_date)
               FROM biz.asset_factor_daily
               WHERE factor_date > CURRENT_DATE
               GROUP BY 1 ORDER BY 2 DESC""")
        out["future_by_code"] = cur.fetchall()

        cur.execute(
            """SELECT count(*), count(DISTINCT asset_id), min(factor_date), max(factor_date),
                      count(*) FILTER (WHERE factor_date > CURRENT_DATE)
               FROM biz.asset_factor_daily WHERE factor_code=%s""", (TARGET_CODE,))
        out["target"] = cur.fetchone()

        cur.execute(
            """SELECT count(*) FROM biz.asset_unlock_event
               WHERE unlock_value_usd IS NOT NULL AND unlock_value_usd > 0""")
        out["event_rows"] = cur.fetchone()[0]
    return out


def do_backup(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
                backup_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                repair_tag TEXT NOT NULL,
                payload JSONB NOT NULL)
        """)
        cur.execute(
            f"""INSERT INTO {BACKUP_TABLE} (repair_tag, payload)
                SELECT %s, to_jsonb(f) FROM biz.asset_factor_daily f
                WHERE f.factor_code=%s
                ON CONFLICT DO NOTHING""",
            (REPAIR_TAG, TARGET_CODE))
        cur.execute(f"SELECT count(*) FROM {BACKUP_TABLE} WHERE repair_tag=%s", (REPAIR_TAG,))
        return cur.fetchone()[0]


def do_purge(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM biz.asset_factor_daily WHERE factor_code=%s", (TARGET_CODE,))
        return cur.rowcount


def do_rollback(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO biz.asset_factor_daily
                SELECT (payload->>'asset_id')::bigint,
                       (payload->>'factor_date')::date,
                       payload->>'factor_code',
                       (payload->>'factor_value')::numeric,
                       payload->>'factor_source',
                       payload->'metadata',
                       now(), now()
                FROM {BACKUP_TABLE} WHERE repair_tag=%s
                ON CONFLICT (asset_id, factor_date, factor_code) DO NOTHING""",
            (REPAIR_TAG,))
        n = cur.rowcount
        cur.execute(f"DELETE FROM {BACKUP_TABLE} WHERE repair_tag=%s", (REPAIR_TAG,))
        return n


def verify(conn) -> tuple[int, int]:
    """返回 (目标因子剩余行数, 全表未来日期行数)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM biz.asset_factor_daily WHERE factor_code=%s",
                    (TARGET_CODE,))
        left = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM biz.asset_factor_daily WHERE factor_date > CURRENT_DATE")
        future = cur.fetchone()[0]
    return left, future


if __name__ == "__main__":
    sys.exit(main())
