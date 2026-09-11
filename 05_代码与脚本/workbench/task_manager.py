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
# b2_ai_noise_clean_by_asset_auto 等按资产循环的长任务，单轮之间可能数十分钟无日志，
# 但仍正常推进。放宽到 90 分钟，避免误杀大循环任务。
LOG_STUCK_MINUTES = 90  # running 任务超过该时长无新日志，视为卡死，提前收割

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
            min_size=0,
            max_size=2,
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
                INSERT INTO sys.task (task_id, name, status, cmd, started_at, ended_at, stats, error, category)
                VALUES (%s, %s, %s, %s::text[], %s, %s, %s::jsonb, %s, %s)
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
                    task.get("category", "core"),
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


# ── 类别并发配置 ──────────────────────────────────────────────
# chain: 链上重任务（易卡死/耗时数小时），默认 2 槽；链上任务多为网络 IO 等待，
#        限制过高易触发 RPC 限流，可用环境变量 CATEGORY_MAX_CHAIN 调低
# core:  核心业务（催化剂/早报/采集），保底可用
# monitor: 监控类（低优先级，仅空闲时跑）
CATEGORY_MAX = {
    "chain": int(os.getenv("CATEGORY_MAX_CHAIN", "2")),
    "core": int(os.getenv("CATEGORY_MAX_CORE", "2")),
    "monitor": int(os.getenv("CATEGORY_MAX_MONITOR", "1")),
}


# ── TaskManager ─────────────────────────────────────────────

