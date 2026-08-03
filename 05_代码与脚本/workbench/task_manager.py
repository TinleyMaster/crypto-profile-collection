"""
后台任务管理器：用线程池跑各种采集脚本，Web 端只管发指令和查状态。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os

# Docker 或本地环境判断脚本路径
if os.path.exists("/app/scripts/bin"):
    WORKER_SCRIPTS_DIR = Path("/app/scripts/bin")
else:
    WORKER_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "bin"


@dataclass
class TaskInfo:
    task_id: str
    name: str
    status: str  # pending / running / done / failed / stopped
    cmd: list[str]
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    log_tail: list[str] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    stats: dict = field(default_factory=dict)
    error: Optional[str] = None


class TaskManager:
    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, TaskInfo] = {}
        self._lock = threading.Lock()
        self._max_concurrent = max_concurrent
        self._thread = threading.Thread(target=self._runner_loop, daemon=True)
        self._pending: list[str] = []
        self._stop_flag = False
        self._cv = threading.Condition()
        self._thread.start()

    # ── 公共 API ──

    def submit_task(self, name: str, cmd: list[str]) -> str:
        task_id = uuid.uuid4().hex[:12]
        info = TaskInfo(task_id=task_id, name=name, status="pending", cmd=cmd)
        with self._lock:
            self._tasks[task_id] = info
            self._pending.append(task_id)
        with self._cv:
            self._cv.notify()
        return task_id

    def stop_task(self, task_id: str) -> bool:
        with self._lock:
            info = self._tasks.get(task_id)
            if not info:
                return False
            if info.status in ("done", "failed", "stopped"):
                return False
            if info.process:
                try:
                    info.process.terminate()
                except Exception:
                    pass
            info.status = "stopped"
            info.ended_at = time.time()
        return True

    def list_tasks(self, limit: int = 20) -> list[dict]:
        with self._lock:
            items = list(self._tasks.values())
        items.sort(key=lambda t: t.started_at or 0, reverse=True)
        return [self._serialize(t) for t in items[:limit]]

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            info = self._tasks.get(task_id)
            if not info:
                return None
            return self._serialize(info)

    def get_task_log(self, task_id: str, limit: int = 200) -> Optional[list[str]]:
        with self._lock:
            info = self._tasks.get(task_id)
            if not info:
                return None
            return list(info.log_tail[-limit:])

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == "running")

    # ── 内部 ──

    def _serialize(self, info: TaskInfo) -> dict:
        return {
            "task_id": info.task_id,
            "name": info.name,
            "status": info.status,
            "started_at": info.started_at,
            "ended_at": info.ended_at,
            "elapsed_sec": (
                round((info.ended_at or time.time()) - info.started_at, 1)
                if info.started_at
                else None
            ),
            "last_log": info.log_tail[-1] if info.log_tail else "",
            "stats": info.stats,
            "error": info.error,
        }

    def _runner_loop(self):
        while not self._stop_flag:
            # 取一个 pending 任务
            task_id = None
            with self._lock:
                running = sum(1 for t in self._tasks.values() if t.status == "running")
                if running < self._max_concurrent and self._pending:
                    task_id = self._pending.pop(0)
                    self._tasks[task_id].status = "running"
                    self._tasks[task_id].started_at = time.time()

            if task_id:
                self._run_task(task_id)
                continue

            # 没事做就等
            with self._cv:
                self._cv.wait(timeout=2)

    def _run_task(self, task_id: str):
        with self._lock:
            info = self._tasks[task_id]
            cmd = list(info.cmd)

        env = {
            **__import__("os").environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKER_SCRIPTS_DIR.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,  # 二进制读，手动用 utf-8 解码，避免 Windows GBK 问题
                env=env,
                bufsize=0,
            )
            with self._lock:
                if self._tasks[task_id].status == "stopped":
                    proc.terminate()
                    return
                self._tasks[task_id].process = proc

            # 逐行读日志
            assert proc.stdout
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                with self._lock:
                    info = self._tasks[task_id]
                    info.log_tail.append(line)
                    if len(info.log_tail) > 1000:
                        info.log_tail = info.log_tail[-1000:]
                    # 尝试解析 JSON 统计行
                    self._try_parse_stats(info, line)

            proc.wait()
            with self._lock:
                info = self._tasks[task_id]
                if info.status == "stopped":
                    return
                info.ended_at = time.time()
                info.status = "done" if proc.returncode == 0 else "failed"
                if proc.returncode != 0:
                    info.error = f"exit code {proc.returncode}"
        except Exception as e:
            with self._lock:
                info = self._tasks[task_id]
                info.ended_at = time.time()
                info.status = "failed"
                info.error = str(e)[:200]

    def _try_parse_stats(self, info: TaskInfo, line: str):
        """尝试从输出行提取统计数据"""
        # JSON 行：{"status": "complete", ...}
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    info.stats.update(data)
                return
            except Exception:
                pass

        # 进度行：[380/500 76%] OK:265 FAIL:115 +209 docs | 1.9/s ETA:62s
        if stripped.startswith("[") and "]" in stripped:
            try:
                parts = stripped.split("|")
                if len(parts) >= 2:
                    left = parts[0]
                    # 提取进度百分比
                    pct = ""
                    for token in left.split():
                        if (
                            "%" in token
                            and token.replace("%", "")
                            .replace("[", "")
                            .replace("]", "")
                            .isdigit()
                        ):
                            pct = token
                            break
                    if pct:
                        info.stats["progress_pct"] = pct
                    # OK/FAIL 数
                    for token in left.split():
                        if token.startswith("OK:"):
                            info.stats["ok"] = token.split(":")[1]
                        elif token.startswith("FAIL:"):
                            info.stats["fail"] = token.split(":")[1]
                        elif token.startswith("+") and "docs" in token:
                            info.stats["discovered"] = token.split("+")[1].split()[0]
            except Exception:
                pass
