from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
import psycopg_pool


_pool: psycopg_pool.ConnectionPool | None = None


def _get_pool(database_url: str) -> psycopg_pool.ConnectionPool:
    """惰性创建全局连接池（max_size=5，避免打爆数据库连接）。"""
    global _pool
    if _pool is None:
        _pool = psycopg_pool.ConnectionPool(
            database_url,
            min_size=1,
            max_size=5,
            open=True,
            timeout=30,
            kwargs={"connect_timeout": 30},
        )
    return _pool


@contextmanager
def get_connection(database_url: str) -> Iterator[psycopg.Connection]:
    """从连接池获取数据库连接（上下文管理器，自动 commit/rollback/归还）。

    全局共享一个连接池（max_size=5），避免多脚本并发时打爆数据库连接数。
    """
    pool = _get_pool(database_url)
    with pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            # 连接已丢失时 rollback 本身也会抛错，吞掉避免掩盖原始异常
            try:
                conn.rollback()
            except Exception:
                pass
            raise
