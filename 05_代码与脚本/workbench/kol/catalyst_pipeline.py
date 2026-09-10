"""
KOL 催化剂管线：将 catalyst/news_media 类型 KOL 的帖子写入催化剂表。

设计思路：
  - 复用已有的 catalyst.pipeline（跨源去重 + 多资产关联 + 合并写入）
  - 复用 catalyst.linker（交易对 → asset_id 映射）
  - source_code 规则：kol_{kol_type}_{platform_code}_{profile_id}
    例：kol_catalyst_binance_square_5
  - 帖子的 trading_pairs / related_coins 直接作为关联交易对用于资产匹配
  - 帖子正文第一行当 title，全部正文当 body_text
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 路径兼容
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent  # workbench/
    CODE_ROOT = WORKSPACE_ROOT.parent  # 05_代码与脚本/
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

# 导入 catalyst 模块
from catalyst.models import CatalystItem  # noqa: E402
from catalyst.pipeline import upsert_catalyst_item  # noqa: E402
from catalyst.db import get_conn  # noqa: E402

logger = logging.getLogger(__name__)


def build_source_code(profile: dict) -> str:
    """根据博主档案生成 catalyst source_code。

    规则：kol_{kol_type}_{platform_code}_{profile_id}
    例：kol_catalyst_binance_square_5
    """
    kol_type = profile.get("kol_type", "kol")
    platform = profile.get("platform_code", "unknown")
    pid = profile.get("profile_id", 0)
    return f"kol_{kol_type}_{platform}_{pid}"


def build_event_category(profile: dict) -> str:
    """生成催化剂 event_category（展示用分类名）。"""
    nickname = profile.get("nickname", "")
    kol_type = profile.get("kol_type", "kol")
    type_label = {
        "catalyst": "催化剂账号",
        "news_media": "新闻媒体",
        "kol": "KOL观点",
    }.get(kol_type, kol_type)
    return f"{nickname} · {type_label}" if nickname else type_label


def scraped_post_to_catalyst(post, profile: dict) -> CatalystItem | None:
    """将 ScrapedPost 转换为 CatalystItem。

    Args:
        post: ScrapedPost 对象
        profile: kol_profile 行 dict

    Returns:
        CatalystItem，无正文则返回 None
    """
    if not post.content_text:
        return None

    # 标题：取正文第一行或前 80 字
    title = _extract_title(post.content_text)

    # 发布时间
    published_at = _parse_posted_at(post.posted_at)

    # 关联交易对：优先用 trading_pairs（结构化）
    related_pairs = list(post.trading_pairs) if post.trading_pairs else []

    # source_code 用 profile 维度生成
    source_code = build_source_code(profile)

    # 把 related_coins 的价格等信息塞进 raw_json 方便留底
    raw_json = dict(post.raw_json or {})
    if post.related_coins:
        raw_json["_parsed_related_coins"] = post.related_coins
    # 互动数据也放进去
    raw_json["_parsed_stats"] = {
        "share_count": post.share_count,
        "like_count": post.like_count,
        "view_count": post.view_count,
    }

    return CatalystItem(
        source_code=source_code,
        source_item_id=post.platform_post_id,
        source_item_code=post.platform_post_id,
        title=title,
        body_text=post.content_text,
        body_html="",
        published_at=published_at,
        event_category=build_event_category(profile),
        event_subcategory="",
        related_pairs=related_pairs,
        source_url=post.post_url,
        seo_keywords=[],
        share_count=post.share_count or 0,
        raw_json=raw_json,
    )


def process_catalyst_posts(posts: list, profile: dict) -> dict:
    """将一批帖子批量写入催化剂表。

    Args:
        posts: ScrapedPost 列表
        profile: kol_profile 行 dict

    Returns:
        统计 dict: {total, inserted, merged, skipped, errors}
    """
    stats = {
        "total": len(posts),
        "inserted": 0,
        "merged": 0,
        "skipped": 0,
        "errors": [],
    }

    if not posts:
        return stats

    with get_conn() as conn:
        for post in posts:
            try:
                # 用 savepoint 保证单条失败不影响其他
                with conn.transaction():
                    item = scraped_post_to_catalyst(post, profile)
                    if not item:
                        stats["skipped"] += 1
                        continue

                    # 先查 hash 判断是新增还是合并
                    existing = conn.execute(
                        "SELECT catalyst_id FROM biz.asset_catalyst WHERE content_hash = %s",
                        (item.content_hash,),
                    ).fetchone()

                    row = upsert_catalyst_item(item, conn, link_source="trading_pairs")

                    if existing:
                        stats["merged"] += 1
                    else:
                        stats["inserted"] += 1

            except Exception as e:
                stats["skipped"] += 1
                stats["errors"].append(f"post {post.platform_post_id}: {e}")
                logger.error("catalyst post ingest failed: %s", e)

    return stats


# ============================================================
# 内部工具函数
# ============================================================

def _extract_title(text: str, max_len: int = 80) -> str:
    """从正文提取标题（第一行或前 max_len 字）。"""
    if not text:
        return ""
    first_line = text.strip().split("\n")[0].strip()
    if first_line:
        if len(first_line) > max_len:
            return first_line[:max_len] + "..."
        return first_line
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _parse_posted_at(posted_at: str) -> float:
    """解析 ISO 8601 时间字符串为秒级时间戳。解析失败返回当前时间。"""
    if not posted_at:
        return datetime.now(timezone.utc).timestamp()
    try:
        if posted_at.endswith("Z"):
            posted_at = posted_at[:-1] + "+00:00"
        dt = datetime.fromisoformat(posted_at)
        return dt.timestamp()
    except Exception:
        return datetime.now(timezone.utc).timestamp()
