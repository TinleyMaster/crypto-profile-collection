"""
P1: 白皮书路径相对化迁移脚本
将 storage_path 从 Windows 绝对路径改为相对于 DOCS_STORAGE_ROOT 的相对路径
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)

WINDOWS_MARKER = r"\docs_storage\\"

def migrate_paths(dry_run: bool = True):
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 查出所有有 storage_path 的记录
            cur.execute("""
                SELECT doc_id, storage_path
                FROM biz.doc_asset
                WHERE storage_path IS NOT NULL
                ORDER BY doc_id
            """)
            rows = cur.fetchall()
            print(f"共 {len(rows)} 条有 storage_path 的记录")

            need_migrate = []
            already_relative = []
            for doc_id, path in rows:
                # 判断是否是 Windows 绝对路径（包含盘符:\）
                if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
                    # Windows 绝对路径，找 docs_storage 位置
                    idx = path.lower().find("\\docs_storage\\")
                    if idx >= 0:
                        # 截取 docs_storage 之后的部分
                        rel_path = path[idx + len("\\docs_storage\\"):]
                        # 反斜杠转正斜杠
                        rel_path = rel_path.replace("\\", "/")
                        need_migrate.append((doc_id, path, rel_path))
                    else:
                        print(f"  [警告] doc_id={doc_id}: Windows 路径但不含 docs_storage: {path}")
                else:
                    already_relative.append((doc_id, path))

            print(f"  需要迁移: {len(need_migrate)} 条")
            print(f"  已是相对路径: {len(already_relative)} 条")

            if need_migrate:
                print("\n=== 迁移预览（前5条）===")
                for doc_id, old, new in need_migrate[:5]:
                    print(f"  doc_id={doc_id}")
                    print(f"    旧: {old[:70]}...")
                    print(f"    新: {new}")

                if dry_run:
                    print("\n[DRY RUN] 未实际执行 UPDATE，加 --apply 参数执行迁移")
                else:
                    # 执行批量更新
                    cur.executemany("""
                        UPDATE biz.doc_asset
                        SET storage_path = %s, updated_at = NOW()
                        WHERE doc_id = %s
                    """, [(new, doc_id) for doc_id, old, new in need_migrate])
                    print(f"\n已更新 {len(need_migrate)} 条记录")

                    # 验证
                    cur.execute("""
                        SELECT COUNT(*) FROM biz.doc_asset
                        WHERE storage_path IS NOT NULL
                          AND storage_path ~ '^[A-Za-z]:'
                    """)
                    remaining = cur.fetchone()[0]
                    print(f"剩余 Windows 绝对路径记录: {remaining}")


if __name__ == "__main__":
    dry_run = "--apply" not in sys.argv
    migrate_paths(dry_run=dry_run)
