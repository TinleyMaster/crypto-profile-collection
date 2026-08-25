#!/usr/bin/env python3
"""
币安新闻/公告催化剂抓取入口
- 抓取 catalogId=48（上新）+ 49（综合新闻）
- 增量模式：只拉比数据库中最新发布时间之后的文章
- 落库 biz.asset_catalyst
- 自动关联 asset_id（通过交易对 → symbol → asset_id）

用法：
    python ingest_binance_news.py                # 增量抓取（默认5页）
    python ingest_binance_news.py --full        # 全量抓取（50页）
    python ingest_binance_news.py --pages 10    # 指定页数
    python ingest_binance_news.py --catalogs 48,49   # 指定栏目
"""
import os
import sys
import argparse
import logging

# 路径设置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(WORKSPACE, "src"))
sys.path.insert(0, os.path.join(WORKSPACE, "..", "workbench", "catalyst"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ingest_binance_news")

from binance_news import BinanceNewsScraper, KNOWN_CATALOGS
from db import (
    upsert_catalyst,
    get_latest_publish_time,
    map_pairs_to_asset_id,
    get_conn,
)


def main():
    parser = argparse.ArgumentParser(description="币安新闻催化剂抓取")
    parser.add_argument(
        "--catalogs",
        default="48,49",
        help="栏目ID，逗号分隔（默认 48=上新, 49=综合新闻）",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=5,
        help="每个栏目最大翻页数（默认 5）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="全量抓取（50页，忽略增量截断）",
    )
    parser.add_argument(
        "--no-incremental",
        action="store_true",
        help="关闭增量模式（从头开始抓）",
    )
    args = parser.parse_args()

    catalog_ids = [int(c.strip()) for c in args.catalogs.split(",") if c.strip()]
    max_pages = 50 if args.full else args.pages
    incremental = not args.no_incremental

    logger.info(
        "开始抓取币安新闻，栏目=%s, 页数=%d, 增量=%s",
        catalog_ids, max_pages, incremental,
    )

    total_inserted = 0
    total_with_asset = 0

    # 复用单数据库连接，避免每次 upsert 都重连
    with get_conn() as db_conn, BinanceNewsScraper(request_interval=1.2) as scraper:
        for catalog_id in catalog_ids:
            source_code = KNOWN_CATALOGS.get(catalog_id, {}).get(
                "source_code", f"binance_catalog_{catalog_id}"
            )
            cat_name = KNOWN_CATALOGS.get(catalog_id, {}).get("name", f"catalog_{catalog_id}")

            # 增量：取上次最新发布时间
            since_ts = None
            if incremental:
                since_ts = get_latest_publish_time(source_code, conn=db_conn)
                if since_ts:
                    from datetime import datetime
                    logger.info(
                        "栏目 %s (%s): 增量起点 = %s",
                        catalog_id, cat_name,
                        datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S"),
                    )

            # 抓取栏目列表
            articles = scraper.fetch_catalog(
                catalog_id,
                max_pages=max_pages,
                since_release_date=since_ts,
            )
            if not articles:
                logger.info("栏目 %s: 无新文章", catalog_id)
                continue

            logger.info("栏目 %s: %d 篇新文章，开始拉详情", catalog_id, len(articles))

            # 逐条拉详情 + 落库
            cat_inserted = 0
            cat_with_asset = 0
            for idx, art in enumerate(articles, 1):
                code = art.get("code")
                if not code:
                    continue
                detail = scraper.fetch_detail(code)
                if not detail:
                    continue

                parsed = scraper.parse_detail(detail, source_code)

                # 关联资产
                asset_id = None
                if parsed.related_pairs:
                    asset_id = map_pairs_to_asset_id(parsed.related_pairs, conn=db_conn)
                    if asset_id:
                        cat_with_asset += 1

                # 落库
                data = {
                    "source_code": parsed.source_code,
                    "source_article_id": parsed.source_article_id,
                    "source_article_code": parsed.source_article_code,
                    "asset_id": asset_id,
                    "title": parsed.title,
                    "body_text": parsed.body_text,
                    "body_html": parsed.body_html,
                    "published_at": parsed.published_at,
                    "event_category": parsed.event_category,
                    "event_subcategory": parsed.event_subcategory,
                    "related_pairs": parsed.related_pairs,
                    "source_url": parsed.source_url,
                    "seo_keywords": parsed.seo_keywords,
                    "share_count": parsed.share_count,
                    "raw_json": parsed.raw_json,
                }
                try:
                    upsert_catalyst(data, conn=db_conn)
                    cat_inserted += 1
                except Exception as e:
                    logger.error("落库失败 id=%s: %s", parsed.source_article_id, e)
                    db_conn.rollback()

                if idx % 5 == 0:
                    logger.info("  进度 %d/%d，已插入 %d", idx, len(articles), cat_inserted)

            logger.info(
                "栏目 %s 完成：插入 %d 条，关联资产 %d 条",
                catalog_id, cat_inserted, cat_with_asset,
            )
            total_inserted += cat_inserted
            total_with_asset += cat_with_asset

    logger.info(
        "全部完成：共插入 %d 条，关联资产 %d 条",
        total_inserted, total_with_asset,
    )


if __name__ == "__main__":
    main()
