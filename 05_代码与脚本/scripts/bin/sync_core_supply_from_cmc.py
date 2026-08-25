"""以 CMC 权威快照为基准，同步 / 修正 core.asset 主表的 supply 与市值字段。

两种模式：
  --sync     全量同步（默认）：对所有有 CMC 映射且有最新快照的资产，
             用 CMC 值覆盖 core.asset 的 circulating_supply / total_supply /
             market_cap / fdv / market_cap_rank / ath_usd，确保主表与权威源一致。
  --fix      修复模式：只修偏离 >10x 的脏数据（用于历史脏数据清理）。

背景：
  core.asset 的 supply 字段由 CG 导入时写入，存在两类问题：
  1. 跨源口径漂移：CG 与 CMC 对流通量的定义/估算不同，长期不同步
  2. 长尾缩放错误：部分 meme/长尾币 CG 返回的是原始基础单位（含 decimals），
     未做 /10^decimals 缩放，导致数量级差 6~15 个数量级

  本脚本以 CMC 为权威源，统一主表口径。

用法：
  python sync_core_supply_from_cmc.py --sync --dry-run   # 预览全量同步
  python sync_core_supply_from_cmc.py --sync             # 执行全量同步
  python sync_core_supply_from_cmc.py --fix --ratio 10   # 只修偏离 >10x 的
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
        description="Sync core.asset supply/market fields from CMC snapshot."
    )
    parser.add_argument("--sync", action="store_true",
                        help="全量同步：用 CMC 最新快照覆盖所有有映射的资产")
    parser.add_argument("--fix", action="store_true",
                        help="修复模式：只修偏离 >ratio 倍的脏数据")
    parser.add_argument("--ratio", type=float, default=10.0,
                        help="修复模式的偏离倍数阈值，默认 10")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预览，不写库")
    return parser


def _get_latest_snapshot_sql() -> str:
    """取每个 cmc_id 的最新快照。"""
    return """
        SELECT DISTINCT ON (cmc_id)
               cmc_id,
               price_usd,
               market_cap,
               fdv,
               total_supply,
               circulating_supply,
               cmc_rank,
               ath,
               quote_time
        FROM src_cmc.cmc_asset_quote_snapshot
        WHERE cmc_id IS NOT NULL
        ORDER BY cmc_id, quote_time DESC
    """


def collect_sync_rows(conn) -> list[dict]:
    """全量同步：返回所有有 CMC 主映射且有最新快照的资产。"""
    sql = f"""
        WITH latest AS ({_get_latest_snapshot_sql()})
        SELECT a.asset_id, a.canonical_symbol,
               a.market_cap_rank, a.market_cap, a.fdv,
               a.circulating_supply AS asset_cs,
               a.total_supply AS asset_ts,
               a.ath_usd,
               l.cmc_rank, l.market_cap AS cmc_market_cap,
               l.fdv AS cmc_fdv,
               l.circulating_supply AS cmc_cs,
               l.total_supply AS cmc_ts,
               l.ath AS cmc_ath,
               l.price_usd AS cmc_price,
               l.quote_time
        FROM core.asset a
        JOIN core.asset_source_map m
          ON m.asset_id = a.asset_id
         AND m.source_code = 'cmc'
         AND m.is_primary = TRUE
        JOIN latest l ON l.cmc_id = m.source_asset_key::bigint
        ORDER BY COALESCE(l.cmc_rank, 999999), a.asset_id
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql)
        return cur.fetchall()


