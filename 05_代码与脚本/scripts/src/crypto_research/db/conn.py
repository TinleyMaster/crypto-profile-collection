from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
import psycopg_pool


_pool: psycopg_pool.ConnectionPool | None = None


def _get_pool(database_url: str) -> psycopg_pool.ConnectionPool:
    """惰性创建全局连接池（max_size=2，避免打爆数据库连接）。

    注意：每个独立 Python 进程各有一个池实例。
    子进程脚本通常只用 1 条连接，web 进程 2 条足够。
    """
    global _pool
    if _pool is None:
        # D3（2026-09-22 审计）：max_size 2→5。大盘 overview 一次并行拉 25 个数据源，
        # 其中 7+ 个走本池（onchain/cm_activity/mvrv_history/divergence/btc_cycle/mvrv_universe…），
        # 2 连接在慢查询时会被占满，其余线程 checkout 等到 30s 超时 → 快照偶发
        # mvrv_universe status=error（值 "3"，间歇性，09-20 ok / 09-19/21/22 error）。
        # 5 连接对远程 PG 压力可忽略，显著降低快照期池争抢。
        _pool = psycopg_pool.ConnectionPool(
            database_url,
            min_size=0,
            max_size=5,
            open=True,
            timeout=30,
            # lock_timeout=30s：被其他事务持锁时快速失败并留痕，
            # 避免 UPDATE 无限等锁导致任务"90 分钟无日志"被看护误杀（2026-09-15 P0）
            kwargs={"connect_timeout": 30, "options": "-c lock_timeout=30000"},
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
