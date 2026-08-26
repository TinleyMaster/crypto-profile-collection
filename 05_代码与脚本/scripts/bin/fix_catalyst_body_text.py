"""
修复存量催化剂的 body_text JSON 污染（P1-A）。

问题：早期 CMS 抓取直接把 body（JSON block tree）存进了 body_text，
      导致 body_text 是 {"node":"root"...} 这种 JSON 噪声。
修法：从 raw_json 的 contentJson 重新提取纯文本，写回 body_text。

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


def _extract_text_from_content_json(content_json) -> str:
    """从 CMS contentJson block tree 提取纯文本（与 catalyst/sources/binance_cms.py 一致）。"""
    if not content_json:
        return ""
    if isinstance(content_json, str):
        try:
            content_json = json.loads(content_json)
        except Exception:
            return ""
    if not isinstance(content_json, dict):
        return ""

    blocks = content_json.get("blocks") or []
    texts = []

    def _walk(block):
        if not isinstance(block, dict):
            return
        # 文本节点
        if block.get("type") == "text" or block.get("nodeType") == "text":
            text = block.get("value") or block.get("text") or ""
            if text:
                texts.append(text)
        # 段落/标题等容器
        for key in ("children", "content", "blocks"):
            children = block.get(key)
            if isinstance(children, list):
                for child in children:
                    _walk(child)

    for block in blocks:
        _walk(block)

    # 段落之间加换行
    result = "\n".join(t.strip() for t in texts if t and t.strip())
    return result.strip()


def _strip_html(html: str) -> str:
    """简易 HTML 去标签（兜底用）。"""
    if not html:
        return ""
    import re
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_polluted(conn, limit: int = 100) -> list[dict]:
    """获取 body_text 疑似 JSON 污染的记录。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT catalyst_id, source_code, body_text, body_html, raw_json
            FROM biz.asset_catalyst
            WHERE body_text IS NOT NULL
              AND body_text LIKE '{%'
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

    # 优先从 contentJson 提取
    content_json = raw.get("contentJson") if isinstance(raw, dict) else None
    new_text = _extract_text_from_content_json(content_json)

    # 兜底：HTML 去标签
    if not new_text and cat.get("body_html"):
        new_text = _strip_html(cat["body_html"])

    if not dry_run and new_text:
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
