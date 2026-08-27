"""
KOL 博主发现模块：从分享链接自动解析并拉取博主信息。

支持的链接格式：
- 币安广场分享链接：
    https://app.binance.com/uni-qr/cpro/{username}?...
    https://www.binance.com/zh-CN/square/profile/{username}
    https://www.binance.com/en/square/profile/{username}
    https://www.binance.com/square/profile/{username}

流程：
    share_url → parse_username() → scraper.fetch_profile() → 博主信息预览
    → 用户确认 → upsert_profile() 入库 → 自动开始监控
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from urllib.parse import urlparse, unquote

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredProfile:
    """发现的博主信息（预览用，用户确认后再入库）。"""
    platform_code: str
    platform_user_id: str       # username（如 Square-Creator-xxx 或展示 handle）
    nickname: str
    avatar_url: str = ""
    follower_count: int = 0
    profile_url: str = ""       # 博主主页 URL
    extra: dict | None = None   # 平台特有信息


# ============================================================
# 链接解析
# ============================================================

# 币安广场链接模式（按优先级排列）
_BINANCE_SQUARE_PATTERNS = [
    # app 分享链接：/uni-qr/cpro/{username}
    re.compile(r"/uni-qr/cpro/([^/?#]+)", re.IGNORECASE),
    # web 主页：/square/profile/{username} （各种语言前缀）
    re.compile(r"/(?:[a-z]{2}(?:-[A-Z]{2})?/)?square/profile/([^/?#]+)", re.IGNORECASE),
    # 直接 cpro 路径
    re.compile(r"/cpro/([^/?#]+)", re.IGNORECASE),
]


def parse_share_url(share_url: str) -> tuple[str, str] | None:
    """解析分享链接，返回 (platform_code, username)。

    支持的平台：
    - binance_square: 币安广场

    Returns:
        (platform_code, username) 或 None（无法识别）
    """
    if not share_url:
        return None

    share_url = share_url.strip()

    # 去掉 URL 编码
    try:
        share_url = unquote(share_url)
    except Exception:
        pass

    parsed = urlparse(share_url)
    path = parsed.path or ""
    netloc = parsed.netloc.lower()

    # 币安域名
    if "binance.com" in netloc or "binance" in netloc:
        for pattern in _BINANCE_SQUARE_PATTERNS:
            m = pattern.search(path)
            if m:
                username = m.group(1).strip()
                if username:
                    return ("binance_square", username)

    # 兜底：如果用户直接输入 username（不含 /），也当币安广场处理
    if "/" not in share_url and "." not in share_url and len(share_url) >= 3:
        return ("binance_square", share_url)

    return None


# ============================================================
# 博主信息拉取
# ============================================================

def discover_profile(share_url: str) -> DiscoveredProfile:
    """从分享链接发现博主（解析 + 拉取信息）。

    Args:
        share_url: 分享链接或 username

    Returns:
        DiscoveredProfile 博主预览信息

    Raises:
        ValueError: 无法识别链接格式
        RuntimeError: 拉取博主信息失败（404 / 被拦截等）
    """
    parsed = parse_share_url(share_url)
    if not parsed:
        raise ValueError(f"无法识别的分享链接格式: {share_url}")

    platform_code, username = parsed

    if platform_code == "binance_square":
        return _discover_binance_square(username)
    else:
        raise ValueError(f"暂不支持的平台: {platform_code}")


def _discover_binance_square(username: str) -> DiscoveredProfile:
    """从币安广场拉取博主信息。"""
    from .scraper import BinanceSquareScraper

    scraper = BinanceSquareScraper()
    try:
        # _get_square_uid 会同时缓存 follower_count
        square_uid = scraper._get_square_uid(username)
        follower_count = scraper.get_cached_follower_count(username) or 0

        # 重新请求一次拿完整用户信息（nickname / avatar 等）
        # _get_square_uid 已经拿到了 data，但没返回全量
        # 直接调 user/client 拿完整信息
        payload = {
            "username": username,
            "getFollowCount": True,
            "queryFollowersInfo": True,
            "queryRelationTokens": True,
        }
        resp = scraper._session.post(
            scraper.USER_API,
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"获取博主信息失败: HTTP {resp.status_code}")

        data = resp.json()
        if data.get("code") != "000000":
            raise RuntimeError(f"获取博主信息失败: {data.get('message')}")

        user_data = data.get("data", {})
        nickname = user_data.get("nickname") or user_data.get("nickName") or username
        avatar_url = user_data.get("avatarUrl") or user_data.get("photoUrl") or ""
        follower_count = user_data.get("followersCount") or user_data.get("followerCount") or 0

        # 构造主页 URL
        profile_url = f"https://www.binance.com/zh-CN/square/profile/{username}"

        return DiscoveredProfile(
            platform_code="binance_square",
            platform_user_id=username,
            nickname=str(nickname),
            avatar_url=str(avatar_url),
            follower_count=int(follower_count) if follower_count else 0,
            profile_url=profile_url,
            extra={
                "square_uid": square_uid,
                "bio": user_data.get("bio") or user_data.get("userBio") or "",
            },
        )
    finally:
        scraper._session.close()
