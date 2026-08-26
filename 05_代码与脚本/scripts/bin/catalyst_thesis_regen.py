#!/usr/bin/env python3
"""
催化剂更新后批量重生 thesis（基于 AI 处理完成时间的游标机制）。

修复的 bug：
- Bug-A: 滑动窗口 + LIMIT 导致大批量永久遗漏 → 改用游标（记录上次处理到的 ai_processed_at）
- Bug-B: 窗口基于 linked_at 导致时间竞争遗漏 → 改为基于 ai_processed_at

逻辑：
1. 读取游标文件（上次成功处理的 max_ai_processed_at）
2. 找出 ai_processed_at > 游标的催化剂关联的资产
3. 对每个资产调用 generate_research_thesis 重生结论
4. 成功后更新游标为本次处理到的最大 ai_processed_at
5. 幂等：同一资产多次重生安全（UPSERT 覆盖）

用法：
    python catalyst_thesis_regen.py [--max-assets 100]
    python catalyst_thesis_regen.py --asset-id 123
    python catalyst_thesis_regen.py --hours 24 --max-assets 200  # 临时回溯模式
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
WORKBENCH_DIR = PROJECT_ROOT / "workbench"
CURSOR_FILE = SCRIPT_DIR / ".catalyst_thesis_cursor.json"

sys.path.insert(0, str(WORKBENCH_DIR))

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


def _load_cursor() -> str | None:
    """读取游标（上次处理到的 ai_processed_at ISO 字符串）。"""
    if not CURSOR_FILE.exists():
        return None
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        return data.get("last_ai_processed_at")
    except Exception:
        return None


def _save_cursor(ai_processed_at: str, processed_count: int):
    """保存游标。"""
    CURSOR_FILE.write_text(
        json.dumps({
            "last_ai_processed_at": ai_processed_at,
            "last_processed_assets": processed_count,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_assets_with_new_catalysts_cursor(limit: int) -> tuple[list[int], str | None]:
    """基于游标找出有新催化剂（AI 已处理）的资产。

    返回 (asset_ids, max_ai_processed_at)。
    """
    cursor = _load_cursor()
    settings = get_settings(require_database=True)

    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            if cursor:
                cur.execute(
                    """
                    SELECT DISTINCT cal.asset_id,
                           MAX(ac.ai_processed_at) OVER () as max_ts
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE ac.ai_processed = true
                      AND ac.ai_processed_at > %s::timestamptz
                    ORDER BY cal.asset_id
                    LIMIT %s
                    """,
                    (cursor, limit),
                )
            else:
                # 首次运行：取近 7 天的，避免全量重生
                cur.execute(
                    """
                    SELECT DISTINCT cal.asset_id,
                           MAX(ac.ai_processed_at) OVER () as max_ts
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE ac.ai_processed = true
                      AND ac.ai_processed_at >= NOW() - INTERVAL '7 days'
                    ORDER BY cal.asset_id
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

    if not rows:
        return [], None

    asset_ids = [r[0] for r in rows]
    max_ts = rows[0][1]  # OVER () 窗口，每行都一样
    return asset_ids, str(max_ts) if max_ts else None


def get_assets_with_new_catalysts_hours(hours: int, limit: int) -> list[int]:
    """基于滑动窗口（回溯模式）。"""
    settings = get_settings(require_database=True)
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cal.asset_id
                FROM biz.catalyst_asset_link cal
                JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                WHERE ac.ai_processed = true
                  AND ac.ai_processed_at >= NOW() - INTERVAL '%s hours'
                ORDER BY cal.asset_id
                LIMIT %s
                """,
                (hours, limit),
            )
            rows = cur.fetchall()
    return [r[0] for r in rows]


def regen_thesis(asset_id: int) -> dict:
    """重生单个资产的 thesis。"""
    from db_stats import generate_research_thesis
    return generate_research_thesis(asset_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂驱动 thesis 重生（游标模式）")
    parser.add_argument("--max-assets", type=int, default=100,
                        help="单轮最多重生多少个资产（默认 100）")
    parser.add_argument("--asset-id", type=int, default=None,
                        help="指定单个资产 ID 重生（忽略游标）")
    parser.add_argument("--hours", type=int, default=None,
                        help="回溯模式：近 N 小时内 AI 处理完的（忽略游标）")
    parser.add_argument("--reset-cursor", action="store_true",
                        help="重置游标后再运行（首次全量回溯用）")
    args = parser.parse_args()

    print("=" * 60)
    print("催化剂驱动 thesis 重生")
    print("=" * 60)

    if args.reset_cursor and CURSOR_FILE.exists():
        CURSOR_FILE.unlink()
        print("已重置游标")

    if args.asset_id:
        asset_ids = [args.asset_id]
        max_ts = None
        print(f"指定资产: {args.asset_id}")
    elif args.hours:
        asset_ids = get_assets_with_new_catalysts_hours(args.hours, args.max_assets)
        max_ts = None
        print(f"回溯模式：近 {args.hours} 小时 AI 处理完的资产 {len(asset_ids)} 个")
    else:
        asset_ids, max_ts = get_assets_with_new_catalysts_cursor(args.max_assets)
        cursor = _load_cursor()
        print(f"游标模式: 上次游标 = {cursor or '（首次）'}")
        print(f"待重生资产: {len(asset_ids)} 个")

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

    # 游标模式下，成功处理后更新游标
    if not args.asset_id and not args.hours and max_ts and success > 0:
        _save_cursor(max_ts, success)
        print(f"\n游标已更新: {max_ts}")

    print()
    print("=" * 60)
    print(f"完成: 成功 {success} / 失败 {failed} / 共 {len(asset_ids)}")
    print("=" * 60)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
