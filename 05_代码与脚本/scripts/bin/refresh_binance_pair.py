"""刷新「币安可交易池」映射表 biz.asset_binance_pair。

每日变化榜的池子已从 CMC rank<=1000 全市场改为币安 USDT-M 永续合约可交易池，
本脚本负责把币安合约 symbol 映射到 core.asset.asset_id。

归一化规则（见 fix_095_asset_binance_pair.sql）：
    BTCUSDT        → BTC          (exact)
    1000PEPEUSDT   → PEPE         (strip，去掉币安合约的 1000/1000000 倍数前缀)
    1000000BOBUSDT → BOB          (strip)

core.asset.canonical_symbol 有重复（22648 行 / 19442 唯一），故同一 base_symbol 命中多行时
按「优先非 Bridged/Wrapped/Peg、其次 market_cap_rank 最小」取一条，避免裸 join 扇出。

用法：
    python bin/refresh_binance_pair.py              # 刷新（近 30 天活跃的合约）
    python bin/refresh_binance_pair.py --days 7     # 只取近 7 天活跃
    python bin/refresh_binance_pair.py --dry-run    # 预览，不写库
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

MULT_PREFIX = re.compile(r"^(1000000|1000)")
QUOTE_SUFFIX = "USDT"

POOL_SQL = """
-- 池子 = 库内所有 1d 合约（榜单实际消费的周期，全历史覆盖）
--        ∪ 近 N 天活跃的 1h 合约（新上架合约在 1d 回填前先靠 1h 兜住）
SELECT DISTINCT symbol FROM biz.asset_klines WHERE interval = '1d'
UNION
SELECT DISTINCT symbol FROM biz.asset_klines
WHERE interval = '1h' AND open_time > now() - make_interval(days => %s)
"""

ASSET_SQL = """
SELECT UPPER(canonical_symbol) AS sym, asset_id, market_cap_rank, canonical_name
FROM core.asset
WHERE canonical_symbol IS NOT NULL
"""

UPSERT_SQL = """
INSERT INTO biz.asset_binance_pair (symbol, asset_id, base_symbol, match_kind, updated_at)
VALUES (%s, %s, %s, %s, NOW())
ON CONFLICT (symbol) DO UPDATE SET
    asset_id    = EXCLUDED.asset_id,
    base_symbol = EXCLUDED.base_symbol,
    match_kind  = EXCLUDED.match_kind,
    updated_at  = NOW()
"""


def split_prefix(symbol: str) -> tuple[str, bool]:
    """拆出基础符号，返回 (base_symbol, 是否去掉了倍数前缀)。"""
    s = symbol.upper()
    if s.endswith(QUOTE_SUFFIX):
        s = s[: -len(QUOTE_SUFFIX)]
    stripped = MULT_PREFIX.sub("", s)
    return stripped, stripped != s


def pick_best(cands: list[tuple[int, int | None, str | None]]) -> tuple[int, int | None]:
    """同 base_symbol 的多个候选里挑一条：(asset_id, market_cap_rank)。"""
    def key(c: tuple[int, int | None, str | None]):
        name = (c[2] or "").upper()
        noisy = any(t in name for t in ("BRIDGED", "WRAPPED", "PEG "))
        rank = c[1] if c[1] is not None else 10**9
        return (noisy, rank, c[0])

    best = min(cands, key=key)
    return best[0], best[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新币安可交易池映射")
    parser.add_argument("--days", type=int, default=30, help="取近 N 天活跃的合约（默认 30）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(POOL_SQL, (args.days,))
            pool = sorted(r[0] for r in cur.fetchall())

            cur.execute(ASSET_SQL)
            assets: dict[str, list[tuple[int, int | None, str | None]]] = {}
            for sym, aid, rank, name in cur.fetchall():
                assets.setdefault(sym, []).append((int(aid), rank, name))

        if not pool:
            print("池子为空：biz.asset_klines 近 %d 天没有 1h 数据" % args.days)
            return 1

        rows: list[tuple[str, int | None, str, str]] = []
        for symbol in pool:
            base, was_stripped = split_prefix(symbol)
            cands = assets.get(base)
            if cands:
                aid, _rank = pick_best(cands)
                rows.append((symbol, aid, base, "strip" if was_stripped else "exact"))
            else:
                rows.append((symbol, None, base, "none"))

        matched = [r for r in rows if r[1] is not None]
        unmatched = [r for r in rows if r[1] is None]

        if not args.dry_run:
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, rows)
            conn.commit()

        print(f"池子 {len(rows)} 个合约；映射成功 {len(matched)}（{100*len(matched)/len(rows):.1f}%）"
              f"，未映射 {len(unmatched)}")
        kinds: dict[str, int] = {}
        for r in rows:
            kinds[r[3]] = kinds.get(r[3], 0) + 1
        print("match_kind 分布：", dict(sorted(kinds.items(), key=lambda x: -x[1])))
        if unmatched:
            print(f"\n未映射（币安有合约、core.asset 无对应资产，已排除出榜单）：")
            for symbol, _, base, _ in unmatched:
                print(f"   {symbol:<22s} → {base}")
        if args.dry_run:
            print("\n[dry-run] 未写库")

    return 0


if __name__ == "__main__":
    sys.exit(main())