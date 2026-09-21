"""初始化币安广场新闻媒体账号为催化剂数据源（news_media 类型）。

新增账号（用户 2026-09-18 提供）：
  PANews / Foresight_News / BlockBeats / MarsBit_News / chaincatcher_

流程：
1. 探测 username → squareUid + 粉丝数（user/client 接口）
2. 插入 biz.kol_profile（platform_code='binance_square'，kol_type='news_media'）
3. 打印新增 source_code（kol_catalyst_binance_square_{profile_id}）

news_media 与 catalyst 同走 KOL runner 催化剂管线（_CATALYST_TYPES），
帖子自动写入 biz.asset_catalyst，source_code = kol_catalyst_binance_square_{profile_id}。

用法：
    python init_news_media_catalyst_kols.py
"""
import sys
from pathlib import Path

# catalyst / kol 包所在目录，兼容两种部署结构：
#   本地开发：<project>/workbench/catalyst/  容器部署：/app/catalyst/（Dockerfile 扁平拷贝）
# 容器内原写法 parent.parent.parent/"workbench" 指向不存在的 /app/workbench。
_SCRIPT_PATH = Path(__file__).resolve()
_WB_CANDIDATE = _SCRIPT_PATH.parent.parent.parent / "workbench"
WORKBENCH = (
    _WB_CANDIDATE
    if (_WB_CANDIDATE / "catalyst" / "__init__.py").exists()
    else _SCRIPT_PATH.parent.parent.parent
)
sys.path.insert(0, str(WORKBENCH))
SCRIPTS_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SCRIPTS_SRC))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Json  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from kol.scraper import BinanceSquareScraper, _NotFoundError, _BlockedError  # noqa: E402

# 目标账号：用户名（user/client 接口参数）
TARGETS = [
    "PANews",
    "Foresight_News",
    "BlockBeats",
    "MarsBit_News",
    "chaincatcher_",
]

# 展示名（比 username 更可读，抓取去重用 platform_user_id 不影响）
DISPLAY_NAMES = {
    "PANews": "PANews",
    "Foresight_News": "Foresight News",
    "BlockBeats": "BlockBeats",
    "MarsBit_News": "MarsBit News",
    "chaincatcher_": "ChainCatcher",
}

NOTES = "币安广场新闻媒体 · 催化剂类 KOL（2026-09-18 用户新增）"


def main() -> int:
    scraper = BinanceSquareScraper()
    try:
        resolved = []
        print("=== 1. 探测账号 ===")
        for username in TARGETS:
            try:
                uid = scraper._get_square_uid(username)
                followers = scraper.get_cached_follower_count(username)
                resolved.append({
                    "username": username,
                    "nickname": DISPLAY_NAMES.get(username, username),
                    "square_uid": uid,
                    "followers": followers,
                    "extra_json": {
                        "square_uid": uid,
                        "source": "manual_init_2026-09-18",
                    },
                })
                print(f"  ✅ {username}: squareUid={uid} followers={followers}")
            except (_NotFoundError, _BlockedError) as e:
                print(f"  ❌ {username}: {e}")
            except Exception as e:
                print(f"  ❌ {username}: {type(e).__name__}: {e}")

        if not resolved:
            print("\n无可用账号，退出")
            return 1

        print("\n=== 2. 写入 kol_profile ===")
        url = get_settings(require_database=True).database_url
        conn = psycopg.connect(url, row_factory=dict_row)
        try:
            for r in resolved:
                # upsert 语义：存在则更新，不存在则插入
                existing = conn.execute(
                    "SELECT profile_id FROM biz.kol_profile "
                    "WHERE platform_code = 'binance_square' AND platform_user_id = %s",
                    (r["username"],),
                ).fetchone()
                if existing:
                    pid = existing["profile_id"]
                    conn.execute(
                        "UPDATE biz.kol_profile SET nickname = %s, follower_count = %s, "
                        "kol_type = 'news_media', is_active = TRUE, notes = %s, "
                        "extra_json = COALESCE(extra_json, '{}'::jsonb) || %s::jsonb, "
                        "updated_at = NOW() WHERE profile_id = %s",
                        (r["nickname"], r["followers"], NOTES, Json(r["extra_json"]), pid),
                    )
                else:
                    row = conn.execute(
                        """
                        INSERT INTO biz.kol_profile
                        (platform_code, platform_user_id, nickname, follower_count,
                         is_active, kol_type, notes, extra_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING profile_id
                        """,
                        ("binance_square", r["username"], r["nickname"], r["followers"],
                         True, "news_media", NOTES, Json(r["extra_json"])),
                    ).fetchone()
                    pid = row["profile_id"]
                print(f"  ✅ {r['username']}: profile_id={pid} "
                      f"source_code=kol_catalyst_binance_square_{pid}")
            conn.commit()

            # 3. 打印汇总
            print("\n=== 3. 汇总 ===")
            for r in resolved:
                row = conn.execute(
                    "SELECT profile_id, platform_user_id, kol_type, is_active "
                    "FROM biz.kol_profile WHERE platform_user_id = %s",
                    (r["username"],),
                ).fetchone()
                if row:
                    print(f"  {row['platform_user_id']:<20} id={row['profile_id']} "
                          f"type={row['kol_type']} active={row['is_active']} "
                          f"→ source_code=kol_catalyst_binance_square_{row['profile_id']}")
        finally:
            conn.close()
        print("\n✅ 完成：5 个新闻媒体账号已启用为催化剂数据源")
        print("  下次 KOL runner 轮询将自动抓取并写入 biz.asset_catalyst")
        return 0
    finally:
        scraper._session.close()


if __name__ == "__main__":
    sys.exit(main())
