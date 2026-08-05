"""
后台任务管理器：用线程池跑各种采集脚本，Web 端只管发指令和查状态。

任务状态持久化到 JSON 文件，支持多 worker（gunicorn 多进程）共享状态。
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Docker 或本地环境判断脚本路径
if os.path.exists("/app/scripts/bin"):
    WORKER_SCRIPTS_DIR = Path("/app/scripts/bin")
    STATE_DIR = Path("/app/task_state")
else:
    WORKER_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "bin"
    STATE_DIR = Path(__file__).resolve().parent / "task_state"

STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "tasks.json"
LOCK_FILE = STATE_DIR / "tasks.lock"
LOG_DIR = STATE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MAX_LOG_LINES = 1000


@dataclass
class TaskInfo:
    task_id: str
    name: str
    status: str  # pending / running / done / failed / stopped
    cmd: list[str]
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    stats: dict = field(default_factory=dict)
    error: Optional[str] = None


# ── 文件锁辅助 ──────────────────────────────────────────────

class _FileLock:
    """基于 fcntl 的跨进程文件锁。"""
    def __init__(self, path: Path):
        self.path = path
        self._fd: Optional[int] = None

    def __enter__(self):
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _lock():
    return _FileLock(LOCK_FILE)


# ── 状态读写 ────────────────────────────────────────────────

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"tasks": {}, "pending": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"tasks": {}, "pending": []}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _append_log(task_id: str, line: str) -> None:
    log_path = LOG_DIR / f"{task_id}.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _read_log(task_id: str, limit: int = 200) -> list[str]:
    log_path = LOG_DIR / f"{task_id}.log"
    if not log_path.exists():
        return []
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.rstrip("\n") for l in lines[-limit:]]


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
        with _lock():
            state = _load_state()
            state["tasks"][task_id] = {
                "task_id": task_id,
                "name": name,
                "status": "pending",
                "cmd": cmd,
                "started_at": now,  # pending 也有时间戳，用于排序
                "ended_at": None,
                "stats": {},
                "error": None,
            }
            state["pending"].append(task_id)
            _save_state(state)
        # 提交后立刻写一条启动日志，确认任务注册成功
        _append_log(task_id, f"[TASK] 任务已提交: {name}")
        _append_log(task_id, f"[TASK] CMD: {' '.join(cmd)}")
        return task_id

    def stop_task(self, task_id: str) -> bool:
        with _lock():
            state = _load_state()
            task = state["tasks"].get(task_id)
            if not task:
                return False
            if task["status"] in ("done", "failed", "stopped"):
                return False
            task["status"] = "stopped"
            task["ended_at"] = time.time()
            # 从 pending 队列移除
            if task_id in state["pending"]:
                state["pending"].remove(task_id)
            _save_state(state)

        # 如果是本进程启动的，杀掉进程
        proc = self._local_procs.get(task_id)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        return True

    def list_tasks(self, limit: int = 20) -> list[dict]:
        with _lock():
            state = _load_state()
        items = list(state["tasks"].values())
        # 排序：running > pending > done/failed/stopped，同状态按时间倒序
        status_order = {"running": 0, "pending": 1, "done": 2, "failed": 3, "stopped": 4}
        items.sort(key=lambda t: (
            status_order.get(t.get("status"), 9),
            -(t.get("started_at") or 0)
        ))
        return [self._serialize(t) for t in items[:limit]]

    def get_task(self, task_id: str) -> Optional[dict]:
        with _lock():
            state = _load_state()
            task = state["tasks"].get(task_id)
            if not task:
                return None
            return self._serialize(task)

    def get_task_log(self, task_id: str, limit: int = 200) -> Optional[list[str]]:
        with _lock():
            state = _load_state()
            if task_id not in state["tasks"]:
                return None
        return _read_log(task_id, limit)

    def running_count(self) -> int:
        with _lock():
            state = _load_state()
            return sum(1 for t in state["tasks"].values() if t["status"] == "running")

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
        while not self._stop_flag:
            task_id = None
            with _lock():
                state = _load_state()
                running = sum(1 for t in state["tasks"].values() if t["status"] == "running")
                if running < self._max_concurrent and state["pending"]:
                    task_id = state["pending"].pop(0)
                    state["tasks"][task_id]["status"] = "running"
                    state["tasks"][task_id]["started_at"] = time.time()
                    _save_state(state)

            if task_id:
                self._run_task(task_id)
                continue

            time.sleep(1)

    def _run_task(self, task_id: str):
        with _lock():
            state = _load_state()
            task = state["tasks"].get(task_id)
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

            with _lock():
                state = _load_state()
                task = state["tasks"].get(task_id)
                if not task:
                    return
                if task["status"] == "stopped":
                    return
                task["ended_at"] = time.time()
                task["status"] = "done" if returncode == 0 else "failed"
                if returncode != 0:
                    task["error"] = f"exit code {returncode}"
                _save_state(state)

        except Exception as e:
            self._local_procs.pop(task_id, None)
            _append_log(task_id, f"[ERROR] {str(e)[:200]}")
            with _lock():
                state = _load_state()
                task = state["tasks"].get(task_id)
                if not task:
                    return
                task["ended_at"] = time.time()
                task["status"] = "failed"
                task["error"] = str(e)[:200]
                _save_state(state)

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
            with _lock():
                state = _load_state()
                task = state["tasks"].get(task_id)
                if task:
                    task.setdefault("stats", {}).update(stats)
                    _save_state(state)
