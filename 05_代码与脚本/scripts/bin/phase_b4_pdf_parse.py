"""
Phase B4: 文档解析 — PDF → Markdown
将已下载的 PDF 白皮书解析为 Markdown 文本，保存到同目录，更新 parse_status。
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

STORAGE_ROOT = Path(__file__).resolve().parents[3] / "docs_storage"


def parse_pdf_to_markdown(pdf_path: Path) -> str | None:
    """使用 pypdf 提取 PDF 文本并整理为 Markdown。返回 None 表示解析失败。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        pages: list[str] = []

        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                # 清理多余空白
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                cleaned = "\n\n".join(lines)
                pages.append(f"## Page {i + 1}\n\n{cleaned}")

        if not pages:
            return None

        # 标题用文件名
        title = pdf_path.stem
        md = f"# {title}\n\n"
        md += f"> Source: {pdf_path.name}\n\n"
        md += "\n\n".join(pages)
        return md.strip()

    except Exception as e:
        print(f"    parse error: {e}")
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B4: PDF 解析为 Markdown")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=50, help="最大处理数量")
    p.add_argument("--storage-root", type=str, default=str(STORAGE_ROOT))
    p.add_argument("--force", action="store_true", help="强制重新解析已完成的")
    return p


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    storage_root = Path(args.storage_root)

    # 查询待解析的 doc_asset
    where = (
        "da.storage_path IS NOT NULL"
        if args.force
        else "da.storage_path IS NOT NULL AND da.parse_status = '待解析'"
    )
    with get_connection(settings.database_url) as conn:
        import psycopg
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                f"""
                SELECT da.doc_id, da.file_name, da.storage_path, da.parse_status
                FROM biz.doc_asset da
                WHERE {where}
                ORDER BY da.doc_id
                LIMIT %s
                """,
                (args.limit,),
            )
            records = [dict(row) for row in cur.fetchall()]

    if not records:
        print(json.dumps({"status": "no_pending"}))
        return 0

    print(f"待解析: {len(records)} 个 PDF")

    parsed = 0
    failed = 0

    for rec in records:
        doc_id = rec["doc_id"]
        storage_path = Path(rec["storage_path"])
        file_name = rec["file_name"] or "document.pdf"

        if not storage_path.exists():
            failed += 1
            print(f"[{parsed + failed}/{len(records)}] MISSING: {storage_path}")
            continue

        md_path = storage_path.with_suffix(".md")

        # 跳过已解析的（.md 文件已存在且非 force）
        if md_path.exists() and not args.force:
            if not args.dry_run:
                _mark_parsed(settings.database_url, doc_id, str(md_path))
            parsed += 1
            print(f"[{parsed + failed}/{len(records)}] SKIP(cached): {file_name}")
            continue

        # 解析 PDF
        md_text = parse_pdf_to_markdown(storage_path)

        if md_text is None:
            if not args.dry_run:
                _mark_failed(settings.database_url, doc_id)
            failed += 1
            print(f"[{parsed + failed}/{len(records)}] FAIL(empty): {file_name}")
            continue

        if args.dry_run:
            print(f"[{parsed + failed}/{len(records)}] DRY: {file_name} ({len(md_text)} chars)")
            parsed += 1
            continue

        # 写出 Markdown
        md_path.write_text(md_text, encoding="utf-8")
        _mark_parsed(settings.database_url, doc_id, str(md_path))

        parsed += 1
        print(f"[{parsed + failed}/{len(records)}] OK: {file_name} -> {md_path.name} ({len(md_text)} chars)")

    print(json.dumps({
        "status": "complete",
        "candidates": len(records),
        "parsed": parsed,
        "failed": failed,
    }))
    return 0


def _mark_parsed(database_url: str, doc_id: int, md_path: str) -> None:
    from crypto_research.db.conn import get_connection
    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE biz.doc_asset
                   SET parse_status = '已解析', updated_at = NOW()
                   WHERE doc_id = %s""",
                (doc_id,),
            )


def _mark_failed(database_url: str, doc_id: int) -> None:
    from crypto_research.db.conn import get_connection
    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE biz.doc_asset
                   SET parse_status = '解析失败', updated_at = NOW()
                   WHERE doc_id = %s""",
                (doc_id,),
            )


if __name__ == "__main__":
    raise SystemExit(main())
