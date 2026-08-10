from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg


@contextmanager
def get_connection(database_url: str) -> Iterator[psycopg.Connection]:
    """获取数据库连接（上下文管理器，自动 commit/rollback/close）。
    
    注意：每次调用创建新连接，调用方需控制并发数避免打爆连接池。
    后续可考虑迁移到 psycopg_pool.ConnectionPool。
    """
    conn = psycopg.connect(database_url, connect_timeout=30)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
