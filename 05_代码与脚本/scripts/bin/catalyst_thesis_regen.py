#!/usr/bin/env python3
"""
催化剂更新后批量重生 thesis（DB 存储复合游标，永不遗漏）。

修复的 bug：
- Bug-A v1: 滑动窗口 + LIMIT 永久遗漏 → 改用游标
- Bug-A v2: 游标用 MAX OVER () + 按 asset_id 排序 → LIMIT 截断时确定性漏高 ID 资产
  → 本版修复：复合游标 (last_ts, last_asset_id) + 同维度排序 (ai_processed_at, asset_id)
- Bug-B: 窗口基于 linked_at 导致时间竞争 → 改为基于 ai_processed_at
- 附加：游标从本地文件改为 DB 存储，避免容器重建/多实例问题

游标机制：
- 复合键：(ai_processed_at, asset_id)，查询 ORDER BY 与游标同维度
- 满批（返回 == LIMIT）：游标推进到最后一行的复合键，下次从其后继续
- 不满批（返回 < LIMIT）：本批处理完，游标推进到 (infinity, 0)，下次取最新
- 幂等：同一资产多次重生安全（UPSERT 覆盖）

用法：
    python catalyst_thesis_regen.py [--max-assets 100]
    python catalyst_thesis_regen.py --asset-id 123
    python catalyst_thesis_regen.py --hours 24 --max-assets 200  # 临时回溯模式
    python catalyst_thesis_regen.py --reset-cursor              # 重置游标（首次全量用）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent

sys.path.insert(0, str(WORKBENCH_DIR))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


# ── 游标读写（DB 存储）──

def _load_cursor() -> tuple[str | None, int | None]:
    """读取复合游标 (last_ts, last_asset_id)。"""
    settings = get_settings(require_database=True)
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_ts, last_asset_id FROM biz.catalyst_regen_cursor WHERE id = 1"
            )
            row = cur.fetchone()
    if not row:
        return None, None
    return (str(row[0]) if row[0] else None, row[1])


def _save_cursor(last_ts: str | None, last_asset_id: int | None, processed: int):
    """保存复合游标。"""
    settings = get_settings(require_database=True)
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biz.catalyst_regen_cursor
                    (id, last_ts, last_asset_id, updated_at, processed_count)
                VALUES (1, %s, %s, NOW(), %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_ts = EXCLUDED.last_ts,
                    last_asset_id = EXCLUDED.last_asset_id,
                    updated_at = NOW(),
                    processed_count = biz.catalyst_regen_cursor.processed_count + EXCLUDED.processed_count
                """,
                (last_ts, last_asset_id, processed),
            )
        conn.commit()


def _reset_cursor():
    """重置游标（清空）。"""
    settings = get_settings(require_database=True)
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM biz.catalyst_regen_cursor WHERE id = 1"
            )
        conn.commit()


# ── 查询待重生资产（复合游标 + 同维度排序）──

def get_assets_cursor(limit: int) -> tuple[list[int], str | None, int | None, bool]:
    """基于复合游标找出有新催化剂（AI 已处理）的资产。

    返回 (asset_ids, new_last_ts, new_last_asset_id, is_complete)。
    is_complete=True 表示本批已处理完（不满批），下次可直接取最新。
    """
    last_ts, last_aid = _load_cursor()
    settings = get_settings(require_database=True)

    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            if last_ts and last_ts != "infinity":
                # 复合游标：(ts, asset_id) 元组比较
                cur.execute(
                    """
                    SELECT DISTINCT ON (ac.ai_processed_at, cal.asset_id)
                           cal.asset_id, ac.ai_processed_at
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE ac.ai_processed = true
                      AND (ac.ai_processed_at, cal.asset_id) > (%s::timestamptz, %s::bigint)
                    ORDER BY ac.ai_processed_at, cal.asset_id
                    LIMIT %s
                    """,
                    (last_ts, last_aid or 0, limit),
                )
            elif last_ts == "infinity":
                # 已追平，取"上次更新之后"的新数据
                cur.execute(
                    """
                    SELECT DISTINCT ON (ac.ai_processed_at, cal.asset_id)
                           cal.asset_id, ac.ai_processed_at
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE ac.ai_processed = true
                      AND ac.ai_processed_at > (
                          SELECT updated_at FROM biz.catalyst_regen_cursor WHERE id = 1
                      )
                    ORDER BY ac.ai_processed_at, cal.asset_id
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                # 首次运行：取近 7 天的，避免全量重生
                cur.execute(
                    """
                    SELECT DISTINCT ON (ac.ai_processed_at, cal.asset_id)
                           cal.asset_id, ac.ai_processed_at
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE ac.ai_processed = true
                      AND ac.ai_processed_at >= NOW() - INTERVAL '7 days'
                    ORDER BY ac.ai_processed_at, cal.asset_id
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

    if not rows:
        return [], None, None, True

    asset_ids = [r[0] for r in rows]
    is_complete = len(rows) < limit
    last_row = rows[-1]
    new_ts = str(last_row[1]) if last_row[1] else None
    new_aid = last_row[0]

    return asset_ids, new_ts, new_aid, is_complete


def get_assets_hours(hours: int, limit: int) -> list[int]:
    """基于滑动窗口（回溯模式，不影响游标）。"""
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


# ── 重生 thesis ──

def regen_thesis(asset_id: int) -> dict:
    """重生单个资产的 thesis。"""
    from db_stats import generate_research_thesis
    return generate_research_thesis(asset_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂驱动 thesis 重生（DB 复合游标）")
    parser.add_argument("--max-assets", type=int, default=100,
                        help="单轮最多重生多少个资产（默认 100）")
    parser.add_argument("--asset-id", type=int, default=None,
                        help="指定单个资产 ID 重生（忽略游标）")
    parser.add_argument("--hours", type=int, default=None,
                        help="回溯模式：近 N 小时内 AI 处理完的（忽略游标，不更新游标）")
    parser.add_argument("--reset-cursor", action="store_true",
                        help="重置游标后再运行（首次全量回溯用）")
    args = parser.parse_args()

    print("=" * 60)
    print("催化剂驱动 thesis 重生")
    print("=" * 60)

    if args.reset_cursor:
        _reset_cursor()
        print("已重置游标")

    is_cursor_mode = not args.asset_id and not args.hours

    if args.asset_id:
        asset_ids = [args.asset_id]
        print(f"指定资产: {args.asset_id}")
    elif args.hours:
        asset_ids = get_assets_hours(args.hours, args.max_assets)
        print(f"回溯模式：近 {args.hours} 小时 AI 处理完的资产 {len(asset_ids)} 个")
    else:
        asset_ids, new_ts, new_aid, is_complete = get_assets_cursor(args.max_assets)
        cur_ts, cur_aid = _load_cursor()
        print(f"游标模式: 上次游标 = ({cur_ts or 'NULL'}, {cur_aid or 'NULL'})")
        print(f"待重生资产: {len(asset_ids)} 个 (本批{'已完' if is_complete else '未完'})")

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
    if is_cursor_mode and success > 0:
        if is_complete:
            # 不满批 = 全部处理完，标记为追平（infinity），下次从 updated_at 之后取新数据
            _save_cursor("infinity", 0, success)
            print(f"\n游标已追平（本批处理完成）")
        else:
            # 满批 = 还有下一批，游标推进到最后一行
            _save_cursor(new_ts, new_aid, success)
            print(f"\n游标已更新: ({new_ts}, {new_aid})")

    print()
    print("=" * 60)
    print(f"完成: 成功 {success} / 失败 {failed} / 共 {len(asset_ids)}")
    print("=" * 60)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
