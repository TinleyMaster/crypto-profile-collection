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


def fetch_protocol_history(slug: str) -> list[tuple[date, float]]:
    """拉单个协议历史 TVL，返回 [(date, tvl_usd), ...] 按日期升序。"""
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
        print(f"  ⚠️  {slug} 拉取失败: {e}", file=sys.stderr)
        return []


def aggregate_category_tvl(
    protocols: list[dict],
    days: int,
    top_n: int,
    dry_run: bool = False,
) -> dict[tuple[date, str], tuple[float, int]]:
    """聚合赛道 TVL：返回 {(date, category): (total_tvl, protocol_count)}。"""
    cutoff = date.today() - timedelta(days=days)
    print(f"[category_tvl] 回填天数: {days} (截止 {cutoff})")
    print(f"[category_tvl] 头部协议数: {top_n}")

    # 取头部 N 个
    top_protocols = protocols[:top_n]
    top_tvl = sum(p.get("tvl", 0) for p in top_protocols)
    total_tvl = sum(p.get("tvl", 0) for p in protocols if p.get("tvl", 0) > 0)
    coverage = top_tvl / total_tvl * 100 if total_tvl > 0 else 0
    print(f"[category_tvl] 头部 {top_n} 协议覆盖 TVL: ${top_tvl/1e9:.2f}B ({coverage:.1f}%)")

    # 聚合
    # {(date, category): [total_tvl, count]}
    agg: dict[tuple[date, str], list[float, int]] = {}
    success = 0
    skipped = 0

    for i, proto in enumerate(top_protocols):
        slug = proto.get("slug", "")
        category = proto.get("category", "Unknown")
        name = proto.get("name", slug)

        if not slug:
            skipped += 1
            continue

        if (i + 1) % 50 == 0 or i == 0:
            print(f"  进度: {i+1}/{top_n}  {name} ({category})")

        history = fetch_protocol_history(slug)
        if not history:
            skipped += 1
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

    print(f"[category_tvl] 拉取完成: 成功 {success}, 跳过/失败 {skipped}")
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

        # 2. 拉历史 + 聚合
        agg_data = aggregate_category_tvl(
            protocols,
            days=args.days,
            top_n=args.top,
            dry_run=args.dry_run,
        )

        if not agg_data:
            print("[category_tvl] ⚠️  没有聚合到任何数据")
            return

        # 3. 写入
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
                    LIMIT 5
                """, (args.days,))
                rows = cur.fetchall()
                print(f"\n[category_tvl] ✅ 验证（最近 5 天）:")
                for r in rows:
                    print(f"  {r[0]}: {r[1]} 个赛道, 总TVL ${float(r[2])/1e9:.2f}B")

    print("[category_tvl] done ✓")


if __name__ == "__main__":
    main()
