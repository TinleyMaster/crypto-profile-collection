#!/usr/bin/env python3
"""
催化剂更新后批量重生 thesis（仅重生有新催化剂关联的资产）。

逻辑：
1. 找出近 N 小时内有新催化剂关联的资产
2. 对每个资产调用 generate_research_thesis 重生结论
3. 确保 catalysts_json 含真实 catalyst_id

用法：
    python catalyst_thesis_regen.py [--hours 24] [--max-assets 50]
    python catalyst_thesis_regen.py --asset-id 123
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
WORKBENCH_DIR = PROJECT_ROOT / "workbench"

sys.path.insert(0, str(WORKBENCH_DIR))

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


def get_assets_with_new_catalysts(hours: int, limit: int) -> list[int]:
    """找出近 N 小时内有新催化剂关联的资产 ID。"""
    settings = get_settings(require_database=True)
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cal.asset_id
                FROM biz.catalyst_asset_link cal
                JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                WHERE cal.linked_at >= NOW() - INTERVAL '%s hours'
                  AND ac.ai_processed = true
                ORDER BY cal.asset_id
                LIMIT %s
                """,
                (hours, limit),
            )
            rows = cur.fetchall()
    return [r[0] for r in rows]


def regen_thesis(asset_id: int) -> dict:
    """重生单个资产的 thesis。"""
    # 延迟导入，避免无 LLM 时启动失败
    from db_stats import generate_research_thesis
    return generate_research_thesis(asset_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂更新后批量重生 thesis")
    parser.add_argument("--hours", type=int, default=24,
                        help="查找近 N 小时内有新催化剂的资产（默认 24）")
    parser.add_argument("--max-assets", type=int, default=50,
                        help="最多重生多少个资产（默认 50）")
    parser.add_argument("--asset-id", type=int, default=None,
                        help="指定单个资产 ID 重生（忽略 --hours）")
    args = parser.parse_args()

    print("=" * 60)
    print("催化剂驱动 thesis 重生")
    print("=" * 60)

    if args.asset_id:
        asset_ids = [args.asset_id]
        print(f"指定资产: {args.asset_id}")
    else:
        asset_ids = get_assets_with_new_catalysts(args.hours, args.max_assets)
        print(f"近 {args.hours} 小时有新催化剂的资产: {len(asset_ids)} 个")
        if not asset_ids:
            print("无需重生")
            return 0

    success = 0
    failed = 0

    for i, aid in enumerate(asset_ids, 1):
        print(f"\n[{i}/{len(asset_ids)}] 重生 asset_id={aid} ...")
        try:
            result = regen_thesis(aid)
            if result.get("ok"):
                print(f"  ✅ 成功 (stance={result.get('stance', '?')})")
                success += 1
            else:
                print(f"  ❌ 失败: {result.get('error', 'unknown')}")
                failed += 1
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"完成: 成功 {success} / 失败 {failed} / 共 {len(asset_ids)}")
    print("=" * 60)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
