"""
补齐下载缺失的白皮书文件。
从 doc_asset 表中找出 doc_type=whitepaper 但 storage_path 为空的记录，
尝试从 source_url 下载 PDF 到 docs_storage 目录，并更新 storage_path。

目录结构：{symbol}_{asset_id}/whitepapers/{file_name}
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)

# 文档存储根目录
DOCS_STORAGE_ROOT = Path(r"E:\瞎搞乱搞\web3\加密货币研究报告\docs_storage")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def download_whitepaper(url: str, save_path: Path, timeout: int = 30) -> bool:
    """下载白皮书 PDF 到本地。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True, stream=True)
        resp.raise_for_status()

        # 检查 Content-Type，确保是 PDF 或二进制
        ct = resp.headers.get("Content-Type", "").lower()
        if "html" in ct and "pdf" not in ct:
            print(f"    [跳过] 返回的是 HTML，不是 PDF: {ct}")
            return False

        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        # 验证文件大小
        size = save_path.stat().st_size
        if size < 1000:  # 小于 1KB 大概率不是有效 PDF
            print(f"    [跳过] 文件太小 ({size} bytes)，可能不是有效 PDF")
            save_path.unlink(missing_ok=True)
            return False

        print(f"    [OK] 下载成功，{size:,} bytes")
        return True
    except Exception as e:
        print(f"    [失败] {e}")
        return False


def main(dry_run: bool = True):
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 查出所有缺失文件的白皮书
            cur.execute("""
                SELECT d.doc_id, d.asset_id, d.source_url, d.file_name, d.mime_type,
                       a.canonical_symbol
                FROM biz.doc_asset d
                JOIN core.asset a ON a.asset_id = d.asset_id
                WHERE d.doc_type = 'whitepaper'
                  AND d.storage_path IS NULL
                  AND d.source_url IS NOT NULL
                ORDER BY d.doc_id
            """)
            rows = cur.fetchall()
            print(f"待下载白皮书: {len(rows)} 条")
            print(f"存储根目录: {DOCS_STORAGE_ROOT}")
            print()

            success = 0
            failed = 0

            for i, (doc_id, asset_id, source_url, file_name, mime_type, symbol) in enumerate(rows, 1):
                # 构造相对路径：{symbol}_{asset_id}/whitepapers/{file_name}
                symbol_clean = (symbol or "unknown").lower().replace(" ", "_")
                # 清理文件名中的 URL 编码
                clean_name = file_name or f"whitepaper_{doc_id}.pdf"
                rel_dir = f"{symbol_clean}_{asset_id}/whitepapers"
                rel_path = f"{rel_dir}/{clean_name}"
                save_path = DOCS_STORAGE_ROOT / rel_dir / clean_name

                print(f"[{i}/{len(rows)}] doc_id={doc_id}, {symbol}({asset_id}): {file_name}")
                print(f"    URL: {source_url[:80]}")
                print(f"    保存: {rel_path}")

                if save_path.exists():
                    print(f"    [跳过] 文件已存在")
                    success += 1
                    # 更新数据库
                    if not dry_run:
                        cur.execute("""
                            UPDATE biz.doc_asset
                            SET storage_path = %s, updated_at = NOW()
                            WHERE doc_id = %s AND storage_path IS NULL
                        """, (rel_path, doc_id))
                    continue

                if dry_run:
                    print(f"    [DRY RUN] 不实际下载")
                    continue

                ok = download_whitepaper(source_url, save_path)
                if ok:
                    success += 1
                    # 更新数据库
                    cur.execute("""
                        UPDATE biz.doc_asset
                        SET storage_path = %s, file_size_bytes = %s, updated_at = NOW()
                        WHERE doc_id = %s
                    """, (rel_path, save_path.stat().st_size, doc_id))
                else:
                    failed += 1

                # 礼貌性延迟
                time.sleep(0.5)

            print()
            print(f"=== 完成 ===")
            print(f"  成功: {success}")
            print(f"  失败: {failed}")
            print(f"  总计: {len(rows)}")


if __name__ == "__main__":
    dry_run = "--apply" not in sys.argv
    main(dry_run=dry_run)
