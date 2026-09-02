"""
KOL 信号全量 backfill 脚本
遍历 biz.kol_post 全部帖子，用 classifier 重新分类，UPSERT 到 biz.kol_signal
"""
import os
import sys
import time

WORKSPACE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(WORKSPACE, "src"))
sys.path.insert(0, os.path.join(WORKSPACE, "..", "workbench", "kol"))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto",
)

import psycopg
from psycopg.rows import dict_row
from classifier import classify_post

DSN = os.environ["DATABASE_URL"]
BATCH_SIZE = 10  # 每 N 条 commit 一次


def main():
    conn = psycopg.connect(DSN, row_factory=dict_row, connect_timeout=30)
    cur = conn.cursor()

    # 读取全部帖子
    cur.execute(
        "SELECT post_id, profile_id, content_text, image_urls FROM biz.kol_post ORDER BY post_id"
    )
    posts = cur.fetchall()
    total = len(posts)
    print(f"[backfill] 共 {total} 条帖子待处理")

    success = 0
    failed = 0
    type_counts = {}

    for i, post in enumerate(posts, 1):
        post_id = post["post_id"]
        profile_id = post["profile_id"]
        content = post["content_text"] or ""
        img_count = len(post.get("image_urls") or [])

        try:
            result = classify_post(content, img_count)
        except Exception as e:
            print(f"  [FAIL] post {post_id}: classify 异常 {e}")
            failed += 1
            continue

        if result is None:
            print(f"  [FAIL] post {post_id}: classify 返回 None")
            failed += 1
            continue

        post_type = result["post_type"]
        type_counts[post_type] = type_counts.get(post_type, 0) + 1

        # UPSERT
        try:
            cur.execute(
                """
                INSERT INTO biz.kol_signal
                  (post_id, profile_id, post_type, direction, symbol,
                   entry_condition, entry_price, stop_loss, take_profit,
                   leverage, support_level, resistance_level,
                   already_entered, has_pnl_number, confidence,
                   created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW(), NOW())
                ON CONFLICT (post_id) DO UPDATE SET
                    post_type = EXCLUDED.post_type,
                    direction = EXCLUDED.direction,
                    symbol = EXCLUDED.symbol,
                    entry_condition = EXCLUDED.entry_condition,
                    entry_price = EXCLUDED.entry_price,
                    stop_loss = EXCLUDED.stop_loss,
                    take_profit = EXCLUDED.take_profit,
                    leverage = EXCLUDED.leverage,
                    support_level = EXCLUDED.support_level,
                    resistance_level = EXCLUDED.resistance_level,
                    already_entered = EXCLUDED.already_entered,
                    has_pnl_number = EXCLUDED.has_pnl_number,
                    confidence = EXCLUDED.confidence,
                    updated_at = NOW()
                """,
                (
                    post_id,
                    profile_id,
                    result["post_type"],
                    result["direction"],
                    result["symbol"],
                    result["entry_condition"],
                    result["entry_price"],
                    result["stop_loss"],
                    result["take_profit"],
                    result["leverage"],
                    result["support_level"],
                    result["resistance_level"],
                    result["already_entered"],
                    result["has_pnl_number"],
                    result["confidence"],
                ),
            )
            success += 1
        except Exception as e:
            print(f"  [FAIL] post {post_id}: DB 写入异常 {e}")
            failed += 1
            continue

        print(f"  [{i}/{total}] post {post_id} -> {post_type} (conf={result['confidence']})")

        # 分批提交
        if i % BATCH_SIZE == 0:
            conn.commit()
            print(f"  --- 已提交 {i}/{total} ---")

        # 限速，避免触发 LLM 速率限制
        time.sleep(0.5)

    conn.commit()
    conn.close()

    print()
    print(f"[backfill] 完成！成功 {success}，失败 {failed}")
    print(f"[backfill] 类型分布: {type_counts}")


if __name__ == "__main__":
    main()
