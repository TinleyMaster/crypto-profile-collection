#!/usr/bin/env python3
"""链覆盖状态构建：动态检测「已覆盖 vs 降级」链清单 → UPSERT biz.chain_coverage。

逻辑：
  covered   = biz.onchain_holder_snapshot 去重链（有原生快照）
  degraded  = core.asset_contract 全链 − covered（无原生快照）
每次全量重建，零硬编码链名。

用法：
    python phase_build_chain_coverage.py
    python phase_build_chain_coverage.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

UPSERT_SQL = """
INSERT INTO biz.chain_coverage (chain, coverage_status, has_native_snapshot, asset_count, note, updated_at)
VALUES (%(chain)s, %(coverage_status)s, %(has_native_snapshot)s, %(asset_count)s, %(note)s, NOW())
ON CONFLICT (chain) DO UPDATE SET
    coverage_status     = EXCLUDED.coverage_status,
    has_native_snapshot = EXCLUDED.has_native_snapshot,
    asset_count         = EXCLUDED.asset_count,
    note                = EXCLUDED.note,
    updated_at          = NOW()
"""

UPSERT_KEYS = {"chain", "coverage_status", "has_native_snapshot", "asset_count", "note"}

DEGRADED_NOTE = "无原生 holder 快照，仅定性活跃度兜底（MEME-06）"


def main() -> int:
    parser = argparse.ArgumentParser(description="链覆盖状态构建")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 1. 有原生快照的链 = covered
            cur.execute("SELECT DISTINCT chain FROM biz.onchain_holder_snapshot")
            covered_chains = {row[0] for row in cur.fetchall()}

            # 2. 全链 asset_count（从 core.asset_contract 按链聚合）
            cur.execute("""
                SELECT chain, COUNT(*) AS cnt
                FROM core.asset_contract
                WHERE chain IS NOT NULL AND LENGTH(chain) > 0
                GROUP BY chain
            """)
            chain_counts = {row[0]: row[1] for row in cur.fetchall()}

        all_chains = set(chain_counts.keys())
        degraded_chains = all_chains - covered_chains

        print(f"已覆盖链: {len(covered_chains)} 条")
        print(f"降级链:   {len(degraded_chains)} 条")
        print(f"全链合计: {len(all_chains)} 条")

        # 构建 UPSERT 行
        rows = []
        for chain in sorted(covered_chains):
            rows.append({
                "chain": chain,
                "coverage_status": "covered",
                "has_native_snapshot": True,
                "asset_count": chain_counts.get(chain, 0),
                "note": None,
            })
        for chain in sorted(degraded_chains):
            rows.append({
                "chain": chain,
                "coverage_status": "degraded",
                "has_native_snapshot": False,
                "asset_count": chain_counts.get(chain, 0),
                "note": DEGRADED_NOTE,
            })

        print(f"\n待写入: {len(rows)} 行")
        for r in rows:
            status_mark = "✅" if r["coverage_status"] == "covered" else "⚠️"
            print(f"  {status_mark} {r['chain']:20s} {r['coverage_status']:10s} assets={r['asset_count']}")

        if args.dry_run:
            print("\n[dry-run] 不写入数据库")
            return 0

        # UPSERT（逐行 fail-soft）
        success = 0
        fail = 0
        for i, row in enumerate(rows, 1):
            try:
                missing = UPSERT_KEYS - set(row.keys())
                if missing:
                    print(f"  ERROR 缺失占位符 {missing}", file=sys.stderr)
                    fail += 1
                    continue
                with conn.cursor() as cur:
                    cur.execute(UPSERT_SQL, row)
                conn.commit()
                success += 1
            except Exception as e:
                conn.rollback()
                print(f"  ERROR UPSERT chain={row['chain']}: {e}", file=sys.stderr)
                fail += 1
                continue

        print(f"\n{'=' * 60}")
        print(f"写入完成: success={success}, fail={fail}")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
