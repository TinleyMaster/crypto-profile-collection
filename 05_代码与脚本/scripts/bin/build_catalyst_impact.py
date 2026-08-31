#!/usr/bin/env python3
"""催化剂事件因子化：规则推导 event_type → 定向市场影响（零 LLM）。

P0-A: 把 ai_sentiment 转成对具体资产的定向影响（direction + strength + horizon）。

用法：
    python build_catalyst_impact.py --backfill          # 一次性回填所有未推导的 catalyst
    python build_catalyst_impact.py --incremental       # 增量：仅处理新 catalyst
    python build_catalyst_impact.py --catalyst-id 123   # 单条推导
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
_project = SCRIPT_DIR.parent / "src"
if str(_project) not in sys.path:
    sys.path.insert(0, str(_project))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# event_type → (direction, strength, horizon_days)
RULE = {
    "listing":      ("bullish", "strong", 7),
    "delisting":    ("bearish", "strong", 0),
    "burn":         ("bullish", "strong", 30),
    "partnership":  ("bullish", "medium", 14),
    "tech_upgrade": ("bullish", "medium", 14),
    "funding":      ("bullish", "medium", 14),
    "regulation":   ("neutral", "strong", 0),
    "market_update":("neutral", "weak", 0),
}
DEFAULT_RULE = ("neutral", "weak", 0)


def _load_sql(relative: str) -> str:
    sql_dir = SCRIPT_DIR.parent / "sql"
    return (sql_dir / relative).read_text(encoding="utf-8")


def build_for_catalyst(cur, catalyst_id: int, event_type: str) -> int:
    """为单条 catalyst 推导 impact 并 upsert，返回写入行数。"""
    direction, strength, horizon = RULE.get(event_type, DEFAULT_RULE)

    # 查找该 catalyst 关联的所有资产
    cur.execute(
        "SELECT asset_id FROM biz.catalyst_asset_link WHERE catalyst_id = %s",
        (catalyst_id,),
    )
    links = cur.fetchall()
    if not links:
        return 0

    sql = _load_sql("biz/upsert_catalyst_impact.sql")
    params = [
        (catalyst_id, link[0], direction, strength, horizon, "rule")
        for link in links
    ]
    cur.executemany(sql, params)
    return len(params)


def backfill_all(cur) -> int:
    """回填所有已 AI 处理但未推导的 catalyst。"""
    cur.execute("""
        SELECT ac.catalyst_id, ac.ai_event_type
        FROM biz.asset_catalyst ac
        WHERE ac.ai_processed = true
          AND ac.ai_event_type IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM biz.catalyst_impact ci
              WHERE ci.catalyst_id = ac.catalyst_id
          )
    """)
    rows = cur.fetchall()
    total = 0
    for cat_id, event_type in rows:
        total += build_for_catalyst(cur, cat_id, event_type)
    return total


def incremental(cur) -> int:
    """增量：仅处理新 catalyst（与 backfill 逻辑相同，因 backfill 已排除已推导的）。"""
    return backfill_all(cur)


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂事件因子化（规则推导）")
    parser.add_argument("--backfill", action="store_true", help="一次性回填所有未推导的 catalyst")
    parser.add_argument("--incremental", action="store_true", help="增量：仅处理新 catalyst")
    parser.add_argument("--catalyst-id", type=int, help="单条 catalyst ID 推导")
    args = parser.parse_args()

    if not args.backfill and not args.incremental and not args.catalyst_id:
        parser.print_help()
        return 1

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            if args.catalyst_id:
                cur.execute(
                    "SELECT ai_event_type FROM biz.asset_catalyst WHERE catalyst_id = %s",
                    (args.catalyst_id,),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    print(f"catalyst {args.catalyst_id}: not found or no event_type")
                    return 1
                n = build_for_catalyst(cur, args.catalyst_id, row[0])
                conn.commit()
                print(f"catalyst {args.catalyst_id} ({row[0]}): {n} impacts upserted")
                return 0

            if args.backfill:
                n = backfill_all(cur)
                conn.commit()
                print(f"backfill done: {n} impacts upserted")
                return 0

            if args.incremental:
                n = incremental(cur)
                conn.commit()
                print(f"incremental done: {n} impacts upserted")
                return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
