from __future__ import annotations

import atexit
import sys
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
import psycopg_pool
from psycopg.rows import tuple_row


_pool: psycopg_pool.ConnectionPool | None = None


def _reset_pool() -> None:
    """丢弃并关闭全局连接池（建连连续失败时用），下次调用会重建。"""
    global _pool
    if _pool is None:
        return
    try:
        _pool.close(timeout=5)
    except Exception:
        pass
    _pool = None


@atexit.register
def _close_pool_at_exit() -> None:
    """进程退出时显式关闭连接池。

    不做的话 psycopg_pool 的 worker 线程会在解释器退出时打印
    "couldn't stop thread 'pool-1-worker-N' within 5.0 seconds" 告警
    （连接已建立、逻辑正常，只是池没收尾）——显式关闭可消除该噪音。
    """
    global _pool
    if _pool is None:
        return
    try:
        _pool.close(timeout=5)
    except Exception:
        pass
    _pool = None


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
            # timeout=60：pool.connection() 等待可用连接的上限。新 prod 链路
            # 偶发丢 PG 首包，池的 worker 会在后台重试建连，这里给足等待窗口。
            timeout=60,
            # lock_timeout=30s：被其他事务持锁时快速失败并留痕，
            # 避免 UPDATE 无限等锁导致任务"90 分钟无日志"被看护误杀（2026-09-15 P0）
            # TCP keepalives（2026-09-29）：远端连接半开（网络抖动）时，任何
            # connect_timeout / lock_timeout 都覆盖不到，语句会在协议层永久挂起，
            # 表现为「任务长时间无日志」被看护/收割器误杀（catalyst_run_all 周期性
            # stuck 同源）。keepalives 让半开连接 ~60s 内报错而非永久挂起。
            # reconnect_timeout/reconnect_failed（2026-10-21）：prod 迁到
            # 43.165.185.221:32405 后，本机经 FlClash TUN 代理会概率性吞掉 PG
            # 启动包（TCP 握手秒通但 StartupMessage 无回音 → 建连被 RST/超时）。
            # psycopg_pool 只对「已建立后又失效」的连接做重连重试，不重试初次建连
            # ⇒ 池子建不满时 pool.connection() 直接抛 PoolTimeout。
            # 这两项让池在首次建连失败后按 reconnect_timeout 窗口持续重试。
            reconnect_timeout=120,
            reconnect_failed=True,
            kwargs={
                # connect_timeout=30（2026-10-26 由 10 上调）：本机到 prod 走
                # Tailscale DERP 中继（实测 relay "tok"，非直连），实测建连耗时
                # **3.6s / 4.2s / 7.4s / 10.0s 波动**——原先的 10s 卡在边缘，
                # 一次超过就整池失败（error connecting in 'pool-1': connection
                # timeout expired），表现为「连不上库」但网络其实是通的。
                # 30s 能覆盖 DERP 最差抖动；失败仍会抛出，不会静默挂起。
                "connect_timeout": 30,
                "options": "-c lock_timeout=30000",
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
            },
        )
    return _pool


@contextmanager
def get_connection(database_url: str) -> Iterator[psycopg.Connection]:
    """从连接池获取数据库连接（上下文管理器，自动 commit/rollback/归还）。

    全局共享一个连接池（max_size=5），避免多脚本并发时打爆数据库连接数。

    建连重试（2026-10-21）：新 prod 链路偶发丢 PG 首包导致建连失败。
    实现注记：这里用 pool.getconn()/putconn() 显式取还，而不是 pool.connection()：
    后者是生成器式 contextmanager，getconn 失败时其生成器停在 yield 之前，
    调 close() 会抛 "generator didn't stop" 并卡死池 worker 线程；
    getconn/putconn 是官方文档为「无法当 context manager 用」场景推荐的 API。
    """
    attempts = 5
    backoff = 2.0
    conn = None
    for i in range(1, attempts + 1):
        pool = _get_pool(database_url)
        try:
            conn = pool.getconn()
            break
        except Exception as e:
            if i < attempts:
                wait = backoff * i
                print(
                    f"[retry] 取 PG 连接失败（{type(e).__name__}: {str(e)[:70]}），"
                    f"{wait:.0f}s 后重试 {i + 1}/{attempts} ...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                # 池的 worker 线程若因连续建连失败而卡死，重试同一个池是徒劳的
                # ⇒ 丢弃并重建，让下一轮从干净状态开始。
                _reset_pool()
                continue
            raise
    if conn is None:
        raise RuntimeError("无法从连接池取得连接")

    try:
        # 护栏（2026-09-27）：连接是池内复用的，归还时不重置 row_factory。
        # 若任何调用方在借出的连接上写 conn.row_factory = dict_row（而非在
        # cursor(row_factory=...) 上指定），会把 dict_row 毒化进池，后续复用该
        # 连接的代码拿到 dict 行，凡按元组解包的都会 KeyError/ValueError（实测
        # 大盘 overview 链上异动块整块失效）。这里在每次借出时归位为默认元组行，
        # 让「忘记重置」不再能污染他人。
        conn.row_factory = tuple_row
        yield conn
        conn.commit()
    except Exception:
        # 连接已丢失时 rollback 本身也会抛错，吞掉避免掩盖原始异常
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        # 归还连接回池；若连接已损坏，putconn 会将其丢弃并新建。
        try:
            pool.putconn(conn)
        except Exception:
            pass
