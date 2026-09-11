#!/usr/bin/env python3
"""历史回填：赛道 TVL（DeFi Llama 免费 API，按头部协议聚合）。

思路：
1. 拉所有协议列表 → 按 TVL 降序 → 取头部 N 个（覆盖 95%+ 的 TVL）
2. 逐个拉协议历史 TVL（/protocol/{slug}）
3. 按天 + category 聚合 → upsert 到 biz.category_tvl_daily

注意：
- DeFi Llama 免费版 ~500 req/5min，头部 500 协议约 1 小时跑完
- 幂等：同一天同 category 会覆盖更新
- 支持 --days 控制回填天数（默认 90 天）
- 支持 --top 控制头部协议数（默认 500）

用法：
    python backfill_category_tvl_history.py --days 90 --top 500
    python backfill_category_tvl_history.py --days 365 --top 200 --dry-run
"""
from __future__ import annotations

import argparse
import json
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
# 免费版限流 ~500/5min = 1.67/秒，我们保守点 0.8 req/s
REQUEST_INTERVAL = 1.2  # 秒


def ensure_table(conn) -> None:
    """建表（跟 ingest_category_tvl.py 保持一致）。"""
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
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_date
                ON biz.category_tvl_daily(snapshot_date DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_tvl
                ON biz.category_tvl_daily(tvl_usd DESC);
        """)
    conn.commit()


def fetch_all_protocols() -> list[dict]:
    """拉所有协议列表，返回按 TVL 降序排列的列表。"""
    print("[category_tvl] 拉取协议列表 ...")
    r = requests.get(f"{LLAMA_BASE}/protocols", timeout=TIMEOUT)
    r.raise_for_status()
    protocols = r.json()
    # 过滤掉没有 category 或 tvl 的（tvl 可能是 None，用 or 兜底）
    def _safe_tvl(p):
        v = p.get("tvl")
        return float(v) if v is not None else 0.0

    valid = [p for p in protocols if p.get("category") and _safe_tvl(p) > 0]
    valid.sort(key=lambda x: _safe_tvl(x), reverse=True)
    total_tvl = sum(_safe_tvl(p) for p in valid)
    print(f"[category_tvl] 共 {len(protocols)} 个协议, "
          f"有效 {len(valid)} 个, 总TVL ${total_tvl/1e9:.2f}B")
    return valid


def fetch_protocol_history(slug: str, retries: int = 3) -> list[tuple[date, float]]:
    """拉单个协议历史 TVL，返回 [(date, tvl_usd), ...] 按日期升序。
    带重试机制，避免临时网络波动导致失败。
    """
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


def aggregate_category_tvl(
    protocols: list[dict],
    days: int,
    top_n: int,
    start_index: int = 0,
    dry_run: bool = False,
    batch_commit_conn=None,
) -> dict[tuple[date, str], tuple[float, int]]:
    """聚合赛道 TVL：返回 {(date, category): (total_tvl, protocol_count)}。

    支持断点续跑：start_index 指定从第几个协议开始。
    支持分批落库：batch_commit_conn 提供时，每 50 个协议提交一次（防中断白跑）。
    """
    cutoff = date.today() - timedelta(days=days)
    print(f"[category_tvl] 回填天数: {days} (截止 {cutoff})")
    print(f"[category_tvl] 头部协议数: {top_n}"
          + (f", 从第 {start_index} 个开始" if start_index > 0 else ""))

    # 取头部 N 个（并跳过 start_index 之前的）
    top_protocols = protocols[start_index:start_index + top_n]
    # 计算 TVL 覆盖（全量算）
    all_valid = [p for p in protocols if _safe_tvl(p) > 0]
    total_tvl = sum(_safe_tvl(p) for p in all_valid)
    top_tvl = sum(_safe_tvl(p) for p in protocols[:top_n])
    coverage = top_tvl / total_tvl * 100 if total_tvl > 0 else 0
    print(f"[category_tvl] 头部 {top_n} 协议覆盖 TVL: ${top_tvl/1e9:.2f}B ({coverage:.1f}%)")
    if batch_commit_conn:
        print(f"[category_tvl] 分批落库模式：每 50 个协议提交一次")

    # 聚合
    # {(date, category): [total_tvl, count]}
    agg: dict[tuple[date, str], list[float, int]] = {}
    success = 0
    failed = 0
    total_to_fetch = len(top_protocols)
    start_time = time.time()

    for i, proto in enumerate(top_protocols):
        global_idx = start_index + i
        slug = proto.get("slug", "")
        category = proto.get("category", "Unknown")
        name = proto.get("name", slug)

        if not slug:
            failed += 1
            continue

        # 每 10 个打印一次进度（更频繁，方便监控）
        if (i + 1) % 10 == 0 or i == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total_to_fetch - i - 1) / rate / 60 if rate > 0 else 0
            print(f"  进度: {global_idx+1}/{start_index+total_to_fetch} "
                  f"({(i+1)/total_to_fetch*100:.1f}%) "
                  f"{name} ({category}) "
                  f"[{rate:.1f} req/min, ETA {eta:.0f}min]")

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

        # 每 50 个协议批量落库一次（如果提供了 conn）
        if batch_commit_conn and (i + 1) % 50 == 0:
            batch_count = upsert_data(
                batch_commit_conn,
                {k: (v[0], v[1]) for k, v in agg.items()},
                dry_run=dry_run,
            )
            print(f"  📦 分批落库: 已处理 {i+1}/{total_to_fetch} 协议, "
                  f"写入 {batch_count} 条快照")
            # 落库后清空内存中的 agg（避免重复），改用"追加"模式
            # 注意：因为是 upsert，重复写入不会有问题，不清空也可以
            # 但为了内存效率，每 50 个提交一次然后保留 agg 继续累加

    elapsed = time.time() - start_time
    print(f"[category_tvl] 拉取完成: 成功 {success}, 失败 {failed}, "
          f"用时 {elapsed/60:.1f}min")
    print(f"[category_tvl] 聚合结果: {len(agg)} 条 (日期×赛道)")

    # 转成 tuple 形式返回
    return {k: (v[0], v[1]) for k, v in agg.items()}


def upsert_data(
    conn,
    data: dict[tuple[date, str], tuple[float, int]],
    dry_run: bool = False,
) -> int:
    """批量 upsert 到 biz.category_tvl_daily。"""
    if not data:
        return 0

    rows = sorted(data.items(), key=lambda x: (x[0][0], -x[1][0]))

    if dry_run:
        print(f"[category_tvl] DRY RUN: 将 upsert {len(rows)} 条")
        # 显示最近一天的 Top 10 赛道
        latest_date = max(k[0] for k, _ in rows)
        latest_rows = [(k, v) for k, v in rows if k[0] == latest_date]
        latest_rows.sort(key=lambda x: -x[1][0])
        print(f"\n  {latest_date} Top 10 赛道:")
        for (_, cat), (tvl, cnt) in latest_rows[:10]:
            print(f"    {cat:25s} ${tvl/1e9:>8.2f}B  ({cnt} 协议)")
        return len(rows)

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
    print(f"[category_tvl] upserted {rows_inserted} rows")
    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="赛道 TVL 历史回填（DeFi Llama 免费版）")
    parser.add_argument("--days", type=int, default=90, help="回填天数（默认 90）")
    parser.add_argument("--top", type=int, default=500, help="头部协议数（默认 500）")
    parser.add_argument("--start", type=int, default=0, help="从第几个协议开始（断点续跑，默认 0）")
    parser.add_argument("--no-batch", action="store_true", help="不分批落库，全部跑完再写（默认每50个写一次）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    if args.dry_run:
        print("[category_tvl] === DRY RUN 模式 ===")

    with get_connection(settings.database_url) as conn:
        if not args.dry_run:
            ensure_table(conn)

        # 1. 拉协议列表
        protocols = fetch_all_protocols()
        if not protocols:
            print("[category_tvl] ❌ 协议列表为空，退出")
            sys.exit(1)

        # 2. 拉历史 + 聚合（支持分批落库）
        batch_conn = None if (args.dry_run or args.no_batch) else conn
        agg_data = aggregate_category_tvl(
            protocols,
            days=args.days,
            top_n=args.top,
            start_index=args.start,
            dry_run=args.dry_run,
            batch_commit_conn=batch_conn,
        )

        if not agg_data:
            print("[category_tvl] ⚠️  没有聚合到任何数据")
            return

        # 3. 最终写入（分批模式下已经写过了，但最后再跑一次确保完整）
        upsert_data(conn, agg_data, dry_run=args.dry_run)

        # 4. 验证
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT snapshot_date, COUNT(*) as cnt, SUM(tvl_usd) as total_tvl
                    FROM biz.category_tvl_daily
                    WHERE snapshot_date >= CURRENT_DATE - %s
                    GROUP BY snapshot_date
                    ORDER BY snapshot_date DESC
                    LIMIT 10
                """, (args.days,))
                rows = cur.fetchall()
                print(f"\n[category_tvl] ✅ 验证（最近 10 天）:")
                for r in rows:
                    print(f"  {r[0]}: {r[1]} 个赛道, 总TVL ${float(r[2])/1e9:.2f}B")

    print("[category_tvl] done ✓")


if __name__ == "__main__":
    main()
