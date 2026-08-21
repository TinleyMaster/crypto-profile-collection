"""
后台任务管理器：用线程池跑各种采集脚本，Web 端只管发指令和查状态。

任务状态和日志持久化到数据库（sys.task / sys.task_log），
支持跨服务共享状态（调度器和 Flask 主应用即使在不同容器也能看到同一批任务）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg
import psycopg.rows
import psycopg_pool

# Docker 或本地环境判断脚本路径
if os.path.exists("/app/scripts/bin"):
    WORKER_SCRIPTS_DIR = Path("/app/scripts/bin")
else:
    WORKER_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "bin"

MAX_LOG_LINES = 1000
MAX_RUNTIME_HOURS = 12  # 超过此时长的 running 任务视为僵尸，自动标记 failed
LOG_STUCK_MINUTES = 30  # running 任务超过该时长无新日志，视为卡死，提前收割

# ── 数据库连接池 ────────────────────────────────────────────

_pool: psycopg_pool.ConnectionPool | None = None


def _get_pool() -> psycopg_pool.ConnectionPool:
    """惰性创建连接池。"""
    global _pool
    if _pool is None:
        from crypto_research.config import get_settings

        settings = get_settings(require_database=True)
        _pool = psycopg_pool.ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=5,
            open=True,
            timeout=30,
            kwargs={"connect_timeout": 30},
        )
    return _pool


@contextmanager
def _get_db():
    """从连接池取连接，自动 commit/rollback。"""
    with _get_pool().connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ── 时间戳转换 ──────────────────────────────────────────────

def _to_ts(val) -> Optional[datetime]:
    """把 epoch 秒转成 datetime，None 透传。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    return val


def _from_ts(val) -> Optional[float]:
    """把 datetime 转成 epoch 秒，None 透传。"""
    if val is None:
        return None
    return val.timestamp()


# ── 状态读写（数据库版）────────────────────────────────────

def _row_to_task(row: dict) -> dict:
    """把数据库行转成原 task dict 格式（时间戳转 epoch 秒）。"""
    return {
        "task_id": row["task_id"],
        "name": row["name"],
        "status": row["status"],
        "cmd": list(row["cmd"]) if row["cmd"] else [],
        "started_at": _from_ts(row["started_at"]),
        "ended_at": _from_ts(row["ended_at"]),
        "stats": dict(row["stats"]) if row["stats"] else {},
        "error": row["error"],
    }


def _load_task(task_id: str) -> Optional[dict]:
    """读取单个任务，返回 dict（字段名与原 JSON 结构一致）。"""
    with _get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT task_id, name, status, cmd, started_at, ended_at, stats, error "
                "FROM sys.task WHERE task_id = %s",
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_task(dict(row))


def _load_all_tasks() -> dict:
    """读取所有任务，返回 {tasks: {...}, pending: [...]} 结构（兼容旧接口）。"""
    with _get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT task_id, name, status, cmd, started_at, ended_at, stats, error "
                "FROM sys.task ORDER BY started_at DESC"
            )
            rows = cur.fetchall()

    tasks = {}
    pending = []
    for row in rows:
        t = _row_to_task(dict(row))
        tasks[t["task_id"]] = t
        if t["status"] == "pending":
            pending.append(t["task_id"])
    # pending 队列按提交时间正序（FIFO）
    pending.sort(key=lambda tid: tasks[tid].get("started_at") or 0)
    return {"tasks": tasks, "pending": pending}


def _insert_task(task: dict) -> None:
    """插入一条新任务。"""
    with _get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sys.task (task_id, name, status, cmd, started_at, ended_at, stats, error)
                VALUES (%s, %s, %s, %s::text[], %s, %s, %s::jsonb, %s)
                """,
                (
                    task["task_id"],
                    task["name"],
                    task["status"],
                    task.get("cmd") or [],
                    _to_ts(task.get("started_at")),
                    _to_ts(task.get("ended_at")),
                    json.dumps(task.get("stats") or {}),
                    task.get("error"),
                ),
            )


def _update_task(task_id: str, **fields) -> None:
    """更新任务字段。只传需要更新的字段。"""
    if not fields:
        return
    sets = []
    params = []
    for key, val in fields.items():
        if key == "stats":
            sets.append("stats = %s::jsonb")
            params.append(json.dumps(val or {}))
        elif key == "cmd":
            sets.append("cmd = %s::text[]")
            params.append(val or [])
        elif key in ("started_at", "ended_at"):
            sets.append(f"{key} = %s")
            params.append(_to_ts(val))
        else:
            sets.append(f"{key} = %s")
            params.append(val)
    sets.append("updated_at = NOW()")
    params.append(task_id)
    sql = f"UPDATE sys.task SET {', '.join(sets)} WHERE task_id = %s"
    with _get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _append_log(task_id: str, line: str) -> None:
    """追加一行日志到数据库。"""
    with _get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sys.task_log (task_id, line_no, content)
                VALUES (
                    %s,
                    COALESCE((SELECT MAX(line_no) FROM sys.task_log WHERE task_id = %s), 0) + 1,
                    %s
                )
                """,
                (task_id, task_id, line),
            )


