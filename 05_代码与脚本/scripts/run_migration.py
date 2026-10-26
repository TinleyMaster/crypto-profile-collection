"""执行 SQL 迁移文件"""
import os

import psycopg

# 🔴 R4（2026-10-26）：原兜底硬编码了
#   `postgresql://postgres:crypto2026@43.165.185.221:32405/crypto`
# —— 既是**已弃用的旧主机**，又是**明文口令**。改为取不到即报错，不回落泄露凭据。
_DB_URL_HINT = ("未找到 DATABASE_URL：请设置环境变量，或在 scripts/.env 中配置 "
                "DATABASE_URL=postgresql://<user>:<password>@<host>:<port>/<db>")


def _load_db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url
    env_path = __import__('pathlib').Path(__file__).resolve().parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("DATABASE_URL=") and not s.startswith("#"):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    raise RuntimeError(_DB_URL_HINT)


DB_URL = _load_db_url()

sql_path = __import__('pathlib').Path(__file__).resolve().parent / "migrations" / "fix_003_005_008.sql"
sql = sql_path.read_text(encoding="utf-8")

print(f"执行 SQL 迁移: {sql_path.name}")
print(f"SQL 长度: {len(sql)} 字符")
print("连接数据库...")

conn = psycopg.connect(DB_URL, connect_timeout=30, options="-c lock_timeout=30000")
print("已连接，执行中...")

try:
    conn.execute(sql)
    conn.commit()
    print("执行成功！")
except Exception as e:
    conn.rollback()
    print(f"执行失败: {e}")
    raise
finally:
    conn.close()
    print("连接已关闭")