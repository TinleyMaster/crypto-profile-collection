#!/usr/bin/env python3
"""定向回填：仅补齐 biz.cm_asset_onchain_daily.sply_ex_usd（交易所余额）。

关联：04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md §4.6
  交易所余额/净流走 CM Community 原生源；CoinGlass `/api/exchange/balance/*` 已裁决**不接**。

为什么需要这个独立脚本（而不是跑 backfill_cm_onchain.py --start-date）：
  `backfill_cm_onchain.py` 的 UPSERT 对 **20 个列全部无条件 `= EXCLUDED.*`**，而取数侧是
  「`if cm_key in row` 才赋值，缺失即 None」。⇒ 跑一次跨 15 年的宽回填，等于拿 CM 的返回
  整体覆盖既有数据：任何一行里某个字段缺失，该列现存的好值就会被写成 NULL。
  而 `biz.cm_asset_onchain_daily` 是**多个载入脚本共写**的表 ⇒ 为补一列去赌
  `flow_in_ex_usd` / `flow_out_ex_usd`（btc 已有 5635 天）不被清空，不划算。

本脚本的边界（刻意收窄）：
  - **只写 `sply_ex_usd` 一列**，其余列一律不碰；
  - **只 UPDATE 已有行，绝不 INSERT**（不替其他载入方造行/补列）；
  - 取数侧保证值非空（`fetch_asset_metrics` 仅在 `safe_float` 成功时落键）⇒ 不会用 NULL 覆盖；
  - 幂等：重复跑结果一致。

CM 免费层对 `SplyExUSD` 提供全历史（2026-09-29 实测：btc 2021-01-01 = $80.95B、
2025-01-01 = $277.88B 均正常返回），故库内仅 29 天属**采集缺口**，可回填。

⚠️ 口径提醒：CM 返回带 `SplyExUSD-status: "flash"` ⇒ 该字段会被 CM **事后修订**。
    适合做参考基准/展示，不适合做要求严格可复现的判定输入。

用法：
    python backfill_cm_exchange_balance.py --dry-run            # 只预览（默认仅 btc）
    python backfill_cm_exchange_balance.py --coins btc,eth      # 实跑
    python backfill_cm_exchange_balance.py --start-date 2011-01-01
    python backfill_cm_exchange_balance.py --json               # 机器可读

退出码：0 = 完成；1 = 资产未映射/无数据；2 = 参数或连接错误
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
# 复用 backfill_cm_onchain 的取数真源（CM_API_BASE / api_get / fetch_asset_metrics 窗口逻辑），
# 避免第二套分窗与重试实现。该模块 main() 有 __main__ 守卫，可安全导入。
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

from backfill_cm_onchain import (  # noqa: E402
    SELECT_ASSET_ID_SQL,
    fetch_asset_metrics,
)

# 本次只回填这一个 CM 指标（CM 键 → DB 列）
METRIC_MAP = {"SplyExUSD": "sply_ex_usd"}
DB_COL = "sply_ex_usd"

DEFAULT_COINS = ["btc"]

# 只查已有行（不 INSERT）
SELECT_EXISTING_SQL = """
SELECT metric_date, sply_ex_usd
FROM biz.cm_asset_onchain_daily
WHERE asset_id = %s AND metric_date = ANY(%s)
"""

SELECT_SPAN_SQL = """
SELECT min(metric_date), max(metric_date)
FROM biz.cm_asset_onchain_daily
WHERE cm_symbol = %s
"""

# 单列 UPDATE：WHERE 同时钉住 asset_id + metric_date ⇒ 最多命中一行，不会误伤其他列
UPDATE_SQL = """
UPDATE biz.cm_asset_onchain_daily
SET sply_ex_usd = %s
WHERE asset_id = %s AND metric_date = %s
"""


def resolve_asset_id(conn, cm_symbol: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(SELECT_ASSET_ID_SQL, (cm_symbol,))
        row = cur.fetchone()
    return int(row[0]) if row else None


def coin_span(conn, cm_symbol: str) -> tuple[date | None, date | None]:
    with conn.cursor() as cur:
        cur.execute(SELECT_SPAN_SQL, (cm_symbol,))
        row = cur.fetchone()
    if not row or row[0] is None:
        return None, None
    return row[0], row[1]


def fetch_sply(symbol: str, start: date, end: date) -> dict[date, float]:
    """取 CM 的 SplyExUSD → {metric_date: value}（只保留非空值）。"""
    raw = fetch_asset_metrics(symbol, list(METRIC_MAP.keys()), start, end, METRIC_MAP)
    out: dict[date, float] = {}
    for day_str, vals in raw.items():
        val = vals.get(DB_COL)
        if val is None:  # 防御：绝不让 NULL 进入 UPDATE
            continue
        try:
            out[date.fromisoformat(day_str)] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def write_coin(conn, item: dict, dry_run: bool) -> dict:
    """把已取到的 CM 值单列写库（或 dry-run 只统计）。

    注意：本函数**不负责取数**。CM 取数要跨 15 年分窗、耗时分钟级，若在此期间持有 DB
    连接，空闲连接会被服务端/中间件掐断（实测 `consuming input failed: server closed
    the connection unexpectedly`）⇒ 取数必须在无连接状态下完成。
    """
    symbol = item["symbol"]
    values: dict[date, float] = item.get("values") or {}
    stats: dict = {
        "symbol": symbol,
        "start": item["start"].isoformat() if item["start"] else None,
        "end": item["end"].isoformat(),
        "asset_id": item["asset_id"], "fetched": len(values), "matched_rows": 0,
        "updated": 0, "filled_from_null": 0, "no_row": 0, "error": None,
    }
    if not values:
        return stats

    days = sorted(values)
    asset_id = item["asset_id"]

    # 已有行 + 现值：先摸清「哪些日期有行」「哪些行当前为空」⇒ 可精确报出补了多少
    with conn.cursor() as cur:
        cur.execute(SELECT_EXISTING_SQL, (asset_id, days))
        existing = {r[0]: r[1] for r in cur.fetchall()}

    stats["matched_rows"] = len(existing)
    stats["no_row"] = len(values) - len(existing)
    stats["filled_from_null"] = sum(
        1 for d, v in existing.items() if v is None and d in values
    )

    if dry_run:
        return stats

    with conn.cursor() as cur:
        cur.executemany(
            UPDATE_SQL,
            [(values[d], asset_id, d) for d in days if d in existing],
        )
        stats["updated"] = cur.rowcount if cur.rowcount >= 0 else len(existing)
    conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description="定向回填 biz.cm_asset_onchain_daily.sply_ex_usd（交易所余额，只写单列）"
    )
    ap.add_argument("--coins", default="",
                    help=f"逗号分隔币种（CM 符号，默认 {'/'.join(DEFAULT_COINS)}）")
    ap.add_argument("--start-date", default=None,
                    help="起始日 YYYY-MM-DD（默认取库内该币最早行）")
    ap.add_argument("--end-date", default=None,
                    help="终止日 YYYY-MM-DD（默认 T-1）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    coins = [c.strip().lower() for c in args.coins.split(",") if c.strip()] or DEFAULT_COINS
    explicit_start = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else date.today() - timedelta(days=1)

    settings = get_settings(require_database=True)

    results: list[dict] = []

    # ── 阶段 1：短连接定计划（asset_id + 起点）后立即释放 ──
    plan: list[dict] = []
    with get_connection(settings.database_url) as conn:
        for symbol in coins:
            asset_id = resolve_asset_id(conn, symbol)
            db_min, _db_max = coin_span(conn, symbol)
            plan.append({"symbol": symbol, "asset_id": asset_id,
                         "start": explicit_start or db_min, "end": end_date})

    # 前置校验（不产生连接）
    todo: list[dict] = []
    for item in plan:
        if item["asset_id"] is None:
            results.append({"symbol": item["symbol"],
                            "error": "core.asset_source_map 未映射该 cm 符号",
                            "fetched": 0, "updated": 0})
        elif item["start"] is None:
            results.append({"symbol": item["symbol"],
                            "error": "库内无该币任何行，无法定位回填起点",
                            "fetched": 0, "updated": 0})
        elif item["start"] > end_date:
            results.append({"symbol": item["symbol"],
                            "error": f"起始 {item['start']} > 终止 {end_date}",
                            "fetched": 0, "updated": 0})
        else:
            todo.append(item)

    # ── 阶段 2：CM 取数（**不持有 DB 连接**，避免长 fetch 期间空闲连接被掐断）──
    for item in todo:
        if not args.json:
            print(f"[{item['symbol'].upper()}] CM 取数 {item['start']} ~ {end_date}"
                  f"{'（DRY RUN）' if args.dry_run else ''}", flush=True)
        item["values"] = fetch_sply(item["symbol"], item["start"], end_date)
        if not args.json:
            print(f"  取值 {len(item['values'])} 天", flush=True)

    # ── 阶段 3：新连接写库 ──
    with get_connection(settings.database_url) as conn:
        for item in todo:
            results.append(write_coin(conn, item, args.dry_run))

    if args.json:
        print(json.dumps({
            "dry_run": args.dry_run, "col": DB_COL, "results": results,
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n" + "=" * 66)
        print(f"定向回填完成（列：{DB_COL}｜{'DRY RUN，未写库' if args.dry_run else '已写库'}）")
        print("=" * 66)
        for r in results:
            if r.get("error"):
                print(f"  {r['symbol'].upper()}: ❌ {r['error']}")
                continue
            print(f"  {r['symbol'].upper()}: CM 取值 {r['fetched']} 天 | "
                  f"库内命中 {r['matched_rows']} 行 | 更新 {r['updated']} | "
                  f"由空补上 {r['filled_from_null']} | 无对应行 {r['no_row']}")
        if any(r.get("no_row") for r in results):
            print("\n  ⚠️ 「无对应行」的日期未写入：本脚本只 UPDATE、不 INSERT，"
                  "避免替其他载入方造行。如需覆盖，请先确认这些日期该不该有行。")

    errors = [r for r in results if r.get("error")]
    return 1 if errors and all(r.get("error") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())