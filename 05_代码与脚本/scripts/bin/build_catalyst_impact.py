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
    import re as _re

    direction, strength, horizon = RULE.get(event_type, DEFAULT_RULE)

    # 1) 优先：catalyst_asset_link（已建立的链接关系）
    cur.execute(
        "SELECT asset_id FROM biz.catalyst_asset_link WHERE catalyst_id = %s",
        (catalyst_id,),
    )
    links = cur.fetchall()

    # 2) 兜底：从 title 提取 token，匹配 core.asset.canonical_symbol
    if not links:
        cur.execute(
            "SELECT title FROM biz.asset_catalyst WHERE catalyst_id = %s",
            (catalyst_id,),
        )
        row = cur.fetchone()
        title = (row[0] or "") if row else ""
        if title:
            # P0-2: token 长度下限 3（过滤 2 位介词/缩写噪声）+ 扩充 skip_words
            tokens = _re.findall(r'\b([A-Z]{3,6})\b', title)
            skip_words = {
                "THE", "FOR", "AND", "NOT", "HAS", "ITS", "ARE", "WAS", "CEO",
                "CFO", "SEC", "FDA", "GDP", "ETF", "IPO", "OTC", "ALL", "NEW",
                "HIP", "API", "USD", "COIN", "BTC", "ETH", "SOL", "BNB",
            }
            tokens = [t for t in tokens if t not in skip_words]
            if tokens:
                # P0-1: 同 symbol 只取 1 个资产（按市值降序取 canonical）
                # 避免 "ETH Staking" 扩散到 15 个 symbol=ETH 的脏资产
                cur.execute(
                    "SELECT DISTINCT ON (UPPER(k.canonical_symbol)) "
                    "  k.asset_id, UPPER(k.canonical_symbol) "
                    "FROM core.asset k "
                    "WHERE UPPER(k.canonical_symbol) = ANY(%s) "
                    "ORDER BY UPPER(k.canonical_symbol), "
                    "         COALESCE(k.market_cap, 0) DESC NULLS LAST",
                    (tokens,),
                )
                matched = cur.fetchall()
                links = [(r[0],) for r in matched]

    # P1-3: delisting 类事件额外尝试混合大小写匹配（覆盖 HyENA/Lakala 等）
    if not links and event_type == "delisting" and title:
        # 提取 3-8 位混合大小写 token（非全大写，也非全小写）
        mixed_tokens = _re.findall(r'\b([A-Za-z]{3,8})\b', title)
        # 过滤纯小写（介词）和 skip_words
        mixed_tokens = [
            t.upper() for t in mixed_tokens
            if t.upper() not in skip_words
            and t != t.lower()  # 排除全小写
            and t != t.upper()  # 排除全大写（已处理过）
            and not t[0].isdigit()  # 排除数字开头
        ]
        if mixed_tokens:
            cur.execute(
                "SELECT DISTINCT ON (UPPER(k.canonical_symbol)) "
                "  k.asset_id, UPPER(k.canonical_symbol) "
                "FROM core.asset k "
                "WHERE UPPER(k.canonical_symbol) = ANY(%s) "
                "ORDER BY UPPER(k.canonical_symbol), "
                "         COALESCE(k.market_cap, 0) DESC NULLS LAST",
                (mixed_tokens,),
            )
            matched = cur.fetchall()
            links = [(r[0],) for r in matched]

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
