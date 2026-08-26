"""
存量催化剂资产关联展开（linker 全量重算）。

问题：catalyst_asset_link 只有 legacy 单资产链接（12 条），
      多交易对公告未展开到 N:N 关联表。
修法：对所有催化剂重新跑 linker（related_pairs + 正文 cashtag 兜底），
      写入 catalyst_asset_link 表。

用法：
    python scripts/bin/backfill_catalyst_links.py [--dry-run] [--max-items 100]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workbench"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import db_stats  # noqa: E402
from catalyst.linker import map_pairs_to_asset_ids, extract_pairs_from_text  # noqa: E402


def fetch_all_catalysts(limit: int = 500, offset: int = 0) -> list[dict]:
    """获取所有催化剂（分批）。"""
    conn = db_stats.get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT catalyst_id, source_code, title, body_text, related_pairs
        FROM biz.asset_catalyst
        ORDER BY catalyst_id
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    cols = ["catalyst_id", "source_code", "title", "body_text", "related_pairs"]
    result = []
    for row in rows:
        d = dict(zip(cols, row))
        # related_pairs 可能是 list 或 str
        if isinstance(d["related_pairs"], str):
            try:
                d["related_pairs"] = json.loads(d["related_pairs"])
            except Exception:
                d["related_pairs"] = []
        elif d["related_pairs"] is None:
            d["related_pairs"] = []
        result.append(d)
    return result


def link_catalyst(cat: dict, conn) -> tuple[list[int], list[int], str]:
    """对单条催化剂做资产关联。

    Returns:
        (trading_pairs_asset_ids, cashtag_asset_ids, link_source_desc)
    """
    related_pairs = cat.get("related_pairs") or []
    body_text = cat.get("body_text") or ""
    title = cat.get("title") or ""

    # 第一路：related_pairs 官方标签（置信度高）
    pair_asset_ids = []
    if related_pairs:
        pair_asset_ids = map_pairs_to_asset_ids(
            related_pairs, conn, source_hint="binance"
        )

    # 第二路：正文 cashtag 兜底（置信度低）
    cashtag_asset_ids = []
    text_pairs = extract_pairs_from_text(title + "\n" + body_text)
    # 过滤掉已经通过 related_pairs 匹配到的
    if text_pairs:
        all_ids = map_pairs_to_asset_ids(text_pairs, conn, source_hint="binance")
        pair_set = set(pair_asset_ids)
        cashtag_asset_ids = [aid for aid in all_ids if aid not in pair_set]

    return pair_asset_ids, cashtag_asset_ids, ""


def insert_links(catalyst_id: int, asset_ids: list[int], link_source: str,
                 confidence: float, conn) -> int:
    """写入关联表，返回新增条数。"""
    if not asset_ids:
        return 0

    added = 0
    cur = conn.cursor()
    for aid in asset_ids:
        try:
            cur.execute(
                """
                INSERT INTO biz.catalyst_asset_link
                    (catalyst_id, asset_id, link_source, confidence)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (catalyst_id, asset_id) DO NOTHING
                """,
                (catalyst_id, aid, link_source, confidence),
            )
            if cur.rowcount > 0:
                added += 1
        except Exception as e:
            print(f"    写入关联失败 catalyst_id={catalyst_id}, asset_id={aid}: {e}")
    conn.commit()
    cur.close()
    return added


def main():
    parser = argparse.ArgumentParser(description="存量催化剂资产关联展开")
    parser.add_argument("--dry-run", action="store_true", help="只预览不修改")
    parser.add_argument("--max-items", type=int, default=0, help="最多处理条数（0=全部）")
    parser.add_argument("--batch-size", type=int, default=500, help="每批查询数量")
    args = parser.parse_args()

    total = 0
    total_pair_links = 0
    total_cashtag_links = 0
    total_no_link = 0
    offset = 0

    while True:
        batch = fetch_all_catalysts(args.batch_size, offset)
        if not batch:
            break

        print(f"\n处理第 {offset+1}-{offset+len(batch)} 条（共处理 {total} 条已完成）")

        conn = db_stats.get_db_conn()
        try:
            for cat in batch:
                if args.max_items and total >= args.max_items:
                    break

                cid = cat["catalyst_id"]
                pair_ids, cashtag_ids, _ = link_catalyst(cat, conn)

                n_pair = 0
                n_cash = 0
                if not args.dry_run:
                    n_pair = insert_links(cid, pair_ids, "trading_pairs", 0.95, conn)
                    n_cash = insert_links(cid, cashtag_ids, "cashtag", 0.6, conn)
                else:
                    n_pair = len(pair_ids)
                    n_cash = len(cashtag_ids)

                total_pair_links += n_pair
                total_cashtag_links += n_cash

                if not pair_ids and not cashtag_ids:
                    total_no_link += 1
                    status = "无关联"
                else:
                    parts = []
                    if pair_ids:
                        parts.append(f"pairs:{len(pair_ids)}")
                    if cashtag_ids:
                        parts.append(f"cashtag:{len(cashtag_ids)}")
                    status = "+".join(parts)

                title_preview = (cat.get("title") or "")[:50]
                mode = "DRY" if args.dry_run else "OK"
                print(f"  [{cid}] {mode} {status} | {title_preview}")

                total += 1
        finally:
            conn.close()

        if args.max_items and total >= args.max_items:
            break

        if len(batch) < args.batch_size:
            break

        offset += len(batch)

    print(f"\n{'='*60}")
    print(f"处理催化剂总数: {total}")
    print(f"  trading_pairs 关联新增: {total_pair_links}")
    print(f"  cashtag 关联新增:      {total_cashtag_links}")
    print(f"  无任何关联:            {total_no_link}")
    if args.dry_run:
        print("（dry-run 模式，未实际写入）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
