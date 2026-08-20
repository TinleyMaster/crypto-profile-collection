"""
KOL 监控 Web 面板 — Flask 路由。

提供以下 API：
  GET  /api/kol/profiles           博主列表
  POST /api/kol/profiles           新增博主
  PUT  /api/kol/profiles/<id>      更新博主
  POST /api/kol/profiles/<id>/toggle  启用/停用
  POST /api/kol/profiles/<id>/crawl   立即抓取
  GET  /api/kol/signals            信号列表
  GET  /api/kol/signals/<id>       信号详情
  GET  /api/kol/posts              帖子列表
  GET  /api/kol/stats              统计概览
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from flask import Blueprint, jsonify, request

# 路径兼容
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
    STATE_DIR = Path("/app/task_state")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
    CODE_ROOT = WORKSPACE_ROOT.parent
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"
    STATE_DIR = WORKSPACE_ROOT / "task_state"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

from . import db  # noqa: E402

kol_bp = Blueprint("kol", __name__, url_prefix="/api/kol")

# 手动抓取任务状态（文件持久化，跨 gunicorn worker 共享）
STATE_DIR.mkdir(parents=True, exist_ok=True)
_CRAWL_STATE_FILE = STATE_DIR / "kol_crawl_state.json"
_CRAWL_LOCK_FILE = STATE_DIR / "kol_crawl_state.lock"


def _get_file_lock():
    """跨进程文件锁（fcntl，Linux 可用；Windows 降级为线程锁）。"""
    try:
        import fcntl
        fd = os.open(str(_CRAWL_LOCK_FILE), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except (ImportError, OSError):
        # Windows 或不支持时降级
        return None


def _release_file_lock(fd):
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except (ImportError, OSError):
        pass


def _load_crawl_state() -> dict:
    if not _CRAWL_STATE_FILE.exists():
        return {}
    try:
        with open(_CRAWL_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # JSON key 是字符串，转回 int
        return {int(k): v for k, v in data.items()}
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _save_crawl_state(state: dict) -> None:
    tmp = _CRAWL_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, _CRAWL_STATE_FILE)


@kol_bp.route("/stats")
def get_stats():
    """KOL 模块统计概览。"""
    with db.get_conn() as conn:
        total_profiles = conn.execute(
            "SELECT COUNT(*) as cnt FROM biz.kol_profile"
        ).fetchone()["cnt"]
        active_profiles = conn.execute(
            "SELECT COUNT(*) as cnt FROM biz.kol_profile WHERE is_active = TRUE"
        ).fetchone()["cnt"]
        total_posts = conn.execute(
            "SELECT COUNT(*) as cnt FROM biz.kol_post"
        ).fetchone()["cnt"]
        total_signals = conn.execute(
            "SELECT COUNT(*) as cnt FROM biz.kol_signal"
        ).fetchone()["cnt"]
        prediction_signals = conn.execute(
            "SELECT COUNT(*) as cnt FROM biz.kol_signal WHERE post_type = 'prediction'"
        ).fetchone()["cnt"]
        alerts_sent = conn.execute(
            "SELECT COUNT(*) as cnt FROM biz.kol_signal WHERE is_alerted = TRUE"
        ).fetchone()["cnt"]
        today_signals = conn.execute(
            "SELECT COUNT(*) as cnt FROM biz.kol_signal "
            "WHERE created_at >= CURRENT_DATE"
        ).fetchone()["cnt"]

    return jsonify({
        "total_profiles": total_profiles,
        "active_profiles": active_profiles,
        "total_posts": total_posts,
        "total_signals": total_signals,
        "prediction_signals": prediction_signals,
        "alerts_sent": alerts_sent,
        "today_signals": today_signals,
    })


@kol_bp.route("/profiles")
def list_profiles():
    """博主列表。"""
    profiles = db.list_all_profiles()
    # 补充信号统计
    with db.get_conn() as conn:
        for p in profiles:
            stats = conn.execute(
                "SELECT "
                "  COUNT(*) as total_signals, "
                "  SUM(CASE WHEN post_type = 'prediction' THEN 1 ELSE 0 END) as prediction_count, "
                "  SUM(CASE WHEN is_alerted = TRUE THEN 1 ELSE 0 END) as alert_count "
                "FROM biz.kol_signal WHERE profile_id = %s",
                (p["profile_id"],),
            ).fetchone()
            p["signal_stats"] = {
                "total": stats["total_signals"] or 0,
                "prediction": stats["prediction_count"] or 0,
                "alerts": stats["alert_count"] or 0,
            }
    return jsonify(profiles)


@kol_bp.route("/profiles", methods=["POST"])
def create_profile():
    """新增博主。"""
    data = request.get_json() or {}
    required = ["platform_code", "platform_user_id", "nickname"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"缺少必填字段: {field}"}), 400

    try:
        profile = db.upsert_profile(
            platform_code=data["platform_code"],
            platform_user_id=data["platform_user_id"],
            nickname=data["nickname"],
            avatar_url=data.get("avatar_url"),
            follower_count=data.get("follower_count"),
            is_active=data.get("is_active", True),
            notes=data.get("notes"),
            extra_json=data.get("extra_json"),
        )
        return jsonify(profile)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@kol_bp.route("/profiles/<int:profile_id>", methods=["PUT"])
def update_profile(profile_id: int):
    """更新博主。"""
    data = request.get_json() or {}
    existing = db.get_profile(profile_id)
    if not existing:
        return jsonify({"error": "博主不存在"}), 404

    # 用 upsert 的方式更新（基于 platform_code + platform_user_id）
    try:
        profile = db.upsert_profile(
            platform_code=existing["platform_code"],
            platform_user_id=existing["platform_user_id"],
            nickname=data.get("nickname", existing["nickname"]),
            avatar_url=data.get("avatar_url", existing.get("avatar_url")),
            follower_count=data.get("follower_count", existing.get("follower_count")),
            notes=data.get("notes", existing.get("notes")),
            extra_json=data.get("extra_json"),
        )
        # is_active 单独更新
        if "is_active" in data:
            db.set_profile_active(profile_id, data["is_active"])
            profile = db.get_profile(profile_id)
        return jsonify(profile)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@kol_bp.route("/profiles/<int:profile_id>/toggle", methods=["POST"])
def toggle_profile(profile_id: int):
    """启用/停用博主。"""
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "博主不存在"}), 404

    new_state = not profile["is_active"]
    db.set_profile_active(profile_id, new_state)
    return jsonify({"profile_id": profile_id, "is_active": new_state})


@kol_bp.route("/profiles/<int:profile_id>/crawl", methods=["POST"])
def crawl_profile(profile_id: int):
    """手动触发单个博主的立即抓取（异步）。"""
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "博主不存在"}), 404

    lock_fd = _get_file_lock()
    try:
        state = _load_crawl_state()
        if profile_id in state and state[profile_id].get("status") == "running":
            return jsonify({"error": "抓取正在进行中"}), 409

        state[profile_id] = {"status": "running", "started_at": _now_iso()}
        _save_crawl_state(state)
    finally:
        _release_file_lock(lock_fd)

    def _run():
        try:
            from .runner import run_crawl_once
            stats = run_crawl_once(profile_id=profile_id)
            lock_fd = _get_file_lock()
            try:
                state = _load_crawl_state()
                state[profile_id] = {
                    "status": "done",
                    "finished_at": _now_iso(),
                    "stats": stats,
                }
                _save_crawl_state(state)
            finally:
                _release_file_lock(lock_fd)
        except Exception as e:
            lock_fd = _get_file_lock()
            try:
                state = _load_crawl_state()
                state[profile_id] = {
                    "status": "failed",
                    "finished_at": _now_iso(),
                    "error": str(e),
                }
                _save_crawl_state(state)
            finally:
                _release_file_lock(lock_fd)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({"status": "running", "profile_id": profile_id})


@kol_bp.route("/profiles/<int:profile_id>/crawl-status")
def crawl_status(profile_id: int):
    """查询手动抓取状态。"""
    state = _load_crawl_state()
    status = state.get(profile_id, {"status": "idle"})
    return jsonify(status)


@kol_bp.route("/signals")
def list_signals():
    """信号列表（支持筛选）。"""
    profile_id = request.args.get("profile_id", type=int)
    post_type = request.args.get("post_type")
    direction = request.args.get("direction")
    limit = min(request.args.get("limit", 50, type=int), 200)
    offset = request.args.get("offset", 0, type=int)

    signals = db.list_signals(
        profile_id=profile_id,
        post_type=post_type,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    return jsonify(signals)


@kol_bp.route("/signals/<int:signal_id>")
def get_signal(signal_id: int):
    """信号详情。"""
    signal = db.get_signal(signal_id)
    if not signal:
        return jsonify({"error": "信号不存在"}), 404
    return jsonify(signal)


@kol_bp.route("/posts")
def list_posts():
    """帖子列表。"""
    profile_id = request.args.get("profile_id", type=int)
    limit = min(request.args.get("limit", 50, type=int), 200)
    offset = request.args.get("offset", 0, type=int)

    posts = db.list_posts(profile_id=profile_id, limit=limit, offset=offset)
    return jsonify(posts)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