class TaskManager:
    def __init__(self, max_concurrent: int = 2):
        self._max_concurrent = max_concurrent
        self._local_procs: dict[str, subprocess.Popen] = {}
        self._stop_flag = False
        self._thread = threading.Thread(target=self._runner_loop, daemon=True)
        self._thread.start()

    # ── 公共 API ──

    def submit_task(self, name: str, cmd: list[str], category: str = "core") -> str:
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
            "category": category,
        }
        _insert_task(task)
        # 提交后立刻写一条启动日志，确认任务注册成功
        _append_log(task_id, f"[TASK] 任务已提交: {name}")
        _append_log(task_id, f"[TASK] CMD: {' '.join(cmd)}")
        return task_id

    def submit_func_task(self, name: str, func, category: str = "core") -> str:
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
            "category": category,
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
                        # 按类别统计 running 数
                        cur.execute(
                            """
                            SELECT category, COUNT(*) FROM sys.task
                            WHERE status = 'running' GROUP BY category
                            """
                        )
                        running_by_cat = {row[0]: row[1] for row in cur.fetchall()}
                        total_running = sum(running_by_cat.values())

                        if total_running < self._max_concurrent:
                            # 优先取非 monitor 任务（与旧行为兼容）
                            cur.execute(
                                """
                                SELECT task_id, category FROM sys.task
                                WHERE status = 'pending'
                                ORDER BY
                                    (CASE WHEN name ILIKE '%monitor%' THEN 1 ELSE 0 END),
                                    started_at ASC
                                LIMIT 5
                                FOR UPDATE SKIP LOCKED
                                """
                            )
                            candidates = cur.fetchall()
                            # 从候选中选一个 category 未满的
                            for row in candidates:
                                cid, cat = row[0], row[1]
                                cat_max = CATEGORY_MAX.get(cat, 2)
                                if running_by_cat.get(cat, 0) < cat_max:
                                    task_id = cid
                                    cur.execute(
                                        "UPDATE sys.task SET status = 'running', started_at = NOW(), updated_at = NOW() WHERE task_id = %s",
                                        (cid,),
                                    )
                                    break
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

        P1 修复：先 SELECT 出符合条件的 task_id 并终止真实子进程，再标记 DB 状态，
        避免"槽位已释放但进程仍在后台跑"导致的任务重叠和资源泄漏。
        """
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    # 条件1：超 12h 硬超时
                    cur.execute(
                        """
                        SELECT task_id FROM sys.task
                        WHERE status = 'running'
                          AND started_at < NOW() - (%s || ' hours')::interval
                        """,
                        (str(MAX_RUNTIME_HOURS),),
                    )
                    timeout_ids = [r[0] for r in cur.fetchall()]

                    # 条件2：LOG_STUCK_MINUTES 分钟无新日志 + 已运行至少 10min（卡死）
                    cur.execute(
                        """
                        SELECT t.task_id FROM sys.task t
                        WHERE t.status = 'running'
                          AND t.started_at < NOW() - '10 minutes'::interval
                          AND (
                              SELECT MAX(l.created_at)
                              FROM sys.task_log l
                              WHERE l.task_id = t.task_id
                          ) < NOW() - (%s || ' minutes')::interval
                        """,
                        (str(LOG_STUCK_MINUTES),),
                    )
                    stuck_ids = [r[0] for r in cur.fetchall()]

            # 先终止真实子进程，释放底层资源
            for tid in set(timeout_ids) | set(stuck_ids):
                proc = self._local_procs.get(tid)
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

            # 再统一标记 DB 状态（分别保留超时/卡死两种错误文案）
            with _get_db() as conn:
                with conn.cursor() as cur:
                    if timeout_ids:
                        cur.execute(
                            """
                            UPDATE sys.task
                            SET status = 'failed',
                                ended_at = NOW(),
                                error = %s,
                                updated_at = NOW()
                            WHERE task_id = ANY(%s)
                            """,
                            (f"timeout: 运行超过 {MAX_RUNTIME_HOURS}h 自动终止",
                             timeout_ids),
                        )
                    if stuck_ids:
                        cur.execute(
                            """
                            UPDATE sys.task
                            SET status = 'failed',
                                ended_at = NOW(),
                                error = %s,
                                updated_at = NOW()
                            WHERE task_id = ANY(%s)
                            """,
                            (f"stuck: {LOG_STUCK_MINUTES}分钟无新日志，疑似卡死",
                             stuck_ids),
                        )

            total = len(timeout_ids) + len(stuck_ids)
            if total > 0:
                print(f"[TaskManager] 收割 {total} 个僵尸任务 "
                      f"(超时={len(timeout_ids)}, 卡死={len(stuck_ids)}，已终止进程)",
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
            # 已被收割（reaper 置为 failed）时不覆盖状态和错误文案，
            # 否则 terminate 返回码 -15 会顶掉 "timeout/stuck" 提示
            if task["status"] == "failed":
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


# ═══════════════════════════════════════════════════════════════
# AsyncTaskState — 轻量级异步任务状态持久化
# ═══════════════════════════════════════════════════════════════
# 替代原来的 task_state/*.json + fcntl 文件锁方案。
# 统一用 sys.async_task_state 表，按 (task_type, entity_key) 唯一标识。
#
# 用法：
#   from task_manager import AsyncTaskState
#   state = AsyncTaskState("recrawl", str(asset_id))
#   if state.is_running(): return "already running"
#   state.set_running()
#   # ... 后台执行 ...
#   state.set_done(result={"ok": True, ...})
#
# 并发安全：SELECT ... FOR UPDATE 行级锁，多 worker 安全。

class AsyncTaskState:
    """轻量级异步任务状态管理器（数据库持久化）。

    替代 JSON 文件 + fcntl 文件锁方案，支持 gunicorn 多 worker 跨进程共享。
    """

    def __init__(self, task_type: str, entity_key: str):
        """
        Args:
            task_type: 任务类型，如 'recrawl', 'kol_crawl'
            entity_key: 实体标识，如 asset_id 或 profile_id 的字符串
        """
        self.task_type = task_type
        self.entity_key = str(entity_key)
        self._ensure_table()

    # ── 建表（首次使用时自动执行，幂等）──────────────────────

    @staticmethod
    def _ensure_table():
        """确保 sys.async_task_state 表存在。首次调用时建表，后续跳过。"""
        if getattr(AsyncTaskState, "_table_ensured", False):
            return
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS sys.async_task_state (
                            task_type       VARCHAR(50) NOT NULL,
                            entity_key      VARCHAR(200) NOT NULL,
                            status          VARCHAR(20) NOT NULL DEFAULT 'idle',
                            payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
                            started_at      TIMESTAMPTZ,
                            finished_at     TIMESTAMPTZ,
                            error           TEXT,
                            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (task_type, entity_key)
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_async_task_state_status
                        ON sys.async_task_state(status)
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_async_task_state_task_type
                        ON sys.async_task_state(task_type)
                    """)
                conn.commit()
            AsyncTaskState._table_ensured = True
        except Exception as e:
            # 建表失败不致命（可能是并发建表），打印警告后继续
            print(f"[AsyncTaskState] ensure_table warning: {e}", file=sys.stderr)

    # ── 读操作 ──────────────────────────────────────────────

    def get(self) -> dict:
        """读取当前状态。返回 dict，至少含 status 字段。"""
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT status, payload, started_at, finished_at, error
                        FROM sys.async_task_state
                        WHERE task_type = %s AND entity_key = %s
                        """,
                        (self.task_type, self.entity_key),
                    )
                    row = cur.fetchone()
            if not row:
                return {"status": "idle"}
            result = dict(row)
            # payload 可能是 dict 或 json 字符串，统一转 dict
            if isinstance(result.get("payload"), str):
                try:
                    result["payload"] = json.loads(result["payload"])
                except Exception:
                    result["payload"] = {}
            return result
        except Exception as e:
            print(f"[AsyncTaskState] get error: {e}", file=sys.stderr)
            return {"status": "idle"}

    def is_running(self) -> bool:
        """是否处于 running 状态。"""
        return self.get().get("status") == "running"

    def get_result(self):
        """获取完成后的结果（payload）。"""
        state = self.get()
        if state.get("status") not in ("done", "failed"):
            return None
        return state.get("payload") or {}

    # ── 写操作（原子）────────────────────────────────────────

    def set_running(self, payload: dict | None = None) -> bool:
        """标记为 running。返回 True 表示成功抢到执行权（之前不是 running）。"""
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    # UPSERT：不存在则插入 running，存在则更新为 running
                    cur.execute(
                        """
                        INSERT INTO sys.async_task_state (task_type, entity_key, status, payload, started_at, updated_at)
                        VALUES (%s, %s, 'running', %s::jsonb, NOW(), NOW())
                        ON CONFLICT (task_type, entity_key) DO UPDATE
                        SET status = 'running',
                            payload = %s::jsonb,
                            started_at = NOW(),
                            finished_at = NULL,
                            error = NULL,
                            updated_at = NOW()
                        RETURNING status
                        """,
                        (
                            self.task_type, self.entity_key,
                            json.dumps(payload or {}),
                            json.dumps(payload or {}),
                        ),
                    )
                conn.commit()
            return True
        except Exception as e:
            print(f"[AsyncTaskState] set_running error: {e}", file=sys.stderr)
            return False

    def try_start(self, payload: dict | None = None) -> bool:
        """尝试启动：仅当当前不是 running 时才标记为 running 并返回 True。
        已在 running 则返回 False（幂等去重）。
        """
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    # 先尝试行级锁（FOR UPDATE SKIP LOCKED 不支持的话用子查询兜底）
                    # 用 INSERT ... ON CONFLICT DO NOTHING + UPDATE 条件更新模拟
                    cur.execute(
                        """
                        INSERT INTO sys.async_task_state (task_type, entity_key, status, payload, started_at, updated_at)
                        VALUES (%s, %s, 'running', %s::jsonb, NOW(), NOW())
                        ON CONFLICT (task_type, entity_key) DO UPDATE
                        SET status = 'running',
                            payload = %s::jsonb,
                            started_at = NOW(),
                            finished_at = NULL,
                            error = NULL,
                            updated_at = NOW()
                        WHERE sys.async_task_state.status != 'running'
                        RETURNING (xmax = 0) AS inserted
                        """,
                        (
                            self.task_type, self.entity_key,
                            json.dumps(payload or {}),
                            json.dumps(payload or {}),
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
            # row 存在说明执行了 INSERT 或 UPDATE（即成功启动）
            # 不存在说明 status 已经是 running，DO UPDATE 的 WHERE 不匹配
            return row is not None
        except Exception as e:
            print(f"[AsyncTaskState] try_start error: {e}", file=sys.stderr)
            # 出错时保守返回 False，避免重复启动
            return False

    def set_done(self, payload: dict | None = None) -> None:
        """标记为 done，附带结果 payload。"""
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO sys.async_task_state (task_type, entity_key, status, payload, finished_at, updated_at)
                        VALUES (%s, %s, 'done', %s::jsonb, NOW(), NOW())
                        ON CONFLICT (task_type, entity_key) DO UPDATE
                        SET status = 'done',
                            payload = %s::jsonb,
                            finished_at = NOW(),
                            error = NULL,
                            updated_at = NOW()
                        """,
                        (
                            self.task_type, self.entity_key,
                            json.dumps(payload or {}),
                            json.dumps(payload or {}),
                        ),
                    )
                conn.commit()
        except Exception as e:
            print(f"[AsyncTaskState] set_done error: {e}", file=sys.stderr)

    def set_failed(self, error: str, payload: dict | None = None) -> None:
        """标记为 failed，附带错误信息。"""
        try:
            with _get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO sys.async_task_state (task_type, entity_key, status, payload, error, finished_at, updated_at)
                        VALUES (%s, %s, 'failed', %s::jsonb, %s, NOW(), NOW())
                        ON CONFLICT (task_type, entity_key) DO UPDATE
                        SET status = 'failed',
                            payload = %s::jsonb,
                            error = %s,
                            finished_at = NOW(),
                            updated_at = NOW()
                        """,
                        (
                            self.task_type, self.entity_key,
                            json.dumps(payload or {}),
                            error,
                            json.dumps(payload or {}),
                            error,
                        ),
                    )
                conn.commit()
        except Exception as e:
            print(f"[AsyncTaskState] set_failed error: {e}", file=sys.stderr)

    # ── 上下文管理器（with 语法糖）───────────────────────────

    @contextmanager
    def lock_and_run(self, payload: dict | None = None):
        """with 语法：自动 try_start，执行后自动 set_done/set_failed。
        若已在 running 则抛出 RuntimeError。

        用法：
            state = AsyncTaskState("recrawl", asset_id)
            with state.lock_and_run():
                result = do_work()
                state.set_done(result)
        """
        if not self.try_start(payload):
            raise RuntimeError(f"Task {self.task_type}/{self.entity_key} already running")
        try:
            yield self
        except Exception as e:
            self.set_failed(str(e))
            raise
