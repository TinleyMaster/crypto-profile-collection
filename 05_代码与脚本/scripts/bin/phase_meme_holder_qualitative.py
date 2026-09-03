#!/usr/bin/env python3
"""定性持仓活跃度落库：遍历「有 transfer_log 但无 holder_snapshot」的资产×链 → 聚合 → UPSERT。

用法：
    python phase_meme_holder_qualitative.py --limit 100
    python phase_meme_holder_qualitative.py --chain polygon
    python phase_meme_holder_qualitative.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from contextlib import contextmanager

SCRIPT_DIR = Path(__file__).resolve().parent
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

UPSERT_SQL = """
INSERT INTO biz.asset_holder_qualitative
    (asset_id, chain, activity_level, tx_n, active_addrs, cex_in_ratio, source, computed_at)
VALUES (
    %(asset_id)s, %(chain)s, %(activity_level)s, %(tx_n)s, %(active_addrs)s,
    %(cex_in_ratio)s, %(source)s, NOW()
)
ON CONFLICT (asset_id, chain) DO UPDATE SET
    activity_level = EXCLUDED.activity_level,
    tx_n           = EXCLUDED.tx_n,
    active_addrs   = EXCLUDED.active_addrs,
    cex_in_ratio   = EXCLUDED.cex_in_ratio,
    source         = EXCLUDED.source,
    computed_at    = EXCLUDED.computed_at
"""

UPSERT_KEYS = {"asset_id", "chain", "activity_level", "tx_n", "active_addrs", "cex_in_ratio", "source"}


@contextmanager
def _get_db():
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        yield conn


def query_target_assets(conn, limit: int, chain: str | None = None) -> list[tuple[int, str]]:
    """查询「有 transfer_log 但无 holder_snapshot」的 (asset_id, chain) 对。"""
    with conn.cursor() as cur:
        sql = """
            SELECT DISTINCT t.asset_id, t.chain
            FROM biz.onchain_transfer_log t
            WHERE NOT EXISTS (
                SELECT 1 FROM biz.onchain_holder_snapshot s
                WHERE s.asset_id = t.asset_id AND s.chain = t.chain
            )
        """
        params: list = []
        if chain:
            sql += " AND t.chain = %s"
            params.append(chain)
        sql += " ORDER BY t.asset_id, t.chain LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def aggregate_transfers(conn, asset_id: int, chain: str) -> dict | None:
    """聚合单资产×链的 transfer-log 活跃度。"""
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=30)
    sql = """
        SELECT COUNT(*)                                              AS tx_n,
               COUNT(DISTINCT from_address) + COUNT(DISTINCT to_address) AS active_addrs,
               AVG(is_to_exchange::int)                              AS cex_in_ratio
        FROM biz.onchain_transfer_log
        WHERE asset_id = %s AND chain = %s AND block_timestamp >= %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (asset_id, chain, cutoff))
        row = cur.fetchone()
    if not row or (row[0] or 0) == 0:
        return None
    tx_n = row[0] or 0
    active_addrs = row[1] or 0
    cex_ratio = float(row[2]) if row[2] is not None else 0.0
    if tx_n >= 500:
        level = "high"
    elif tx_n >= 100:
        level = "mid"
    else:
        level = "low"
    return {
        "asset_id": asset_id,
        "chain": chain,
        "activity_level": level,
        "tx_n": tx_n,
        "active_addrs": active_addrs,
        "cex_in_ratio": round(cex_ratio, 3),
        "source": "transfer_log",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="定性持仓活跃度落库（transfer-log 估算）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理多少个资产×链组合")
    parser.add_argument("--chain", type=str, default=None, help="只处理指定链")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    with _get_db() as conn:
        targets = query_target_assets(conn, limit=args.limit, chain=args.chain)
        print(f"待处理资产×链: {len(targets)}")

        if not targets:
            print("无数据可处理")
            return 0

        processed = 0
        t0 = time.time()

        for i, (aid, chain) in enumerate(targets, 1):
            try:
                result = aggregate_transfers(conn, aid, chain)
            except Exception as e:
                print(f"  [{i}/{len(targets)}] asset_id={aid} chain={chain} ERROR: {e}", file=sys.stderr)
                continue

            if result is None:
                print(f"  [{i}/{len(targets)}] asset_id={aid} chain={chain} → 无数据")
                continue

            level = result["activity_level"]
            print(f"  [{i}/{len(targets)}] asset_id={aid} chain={chain} → {level} (tx={result['tx_n']}, addrs={result['active_addrs']})")

            if args.dry_run:
                processed += 1
                continue

            # 占位符核对（铁律）：补完 phase 键后再核
            missing = UPSERT_KEYS - set(result.keys())
            if missing:
                print(f"  ERROR: 缺失占位符 {missing}", file=sys.stderr)
                continue

            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, result)
            conn.commit()
            processed += 1

        elapsed = time.time() - t0
        print(f"\n{'=' * 60}")
        print(f"落库完成: {processed} 资产×链, 耗时 {elapsed:.1f}s")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
