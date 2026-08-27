"""以 CMC 权威快照为基准，修正 core.asset 主表的 supply 单位/量级污染。

背景：
  core.asset.total_supply / circulating_supply 存在"量级错误"（如 LOAN：
  主表 5.5e12 vs CMC 3.65e10，偏离 150x）。这与 biz.asset_tokenomics 的
  validate_supply_units 是两回事——后者只校验 tokenomics 表，不碰 core.asset。
  本脚本专门修主表。

逻辑：
  仅对"有 primary CMC 映射 且 有可用 CMC 快照"的资产做比对（缺快照的一律不动，
  避免误清）。对 total_supply / circulating_supply 中偏离 >10x 的字段，用 CMC
  权威值覆盖，并记录修正前后值，便于复核。

用法：
  python fix_core_asset_supply_from_cmc.py            # dry-run，仅列出将修正项
  python fix_core_asset_supply_from_cmc.py --apply    # 执行修正
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fix core.asset supply contamination against CMC snapshot."
    )
    parser.add_argument("--apply", action="store_true", help="执行修正（默认仅 dry-run）")
    parser.add_argument("--ratio", type=float, default=10.0, help="偏离倍数阈值，默认 10")
    return parser


def collect(conn, ratio: float) -> list[dict]:
    """返回所有（有快照时）偏离 >ratio 倍的主表 supply 记录。"""
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (cmc_id) cmc_id,
                   total_supply AS cmc_ts,
                   circulating_supply AS cmc_cs
            FROM src_cmc.cmc_asset_quote_snapshot
            WHERE total_supply IS NOT NULL AND total_supply > 0
            ORDER BY cmc_id, quote_time DESC
        )
        SELECT * FROM (
            SELECT a.asset_id, a.canonical_symbol,
                   a.total_supply AS asset_ts, l.cmc_ts,
                   a.circulating_supply AS asset_cs, l.cmc_cs,
                   CASE WHEN a.total_supply IS NOT NULL AND a.total_supply > 0
                        THEN GREATEST(a.total_supply, l.cmc_ts)/LEAST(a.total_supply, l.cmc_ts)
                        ELSE NULL END AS ts_ratio,
                   CASE WHEN a.circulating_supply IS NOT NULL AND a.circulating_supply > 0
                        THEN GREATEST(a.circulating_supply, l.cmc_cs)/LEAST(a.circulating_supply, l.cmc_cs)
                        ELSE NULL END AS cs_ratio
            FROM core.asset a
            JOIN core.asset_source_map m ON m.asset_id=a.asset_id
                AND m.source_code='cmc' AND m.is_primary=TRUE
            JOIN latest l ON l.cmc_id = m.source_asset_key::bigint
            WHERE (a.total_supply IS NOT NULL AND a.total_supply > 0
                   AND (GREATEST(a.total_supply, l.cmc_ts)/LEAST(a.total_supply, l.cmc_ts)) > %(ratio)s)
               OR (a.circulating_supply IS NOT NULL AND a.circulating_supply > 0
                   AND (GREATEST(a.circulating_supply, l.cmc_cs)/LEAST(a.circulating_supply, l.cmc_cs)) > %(ratio)s)
        ) sub
        ORDER BY GREATEST(ts_ratio, cs_ratio) DESC NULLS LAST
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, {"ratio": ratio})
        return cur.fetchall()


def main() -> int:
    args = build_parser().parse_args()
    ratio = args.ratio

    from crypto_research.config import get_settings  # noqa: E402
    from crypto_research.db.conn import get_connection  # noqa: E402

    settings = get_settings(require_database=not args.apply)
    with get_connection(settings.database_url) as conn:
        rows = collect(conn, ratio)
        if not rows:
            print(f"[OK] 无偏离 >{ratio}x 的主表 supply 记录（仅统计有快照的资产）。")
            return 0

        print(f"[DRY-RUN] 将修正 {len(rows)} 个资产（偏离 >{ratio}x，基准=CMC 快照）：")
        for r in rows:
            fixes = []
            if r["ts_ratio"] and r["ts_ratio"] > ratio:
                fixes.append(f"total_supply {r['asset_ts']:g}→{r['cmc_ts']:g} ({r['ts_ratio']:.1f}x)")
            if r["cs_ratio"] and r["cs_ratio"] > ratio:
                fixes.append(f"circulating_supply {r['asset_cs']:g}→{r['cmc_cs']:g} ({r['cs_ratio']:.1f}x)")
            print(f"  {r['canonical_symbol']:10s} asset_id={r['asset_id']}  " + "; ".join(fixes))

        if not args.apply:
            print("\n（dry-run 结束，加 --apply 执行修正）")
            return 0

        # 执行：逐字段覆盖（写入前做数据质量校验）
        upd = """
            UPDATE core.asset
            SET total_supply = %(cmc_ts)s,
                circulating_supply = %(cmc_cs)s,
                updated_at = NOW()
            WHERE asset_id = %(asset_id)s
        """
        n = 0
        for r in rows:
            new_ts = r["cmc_ts"] if (r["ts_ratio"] and r["ts_ratio"] > ratio) else r["asset_ts"]
            new_cs = r["cmc_cs"] if (r["cs_ratio"] and r["cs_ratio"] > ratio) else r["asset_cs"]

            # 语义修正：0 改为 NULL
            if new_cs is not None and new_cs == 0:
                new_cs = None
            if new_ts is not None and new_ts == 0:
                new_ts = None

            # 内部一致性：circulating <= total
            if (new_cs is not None and new_ts is not None
                    and new_cs > new_ts
                    and new_ts > 0):
                new_ts = new_cs

            params = {
                "asset_id": r["asset_id"],
                "cmc_ts": new_ts,
                "cmc_cs": new_cs,
            }
            with conn.cursor() as cur:
                cur.execute(upd, params)
            n += 1
        conn.commit()
        print(f"\n[APPLY] 已修正 {n} 个资产的主表 supply。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
