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
- 不满批（返回 < LIMIT）：本批处理完，游标推进到本批最后一行的复合键（绝不清空）。
  清空游标会让下轮重扫近 7 天窗口、把同一批资产反复重生 → 白烧 LLM 额度（DeepSeek 402 根因）
- 新鲜度闸门：`--fresh-hours`（默认 24h）内已重生过 thesis 的资产直接跳过，兜底防重复
- 幂等：同一资产多次重生安全（UPSERT 覆盖）

用法：
    python catalyst_thesis_regen.py [--max-assets 100]
    python catalyst_thesis_regen.py --asset-id 123
    python catalyst_thesis_regen.py --hours 24 --max-assets 200  # 临时回溯模式
    python catalyst_thesis_regen.py --reset-cursor              # 重置游标（首次全量用）
    python catalyst_thesis_regen.py --fresh-hours 0             # 关闭新鲜度闸门
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent

sys.path.insert(0, str(WORKBENCH_DIR))
# crypto_research 在 scripts/src/ 下
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


# ── 游标读写（DB 存储）──

def _load_cursor() -> tuple[str | None, int | None]:
    """读取复合游标 (last_ts, last_asset_id)。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_ts, last_asset_id FROM biz.catalyst_regen_cursor WHERE id = 1"
            )
            row = cur.fetchone()
    if not row:
        return None, None
    ts = row[0]
    # infinity 哨兵（旧版误存）：视为无游标，走首次分支
    if ts is None or str(ts) in ("infinity", "-infinity"):
        return None, None
    return (str(ts), row[1])


def _save_cursor(last_ts: str | None, last_asset_id: int | None, processed: int):
    """保存复合游标。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
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
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM biz.catalyst_regen_cursor WHERE id = 1"
            )
        conn.commit()


# ── 查询待重生资产（复合游标 + 同维度排序）──

def get_assets_cursor(
    limit: int, fresh_hours: int = 24
) -> tuple[list[int], str | None, int | None, bool]:
    """基于复合游标找出有新催化剂（AI 已处理）的资产。

    返回 (asset_ids, new_last_ts, new_last_asset_id, is_complete)。
    is_complete=True 表示本批已处理完（不满批），下次可直接取最新。

    fresh_hours: 新鲜度闸门——该小时内已重生过 thesis 的资产直接跳过，
    兜底防止同一资产被反复重生烧 LLM 额度。
    """
    last_ts, last_aid = _load_cursor()
    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            if last_ts:
                # 复合游标：(ts, asset_id) 元组比较
                cur.execute(
                    """
                    SELECT DISTINCT ON (ac.ai_processed_at, cal.asset_id)
                           cal.asset_id, ac.ai_processed_at
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE ac.ai_processed = true
                      AND (ac.ai_processed_at, cal.asset_id) > (%s::timestamptz, %s::bigint)
                      AND NOT EXISTS (
                          SELECT 1 FROM biz.research_thesis rt
                          WHERE rt.asset_id = cal.asset_id
                            AND rt.updated_at >= NOW() - make_interval(hours => %s)
                      )
                    ORDER BY ac.ai_processed_at, cal.asset_id
                    LIMIT %s
                    """,
                    (last_ts, last_aid or 0, fresh_hours, limit),
                )
            else:
                # 首次运行（游标为空）：取近 7 天的，避免全量重生
                cur.execute(
                    """
                    SELECT DISTINCT ON (ac.ai_processed_at, cal.asset_id)
                           cal.asset_id, ac.ai_processed_at
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE ac.ai_processed = true
                      AND ac.ai_processed_at >= NOW() - INTERVAL '7 days'
                      AND NOT EXISTS (
                          SELECT 1 FROM biz.research_thesis rt
                          WHERE rt.asset_id = cal.asset_id
                            AND rt.updated_at >= NOW() - make_interval(hours => %s)
                      )
                    ORDER BY ac.ai_processed_at, cal.asset_id
                    LIMIT %s
                    """,
                    (fresh_hours, limit),
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


def get_assets_hours(hours: int, limit: int, fresh_hours: int = 24) -> list[int]:
    """基于滑动窗口（回溯模式，不影响游标）。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cal.asset_id
                FROM biz.catalyst_asset_link cal
                JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                WHERE ac.ai_processed = true
                  AND ac.ai_processed_at >= NOW() - make_interval(hours => %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM biz.research_thesis rt
                      WHERE rt.asset_id = cal.asset_id
                        AND rt.updated_at >= NOW() - make_interval(hours => %s)
                  )
                ORDER BY cal.asset_id
                LIMIT %s
                """,
                (hours, fresh_hours, limit),
            )
            rows = cur.fetchall()
    return [r[0] for r in rows]


