"""
KOL 模块数据库操作。

使用原生 psycopg3，风格与项目其他模块一致。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

# 路径兼容：Docker / 本地
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent  # workbench/
    CODE_ROOT = WORKSPACE_ROOT.parent  # 05_代码与脚本/
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

from crypto_research.config import get_settings  # noqa: E402

_database_url: str | None = None


def _get_db_url() -> str:
    """惰性获取数据库 URL（避免模块导入时就建立连接导致启动崩溃）。"""
    global _database_url
    if _database_url is None:
        _database_url = get_settings(require_database=True).database_url
    return _database_url


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """获取数据库连接（上下文管理器，自动提交/回滚）。"""
    conn = psycopg.connect(_get_db_url(), connect_timeout=30, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 博主档案 (kol_profile)
# ============================================================

def list_active_profiles(platform_code: str | None = None) -> list[dict]:
    """列出所有启用监控的博主。"""
    sql = "SELECT * FROM biz.kol_profile WHERE is_active = TRUE"
    params: list = []
    if platform_code:
        sql += " AND platform_code = %s"
        params.append(platform_code)
    sql += " ORDER BY profile_id"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def list_all_profiles() -> list[dict]:
    """列出所有博主（含停用）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM biz.kol_profile ORDER BY is_active DESC, profile_id"
        ).fetchall()


def get_profile(profile_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM biz.kol_profile WHERE profile_id = %s",
            (profile_id,),
        ).fetchone()


def upsert_profile(
    platform_code: str,
    platform_user_id: str,
    nickname: str,
    *,
    avatar_url: str | None = None,
    follower_count: int | None = None,
    is_active: bool = True,
    notes: str | None = None,
    extra_json: dict | None = None,
) -> dict:
    """新增或更新博主档案（按 platform_code + platform_user_id 去重）。"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT profile_id FROM biz.kol_profile "
            "WHERE platform_code = %s AND platform_user_id = %s",
            (platform_code, platform_user_id),
        ).fetchone()

        if existing:
            sets = ["nickname = %s", "updated_at = NOW()"]
            params: list = [nickname]
            if avatar_url is not None:
                sets.append("avatar_url = %s")
                params.append(avatar_url)
            if follower_count is not None:
                sets.append("follower_count = %s")
                params.append(follower_count)
            if notes is not None:
                sets.append("notes = %s")
                params.append(notes)
            if extra_json is not None:
                sets.append("extra_json = COALESCE(extra_json, '{}'::jsonb) || %s::jsonb")
                params.append(extra_json)
            params.extend([platform_code, platform_user_id])
            row = conn.execute(
                f"UPDATE biz.kol_profile SET {', '.join(sets)} "
                "WHERE platform_code = %s AND platform_user_id = %s RETURNING *",
                params,
            ).fetchone()
            return row
        else:
            row = conn.execute(
                "INSERT INTO biz.kol_profile "
                "(platform_code, platform_user_id, nickname, avatar_url, "
                " follower_count, is_active, notes, extra_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (platform_code, platform_user_id, nickname, avatar_url,
                 follower_count, is_active, notes, extra_json),
            ).fetchone()
            return row


def update_profile_last_post(profile_id: int, last_post_id: str) -> None:
    """更新博主的最后一条已处理帖子 ID 和最后抓取时间。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE biz.kol_profile SET last_post_id = %s, last_crawled_at = NOW(), "
            "updated_at = NOW() WHERE profile_id = %s",
            (last_post_id, profile_id),
        )


def mark_profile_crawled(profile_id: int) -> None:
    """仅更新 last_crawled_at（抓到 0 帖时也调用，区分「从没抓过」与「抓过没帖」）。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE biz.kol_profile SET last_crawled_at = NOW(), updated_at = NOW() "
            "WHERE profile_id = %s",
            (profile_id,),
        )


def set_profile_active(profile_id: int, is_active: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE biz.kol_profile SET is_active = %s, updated_at = NOW() "
            "WHERE profile_id = %s",
            (is_active, profile_id),
        )


def increment_signal_count(profile_id: int) -> None:
    """博主累计信号数 +1。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE biz.kol_profile SET total_signals = total_signals + 1, "
            "updated_at = NOW() WHERE profile_id = %s",
            (profile_id,),
        )


# ============================================================
# 帖子 (kol_post)
# ============================================================

def insert_post(
    profile_id: int,
    platform_code: str,
    platform_post_id: str,
    content_text: str,
    image_urls: list[str],
    post_url: str | None,
    posted_at: str,  # ISO 格式字符串
    raw_json: dict | None = None,
) -> dict | None:
    """插入一条新帖子。已存在则返回 None（去重）。"""
    with get_conn() as conn:
        try:
            row = conn.execute(
                "INSERT INTO biz.kol_post "
                "(profile_id, platform_code, platform_post_id, content_text, "
                " image_urls, post_url, posted_at, raw_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING *",
                (profile_id, platform_code, platform_post_id, content_text,
                 image_urls, post_url, posted_at,
                 psycopg.types.json.Json(raw_json) if raw_json is not None else None),
            ).fetchone()
            return row
        except psycopg.errors.UniqueViolation:
            return None


def get_post(post_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM biz.kol_post WHERE post_id = %s", (post_id,)
        ).fetchone()


