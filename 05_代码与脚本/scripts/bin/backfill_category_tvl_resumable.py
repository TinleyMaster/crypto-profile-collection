#!/usr/bin/env python3
"""历史回填：赛道 TVL（带 DB 持久化进度，pod 重启/中断自动续跑）。

在 backfill_category_tvl_history.py 的基础上，增加：
- 进度存在 biz.backfill_progress 表，任何 pod 重启都能续上
- 启动时自动读进度 → 从断点继续
- 完成后标记 done，再次运行直接退出
- 失败时记录错误，下次 --resume 自动续跑

用法：
    # 首次启动（90 天，5000 协议，自动续跑）
    python backfill_category_tvl_resumable.py --days 90 --top 5000

    # 从上次断点续跑（进程被杀后直接重跑这条）
    python backfill_category_tvl_resumable.py --resume

    # 重置进度（重新开始）
    python backfill_category_tvl_resumable.py --reset --days 90 --top 5000

    # 查看当前进度
    python backfill_category_tvl_resumable.py --status
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


LLAMA_BASE = "https://api.llama.fi"
TIMEOUT = 30
SOURCE_CODE = "defillama"
REQUEST_INTERVAL = 1.2  # 秒
BATCH_SIZE = 50  # 每 N 个协议落库 + 更新进度
JOB_NAME = "category_tvl_history_backfill"


def safe_tvl(p: dict) -> float:
    v = p.get("tvl")
    return float(v) if v is not None else 0.0


# ========== 进度持久化 ==========

def ensure_progress_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.backfill_progress (
                job_name      VARCHAR(100) PRIMARY KEY,
                current_index INTEGER   NOT NULL DEFAULT 0,
                total_count   INTEGER   NOT NULL DEFAULT 0,
                status        VARCHAR(20) NOT NULL DEFAULT 'running',
                days          INTEGER,
                top_n         INTEGER,
                error_msg     TEXT,
                started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    conn.commit()


def load_progress(conn, job_name: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_name, current_index, total_count, status, days, top_n, error_msg, started_at, updated_at "
            "FROM biz.backfill_progress WHERE job_name = %s",
            (job_name,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "job_name": row[0],
            "current_index": row[1],
            "total_count": row[2],
            "status": row[3],
            "days": row[4],
            "top_n": row[5],
            "error_msg": row[6],
            "started_at": row[7],
            "updated_at": row[8],
        }


def save_progress(conn, job_name: str, current_index: int, total_count: int,
                  status: str, days: int, top_n: int, error_msg: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO biz.backfill_progress
                (job_name, current_index, total_count, status, days, top_n, error_msg, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (job_name) DO UPDATE SET
                current_index = EXCLUDED.current_index,
                total_count   = EXCLUDED.total_count,
                status        = EXCLUDED.status,
                days          = COALESCE(EXCLUDED.days, biz.backfill_progress.days),
                top_n         = COALESCE(EXCLUDED.top_n, biz.backfill_progress.top_n),
                error_msg     = EXCLUDED.error_msg,
                updated_at    = NOW()
        """, (job_name, current_index, total_count, status, days, top_n, error_msg))
    conn.commit()


