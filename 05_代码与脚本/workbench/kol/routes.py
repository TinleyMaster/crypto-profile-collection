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
  POST /api/kol/discover           从分享链接发现博主（预览）
  POST /api/kol/discover/add       确认添加已发现的博主
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from flask import Blueprint, jsonify, request

# 路径兼容
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
    CODE_ROOT = WORKSPACE_ROOT.parent
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

from . import db  # noqa: E402

kol_bp = Blueprint("kol", __name__, url_prefix="/api/kol")

# 手动抓取任务状态（迁移到 sys.async_task_state 表，替代文件+fcntl方案）
# task_type = "kol_crawl", entity_key = str(profile_id)
# 迁移时间: 2026-09-11

# 从 task_manager 导入 AsyncTaskState（注意路径兼容）
if os.path.exists("/app/workbench"):
    import sys as _sys
    if "/app/workbench" not in _sys.path:
        _sys.path.insert(0, "/app/workbench")
try:
    from task_manager import AsyncTaskState  # noqa: E402
except ImportError:
    # 本地开发环境：从上级目录导入
    import sys as _sys
    _parent = str(Path(__file__).resolve().parent.parent)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    from task_manager import AsyncTaskState  # noqa: E402


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

    state = AsyncTaskState("kol_crawl", str(profile_id))
    if not state.try_start({"started_at": _now_iso()}):
        return jsonify({"error": "抓取正在进行中"}), 409

    def _run():
        try:
            from .runner import run_crawl_once
            stats = run_crawl_once(profile_id=profile_id)
            state.set_done({
                "finished_at": _now_iso(),
                "stats": stats,
            })
        except Exception as e:
            state.set_failed(str(e), {
                "finished_at": _now_iso(),
                "error": str(e),
            })

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({"status": "running", "profile_id": profile_id})


@kol_bp.route("/profiles/<int:profile_id>/crawl-status")
def crawl_status(profile_id: int):
    """查询手动抓取状态。"""
    state = AsyncTaskState("kol_crawl", str(profile_id))
    item = state.get()
    status = item.get("status", "idle")
    if status == "idle":
        return jsonify({"status": "idle"})
    # 合并 payload 字段 + status，兼容原返回格式
    payload = item.get("payload") or {}
    result = {"status": status}
    result.update(payload)
    if status == "failed" and "error" not in result and item.get("error"):
        result["error"] = item["error"]
    return jsonify(result)


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


# ============================================================
# 博主发现：分享链接 → 自动解析 + 预览
# ============================================================

@kol_bp.route("/discover", methods=["POST"])
def discover_kol():
    """从分享链接发现博主（返回预览信息，用户确认后再添加）。

    Request:
        { "share_url": "https://app.binance.com/uni-qr/cpro/god_of_trader_tony?..." }

    Response:
        {
            "platform_code": "binance_square",
            "platform_user_id": "god_of_trader_tony",
            "nickname": "币圈交易之神Tony",
            "avatar_url": "...",
            "follower_count": 12345,
            "profile_url": "...",
            "already_exists": false,
            "profile_id": null
        }
    """
    data = request.get_json() or {}
    share_url = (data.get("share_url") or "").strip()
    if not share_url:
        return jsonify({"error": "缺少 share_url 参数"}), 400

    try:
        from .discover import discover_profile
        profile = discover_profile(share_url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"发现博主失败: {e}"}), 500

    # 检查是否已存在
    already_exists = False
    existing_id = None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT profile_id FROM biz.kol_profile "
            "WHERE platform_code = %s AND platform_user_id = %s",
            (profile.platform_code, profile.platform_user_id),
        ).fetchone()
        if row:
            already_exists = True
            existing_id = row["profile_id"]

    return jsonify({
        "platform_code": profile.platform_code,
        "platform_user_id": profile.platform_user_id,
        "nickname": profile.nickname,
        "avatar_url": profile.avatar_url,
        "follower_count": profile.follower_count,
        "profile_url": profile.profile_url,
        "extra": profile.extra,
        "already_exists": already_exists,
        "profile_id": existing_id,
    })


@kol_bp.route("/discover/add", methods=["POST"])
def add_discovered_kol():
    """确认添加已发现的博主（入库并启用监控）。

    Request:
        {
            "platform_code": "binance_square",
            "platform_user_id": "god_of_trader_tony",
            "nickname": "币圈交易之神Tony",
            "avatar_url": "...",
            "follower_count": 12345,
            "notes": "手动添加",
            "auto_crawl": true
        }

    Response:
        { "profile_id": 123, "is_new": true, "crawl_started": true }
    """
    data = request.get_json() or {}
    required = ["platform_code", "platform_user_id", "nickname"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"缺少必填字段: {field}"}), 400

    # 检查是否已存在
    existing = None
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM biz.kol_profile "
            "WHERE platform_code = %s AND platform_user_id = %s",
            (data["platform_code"], data["platform_user_id"]),
        ).fetchone()

    is_new = existing is None

    try:
        profile = db.upsert_profile(
            platform_code=data["platform_code"],
            platform_user_id=data["platform_user_id"],
            nickname=data["nickname"],
            avatar_url=data.get("avatar_url"),
            follower_count=data.get("follower_count"),
            is_active=True,
            notes=data.get("notes"),
            extra_json=data.get("extra"),
        )
    except Exception as e:
        return jsonify({"error": f"添加博主失败: {e}"}), 500

    profile_id = profile["profile_id"]
    auto_crawl = data.get("auto_crawl", True)
    crawl_started = False

    # 如果是新增且开启自动抓取，异步触发首次抓取
    if is_new and auto_crawl:
        state = AsyncTaskState("kol_crawl", str(profile_id))
        crawl_started = state.try_start({"started_at": _now_iso()})

        if crawl_started:
            def _run():
                try:
                    from .runner import run_crawl_once
                    stats = run_crawl_once(profile_id=profile_id)
                    state.set_done({
                        "finished_at": _now_iso(),
                        "stats": stats,
                    })
                except Exception as e:
                    state.set_failed(str(e), {
                        "finished_at": _now_iso(),
                        "error": str(e),
                    })

            t = threading.Thread(target=_run, daemon=True)
            t.start()

    return jsonify({
        "profile_id": profile_id,
        "is_new": is_new,
        "crawl_started": crawl_started,
        "profile": profile,
    })
