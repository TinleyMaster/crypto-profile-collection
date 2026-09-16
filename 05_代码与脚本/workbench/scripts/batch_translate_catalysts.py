"""
批量翻译催化剂标题为中文
====================================
对近期未翻译的催化剂（title_cn IS NULL）批量调用 AI 翻译，写入 title_cn 字段。

使用场景：
- 慢通道里跑一次，把近期所有催化剂都翻译成中文
- 手动跑，回填历史数据

用法：
    python batch_translate_catalysts.py                 # 翻译近 7 天未翻译的，最多 50 条
    python batch_translate_catalysts.py --days 30       # 翻译近 30 天的
    python batch_translate_catalysts.py --limit 200     # 最多 200 条
    python batch_translate_catalysts.py --all           # 全部未翻译的（小心用量！）
    python batch_translate_catalysts.py --dry-run       # 只看会翻译哪些，不实际调用
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import logging

# 确保 workbench 在路径
HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.dirname(HERE)
sys.path.insert(0, WB)

# crypto_research 包路径
CR_SRC = os.path.normpath(os.path.join(WB, "..", "scripts", "src"))
if os.path.isdir(CR_SRC) and CR_SRC not in sys.path:
    sys.path.insert(0, CR_SRC)

from catalyst.db import get_conn
from catalyst.ai_enhance import CatalystTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def fetch_pending(conn, days: int = 7, limit: int = 50, all_time: bool = False) -> list[dict]:
    """获取待翻译的催化剂列表。"""
    if all_time:
        date_filter = ""
        params = (limit,)
    else:
        date_filter = "AND c.published_at >= NOW() - (%s || ' days')::INTERVAL"
        params = (days, limit)

    rows = conn.execute(f"""
        SELECT c.catalyst_id, c.title, c.body_text, c.published_at, c.source_code
        FROM biz.asset_catalyst c
        WHERE c.title_cn IS NULL
          AND c.title IS NOT NULL
          AND c.title <> ''
          {date_filter}
        ORDER BY c.published_at DESC
        LIMIT %s
    """, params).fetchall()
    return [dict(r) for r in rows]


def batch_translate(days: int = 7, limit: int = 50, all_time: bool = False,
                    dry_run: bool = False) -> dict:
    """批量翻译。"""
    translator = CatalystTranslator.from_settings()
    if not translator:
        logger.warning("LLM 不可用，跳过翻译")
        return {"success": 0, "failed": 0, "skipped": 0, "reason": "llm_unavailable"}

    with get_conn() as conn:
        pending = fetch_pending(conn, days=days, limit=limit, all_time=all_time)
        total = len(pending)
        logger.info(f"待翻译催化剂: {total} 条")

        if dry_run:
            for i, c in enumerate(pending[:10], 1):
                logger.info(f"  [{i}] #{c['catalyst_id']} [{c['source_code']}] {c['title'][:80]}")
            if total > 10:
                logger.info(f"  ... 还有 {total - 10} 条")
            return {"success": 0, "failed": 0, "skipped": total, "dry_run": True}

        success = 0
        failed = 0
        for i, c in enumerate(pending, 1):
            cid = c["catalyst_id"]
            title = c["title"] or ""
            body = (c["body_text"] or "")[:2000]  # 截断，省 token

            logger.info(f"[{i}/{total}] #{cid} 翻译中: {title[:60]}...")
            try:
                result = translator.translate(title, body)
                if result and result.get("title_cn"):
                    title_cn = result["title_cn"][:512]
                    conn.execute("""
                        UPDATE biz.asset_catalyst
                        SET title_cn = %s
                        WHERE catalyst_id = %s
                    """, (title_cn, cid))
                    logger.info(f"  ✓ 完成: {title_cn}")
                    success += 1
                else:
                    logger.warning(f"  ✗ 翻译结果为空")
                    failed += 1
            except Exception as e:
                logger.error(f"  ✗ 翻译失败: {e}")
                failed += 1

            # 简单限速
            if i < total:
                time.sleep(0.5)

        logger.info(f"翻译完成: 成功 {success} / 失败 {failed} / 共 {total}")
        return {"success": success, "failed": failed, "total": total}


def main():
    parser = argparse.ArgumentParser(description="批量翻译催化剂标题为中文")
    parser.add_argument("--days", type=int, default=7, help="翻译最近 N 天的（默认7）")
    parser.add_argument("--limit", type=int, default=50, help="最多翻译 N 条（默认50）")
    parser.add_argument("--all", action="store_true", help="翻译全部未翻译的（忽略 --days）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不实际翻译")
    args = parser.parse_args()

    print("=" * 60)
    print("催化剂标题批量翻译")
    print("=" * 60)

    result = batch_translate(
        days=args.days,
        limit=args.limit,
        all_time=args.all,
        dry_run=args.dry_run,
    )

    print("\n" + "=" * 60)
    if result.get("dry_run"):
        print(f"预览模式: 将翻译 {result['skipped']} 条")
    elif result.get("reason") == "llm_unavailable":
        print("LLM 不可用，已跳过")
    else:
        print(f"成功: {result['success']}  |  失败: {result['failed']}  |  总计: {result['total']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