def collect_fix_rows(conn, ratio: float) -> list[dict]:
    """修复模式：返回 supply 偏离 >ratio 倍的资产。"""
    sql = f"""
        WITH latest AS ({_get_latest_snapshot_sql()})
        SELECT a.asset_id, a.canonical_symbol,
               a.total_supply AS asset_ts, l.total_supply AS cmc_ts,
               a.circulating_supply AS asset_cs, l.circulating_supply AS cmc_cs,
               CASE WHEN a.total_supply IS NOT NULL AND a.total_supply > 0
                    AND l.total_supply IS NOT NULL AND l.total_supply > 0
                    THEN GREATEST(a.total_supply, l.total_supply)
                       / LEAST(a.total_supply, l.total_supply)
                    ELSE NULL END AS ts_ratio,
               CASE WHEN a.circulating_supply IS NOT NULL AND a.circulating_supply > 0
                    AND l.circulating_supply IS NOT NULL AND l.circulating_supply > 0
                    THEN GREATEST(a.circulating_supply, l.circulating_supply)
                       / LEAST(a.circulating_supply, l.circulating_supply)
                    ELSE NULL END AS cs_ratio
        FROM core.asset a
        JOIN core.asset_source_map m
          ON m.asset_id = a.asset_id
         AND m.source_code = 'cmc'
         AND m.is_primary = TRUE
        JOIN latest l ON l.cmc_id = m.source_asset_key::bigint
        WHERE (a.total_supply IS NOT NULL AND a.total_supply > 0
               AND l.total_supply IS NOT NULL AND l.total_supply > 0
               AND GREATEST(a.total_supply, l.total_supply)
                   / LEAST(a.total_supply, l.total_supply) > %(ratio)s)
           OR (a.circulating_supply IS NOT NULL AND a.circulating_supply > 0
               AND l.circulating_supply IS NOT NULL AND l.circulating_supply > 0
               AND GREATEST(a.circulating_supply, l.circulating_supply)
                   / LEAST(a.circulating_supply, l.circulating_supply) > %(ratio)s)
        ORDER BY GREATEST(ts_ratio, cs_ratio) DESC NULLS LAST
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, {"ratio": ratio})
        return cur.fetchall()


def _fmt_num(v) -> str:
    if v is None:
        return "NULL"
    try:
        f = float(v)
        if abs(f) >= 1e12:
            return f"{f:.2e}"
        if abs(f) >= 1e6:
            return f"{f/1e6:.1f}M"
        if abs(f) >= 1e3:
            return f"{f/1e3:.1f}K"
        return f"{f:.2f}"
    except (TypeError, ValueError):
        return str(v)


def do_sync(conn, rows: list[dict], dry_run: bool) -> int:
    """全量同步：用 CMC 快照覆盖 core.asset 字段。"""
    upd = """
        UPDATE core.asset
        SET market_cap_rank = %s,
            market_cap = %s,
            fdv = %s,
            circulating_supply = %s,
            total_supply = %s,
            max_supply = %s,
            ath_usd = %s,
            last_ranked_at = %s,
            updated_at = NOW()
        WHERE asset_id = %s
    """
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            if dry_run:
                continue
            cur.execute(upd, (
                r["cmc_rank"],
                r["cmc_market_cap"],
                r["cmc_fdv"],
                r["cmc_cs"],
                r["cmc_ts"],
                None,  # max_supply 快照表无此字段
                r["cmc_ath"],
                r["quote_time"],
                r["asset_id"],
            ))
            n += 1
    return n


def do_fix(conn, rows: list[dict], ratio: float, dry_run: bool) -> int:
    """修复模式：只修偏离 >ratio 倍的 supply 字段。"""
    upd = """
        UPDATE core.asset
        SET total_supply = %s,
            circulating_supply = %s,
            updated_at = NOW()
        WHERE asset_id = %s
    """
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            new_ts = r["cmc_ts"] if (r["ts_ratio"] and r["ts_ratio"] > ratio) else r["asset_ts"]
            new_cs = r["cmc_cs"] if (r["cs_ratio"] and r["cs_ratio"] > ratio) else r["asset_cs"]
            if dry_run:
                continue
            cur.execute(upd, (new_ts, new_cs, r["asset_id"]))
            n += 1
    return n


def main() -> int:
    args = build_parser().parse_args()

    if not (args.sync or args.fix):
        print("请指定 --sync 或 --fix 模式。用 -h 查看帮助。")
        return 1

    from crypto_research.config import get_settings  # noqa: E402
    from crypto_research.db.conn import get_connection  # noqa: E402

    settings = get_settings(require_database=not args.dry_run)
    with get_connection(settings.database_url) as conn:
        if args.sync:
            rows = collect_sync_rows(conn)
            print(f"[SYNC] 待同步资产数：{len(rows)}（有 CMC 主映射 + 有最新快照）")
            if rows:
                print(f"  Top 5: " + ", ".join(
                    f"{r['canonical_symbol']}(#{r['cmc_rank'] or '?'})"
                    for r in rows[:5]
                ))
            if args.dry_run:
                print("（dry-run，未执行）")
                return 0
            n = do_sync(conn, rows, dry_run=False)
            conn.commit()
            print(f"[SYNC] 已同步 {n} 个资产的 supply / 市值 / rank 字段。")

        if args.fix:
            rows = collect_fix_rows(conn, args.ratio)
            if not rows:
                print(f"[FIX] 无偏离 >{args.ratio}x 的记录。")
                return 0
            print(f"[FIX] 偏离 >{args.ratio}x 的资产：{len(rows)} 个")
            for r in rows[:20]:
                fixes = []
                if r["ts_ratio"] and r["ts_ratio"] > args.ratio:
                    fixes.append(
                        f"total {_fmt_num(r['asset_ts'])}→{_fmt_num(r['cmc_ts'])} "
                        f"({r['ts_ratio']:.0f}x)"
                    )
                if r["cs_ratio"] and r["cs_ratio"] > args.ratio:
                    fixes.append(
                        f"circ {_fmt_num(r['asset_cs'])}→{_fmt_num(r['cmc_cs'])} "
                        f"({r['cs_ratio']:.0f}x)"
                    )
                print(f"  {r['canonical_symbol']:12s} asset_id={r['asset_id']:<6d}  "
                      + "; ".join(fixes))
            if len(rows) > 20:
                print(f"  ... 还有 {len(rows) - 20} 个")
            if args.dry_run:
                print("\n（dry-run，加 --apply 执行修正）")
                return 0
            n = do_fix(conn, rows, args.ratio, dry_run=False)
            conn.commit()
            print(f"\n[FIX] 已修正 {n} 个资产的 supply。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
