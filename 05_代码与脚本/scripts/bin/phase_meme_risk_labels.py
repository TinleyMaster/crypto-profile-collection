#!/usr/bin/env python3
"""Meme 五维风险标签计算：遍历有任一轴输入的 asset_id → 评分 → UPSERT。

用法：
    python phase_meme_risk_labels.py --limit 100
    python phase_meme_risk_labels.py --dry-run
    python phase_meme_risk_labels.py --asset-id 1189
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
INSERT INTO biz.asset_risk_labels (
    asset_id, contract_score, contract_label,
    liquidity_score, liquidity_label,
    holder_score, holder_label,
    lifecycle_score, lifecycle_label,
    social_score, social_label,
    axes_computed, total_score, risk_label, flags, detail, computed_at
) VALUES (
    %(asset_id)s, %(contract_score)s, %(contract_label)s,
    %(liquidity_score)s, %(liquidity_label)s,
    %(holder_score)s, %(holder_label)s,
    %(lifecycle_score)s, %(lifecycle_label)s,
    %(social_score)s, %(social_label)s,
    %(axes_computed)s, %(total_score)s, %(risk_label)s, %(flags)s,
    %(detail)s::jsonb, NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    contract_score = EXCLUDED.contract_score, contract_label = EXCLUDED.contract_label,
    liquidity_score = EXCLUDED.liquidity_score, liquidity_label = EXCLUDED.liquidity_label,
    holder_score = EXCLUDED.holder_score, holder_label = EXCLUDED.holder_label,
    lifecycle_score = EXCLUDED.lifecycle_score, lifecycle_label = EXCLUDED.lifecycle_label,
    social_score = EXCLUDED.social_score, social_label = EXCLUDED.social_label,
    axes_computed = EXCLUDED.axes_computed, total_score = EXCLUDED.total_score,
    risk_label = EXCLUDED.risk_label, flags = EXCLUDED.flags, detail = EXCLUDED.detail,
    computed_at = NOW()
"""

UPSERT_KEYS = {
    "asset_id", "contract_score", "contract_label",
    "liquidity_score", "liquidity_label",
    "holder_score", "holder_label",
    "lifecycle_score", "lifecycle_label",
    "social_score", "social_label",
    "axes_computed", "total_score", "risk_label", "flags", "detail",
}


@contextmanager
def _get_db():
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        yield conn


def query_target_assets(conn, limit: int, asset_id: int | None = None) -> list[int]:
    """查询有任一轴输入的 asset_id（非零覆盖优先）。"""
    with conn.cursor() as cur:
        if asset_id:
            return [asset_id]
        cur.execute("""
            SELECT DISTINCT a.asset_id
            FROM core.asset a
            WHERE a.status = 'active'
              AND (
                  EXISTS (SELECT 1 FROM biz.asset_contract_security WHERE asset_id = a.asset_id)
                  OR EXISTS (SELECT 1 FROM biz.asset_liquidity WHERE asset_id = a.asset_id)
                  OR EXISTS (SELECT 1 FROM biz.onchain_holder_snapshot WHERE asset_id = a.asset_id)
                  OR EXISTS (SELECT 1 FROM core.asset WHERE asset_id = a.asset_id AND launch_date IS NOT NULL)
                  OR EXISTS (SELECT 1 FROM biz.kol_signal WHERE asset_id = a.asset_id)
                  OR EXISTS (SELECT 1 FROM biz.asset_github_repo WHERE asset_id = a.asset_id)
              )
            ORDER BY a.asset_id
            LIMIT %s
        """, (limit,))
        return [r[0] for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Meme 五维风险标签计算")
    parser.add_argument("--limit", type=int, default=100, help="扫描资产数量上限")
    parser.add_argument("--asset-id", type=int, default=None, help="单资产 ID")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "workbench"))
    from meme_risk import compute_meme_risk

    with _get_db() as conn:
        assets = query_target_assets(conn, limit=args.limit, asset_id=args.asset_id)
        print(f"待评分资产: {len(assets)}")

        if not assets:
            print("无资产可评分")
            return 0

        processed = 0
        block_count = 0
        t0 = time.time()

        for i, aid in enumerate(assets, 1):
            try:
                result = compute_meme_risk(aid, conn)
            except Exception as e:
                print(f"  [{i}/{len(assets)}] asset_id={aid} ERROR: {e}", file=sys.stderr)
                continue

            label = result.get("risk_label", "?")
            score = result.get("total_score", "?")
            flags = result.get("flags") or []
            flag_str = " | ".join(flags[:3]) if flags else ""
            print(f"  [{i}/{len(assets)}] asset_id={aid} → {label} ({score}) {flag_str}")

            if label == "block":
                block_count += 1

            if args.dry_run:
                processed += 1
                continue

            # 占位符 − dict keys 核对（新铁律）
            missing = UPSERT_KEYS - set(result.keys())
            if missing:
                print(f"  ERROR: 缺失占位符 {missing}", file=sys.stderr)
                continue

            # detail 序列化
            result["detail"] = json.dumps({
                "contract": {"score": result["contract_score"], "flags": []},
                "liquidity": {"score": result["liquidity_score"], "flags": []},
                "holder": {"score": result["holder_score"], "flags": []},
                "lifecycle": {"score": result["lifecycle_score"], "flags": []},
                "social": {"score": result["social_score"], "flags": []},
            }, ensure_ascii=False)

            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, result)
            conn.commit()
            processed += 1

        elapsed = time.time() - t0
        print(f"\n{'=' * 60}")
        print(f"评分完成: {processed} 资产, block={block_count}, 耗时 {elapsed:.1f}s")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
