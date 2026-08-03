"""
Phase B3: 文档下载与落盘
下载 doc_asset 中的 PDF 文件到本地，计算 content_hash，更新 storage_path。

目录结构: docs_storage/{symbol}_{asset_id}/whitepapers/{原始文件名}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# 本地存储根目录
STORAGE_ROOT = Path(__file__).resolve().parents[3] / "docs_storage"


def sanitize_name(name: str) -> str:
    """清理文件名/目录名中的非法字符"""
    return re.sub(r'[\\/:*?"<>|]', '_', name)


def compute_sha256(file_path: Path) -> str:
    """计算文件的 SHA256 哈希"""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase B3: 文档下载与落盘")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--limit", type=int, default=50, help="最大下载数量")
    parser.add_argument("--timeout", type=int, default=10, help="下载超时(秒)")
    parser.add_argument("--storage-root", type=str, default=str(STORAGE_ROOT), help="存储根目录")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    import requests
    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    storage_root = Path(args.storage_root)

    # 查询待下载的 doc_asset，JOIN coin_basic 获取 symbol
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT da.doc_id, da.entity_type, da.asset_id, da.protocol_id,
                       da.doc_type, da.source_url, da.resolved_url, da.file_name,
                       da.mime_type, cb.coin_symbol
                FROM biz.doc_asset da
                LEFT JOIN biz.coin_basic cb ON cb.asset_id = da.asset_id
                WHERE da.storage_path IS NULL
                  AND da.mime_type = 'application/pdf'
                ORDER BY da.doc_id
                LIMIT %s
                """,
                (args.limit,),
            )
            records = [dict(row) for row in cur.fetchall()]

    if not records:
        print(json.dumps({"status": "no_pending", "message": "没有待下载的文档"}))
        return 0

    print(f"待下载: {len(records)}, 存储根目录: {storage_root}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })

    downloaded = 0
    failed = 0
    skipped = 0

    for rec in records:
        doc_id = rec["doc_id"]
        asset_id = rec["asset_id"]
        source_url = rec["resolved_url"] or rec["source_url"]
        coin_symbol = rec.get("coin_symbol") or f"id{asset_id}"

        # 原始文件名（URL 解码，去掉路径只保留文件名部分）
        raw_file_name = rec["file_name"] or "document.pdf"
        file_name = unquote(raw_file_name)
        file_name = sanitize_name(file_name)

        # 清理 symbol 用作目录名
        safe_symbol = sanitize_name(coin_symbol.lower())

        # 目录结构: docs_storage/{symbol}_{asset_id}/whitepapers/
        asset_dir = storage_root / f"{safe_symbol}_{asset_id}" / "whitepapers"
        local_path = asset_dir / file_name

        # 如果文件已存在（可能从旧目录迁移过来的同名文件）
        if local_path.exists():
            try:
                content_hash = compute_sha256(local_path)
                file_size = local_path.stat().st_size
                if not args.dry_run:
                    _update_storage(settings.database_url, doc_id, str(local_path), content_hash, file_size)
                skipped += 1
                print(f"[{downloaded + failed + skipped}/{len(records)}] SKIP(exists): {safe_symbol}_{asset_id}/{file_name}")
                continue
            except Exception as e:
                failed += 1
                print(f"[{downloaded + failed + skipped}/{len(records)}] SKIP error: {file_name} -> {e}")
                continue

        try:
            resp = session.get(source_url, timeout=args.timeout, allow_redirects=True, stream=True)
            resp.raise_for_status()

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "application/pdf" not in content_type and not source_url.lower().endswith(".pdf"):
                failed += 1
                print(f"[{downloaded + failed + skipped}/{len(records)}] SKIP(not PDF): {safe_symbol}_{asset_id} -> {content_type}")
                continue

            # 写入文件
            asset_dir.mkdir(parents=True, exist_ok=True)
            content = resp.content
            local_path.write_bytes(content)

            content_hash = compute_sha256(local_path)
            file_size = len(content)

            if args.dry_run:
                local_path.unlink()
                try:
                    asset_dir.rmdir()
                except OSError:
                    pass
            else:
                _update_storage(settings.database_url, doc_id, str(local_path), content_hash, file_size)

            downloaded += 1
            size_kb = file_size / 1024
            print(f"[{downloaded + failed + skipped}/{len(records)}] OK: {safe_symbol}_{asset_id}/whitepapers/{file_name} ({size_kb:.0f}KB)")

        except Exception as e:
            failed += 1
            err_msg = str(e)[:80]
            print(f"[{downloaded + failed + skipped}/{len(records)}] FAIL: {safe_symbol}_{asset_id} -> {err_msg}")

    print(json.dumps({
        "status": "complete",
        "candidates": len(records),
        "downloaded": downloaded,
        "failed": failed,
        "skipped": skipped,
    }))
    return 0


def _update_storage(database_url: str, doc_id: int, storage_path: str, content_hash: str, file_size: int) -> bool:
    """返回 True 表示成功，False 表示 content_hash 重复"""
    from crypto_research.db.conn import get_connection

    try:
        with get_connection(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT doc_id, storage_path FROM biz.doc_asset WHERE content_hash = %s AND doc_id != %s LIMIT 1",
                    (content_hash, doc_id),
                )
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        UPDATE biz.doc_asset
                        SET storage_path = COALESCE(%s, storage_path),
                            file_size_bytes = %s,
                            parse_status = '待解析',
                            updated_at = NOW()
                        WHERE doc_id = %s
                        """,
                        (existing[1], file_size, doc_id),
                    )
                    return False
                else:
                    cur.execute(
                        """
                        UPDATE biz.doc_asset
                        SET storage_path = %s,
                            content_hash = %s,
                            file_size_bytes = %s,
                            parse_status = '待解析',
                            updated_at = NOW()
                        WHERE doc_id = %s
                        """,
                        (storage_path, content_hash, file_size, doc_id),
                    )
                    return True
    except Exception as e:
        if "uq_biz_doc_asset_content_hash" in str(e):
            try:
                with get_connection(database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE biz.doc_asset
                            SET storage_path = %s,
                                file_size_bytes = %s,
                                parse_status = '待解析',
                                updated_at = NOW()
                            WHERE doc_id = %s
                            """,
                            (storage_path, file_size, doc_id),
                        )
                return False
            except Exception:
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