def _read_log(task_id: str, limit: int = 200) -> list[str]:
    """读日志最后 N 行（正序返回）。"""
    with _get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT content FROM (
                    SELECT content, line_no FROM sys.task_log
                    WHERE task_id = %s
                    ORDER BY line_no DESC
                    LIMIT %s
                ) sub
                ORDER BY line_no ASC
                """,
                (task_id, limit),
            )
            return [row[0] for row in cur.fetchall()]


def _task_exists(task_id: str) -> bool:
    with _get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sys.task WHERE task_id = %s", (task_id,))
            return cur.fetchone() is not None


# ── 兼容旧接口的占位（scheduler.py 可能 import 这些）──────
# 保留 STATE_FILE / LOG_DIR / _lock / _load_state / _save_state
# 但内部改为数据库实现，scheduler.py 无需改动即可工作

STATE_FILE = "sys.task (database)"
LOG_DIR = "sys.task_log (database)"


class _NoopLock:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


def _lock():
    """数据库版不再需要文件锁（行级锁由 DB 处理），返回空锁兼容旧调用。"""
    return _NoopLock()


def _load_state() -> dict:
    """兼容旧接口：返回 {tasks, pending} 结构。"""
    return _load_all_tasks()


def _save_state(state: dict) -> None:
    """兼容旧接口：把整个 state 写回数据库（增量 upsert）。

    注意：旧代码通过 _load_state → 修改 dict → _save_state 的模式工作，
    这里做全量比对，只 upsert 有变化的任务。删除操作不处理（任务只追加不删除）。
    """
    for task_id, task in state["tasks"].items():
        existing = _load_task(task_id)
        if existing is None:
            _insert_task(task)
        else:
            # 比较关键字段，有变化才更新
            changed = {}
            for key in ("name", "status", "cmd", "started_at", "ended_at", "stats", "error"):
                if task.get(key) != existing.get(key):
                    changed[key] = task.get(key)
            if changed:
                _update_task(task_id, **changed)


# ── TaskManager ─────────────────────────────────────────────

class TaskManager:
    def __init__(self, max_concurrent: int = 3):
        self._max_concurrent = max_concurrent
        self._local_procs: dict[str, subprocess.Popen] = {}
        self._stop_flag = False
        self._thread = threading.Thread(target=self._runner_loop, daemon=True)
        self._thread.start()

    # ── 公共 API ──

    def submit_task(self, name: str, cmd: list[str]) -> str:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        task = {
            "task_id": task_id,
            "name": name,
            "status": "pending",
            "cmd": cmd,
            "started_at": now,
            "ended_at": None,
            "stats": {},
            "error": None,
        }
        _insert_task(task)
        # 提交后立刻写一条启动日志，确认任务注册成功
        _append_log(task_id, f"[TASK] 任务已提交: {name}")
        _append_log(task_id, f"[TASK] CMD: {' '.join(cmd)}")
        return task_id

    def submit_func_task(self, name: str, func) -> str:
        """提交一个 Python 可调用任务（后台线程执行 + 实时日志流），返回 task_id。

        func 签名为 func(log) -> dict：
          - log(line): 写一行日志到该任务的日志；
          - 返回的 dict 作为最终结果存入 task["stats"]["result"]。
        """
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        task = {
            "task_id": task_id,
            "name": name,
            "status": "pending",
            "cmd": [],
            "started_at": now,
            "ended_at": None,
            "stats": {},
            "error": None,
        }
        _insert_task(task)
        _append_log(task_id, f"[TASK] 任务已提交: {name}")
        threading.Thread(
            target=self._run_func_task, args=(task_id, func), daemon=True
        ).start()
        return task_id

    def get_task_result(self, task_id: str):
        """读取函数式任务（submit_func_task）的最终结果，无结果返回 None。"""
        task = _load_task(task_id)
        if not task:
            return None
        stats = task.get("stats") or {}
        return stats.get("result")

    def _run_func_task(self, task_id: str, func) -> None:
        _update_task(task_id, status="running", started_at=time.time())

        def log(line: str) -> None:
            _append_log(task_id, str(line))

        name = getattr(func, "__name__", "任务")
        _append_log(task_id, f"[TASK] 开始执行: {name}")
        try:
            result = func(log)
        except Exception as e:
            _append_log(task_id, f"[ERROR] {str(e)[:200]}")
            _update_task(
                task_id,
                ended_at=time.time(),
                status="failed",
                error=str(e)[:200],
            )
            return

        _append_log(task_id, "[TASK] 执行完成")
        task = _load_task(task_id)
        if task:
            stats = task.get("stats") or {}
            if isinstance(result, dict):
                stats["result"] = result
            _update_task(
                task_id,
                ended_at=time.time(),
                status="done",
                stats=stats,
            )

    def stop_task(self, task_id: str) -> bool:
        task = _load_task(task_id)
        if not task:
            return False
        if task["status"] in ("done", "failed", "stopped"):
            return False
        _update_task(task_id, status="stopped", ended_at=time.time())

        # 如果是本进程启动的，杀掉进程
        proc = self._local_procs.get(task_id)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        return True

    def list_tasks(self, limit: int = 20) -> list[dict]:
        state = _load_all_tasks()
        items = list(state["tasks"].values())
        # 排序：running > pending > done/failed/stopped，同状态按时间倒序
        status_order = {"running": 0, "pending": 1, "done": 2, "failed": 3, "stopped": 4}
        items.sort(key=lambda t: (
            status_order.get(t.get("status"), 9),
            -(t.get("started_at") or 0)
        ))
        return [self._serialize(t) for t in items[:limit]]

    def get_task(self, task_id: str) -> Optional[dict]:
        task = _load_task(task_id)
        if not task:
            return None
        return self._serialize(task)

    def get_task_log(self, task_id: str, limit: int = 200) -> Optional[list[str]]:
        if not _task_exists(task_id):
            return None
        return _read_log(task_id, limit)

    def running_count(self) -> int:
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM sys.task WHERE status = 'running'")
                row = cur.fetchone()
                return row[0] if row else 0

    # ── 内部 ──

    def _serialize(self, task: dict) -> dict:
        started = task.get("started_at")
        ended = task.get("ended_at")
        return {
            "task_id": task["task_id"],
            "name": task["name"],
            "status": task["status"],
            "started_at": started,
            "ended_at": ended,
            "elapsed_sec": (
                round((ended or time.time()) - started, 1)
                if started else None
            ),
            "last_log": "",
            "stats": task.get("stats", {}),
            "error": task.get("error"),
        }

    def _runner_loop(self):
        last_reap = 0.0
        while not self._stop_flag:
            task_id = None
            # 原子地取一个 pending 任务：用 SELECT ... FOR UPDATE SKIP LOCKED
            try:
                # 每 60 秒收割一次僵尸任务（超过 MAX_RUNTIME_HOURS 的 running 任务）
                now = time.time()
                if now - last_reap > 60:
                    last_reap = now
                    self._reap_zombie_tasks()

                with _get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM sys.task WHERE status = 'running'")
                        running = cur.fetchone()[0]
                        if running < self._max_concurrent:
                            cur.execute(
                                """
                                SELECT task_id FROM sys.task
                                WHERE status = 'pending'
                                ORDER BY started_at ASC
                                LIMIT 1
                                FOR UPDATE SKIP LOCKED
                                """
                            )
                            row = cur.fetchone()
                            if row:
                                task_id = row[0]
                                cur.execute(
                                    "UPDATE sys.task SET status = 'running', started_at = NOW(), updated_at = NOW() WHERE task_id = %s",
                                    (task_id,),
                                )
            except Exception as e:
                print(f"[TaskManager] runner_loop error: {e}", file=sys.stderr)
                time.sleep(5)
                continue

            if task_id:
                threading.Thread(target=self._run_task, args=(task_id,), daemon=True).start()
                continue

            time.sleep(1)

    def _reap_zombie_tasks(self) -> None:
        """收割僵尸任务，两个条件满足其一即触发：
        1. 运行时长超过 MAX_RUNTIME_HOURS（硬超时）
        2. 最近 LOG_STUCK_MINUTES 分钟无新日志（卡死检测，需已运行至少 10 分钟）
        """
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    # 条件1：超 12h 硬超时
                    cur.execute(
                        """
                        UPDATE sys.task
                        SET status = 'failed',
                            ended_at = NOW(),
                            error = %s,
                            updated_at = NOW()
                        WHERE status = 'running'
                          AND started_at < NOW() - (%s || ' hours')::interval
                        """,
                        (f"timeout: 运行超过 {MAX_RUNTIME_HOURS}h 自动终止",
                         str(MAX_RUNTIME_HOURS)),
                    )
                    count_timeout = cur.rowcount

                    # 条件2：30min 无新日志 + 已运行至少 10min（卡死）
                    cur.execute(
                        """
                        UPDATE sys.task t
                        SET status = 'failed',
                            ended_at = NOW(),
                            error = %s,
                            updated_at = NOW()
                        WHERE t.status = 'running'
                          AND t.started_at < NOW() - '10 minutes'::interval
                          AND (
                              SELECT MAX(l.created_at)
                              FROM sys.task_log l
                              WHERE l.task_id = t.task_id
                          ) < NOW() - (%s || ' minutes')::interval
                        """,
                        (f"stuck: {LOG_STUCK_MINUTES}分钟无新日志，疑似卡死",
                         str(LOG_STUCK_MINUTES)),
                    )
                    count_stuck = cur.rowcount

                    total = count_timeout + count_stuck
                    if total > 0:
                        print(f"[TaskManager] 收割 {total} 个僵尸任务 "
                              f"(超时={count_timeout}, 卡死={count_stuck})",
                              file=sys.stderr)
        except Exception as e:
            print(f"[TaskManager] reap_zombie error: {e}", file=sys.stderr)

    def _run_task(self, task_id: str):
        task = _load_task(task_id)
        if not task:
            return
        cmd = list(task["cmd"])

        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }

        try:
            _append_log(task_id, f"[TASK] 开始执行，cwd={WORKER_SCRIPTS_DIR.parent}")
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKER_SCRIPTS_DIR.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                env=env,
                bufsize=0,
            )
            self._local_procs[task_id] = proc

            assert proc.stdout
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                _append_log(task_id, line)
                self._try_parse_stats(task_id, line)

            proc.wait()
            returncode = proc.returncode
            self._local_procs.pop(task_id, None)

            # 重新读取确认状态（可能被 stop 改过）
            task = _load_task(task_id)
            if not task:
                return
            if task["status"] == "stopped":
                return
            _update_task(
                task_id,
                ended_at=time.time(),
                status="done" if returncode == 0 else "failed",
                error=None if returncode == 0 else f"exit code {returncode}",
            )

        except Exception as e:
            self._local_procs.pop(task_id, None)
            _append_log(task_id, f"[ERROR] {str(e)[:200]}")
            _update_task(
                task_id,
                ended_at=time.time(),
                status="failed",
                error=str(e)[:200],
            )

    def _try_parse_stats(self, task_id: str, line: str):
        stripped = line.strip()
        stats = {}

        # JSON 行
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    stats = data
            except Exception:
                pass

        # 进度行
        if not stats and stripped.startswith("[") and "]" in stripped:
            try:
                parts = stripped.split("|")
                left = parts[0]
                pct = ""
                for token in left.split():
                    if "%" in token:
                        cleaned = token.replace("%", "").replace("[", "").replace("]", "")
                        if cleaned.isdigit():
                            pct = token
                            break
                if pct:
                    stats["progress_pct"] = pct
                for token in left.split():
                    if token.startswith("OK:"):
                        stats["ok"] = token.split(":")[1]
                    elif token.startswith("FAIL:"):
                        stats["fail"] = token.split(":")[1]
                    elif token.startswith("+") and "docs" in token:
                        stats["discovered"] = token.split("+")[1].split()[0]
            except Exception:
                pass

        if stats:
            task = _load_task(task_id)
            if task:
                cur_stats = task.get("stats") or {}
                cur_stats.update(stats)
                _update_task(task_id, stats=cur_stats)
