"""
KOL 帖子抓取器。

支持的平台：
  - binance_square  币安广场（纯 HTTP API，免认证）

架构：每个平台一个 Scraper 类，统一接口 fetch_posts(profile, since_post_id)。
新增平台时只需新增 Scraper 类，其他逻辑（AI 分类、邮件、存档）全部复用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests


@dataclass
class ScrapedPost:
    """抓取到的原始帖子数据（平台无关结构）。"""
    platform_post_id: str
    content_text: str
    image_urls: list[str] = field(default_factory=list)
    post_url: str = ""
    posted_at: str = ""  # ISO 8601 字符串
    raw_json: dict = field(default_factory=dict)
    # 币安广场特有：关联交易对（tradingPairsV2[].symbol），催化剂场景用
    trading_pairs: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    """抓取结果（含帖子 + 诊断信息，用于区分 404 / 反爬 / 空 feed）。"""
    posts: list[ScrapedPost] = field(default_factory=list)
    page_status: str = "ok"  # ok / not_found / blocked / empty_feed / error
    page_title: str = ""
    error_reason: str = ""
    http_status: int | None = None
    follower_count: int | None = None


class BaseScraper:
    """抓取器基类。子类实现 fetch_posts。"""

    platform_code: str = ""

    def fetch_posts(
        self,
        platform_user_id: str,
        since_post_id: str | None = None,
        max_pages: int = 3,
    ) -> FetchResult:
        """
        抓取指定博主的帖子。

        Args:
            platform_user_id: 平台内用户 ID（币安广场为 username，如 Square-Creator-xxx）
            since_post_id: 增量游标，只返回比此 ID 更新的帖子
            max_pages: 最大翻页数

        Returns:
            FetchResult：含帖子列表 + 页面状态诊断
        """
        raise NotImplementedError


# ============================================================
# 币安广场抓取器（纯 HTTP API 模式）
# ============================================================

class BinanceSquareScraper(BaseScraper):
    """
    币安广场帖子抓取器（纯 HTTP API，免认证）。

    使用币安公开 bapi friendly 接口：
      - POST /bapi/composite/v3/friendly/pgc/user/client  → 拿 squareUid
      - GET  /bapi/composite/v2/friendly/pgc/content/queryUserProfilePageContentsWithFilter
        → 帖子列表（timeOffset 翻页）

    platform_user_id 为 username（如 Square-Creator-xxx），
    内部自动解析为 squareUid 后再抓帖。
    """

    platform_code = "binance_square"

    USER_API = "https://www.binance.com/bapi/composite/v3/friendly/pgc/user/client"
    FEED_API = ("https://www.binance.com/bapi/composite/v2/friendly/pgc/content/"
                "queryUserProfilePageContentsWithFilter")
    # 广场热门 feed（按热度排序的推荐流，用于发现新博主）
    HOT_FEED_API = ("https://www.binance.com/bapi/composite/v1/friendly/pgc/"
                    "content/querySquareHomePageContentsWithFilter")

    _DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.binance.com",
        "Referer": "https://www.binance.com/",
        "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }

    # squareUid 缓存（username → uid），避免每轮重复查询
    _uid_cache: dict[str, str] = {}
    # follower_count 缓存（username → count）
    _follower_cache: dict[str, int] = {}

    def __init__(self, headless: bool = True):
        # headless 参数保留以兼容旧接口，API 模式无实际作用
        self.headless = headless
        self._session = requests.Session()
        self._session.headers.update(self._DEFAULT_HEADERS)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._session.close()
        except Exception:
            pass

    def fetch_posts(
        self,
        platform_user_id: str,
        since_post_id: str | None = None,
        max_pages: int = 3,
    ) -> FetchResult:
        """
        抓取币安广场博主的帖子列表。

        流程：
          1. 通过 username 查 squareUid（缓存优先）
          2. 循环 GET feed 接口，timeOffset 翻页
          3. 组装 ScrapedPost，增量过滤
        """
        result = FetchResult()

        # Step 1: 拿 squareUid
        try:
            square_uid = self._get_square_uid(platform_user_id)
        except _NotFoundError as e:
            result.page_status = "not_found"
            result.error_reason = str(e)
            return result
        except _BlockedError as e:
            result.page_status = "blocked"
            result.error_reason = str(e)
            return result
        except Exception as e:
            result.page_status = "error"
            result.error_reason = f"获取博主信息失败: {e}"
            return result

        # 写入粉丝数（从 USER_API 缓存中取）
        result.follower_count = self.get_cached_follower_count(platform_user_id)

        # Step 2: 翻页抓帖
        posts: list[ScrapedPost] = []
        seen_ids: set[str] = set()
        time_offset = "-1"
        page = 0

        try:
            while page < max_pages:
                page += 1
                resp_data = self._fetch_feed_page(square_uid, time_offset)
                contents = resp_data.get("contents", [])

                if not contents:
                    break

                for item in contents:
                    post = self._parse_post_item(item)
                    if post and post.platform_post_id not in seen_ids:
                        seen_ids.add(post.platform_post_id)
                        posts.append(post)

                # 判断是否还有下一页
                if not resp_data.get("isExistSecondPage", False):
                    break

                next_offset = resp_data.get("timeOffset")
                if not next_offset or str(next_offset) == time_offset:
                    break
                time_offset = str(next_offset)

                # 轻微限速，避免触发风控
                time.sleep(0.3)

        except _BlockedError as e:
            result.page_status = "blocked"
            result.error_reason = str(e)
            result.posts = posts  # 已抓到的部分也返回
            return result
        except Exception as e:
            result.page_status = "error"
            result.error_reason = f"抓取帖子失败: {e}"
            result.posts = posts
            return result

        # 按时间倒序
        posts.sort(key=lambda p: p.posted_at, reverse=True)

        # 增量过滤：只返回比 since_post_id 更新的
        if since_post_id and posts:
            idx = None
            for i, p in enumerate(posts):
                if p.platform_post_id == since_post_id:
                    idx = i
                    break
            if idx is not None:
                posts = posts[:idx]

        result.posts = posts

        if not posts:
            result.page_status = "empty_feed"
            result.error_reason = "接口返回正常但无帖子（博主可能未发帖或已全部增量过滤）"

        return result

    # --------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------

    def _get_square_uid(self, username: str) -> str:
        """通过 username 获取 squareUid，带缓存。同时缓存 follower_count。"""
        if username in self._uid_cache:
            return self._uid_cache[username]

        payload = {
            "username": username,
            "getFollowCount": True,
            "queryFollowersInfo": True,
            "queryRelationTokens": True,
        }
        resp = self._session.post(
            self.USER_API,
            json=payload,
            timeout=15,
        )

        if resp.status_code == 404:
            raise _NotFoundError(f"博主不存在 (HTTP 404, username={username})")
        if resp.status_code in (403, 429, 202):
            raise _BlockedError(f"用户接口被拦截 (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise _BlockedError(f"用户接口异常 (HTTP {resp.status_code})")

        data = resp.json()
        if data.get("code") != "000000":
            msg = data.get("message") or data.get("messageDetail") or str(data)
            if "not found" in msg.lower() or "不存在" in msg:
                raise _NotFoundError(f"博主不存在: {msg}")
            raise _BlockedError(f"用户接口返回错误: {msg}")

        user_data = data.get("data", {})
        uid = user_data.get("squareUid")
        if not uid:
            raise _NotFoundError(f"返回数据中无 squareUid: {list(user_data.keys())}")

        self._uid_cache[username] = uid

        # 缓存粉丝数（优先取 followersCount，兼容不同字段名）
        follower_count = user_data.get("followersCount") or user_data.get("followerCount")
        if follower_count is not None:
            try:
                self._follower_cache[username] = int(follower_count)
            except (ValueError, TypeError):
                pass

        return uid

    def get_cached_follower_count(self, username: str) -> int | None:
        """获取缓存的粉丝数（需先调用过 _get_square_uid）。"""
        return self._follower_cache.get(username)

    def _fetch_feed_page(self, square_uid: str, time_offset: str) -> dict:
        """抓取一页帖子，返回 data 字段内容。"""
        params = {
            "targetSquareUid": square_uid,
            "timeOffset": time_offset,
            "filterType": "ALL",
        }
        resp = self._session.get(
            self.FEED_API,
            params=params,
            timeout=15,
        )

        if resp.status_code in (403, 429, 202):
            raise _BlockedError(f"帖子接口被拦截 (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise _BlockedError(f"帖子接口异常 (HTTP {resp.status_code})")

        data = resp.json()
        if data.get("code") != "000000":
            msg = data.get("message") or data.get("messageDetail") or str(data)
            raise _BlockedError(f"帖子接口返回错误: {msg}")

        return data.get("data", {})

    def _parse_post_item(self, item: dict) -> ScrapedPost | None:
        """将 API 返回的单条帖子解析为 ScrapedPost。"""
        post_id = item.get("id") or item.get("postId")
        if not post_id:
            return None
        post_id = str(post_id)

        # 正文：优先 bodyTextOnly（纯文本，直接喂 AI）
        content = item.get("bodyTextOnly") or item.get("content") or item.get("title", "")
        if isinstance(content, dict):
            content = content.get("text", "") or str(content)
        content = str(content or "").strip()

        # 图片
        images: list[str] = []
        image_list = item.get("imageList") or []
        if isinstance(image_list, list):
            for img in image_list:
                if isinstance(img, str):
                    images.append(img)
                elif isinstance(img, dict):
                    url = img.get("url") or img.get("imageUrl") or img.get("src")
                    if url:
                        images.append(url)

        # 发帖时间：latestReleaseTime 毫秒时间戳
        posted_at = ""
        ts = item.get("latestReleaseTime") or item.get("createTime")
        if ts and isinstance(ts, (int, float)):
            if ts > 1e12:
                ts = ts / 1000
            posted_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        # 帖子 URL
        post_url = ""
        share_link = item.get("shareLink")
        if share_link:
            post_url = share_link if share_link.startswith("http") else f"https://www.binance.com{share_link}"
        else:
            post_url = f"https://www.binance.com/zh-CN/square/post/{post_id}"

        # 关联交易对（tradingPairsV2[].symbol），催化剂场景用
        trading_pairs: list[str] = []
        tp_list = item.get("tradingPairsV2") or []
        if isinstance(tp_list, list):
            for tp in tp_list:
                if isinstance(tp, dict):
                    sym = tp.get("symbol") or tp.get("pair") or ""
                    if sym:
                        trading_pairs.append(str(sym).upper())
                elif isinstance(tp, str):
                    trading_pairs.append(tp.upper())

        return ScrapedPost(
            platform_post_id=post_id,
            content_text=content,
            image_urls=images,
            post_url=post_url,
            posted_at=posted_at,
            raw_json=item,
            trading_pairs=trading_pairs,
        )

    def discover_creators(
        self,
        max_pages: int = 5,
        min_followers: int = 10000,
    ) -> list[dict]:
        """从广场热门 feed 发现高粉丝博主。

        Args:
            max_pages: 翻页数（每页约 20 条）
            min_followers: 最低粉丝数阈值，低于则跳过

        Returns:
            list[dict]: 符合条件的博主列表，每项含
                username, nickname, avatar_url, follower_count, square_uid
        """
        creators: dict[str, dict] = {}  # username -> info（去重）
        time_offset = "-1"

        for page in range(max_pages):
            try:
                resp = self._session.get(
                    self.HOT_FEED_API,
                    params={
                        "timeOffset": time_offset,
                        "filterType": "ALL",
                        "topicId": "",
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                if data.get("code") != "000000":
                    break

                contents = data.get("data", {}).get("contents", [])
                if not contents:
                    break

                for item in contents:
                    creator = item.get("creator") or item.get("creatorVO") or {}
                    username = creator.get("username") or creator.get("userName")
                    nickname = creator.get("nickName") or creator.get("nickname") or ""
                    avatar = creator.get("avatarUrl") or creator.get("avatar") or ""
                    square_uid = creator.get("squareUid") or ""
                    followers = creator.get("followersCount") or creator.get("followerCount") or 0

                    if not username:
                        continue
                    if username in creators:
                        continue

                    try:
                        follower_count = int(followers) if followers else 0
                    except (ValueError, TypeError):
                        follower_count = 0

                    if follower_count < min_followers:
                        continue

                    creators[username] = {
                        "username": username,
                        "nickname": nickname,
                        "avatar_url": avatar,
                        "follower_count": follower_count,
                        "square_uid": str(square_uid) if square_uid else "",
                    }

                # 翻页
                if not data.get("data", {}).get("isExistSecondPage", False):
                    break
                next_offset = data.get("data", {}).get("timeOffset")
                if not next_offset or str(next_offset) == time_offset:
                    break
                time_offset = str(next_offset)

                time.sleep(0.5)  # 限速

            except Exception:
                break

        return list(creators.values())


# ============================================================
# 自定义异常（用于场景区分）
# ============================================================

class _NotFoundError(Exception):
    """博主不存在。"""
    pass


class _BlockedError(Exception):
    """被反爬/风控拦截。"""
    pass


# ============================================================
# 抓取器工厂
# ============================================================

_SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "binance_square": BinanceSquareScraper,
}


def get_scraper_class(platform_code: str) -> type[BaseScraper] | None:
    """根据平台编码获取抓取器类。"""
    return _SCRAPER_REGISTRY.get(platform_code)


def register_scraper(platform_code: str, scraper_cls: type[BaseScraper]) -> None:
    """注册新平台抓取器（扩展用）。"""
    _SCRAPER_REGISTRY[platform_code] = scraper_cls