# ── 重生 thesis ──

def regen_thesis(asset_id: int) -> dict:
    """重生单个资产的 thesis。"""
    from db_stats import generate_research_thesis
    return generate_research_thesis(asset_id)


# ── 失败重试队列（bounded-retry + 死信）──

def _ensure_failed_table():
    """确保 catalyst_regen_failed 表存在。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.catalyst_regen_failed (
                    asset_id        BIGINT PRIMARY KEY,
                    error_msg       TEXT,
                    attempt_count   INT DEFAULT 1,
                    next_retry_at   TIMESTAMPTZ DEFAULT NOW(),
                    status          TEXT DEFAULT 'pending',  -- pending / dead
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()


def _enqueue_failed(failed_list: list[tuple[int, str]]):
    """将失败资产写入重试队列（UPSERT，attempt_count+1，指数退避）。"""
    if not failed_list:
        return
    _ensure_failed_table()
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            for aid, err in failed_list:
                cur.execute("""
                    INSERT INTO biz.catalyst_regen_failed
                        (asset_id, error_msg, attempt_count, next_retry_at, status, updated_at)
                    VALUES (%s, %s, 1, NOW() + INTERVAL '5 minutes', 'pending', NOW())
                    ON CONFLICT (asset_id) DO UPDATE SET
                        error_msg = EXCLUDED.error_msg,
                        attempt_count = biz.catalyst_regen_failed.attempt_count + 1,
                        next_retry_at = NOW() + (
                            CASE biz.catalyst_regen_failed.attempt_count
                                WHEN 1 THEN INTERVAL '10 minutes'
                                WHEN 2 THEN INTERVAL '30 minutes'
                                ELSE INTERVAL '2 hours'
                            END
                        ),
                        status = CASE
                            WHEN biz.catalyst_regen_failed.attempt_count >= 3 THEN 'dead'
                            ELSE 'pending'
                        END,
                        updated_at = NOW()
                """, (aid, err[:500]))
        conn.commit()


def _retry_from_queue(max_attempts: int = 3) -> tuple[int, int]:
    """从重试队列捞 pending 资产重试，返回 (retried_success, dead_count)。"""
    _ensure_failed_table()
    settings = get_settings(require_database=True)
    retried = 0
    dead = 0

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT asset_id, attempt_count
                FROM biz.catalyst_regen_failed
                WHERE status = 'pending' AND next_retry_at <= NOW()
                ORDER BY next_retry_at
                LIMIT 10
            """)
            rows = cur.fetchall()

    if not rows:
        # 统计 dead 数
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM biz.catalyst_regen_failed WHERE status = 'dead'")
                dead = cur.fetchone()[0]
        return 0, dead

    for aid, attempt in rows:
        print(f"  🔁 重试 asset_id={aid} (第 {attempt} 次) ...")
        try:
            result = regen_thesis(aid)
            if result.get("ok"):
                print(f"    ✅ 重试成功")
                retried += 1
                # 删除成功记录
                with get_connection(settings.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM biz.catalyst_regen_failed WHERE asset_id = %s", (aid,))
                    conn.commit()
            else:
                err = result.get("error", "unknown")
                print(f"    ❌ 重试失败: {err}")
                _update_failed(aid, err)
        except Exception as e:
            print(f"    ❌ 重试异常: {e}")
            _update_failed(aid, str(e))

    # 统计 dead 数
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.catalyst_regen_failed WHERE status = 'dead'")
            dead = cur.fetchone()[0]

    return retried, dead


def _update_failed(aid: int, err: str):
    """更新失败记录：attempt_count+1，超限则标 dead。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE biz.catalyst_regen_failed
                SET attempt_count = attempt_count + 1,
                    error_msg = %s,
                    status = CASE
                        WHEN attempt_count >= 3 THEN 'dead'
                        ELSE 'pending'
                    END,
                    next_retry_at = NOW() + (
                        CASE attempt_count
                            WHEN 1 THEN INTERVAL '10 minutes'
                            WHEN 2 THEN INTERVAL '30 minutes'
                            ELSE INTERVAL '2 hours'
                        END
                    ),
                    updated_at = NOW()
                WHERE asset_id = %s
            """, (err[:500], aid))
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂驱动 thesis 重生（DB 复合游标）")
    parser.add_argument("--max-assets", type=int, default=100,
                        help="单轮最多重生多少个资产（默认 100）")
    parser.add_argument("--fresh-hours", type=int, default=24,
                        help="新鲜度闸门：N 小时内已重生 thesis 的资产跳过（默认 24，0=关闭）")
    parser.add_argument("--asset-id", type=int, default=None,
                        help="指定单个资产 ID 重生（忽略游标）")
    parser.add_argument("--hours", type=int, default=None,
                        help="回溯模式：近 N 小时内 AI 处理完的（忽略游标，不更新游标）")
    parser.add_argument("--reset-cursor", action="store_true",
                        help="重置游标后再运行（首次全量回溯用）")
    args = parser.parse_args()

    print("=" * 60)
    print("催化剂驱动 thesis 重生")
    print(f"新鲜度闸门: {args.fresh_hours}h（0=关闭）")
    print("=" * 60)

    if args.reset_cursor:
        _reset_cursor()
        print("已重置游标")

    is_cursor_mode = not args.asset_id and not args.hours

    if args.asset_id:
        asset_ids = [args.asset_id]
        print(f"指定资产: {args.asset_id}")
    elif args.hours:
        asset_ids = get_assets_hours(args.hours, args.max_assets, args.fresh_hours)
        print(f"回溯模式：近 {args.hours} 小时 AI 处理完的资产 {len(asset_ids)} 个")
    else:
        asset_ids, new_ts, new_aid, is_complete = get_assets_cursor(
            args.max_assets, args.fresh_hours
        )
        cur_ts, cur_aid = _load_cursor()
        print(f"游标模式: 上次游标 = ({cur_ts or 'NULL'}, {cur_aid or 'NULL'})")
        print(f"待重生资产: {len(asset_ids)} 个 (本批{'已完' if is_complete else '未完'})")

    if not asset_ids:
        print("无需重生")
        return 0

    success = 0
    failed = 0
    failed_list: list[tuple[int, str]] = []  # (asset_id, error_msg)

    for i, aid in enumerate(asset_ids, 1):
        print(f"\n[{i}/{len(asset_ids)}] 重生 asset_id={aid} ...")
        try:
            result = regen_thesis(aid)
            if result.get("ok"):
                print(f"  ✅ 成功 (stance={result.get('stance', '?')})")
                success += 1
            else:
                err = result.get("error", "unknown")
                print(f"  ❌ 失败: {err}")
                failed += 1
                failed_list.append((aid, err))
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            failed += 1
            failed_list.append((aid, str(e)))

    # 游标模式：始终推进游标到本批末行，保证前向进度
    if is_cursor_mode:
        if is_complete:
            save_ts = new_ts or datetime.now(timezone.utc).isoformat()
            _save_cursor(save_ts, new_aid or 0, success)
            print(f"\n游标已追平（推进到 ({save_ts}, {new_aid or 0})，下次只取新数据）")
        else:
            _save_cursor(new_ts, new_aid, success)
            print(f"\n游标已更新: ({new_ts}, {new_aid})")
        # 失败资产隔离进重试队列（不阻塞游标前进）
        if failed_list:
            _enqueue_failed(failed_list)
            print(f"  ⚠️ {failed} 个失败资产已写入重试队列，下轮/本 run 末尾单独重试")

    # 重试队列处理（每轮 run 末尾）
    if is_cursor_mode:
        retried, dead = _retry_from_queue(max_attempts=3)
        if retried > 0:
            print(f"\n🔁 重试队列: 成功 {retried} 个")
        if dead > 0:
            print(f"  💀 死信: {dead} 个资产超限（已放弃，不阻塞流水线）")

    print()
    print("=" * 60)
    print(f"完成: 成功 {success} / 失败 {failed} / 共 {len(asset_ids)}")
    print("=" * 60)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
