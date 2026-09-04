"""CMC 快照缺口自检 + 自动回填（A 补配套脚本）。

每天定时跑（建议 09:00 UTC），检查最近 N 天的快照密度：
- 某一天快照行数 < threshold → 标记为缺口
- 缺口日用 historical API 自动回填到 src_cmc.cmc_asset_quote_snapshot
- 回填失败 → 打印告警，不下跪（不影响主流程）

设计原则：
- 只读判断 + 幂等写入，重跑无害
- 默认 dry-run，必须 --apply 才真写
- 阈值可调：默认 7000（正常日 ~8000，留 1000 冗余）

用法：
    python check_cmc_snapshot_gap.py --days 3 --threshold 7000
    python check_cmc_snapshot_gap.py --days 3 --apply --batch-size 100
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.db.upsert import load_sql, execute_many  # noqa: E402
from crypto_research.clients.cmc_client import CMCClient  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check CMC snapshot gap and auto-backfill with historical API."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Check last N days for gaps. Default: 3.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=7000,
        help="Min rows per day to consider healthy. Default: 7000.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually backfill gaps (default: dry-run, just report).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="CMC IDs per historical API call (max 100). Default: 100.",
    )
    return parser


def check_gaps(conn, days: int, threshold: int) -> list[dict]:
    """检查最近 days 天的快照密度，返回缺口日列表。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DATE(quote_time) AS d,
                   COUNT(*) AS rows,
                   COUNT(DISTINCT cmc_id) AS assets
            FROM src_cmc.cmc_asset_quote_snapshot
            WHERE DATE(quote_time) >= CURRENT_DATE - %s
              AND DATE(quote_time) <  CURRENT_DATE
            GROUP BY 1
            ORDER BY 1 DESC
        """, (days,))
        rows = cur.fetchall()

    # 构造完整日期列表（防止某天完全没数据时不在结果里）
    today = datetime.now(timezone.utc).date()
    all_dates = [today - timedelta(days=i + 1) for i in range(days)]  # 昨天往前 N 天

    existing = {r[0]: {"date": r[0], "rows": r[1], "assets": r[2], "is_gap": r[1] < threshold}
                for r in rows}

    result = []
    for d in all_dates:
        if d in existing:
            result.append(existing[d])
        else:
            result.append({"date": d, "rows": 0, "assets": 0, "is_gap": True})
    return result


def fetch_all_assets(conn) -> list[tuple[int, int]]:
    """获取所有有 cmc_id 的资产，返回 [(asset_id, cmc_id)]。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cb.asset_id, cb.cmc_id
            FROM biz.coin_basic cb
            WHERE cb.cmc_id IS NOT NULL
        """)
        rows = cur.fetchall()
    return [(r[0], r[1]) for r in rows]


def backfill_one_day(
    conn,
    cmc: CMCClient,
    target_date: str,
    assets: list[tuple[int, int]],
    batch_size: int,
) -> dict:
    """用 CMC historical API 回填某一天的快照。返回统计。"""
    asset_id_map = {cmc_id: aid for aid, cmc_id in assets}
    batch_size = min(batch_size, 100)
    total_batches = (len(assets) + batch_size - 1) // batch_size
    total_rows = 0
    upsert_sql = load_sql("src_cmc/upsert_cmc_quote_snapshot.sql")

    PCT_LIMIT = 1e10
    DOM_LIMIT = 100.0

    for batch_idx in range(total_batches):
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + batch_size, len(assets))
        batch_cmc_ids = [cmc_id for _, cmc_id in assets[batch_start:batch_end]]

        try:
            resp = cmc.get_quotes_historical(
                ids=batch_cmc_ids,
                time_start=target_date,
                time_end=(datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"),
                interval="daily",
            )
        except Exception as e:
            print(f"  [WARN] batch {batch_idx + 1}/{total_batches} failed: {e}")
            time.sleep(2)
            continue

        # 解析快照行
        data = resp.get("data") or {}
        params = []
        skipped = 0
        for cmc_id_str, coin_data in data.items():
            cmc_id = int(cmc_id_str)
            asset_id = asset_id_map.get(cmc_id)
            if asset_id is None:
                continue
            quotes = coin_data.get("quotes") or []
            for qe in quotes:
                ts = qe.get("timestamp")
                if not ts:
                    continue
                try:
                    quote_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                q = (qe.get("quote") or {}).get("USD") or {}

                # range guard（同 0c2a639）
                bad = False
                for k in ("percent_change_1h", "percent_change_24h",
                          "percent_change_7d", "percent_change_30d"):
                    v = q.get(k)
                    if v is not None and abs(float(v)) >= PCT_LIMIT:
                        bad = True
                        break
                dom = q.get("market_cap_dominance")
                if dom is not None and float(dom) >= DOM_LIMIT:
                    bad = True
                if bad:
                    skipped += 1
                    continue

                params.append((
                    cmc_id, quote_time,
                    q.get("price"), q.get("market_cap"),
                    q.get("fully_diluted_market_cap"), q.get("volume_24h"),
                    q.get("circulating_supply"), q.get("total_supply"),
                    q.get("max_supply"),
                    q.get("percent_change_1h"), q.get("percent_change_24h"),
                    q.get("percent_change_7d"), q.get("percent_change_30d"),
                    q.get("market_cap_dominance"),
                    None,   # raw_response_id
                    False,  # is_anomaly
                ))

        if params:
            execute_many(conn, upsert_sql, params)
            total_rows += len(params)
        conn.commit()

        print(f"  batch {batch_idx + 1}/{total_batches}: "
              f"+{len(params)} snapshot rows"
              + (f" (skipped {skipped} dirty)" if skipped else ""))
        time.sleep(1.0)

    return {"rows_upserted": total_rows}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    cmc = CMCClient(settings)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[snapshot-gap-check] mode={mode} days={args.days} threshold={args.threshold}")

    with get_connection(settings.database_url) as conn:
        # ① 检查缺口
        gaps = check_gaps(conn, args.days, args.threshold)
        print(f"\n最近 {args.days} 天快照状态：")
        print(f"{'日期':<12} {'行数':>8} {'独立币种':>8} {'状态':<10}")
        print("-" * 42)
        for g in gaps:
            status = "❌ GAP" if g["is_gap"] else "✅ OK"
            print(f"{str(g['date']):<12} {g['rows']:>8} {g['assets']:>8} {status:<10}")

        gap_days = [g["date"].isoformat() for g in gaps if g["is_gap"]]
        if not gap_days:
            print(f"\n✅ 无缺口，所有日期均 >= {args.threshold} 行")
            return 0

        print(f"\n发现 {len(gap_days)} 个缺口日：{', '.join(gap_days)}")

        if not args.apply:
            print("[DRY-RUN] 未执行回填。加 --apply 执行回填。")
            return 0

        # ② 回填缺口
        assets = fetch_all_assets(conn)
        print(f"\n目标资产数：{len(assets)}")

        for gap_day in gap_days:
            print(f"\n📌 回填 {gap_day} ...")
            stat = backfill_one_day(conn, cmc, gap_day, assets, args.batch_size)
            print(f"   ✅ {gap_day} 完成，upsert {stat['rows_upserted']} 行")

            # 回填后复验
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*), COUNT(DISTINCT cmc_id)
                    FROM src_cmc.cmc_asset_quote_snapshot
                    WHERE DATE(quote_time) = %s
                """, (gap_day,))
                r = cur.fetchone()
                ok = "✅" if r[0] >= args.threshold else "⚠️"
                print(f"   {ok} 回填后：{r[0]} 行 / {r[1]} 币种"
                      + (" （仍低于阈值）" if r[0] < args.threshold else ""))

    print(f"\n[DONE] 缺口回填完成（{len(gap_days)} 天）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