def reset_progress(conn, job_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM biz.backfill_progress WHERE job_name = %s", (job_name,))
    conn.commit()


# ========== 数据采集 ==========

def fetch_all_protocols() -> list[dict]:
    print("[category_tvl] 拉取协议列表 ...")
    r = requests.get(f"{LLAMA_BASE}/protocols", timeout=TIMEOUT)
    r.raise_for_status()
    protocols = r.json()
    valid = [p for p in protocols if p.get("category") and safe_tvl(p) > 0]
    valid.sort(key=lambda x: safe_tvl(x), reverse=True)
    total_tvl = sum(safe_tvl(p) for p in valid)
    print(f"[category_tvl] 共 {len(protocols)} 个协议, "
          f"有效 {len(valid)} 个, 总TVL ${total_tvl/1e9:.2f}B")
    return valid


def fetch_protocol_history(slug: str, retries: int = 3) -> list[tuple[date, float]]:
    for attempt in range(retries):
        try:
            r = requests.get(f"{LLAMA_BASE}/protocol/{slug}", timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            tvl_list = data.get("tvl", []) or []
            result = []
            for item in tvl_list:
                ts = item.get("date")
                tvl = item.get("totalLiquidityUSD")
                if ts is None or tvl is None:
                    continue
                try:
                    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
                    result.append((dt, float(tvl)))
                except (ValueError, TypeError, OSError):
                    continue
            result.sort(key=lambda x: x[0])
            return result
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"  ⚠️  {slug} 第{attempt+1}次失败: {e}，{wait}s后重试...",
                      file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  ❌ {slug} 拉取失败 ({retries}次重试后): {e}",
                      file=sys.stderr)
    return []


def ensure_category_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.category_tvl_daily (
                snapshot_date   DATE           NOT NULL,
                category        VARCHAR(100)   NOT NULL,
                tvl_usd         NUMERIC(28, 2) NOT NULL DEFAULT 0,
                protocol_count  INTEGER        NOT NULL DEFAULT 0,
                source_code     VARCHAR(20)    NOT NULL DEFAULT 'defillama',
                fetched_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                PRIMARY KEY (snapshot_date, category)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_date
                ON biz.category_tvl_daily(snapshot_date DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_tvl
                ON biz.category_tvl_daily(tvl_usd DESC)
        """)
    conn.commit()


def upsert_data(conn, data: dict) -> int:
    if not data:
        return 0
    rows = sorted(data.items(), key=lambda x: (x[0][0], -x[1][0]))
    rows_inserted = 0
    with conn.cursor() as cur:
        for (dt, category), (tvl, cnt) in rows:
            cur.execute("""
                INSERT INTO biz.category_tvl_daily
                    (snapshot_date, category, tvl_usd, protocol_count,
                     source_code, fetched_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (snapshot_date, category) DO UPDATE
                SET tvl_usd = EXCLUDED.tvl_usd,
                    protocol_count = EXCLUDED.protocol_count,
                    source_code = EXCLUDED.source_code,
                    updated_at = NOW()
            """, (dt, category, tvl, cnt, SOURCE_CODE))
            rows_inserted += 1
    conn.commit()
    print(f"  📦 upserted {rows_inserted} rows")
    return rows_inserted


# ========== 主流程 ==========

def run_backfill(conn, protocols: list[dict], days: int, top_n: int, start_index: int = 0) -> None:
    cutoff = date.today() - timedelta(days=days)
    total = min(top_n, len(protocols))
    print(f"[category_tvl] 回填 {days} 天（截止 {cutoff}）, 协议 {start_index+1}~{total}")

    agg: dict[tuple[date, str], list[float, int]] = {}
    success = 0
    failed = 0
    start_time = time.time()
    last_save_idx = start_index

    for i in range(start_index, total):
        proto = protocols[i]
        slug = proto.get("slug", "")
        category = proto.get("category", "Unknown")
        name = proto.get("name", slug)
        global_idx = i + 1  # 1-based

        # 进度打印（每 10 个）
        if global_idx % 10 == 0 or i == start_index:
            elapsed = time.time() - start_time
            rate = (i - start_index + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate / 60 if rate > 0 else 0
            print(f"  进度: {global_idx}/{total} ({global_idx/total*100:.1f}%) "
                  f"{name} ({category}) [{rate:.1f} req/min, ETA {eta:.0f}min]")

        if not slug:
            failed += 1
            continue

        history = fetch_protocol_history(slug)
        if not history:
            failed += 1
            time.sleep(REQUEST_INTERVAL)
            continue

        success += 1
        for dt, tvl in history:
            if dt < cutoff:
                continue
            key = (dt, category)
            if key not in agg:
                agg[key] = [0.0, 0]
            agg[key][0] += tvl
            agg[key][1] += 1

        time.sleep(REQUEST_INTERVAL)

        # 每 BATCH_SIZE 个落库 + 更新进度
        if (i - start_index + 1) % BATCH_SIZE == 0:
            upsert_data(conn, {k: (v[0], v[1]) for k, v in agg.items()})
            save_progress(conn, JOB_NAME, global_idx, total, "running", days, top_n)
            last_save_idx = global_idx
            agg.clear()  # 清空避免重复/内存膨胀

    # 最后一批
    if agg:
        upsert_data(conn, {k: (v[0], v[1]) for k, v in agg.items()})

    elapsed = time.time() - start_time
    print(f"[category_tvl] ✅ 完成: 成功 {success}, 失败 {failed}, 用时 {elapsed/60:.1f}min")
    save_progress(conn, JOB_NAME, total, total, "done", days, top_n)


def verify(conn, days: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT snapshot_date, COUNT(*) as cnt, SUM(tvl_usd) as total_tvl
            FROM biz.category_tvl_daily
            WHERE snapshot_date >= CURRENT_DATE - %s
            GROUP BY snapshot_date
            ORDER BY snapshot_date DESC
            LIMIT 12
        """, (days,))
        rows = cur.fetchall()
        print(f"\n[category_tvl] 📊 验证（最近 12 天）:")
        print(f"  {'日期':<12} {'赛道数':<8} {'总TVL(B)':<12}")
        print(f"  {'-'*34}")
        for r in rows:
            print(f"  {str(r[0]):<12} {r[1]:<8} ${float(r[2])/1e9:<11.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="赛道 TVL 历史回填（DB 持久化进度，自动续跑）")
    parser.add_argument("--days", type=int, default=90, help="回填天数（默认 90）")
    parser.add_argument("--top", type=int, default=5000, help="头部协议数（默认 5000）")
    parser.add_argument("--start", type=int, default=0, help="强制从第几个协议开始（忽略 DB 进度）")
    parser.add_argument("--resume", action="store_true", help="从 DB 读取上次进度续跑")
    parser.add_argument("--reset", action="store_true", help="重置进度后重新开始")
    parser.add_argument("--status", action="store_true", help="仅查看当前进度，不执行回填")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        ensure_progress_table(conn)
        ensure_category_table(conn)

        # 仅查看状态
        if args.status:
            prog = load_progress(conn, JOB_NAME)
            if not prog:
                print(f"[category_tvl] 任务 '{JOB_NAME}' 不存在（还没开始过）")
                return
            print(f"[category_tvl] 任务: {prog['job_name']}")
            print(f"  状态:     {prog['status']}")
            print(f"  进度:     {prog['current_index']}/{prog['total_count']} "
                  f"({prog['current_index']/max(prog['total_count'],1)*100:.1f}%)")
            print(f"  天数:     {prog['days']}")
            print(f"  协议数:   {prog['top_n']}")
            print(f"  开始时间: {prog['started_at']}")
            print(f"  更新时间: {prog['updated_at']}")
            if prog["error_msg"]:
                print(f"  错误:     {prog['error_msg']}")
            return

        # 重置
        if args.reset:
            reset_progress(conn, JOB_NAME)
            print(f"[category_tvl] 已重置任务 '{JOB_NAME}'")

        # 决定 start_index
        start_index = args.start
        days = args.days
        top_n = args.top

        if args.resume or args.start == 0:
            prog = load_progress(conn, JOB_NAME)
            if prog:
                if prog["status"] == "done":
                    print(f"[category_tvl] ✅ 任务已完成（{prog['current_index']}/{prog['total_count']}），无需重跑")
                    verify(conn, days)
                    return
                if prog["status"] == "running" or prog["status"] == "failed":
                    start_index = prog["current_index"]
                    days = prog["days"] or days
                    top_n = prog["top_n"] or top_n
                    print(f"[category_tvl] 🔄 从断点续跑: {start_index}/{top_n} "
                          f"(上次状态: {prog['status']})")
                    if prog.get("error_msg"):
                        print(f"  上次错误: {prog['error_msg']}")

        # 拉协议列表
        try:
            protocols = fetch_all_protocols()
        except Exception as e:
            print(f"[category_tvl] ❌ 拉取协议列表失败: {e}", file=sys.stderr)
            save_progress(conn, JOB_NAME, start_index, top_n, "failed", days, top_n, str(e))
            sys.exit(1)

        if not protocols:
            print("[category_tvl] ❌ 协议列表为空，退出")
            sys.exit(1)

        # 执行回填
        try:
            run_backfill(conn, protocols, days, top_n, start_index)
        except Exception as e:
            print(f"[category_tvl] 💥 运行异常: {e}", file=sys.stderr)
            # 尝试获取当前进度（粗略：用 start_index，实际可能已经推进了）
            save_progress(conn, JOB_NAME, start_index, top_n, "failed", days, top_n, str(e))
            raise

        # 验证
        verify(conn, days)

    print("[category_tvl] done ✓")


if __name__ == "__main__":
    main()
