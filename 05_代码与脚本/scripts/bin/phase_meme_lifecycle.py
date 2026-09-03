#!/usr/bin/env python3
"""Meme 四阶段生命周期分类：遍历 asset → 拉四轴输入 → classify → UPSERT。

用法：
    python phase_meme_lifecycle.py --limit 100
    python phase_meme_lifecycle.py --dry-run
    python phase_meme_lifecycle.py --asset-id 2
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
INSERT INTO biz.asset_lifecycle (
    asset_id, stage, age_days, liquidity_usd,
    holder_change_30d, social_score, proxy_used, computed_at, detail
) VALUES (
    %(asset_id)s, %(stage)s, %(age_days)s, %(liquidity_usd)s,
    %(holder_change_30d)s, %(social_score)s, %(proxy_used)s, NOW(), %(detail)s::jsonb
)
ON CONFLICT (asset_id) DO UPDATE SET
    stage = EXCLUDED.stage, age_days = EXCLUDED.age_days,
    liquidity_usd = EXCLUDED.liquidity_usd, holder_change_30d = EXCLUDED.holder_change_30d,
    social_score = EXCLUDED.social_score, proxy_used = EXCLUDED.proxy_used,
    computed_at = NOW(), detail = EXCLUDED.detail
"""

UPSERT_KEYS = {
    "asset_id", "stage", "age_days", "liquidity_usd",
    "holder_change_30d", "social_score", "proxy_used", "detail",
}


@contextmanager
def _get_db():
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        yield conn


def query_target_assets(conn, limit: int, asset_id: int | None = None) -> list[int]:
    """查询有任一轴输入的 asset_id。"""
    with conn.cursor() as cur:
        if asset_id:
            return [asset_id]
        cur.execute("""
            SELECT DISTINCT a.asset_id
            FROM core.asset a
            WHERE a.status = 'active'
              AND (
                  a.launch_date IS NOT NULL
                  OR EXISTS (SELECT 1 FROM biz.asset_liquidity WHERE asset_id = a.asset_id)
                  OR EXISTS (SELECT 1 FROM biz.onchain_holder_snapshot WHERE asset_id = a.asset_id)
                  OR EXISTS (SELECT 1 FROM biz.kol_signal WHERE asset_id = a.asset_id)
                  OR EXISTS (SELECT 1 FROM biz.asset_github_repo WHERE asset_id = a.asset_id)
              )
            ORDER BY a.asset_id
            LIMIT %s
        """, (limit,))
        return [r[0] for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Meme 四阶段生命周期分类")
    parser.add_argument("--limit", type=int, default=100, help="扫描资产数量上限")
    parser.add_argument("--asset-id", type=int, default=None, help="单资产 ID")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "workbench"))
    from meme_lifecycle import compute_lifecycle

    with _get_db() as conn:
        assets = query_target_assets(conn, limit=args.limit, asset_id=args.asset_id)
        print(f"待分类资产: {len(assets)}")

        if not assets:
            print("无资产可分类")
            return 0

        processed = 0
        stage_counts: dict[str, int] = {}
        t0 = time.time()

        for i, aid in enumerate(assets, 1):
            try:
                result = compute_lifecycle(aid, conn)
            except Exception as e:
                print(f"  [{i}/{len(assets)}] asset_id={aid} ERROR: {e}", file=sys.stderr)
                continue

            stage = result.get("stage", "?")
            age = result.get("age_days", "?")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            print(f"  [{i}/{len(assets)}] asset_id={aid} → {stage} (age={age}d)")

            if args.dry_run:
                processed += 1
                continue

            # detail 序列化（phase 负责补的键，必须在缺失核对前补齐）
            result["detail"] = json.dumps({
                "stage": result["stage"],
                "age_days": result["age_days"],
                "liquidity_usd": result["liquidity_usd"],
                "holder_change_30d": result["holder_change_30d"],
                "social_score": result["social_score"],
                "proxy_used": result["proxy_used"],
            }, ensure_ascii=False)

            # 占位符 − dict keys 核对（新铁律）：补完 phase 键后再核，避免误杀
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
        print(f"分类完成: {processed} 资产, 耗时 {elapsed:.1f}s")
        for stage, cnt in sorted(stage_counts.items(), key=lambda x: -x[1]):
            print(f"  {stage}: {cnt}")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