def list_posts(
    profile_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM biz.kol_post"
    params: list = []
    if profile_id:
        sql += " WHERE profile_id = %s"
        params.append(profile_id)
    sql += " ORDER BY posted_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def list_posts_pending_ai(limit: int = 20) -> list[dict]:
    """列出待 AI 分析的帖子（ai_failed 也包含，靠 retry_count 控制）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT p.*, pr.platform_code, pr.nickname as profile_nickname "
            "FROM biz.kol_post p JOIN biz.kol_profile pr ON p.profile_id = pr.profile_id "
            "WHERE p.post_id NOT IN (SELECT post_id FROM biz.kol_signal) "
            "  AND p.ai_retry_count < 3 "
            "ORDER BY p.posted_at ASC LIMIT %s",
            (limit,),
        ).fetchall()


def mark_post_ai_failed(post_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE biz.kol_post SET ai_failed = TRUE, ai_retry_count = ai_retry_count + 1, "
            "updated_at = NOW() WHERE post_id = %s",
            (post_id,),
        )


def mark_post_ai_ok(post_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE biz.kol_post SET ai_failed = FALSE, updated_at = NOW() "
            "WHERE post_id = %s",
            (post_id,),
        )


# ============================================================
# 信号 (kol_signal)
# ============================================================

def insert_signal(data: dict) -> dict | None:
    """插入一条信号记录。data 包含所有业务字段。

    若 post_id 已存在（双调度去重），返回 None。
    """
    fields = [
        "post_id", "profile_id", "asset_id", "post_type", "direction",
        "symbol", "entry_condition", "entry_price", "stop_loss",
        "take_profit", "leverage", "support_level", "resistance_level",
        "already_entered", "has_pnl_number", "confidence",
    ]
    columns = ", ".join(fields)
    placeholders = ", ".join([f"%({f})s" for f in fields])
    with get_conn() as conn:
        row = conn.execute(
            f"INSERT INTO biz.kol_signal ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (post_id) DO NOTHING RETURNING *",
            data,
        ).fetchone()
        return row


def get_signal(signal_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT s.*, p.content_text, p.posted_at, p.post_url, p.image_urls, "
            "pr.nickname as profile_nickname, pr.platform_code, pr.follower_count "
            "FROM biz.kol_signal s "
            "JOIN biz.kol_post p ON s.post_id = p.post_id "
            "JOIN biz.kol_profile pr ON s.profile_id = pr.profile_id "
            "WHERE s.signal_id = %s",
            (signal_id,),
        ).fetchone()


def list_signals(
    profile_id: int | None = None,
    post_type: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = (
        "SELECT s.*, p.posted_at, p.post_url, p.content_text, "
        "pr.nickname as profile_nickname, pr.platform_code "
        "FROM biz.kol_signal s "
        "JOIN biz.kol_post p ON s.post_id = p.post_id "
        "JOIN biz.kol_profile pr ON s.profile_id = pr.profile_id "
        "WHERE 1=1"
    )
    params: list = []
    if profile_id:
        sql += " AND s.profile_id = %s"
        params.append(profile_id)
    if post_type:
        sql += " AND s.post_type = %s"
        params.append(post_type)
    if direction:
        sql += " AND s.direction = %s"
        params.append(direction)
    sql += " ORDER BY s.created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def list_signals_pending_alert(confidence_threshold: float = 0.8,
                               max_age_hours: int = 24) -> list[dict]:
    """列出待发送邮件的信号：prediction/analysis + 有标的 + 有方向 + 高置信度 + 24h内 + 未发过。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT s.*, p.content_text, p.posted_at, p.post_url, p.image_urls, "
            "pr.nickname as profile_nickname, pr.platform_code, pr.follower_count "
            "FROM biz.kol_signal s "
            "JOIN biz.kol_post p ON s.post_id = p.post_id "
            "JOIN biz.kol_profile pr ON s.profile_id = pr.profile_id "
            "WHERE s.post_type IN ('prediction', 'analysis') "
            "  AND s.already_entered = FALSE "
            "  AND s.confidence >= %s "
            "  AND s.is_alerted = FALSE "
            "  AND s.alert_failed = FALSE "
            "  AND s.asset_id IS NOT NULL "
            "  AND s.direction IN ('long', 'short') "
            "  AND p.posted_at >= NOW() - (%s || ' hours')::interval "
            "ORDER BY s.created_at ASC",
            (confidence_threshold, str(max_age_hours)),
        ).fetchall()


def mark_signal_alerted(signal_id: int, success: bool, error: str | None = None) -> None:
    with get_conn() as conn:
        if success:
            conn.execute(
                "UPDATE biz.kol_signal SET is_alerted = TRUE, alerted_at = NOW(), "
                "alert_failed = FALSE, alert_error = NULL, updated_at = NOW() "
                "WHERE signal_id = %s",
                (signal_id,),
            )
        else:
            conn.execute(
                "UPDATE biz.kol_signal SET alert_failed = TRUE, alert_error = %s, "
                "updated_at = NOW() WHERE signal_id = %s",
                (error, signal_id),
            )


# ============================================================
# 币种匹配辅助
# ============================================================

def find_asset_by_symbol(symbol: str) -> int | None:
    """通过 symbol 查找 asset_id（不区分大小写）。"""
    if not symbol:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT asset_id FROM core.asset "
            "WHERE UPPER(canonical_symbol) = UPPER(%s) "
            "ORDER BY asset_id LIMIT 1",
            (symbol.strip(),),
        ).fetchone()
        return row["asset_id"] if row else None
