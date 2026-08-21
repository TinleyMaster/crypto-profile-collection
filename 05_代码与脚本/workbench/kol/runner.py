"""
KOL 监控主流程：抓取 → 存档 → AI 分类 → 币种匹配 → 邮件提醒。

设计：
  - 按平台分组，每个平台使用一个浏览器实例（Playwright 不能跨线程）
  - 同一平台内的博主串行抓取（浏览器复用）
  - AI 分类和邮件发送在抓取完成后批量处理
  - 单个博主失败不影响其他博主
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime, timezone

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
from .scraper import get_scraper_class  # noqa: E402
from .classifier import classify_post  # noqa: E402
from .asset_match import match_asset  # noqa: E402
from .notifier import send_signal_alert  # noqa: E402


def run_crawl_once(platform_code: str | None = None,
                   profile_id: int | None = None,
                   headless: bool = True) -> dict:
    """
    执行一轮抓取。

    Args:
        platform_code: 只抓取指定平台，None = 全部平台
        profile_id: 只抓取指定博主，None = 全部启用的博主
        headless: 是否无头模式

    Returns:
        统计信息 dict
    """
    stats = {
        "platforms": 0,
        "profiles_total": 0,
        "profiles_success": 0,
        "profiles_failed": 0,
        "profiles_empty": 0,
        "posts_new": 0,
        "posts_duplicate": 0,
        "signals_created": 0,
        "alerts_sent": 0,
        "alerts_failed": 0,
        "errors": [],
    }

    # 获取博主列表
    if profile_id:
        profile = db.get_profile(profile_id)
        profiles = [profile] if profile else []
    else:
        profiles = db.list_active_profiles(platform_code)

    if not profiles:
        print("[KOL][runner] 没有需要抓取的博主")
        return stats

    stats["profiles_total"] = len(profiles)

    # 按平台分组
    by_platform: dict[str, list[dict]] = {}
    for p in profiles:
        pc = p["platform_code"]
        by_platform.setdefault(pc, []).append(p)

    stats["platforms"] = len(by_platform)

    # 逐平台抓取（每个平台一个浏览器实例）
    for plat_code, plat_profiles in by_platform.items():
        scraper_cls = get_scraper_class(plat_code)
        if not scraper_cls:
            msg = f"平台 {plat_code} 无抓取器，跳过"
            print(f"[KOL][runner] {msg}")
            stats["errors"].append(msg)
            stats["profiles_failed"] += len(plat_profiles)
            continue

        print(f"[KOL][runner] 开始抓取平台: {plat_code} ({len(plat_profiles)} 位博主)")

        try:
            with scraper_cls(headless=headless) as scraper:
                for profile in plat_profiles:
                    try:
                        _crawl_one_profile(scraper, profile, stats)
                        stats["profiles_success"] += 1
                    except Exception as e:
                        stats["profiles_failed"] += 1
                        err_msg = f"博主 {profile['nickname']} 抓取失败: {e}"
                        print(f"[KOL][runner] {err_msg}")
                        traceback.print_exc()
                        stats["errors"].append(err_msg)
        except Exception as e:
            msg = f"平台 {plat_code} 浏览器启动失败: {e}"
            print(f"[KOL][runner] {msg}")
            traceback.print_exc()
            stats["errors"].append(msg)
            stats["profiles_failed"] += len(plat_profiles)

    # 处理待 AI 分类的帖子
    _process_pending_ai(stats)

    # 处理待发送邮件的信号
    _process_pending_alerts(stats)

    print(f"[KOL][runner] 本轮完成: "
          f"博主 {stats['profiles_success']}成功/{stats['profiles_empty']}空/{stats['profiles_failed']}失败 / "
          f"新帖 {stats['posts_new']} / "
          f"信号 {stats['signals_created']} / "
          f"告警 {stats['alerts_sent']}")

    return stats


def _crawl_one_profile(scraper, profile: dict, stats: dict) -> None:
    """抓取单个博主的新帖子并存入数据库。"""
    profile_id = profile["profile_id"]
    nickname = profile["nickname"]
    user_id = profile["platform_user_id"]
    last_post_id = profile.get("last_post_id")

    print(f"[KOL][runner] 抓取博主: {nickname} (last_post_id={last_post_id})")

    result = scraper.fetch_posts(
        platform_user_id=user_id,
        since_post_id=last_post_id,
        max_pages=3,
    )
    posts = result.posts

    # 每次抓取都更新 last_crawled_at（含 0 帖场景）
    db.mark_profile_crawled(profile_id)

    # 更新粉丝数（如果抓取到了）
    if result.follower_count is not None:
        db.upsert_profile(
            platform_code=profile["platform_code"],
            platform_user_id=user_id,
            nickname=nickname,
            follower_count=result.follower_count,
        )

    # 0 帖场景：区分 not_found / blocked / empty_feed / error
    if not posts:
        stats["profiles_empty"] += 1
        status_label = {
            "not_found": "博主不存在",
            "blocked": "被反爬拦截",
            "empty_feed": "无新帖/空feed",
            "error": "抓取异常",
            "ok": "无新帖",
        }.get(result.page_status, result.page_status)
        warn_msg = f"博主 {nickname} 0帖 [{status_label}]: {result.error_reason}"
        print(f"[KOL][runner]   ⚠️ {warn_msg}")
        stats["errors"].append(warn_msg)
        return

    print(f"[KOL][runner]   抓到 {len(posts)} 条帖子")

    # 按时间从旧到新插入（确保 last_post_id 是最新的）
    posts_sorted = sorted(posts, key=lambda p: p.posted_at)
    latest_post_id = None

    for post in posts_sorted:
        # 处理 posted_at 格式
        posted_at = post.posted_at
        if not posted_at:
            posted_at = datetime.now(timezone.utc).isoformat()

        result_db = db.insert_post(
            profile_id=profile_id,
            platform_code=profile["platform_code"],
            platform_post_id=post.platform_post_id,
            content_text=post.content_text,
            image_urls=post.image_urls,
            post_url=post.post_url,
            posted_at=posted_at,
            raw_json=post.raw_json,
        )

        if result_db:
            stats["posts_new"] += 1
            latest_post_id = post.platform_post_id
        else:
            stats["posts_duplicate"] += 1

    # 更新 last_post_id
    if latest_post_id:
        db.update_profile_last_post(profile_id, latest_post_id)


def _process_pending_ai(stats: dict, batch_size: int = 20) -> None:
    """处理待 AI 分类的帖子。"""
    pending = db.list_posts_pending_ai(limit=batch_size)
    if not pending:
        return

    print(f"[KOL][runner] 待 AI 分类: {len(pending)} 条")

    for post in pending:
        post_id = post["post_id"]
        try:
            result = classify_post(
                content_text=post["content_text"],
                image_count=len(post.get("image_urls", [])),
            )

            if result is None:
                db.mark_post_ai_failed(post_id)
                print(f"[KOL][runner]   AI 分类失败 post_id={post_id}")
                continue

            # 币种匹配
            asset_id = match_asset(result.get("symbol"))

            # 组装信号数据
            signal_data = {
                "post_id": post_id,
                "profile_id": post["profile_id"],
                "asset_id": asset_id,
                **result,
            }

            signal = db.insert_signal(signal_data)
            db.mark_post_ai_ok(post_id)
            if signal is None:
                # 重复信号（双调度去重），跳过计数
                print(f"[KOL][runner]   信号已存在(去重): post_id={post_id}")
                continue
            stats["signals_created"] += 1

            # 博主信号数 +1（所有类型都计数）
            db.increment_signal_count(post["profile_id"])

            print(f"[KOL][runner]   信号: {result['post_type']} "
                  f"{result.get('symbol', '?')} "
                  f"(置信度 {result['confidence']:.2f})")

        except Exception as e:
            db.mark_post_ai_failed(post_id)
            print(f"[KOL][runner]   AI 分类异常 post_id={post_id}: {e}")
            traceback.print_exc()


def _process_pending_alerts(stats: dict, confidence_threshold: float = 0.8) -> None:
    """处理待发送邮件的信号。"""
    # 从环境变量读取阈值
    threshold = float(os.getenv("KOL_ALERT_CONFIDENCE", confidence_threshold))
    max_age_hours = int(os.getenv("KOL_ALERT_MAX_AGE_HOURS", 24))

    pending = db.list_signals_pending_alert(
        confidence_threshold=threshold,
        max_age_hours=max_age_hours,
    )
    if not pending:
        return

    print(f"[KOL][runner] 待发送告警: {len(pending)} 条")

    for signal in pending:
        signal_id = signal["signal_id"]
        try:
            success, msg = send_signal_alert(signal)
            if success:
                db.mark_signal_alerted(signal_id, success=True)
                stats["alerts_sent"] += 1
                print(f"[KOL][runner]   告警已发送 signal_id={signal_id}")
            else:
                db.mark_signal_alerted(signal_id, success=False, error=msg)
                stats["alerts_failed"] += 1
                print(f"[KOL][runner]   告警失败 signal_id={signal_id}: {msg}")
        except Exception as e:
            db.mark_signal_alerted(signal_id, success=False, error=str(e))
            stats["alerts_failed"] += 1
            print(f"[KOL][runner]   告警异常 signal_id={signal_id}: {e}")
            traceback.print_exc()


# ============================================================
# 命令行入口
# ============================================================

def main():
    """命令行入口：执行一轮抓取。"""
    import argparse

    parser = argparse.ArgumentParser(description="KOL 信号监控 — 抓取一轮")
    parser.add_argument("--platform", type=str, default=None,
                        help="只抓取指定平台")
    parser.add_argument("--profile-id", type=int, default=None,
                        help="只抓取指定博主 ID")
    parser.add_argument("--headed", action="store_true",
                        help="有头模式（调试用）")
    parser.add_argument("--interval", type=int, default=0,
                        help="循环间隔秒数，0=只跑一次")
    args = parser.parse_args()

    if args.interval > 0:
        print(f"[KOL][runner] 循环模式，间隔 {args.interval} 秒")
        while True:
            try:
                run_crawl_once(
                    platform_code=args.platform,
                    profile_id=args.profile_id,
                    headless=not args.headed,
                )
            except Exception as e:
                print(f"[KOL][runner] 本轮异常: {e}")
                traceback.print_exc()
            time.sleep(args.interval)
    else:
        run_crawl_once(
            platform_code=args.platform,
            profile_id=args.profile_id,
            headless=not args.headed,
        )


if __name__ == "__main__":
    main()
