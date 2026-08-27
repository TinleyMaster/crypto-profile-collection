"""
修复存量催化剂的 body_text JSON 污染（P1-A）。

问题：早期 CMS 抓取直接把 body（JSON node/child 树）存进了 body_text，
      导致 body_text 是 {"node":"root"...} 这种 JSON 噪声。
修法：从 body_html（= raw_json['body']）的 node/child 树递归提取纯文本，写回 body_text。

用法：
    python scripts/bin/fix_catalyst_body_text.py [--dry-run] [--max-items 100]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


def _extract_text_from_node_tree(tree_json) -> str:
    """从 CMS 的 node/child 块树提取纯文本（body_html / raw_json['body'] 的真实结构）。"""
    if not tree_json:
        return ""
    if isinstance(tree_json, str):
        try:
            tree_json = json.loads(tree_json)
        except Exception:
            return ""
    if not isinstance(tree_json, dict):
        return ""

    texts = []

    def _walk(node):
        if not isinstance(node, dict):
            return
        if node.get("node") == "text":
            t = node.get("text") or ""
            if t and t.strip():
                texts.append(t.strip())
        if node.get("node") == "element" and isinstance(node.get("text"), str) and node["text"].strip():
            texts.append(node["text"].strip())
        for child in node.get("child") or []:
            _walk(child)

    _walk(tree_json)
    return "\n".join(texts).strip()


def fetch_polluted(conn, limit: int = 100) -> list[dict]:
    """获取 body_text 疑似 JSON 污染的记录。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT catalyst_id, source_code, body_text, body_html, raw_json
            FROM biz.asset_catalyst
            WHERE body_text IS NOT NULL
              AND body_text LIKE '{%%}'
              AND raw_json IS NOT NULL
            ORDER BY catalyst_id
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fix_one(conn, cat: dict, dry_run: bool = False) -> tuple[str, int]:
    """修复单条，返回 (新 body_text, 字符数变化)。"""
    raw = cat.get("raw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}

    old_len = len(cat.get("body_text") or "")

    # 正文树优先从 body_html（= raw_json['body']，均为 node/child 结构）提取
    new_text = _extract_text_from_node_tree(cat.get("body_html"))
    if not new_text and isinstance(raw, dict):
        new_text = _extract_text_from_node_tree(raw.get("body"))

    if not new_text:
        return "", 0

    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE biz.asset_catalyst SET body_text = %s WHERE catalyst_id = %s",
                (new_text, cat["catalyst_id"]),
            )

    return new_text, len(new_text) - old_len


def main():
    parser = argparse.ArgumentParser(description="修复催化剂 body_text JSON 污染")
    parser.add_argument("--dry-run", action="store_true", help="只预览不修改")
    parser.add_argument("--max-items", type=int, default=0, help="最多处理条数（0=全部）")
    parser.add_argument("--batch-size", type=int, default=100, help="每批查询数量")
    args = parser.parse_args()

    settings = get_settings(require_database=not args.dry_run)

    total_fixed = 0
    total_skipped = 0

    with get_connection(settings.database_url) as conn:
        while True:
            batch = fetch_polluted(conn, args.batch_size)
            if not batch:
                print("没有发现 JSON 污染的记录，完成。")
                break

            print(f"\n获取到 {len(batch)} 条疑似污染的记录")

            for cat in batch:
                if args.max_items and total_fixed >= args.max_items:
                    break

                cid = cat["catalyst_id"]
                new_text, delta = fix_one(conn, cat, dry_run=args.dry_run)

                if not new_text:
                    total_skipped += 1
                    print(f"  [{cid}] ✗ 无法提取文本，跳过")
                    continue

                preview = new_text[:80].replace("\n", " ")
                status = "DRY" if args.dry_run else "OK"
                print(f"  [{cid}] {status} 字符变化: {delta:+d} | {preview}")
                total_fixed += 1

            if args.max_items and total_fixed >= args.max_items:
                break

            if len(batch) < args.batch_size:
                break

    print(f"\n完成：修复 {total_fixed} 条，跳过 {total_skipped} 条"
          + ("（dry-run 模式，未实际修改）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
