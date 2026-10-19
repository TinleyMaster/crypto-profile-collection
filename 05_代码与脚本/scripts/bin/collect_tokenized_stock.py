#!/usr/bin/env python3
"""
代币化股票「linker 阶段」历史回扫 —— 把被归属闸门挡下的催化剂落分离表。

背景（2026-10-19）：
    `biz.catalyst_tokenized_stock` 有两个 stage：
      - `source_filter`：源侧闸门拦掉的公告（公告从未入库）— 由 ingest 自动写入；
      - `linker_block` ：公告**已在** `biz.asset_catalyst`，但它的交易对全部指向
        tokenized stock（xStock / bStocks / PreStocks / Dinari / 商品衍生），
        被 `_NON_CRYPTO_NAME_SQL` 挡住、拿不到 asset_id —— **本脚本负责回扫补齐**。

判定口径（与 linker 严格对齐）：
    symbol 在 `core.asset` 里的**全部**行都命中 tokenized 名称正则 ⇒ 该 symbol 是
    代币化股票标的 ⇒ 提到它的催化剂即 stage=linker_block。
    （linker 的 `_NON_CRYPTO_NAME_SQL` 是加在同一批资产查询上的，故口径一致。）

用法：
    python collect_tokenized_stock.py                  # 只读统计（默认）
    python collect_tokenized_stock.py --save           # 写入分离表
    python collect_tokenized_stock.py --save --limit 5000
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from catalyst.db import get_conn                      # noqa: E402
from catalyst.linker import extract_base_symbol, _CANONICAL_TOP_SYMBOLS  # noqa: E402
from catalyst.tokenized import (                       # noqa: E402
    STRONG_ASSET_NAME_SQL, build_row, classify_title,
    classify_asset_name, persist,
)


def load_tokenized_symbols(conn) -> dict[str, str]:
    """返回 {SYMBOL: kind} —— 该 symbol 的全部资产行都是代币化/衍生。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT UPPER(canonical_symbol) AS sym,
                   min(canonical_name)     AS sample_name,
                   count(*)                AS n_total,
                   count(*) FILTER (
                       WHERE LOWER(COALESCE(canonical_name, '')) ~ %(rx)s
                   )                       AS n_tokenized
            FROM core.asset
            WHERE canonical_symbol IS NOT NULL AND canonical_symbol <> ''
            GROUP BY 1
            HAVING count(*) = count(*) FILTER (
                       WHERE LOWER(COALESCE(canonical_name, '')) ~ %(rx)s
                   )
            """,
            {"rx": STRONG_ASSET_NAME_SQL},
        )
        rows = cur.fetchall()
    return {r["sym"]: (classify_asset_name(r["sample_name"], strong_only=True)
                       or "tokenized_stock")
            for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="写入 biz.catalyst_tokenized_stock")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条催化剂（0=全部）")
    args = ap.parse_args()

    print("=" * 68)
    print("代币化股票 · linker 阶段历史回扫")
    print("=" * 68)

    with get_conn() as conn:
        tok_sym = load_tokenized_symbols(conn)
        print(f"纯代币化/衍生 symbol: {len(tok_sym)} 个"
              f"（示例 {', '.join(list(tok_sym)[:8])}）")

        sql = """
            SELECT c.catalyst_id, c.source_code, c.source_article_id, c.title,
                   c.published_at, c.source_url, c.related_pairs
            FROM biz.asset_catalyst c
            WHERE c.related_pairs IS NOT NULL AND cardinality(c.related_pairs) > 0
            ORDER BY c.catalyst_id
        """
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        with conn.cursor() as cur:
            cur.execute(sql)
            cats = cur.fetchall()
        print(f"扫描催化剂（带交易对）: {len(cats)} 条")

        rows: list[dict] = []
        kind_stat: collections.Counter = collections.Counter()
        sym_stat: collections.Counter = collections.Counter()

        for c in cats:
            hits: list[tuple[str, str]] = []
            for pair in c["related_pairs"] or []:
                base = extract_base_symbol(pair)
                if not base or base in _CANONICAL_TOP_SYMBOLS:
                    continue
                kind = tok_sym.get(base)
                if kind:
                    hits.append((base, kind))
            if not hits:
                continue

            title_verdict = classify_title(c["title"] or "")
            kind = hits[0][1]
            if title_verdict:
                kind, reason = title_verdict
            else:
                reason = f"交易对指向代币化标的: " + ", ".join(sorted({b for b, _ in hits}))

            rows.append(build_row(
                stage="linker_block",
                source_code=c["source_code"] or "",
                source_item_id=c["source_article_id"] or str(c["catalyst_id"]),
                title=c["title"] or "",
                published_at=c["published_at"],
                source_url=c["source_url"] or "",
                symbols=sorted({b for b, _ in hits}),
                asset_class=kind,
                detect_reason=reason,
                catalyst_id=c["catalyst_id"],
                raw={"related_pairs": c["related_pairs"]},
            ))
            kind_stat[kind] += 1
            for b, _ in hits:
                sym_stat[b] += 1

        print(f"\n命中催化剂: {len(rows)} 条")
        if kind_stat:
            print("按类别:")
            for k, n in kind_stat.most_common():
                print(f"  {k:<22} {n}")
            print("按标的 top15:")
            for s, n in sym_stat.most_common(15):
                print(f"  {s:<10} {n}")

        if args.save and rows:
            n = persist(conn, rows)
            print(f"\n✅ 已写入 biz.catalyst_tokenized_stock: {n} 条（stage=linker_block）")
        elif rows:
            print("\n（只读模式，未写库；加 --save 写入分离表）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
