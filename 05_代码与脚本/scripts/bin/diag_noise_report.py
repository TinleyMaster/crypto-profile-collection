"""
噪声诊断报告：查看今日新增文档链接的噪声情况。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date

SCRIPT_DIR = Path(__file__).resolve().parent  # bin/
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pathlib import Path
import psycopg
from crypto_research.config import get_settings

settings = get_settings()
DSN = settings.database_url
today = date.today().isoformat()


def run():
    print(f"=== 噪声诊断报告 {today} ===\n")

    for attempt in range(3):
        try:
            conn = psycopg.connect(DSN, connect_timeout=10)
            cur = conn.cursor()
            break
        except Exception as e:
            print(f"连接尝试 {attempt+1}/3 失败: {e}")
            time.sleep(2)
    else:
        print("数据库连接失败，退出")
        return

    # 1. 全表概览
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry")
    all_total = cur.fetchone()[0]
    print(f"biz.doc_source_entry 全表总数: {all_total}")

    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE deep_crawled_at >= CURRENT_DATE")
    today_total = cur.fetchone()[0]
    print(f"今日新增: {today_total}")

    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%'"
    )
    deep_all = cur.fetchone()[0]
    print(f"deep_crawl 全部历史: {deep_all}")

    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' AND deep_crawled_at >= CURRENT_DATE"
    )
    deep_today = cur.fetchone()[0]
    print(f"deep_crawl 今日新增: {deep_today}")

    # 2. 已知噪声域名
    print("\n=== 已知噪声域名（今日 deep_crawl）===")
    noise_patterns = {
        "paperdigest": "%paperdigest.org%",
        "arxiv": "%arxiv.org%",
        "nips (papers.nips.cc)": "%papers.nips.cc%",
        "springer": "%link.springer.com%",
        "researchgate": "%researchgate.net%",
    }
    noise_total = 0
    for label, pattern in noise_patterns.items():
        cur.execute(
            "SELECT COUNT(*) FROM biz.doc_source_entry "
            "WHERE discovered_from LIKE 'deep_crawl:%%' AND deep_crawled_at >= CURRENT_DATE "
            "AND entry_url LIKE %s", (pattern,)
        )
        cnt = cur.fetchone()[0]
        noise_total += cnt
        if cnt > 0:
            print(f"  {label}: {cnt}")
    pct = f"{noise_total/deep_today*100:.1f}%" if deep_today else "N/A"
    print(f"  已知噪声合计: {noise_total} ({pct})")

    # 3. 按 entry_type 分布（今日 deep_crawl）
    print("\n=== entry_type 分布（今日 deep_crawl）===")
    cur.execute(
        "SELECT entry_type, COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' AND deep_crawled_at >= CURRENT_DATE "
        "GROUP BY entry_type ORDER BY COUNT(*) DESC"
    )
    for etype, cnt in cur.fetchall():
        print(f"  {etype}: {cnt}")

    # 4. 域名分布 TOP 25（今日 deep_crawl）
    print("\n=== 域名 TOP 25（今日 deep_crawl）===")
    cur.execute(
        "SELECT SUBSTRING(entry_url FROM 'https?://([^/]+)') AS domain, COUNT(*) AS cnt "
        "FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' AND deep_crawled_at >= CURRENT_DATE "
        "GROUP BY domain ORDER BY cnt DESC LIMIT 25"
    )
    for domain, cnt in cur.fetchall():
        print(f"  {cnt:>6}  {domain}")

    # 5. 非已知噪声的 TOP 域名抽样
    print("\n=== 非已知噪声域名采样（各1条）===")
    cur.execute(
        "SELECT SUBSTRING(entry_url FROM 'https?://([^/]+)') AS domain, COUNT(*) AS cnt "
        "FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' AND deep_crawled_at >= CURRENT_DATE "
        "AND entry_url NOT LIKE '%paperdigest.org%' "
        "AND entry_url NOT LIKE '%arxiv.org%' "
        "AND entry_url NOT LIKE '%papers.nips.cc%' "
        "AND entry_url NOT LIKE '%link.springer.com%' "
        "AND entry_url NOT LIKE '%researchgate.net%' "
        "GROUP BY domain ORDER BY cnt DESC LIMIT 15"
    )
    for domain, cnt in cur.fetchall():
        cur.execute(
            "SELECT entry_url FROM biz.doc_source_entry "
            "WHERE discovered_from LIKE 'deep_crawl:%%' AND deep_crawled_at >= CURRENT_DATE "
            "AND entry_url LIKE %s LIMIT 1",
            (f"%{domain}%",)
        )
        sample = cur.fetchone()
        sample_url = sample[0][:130] if sample else "N/A"
        print(f"  {cnt:>6}  {domain}")
        print(f"          {sample_url}")

    # 6. 按 discovered_from 来源分类（今日全部）
    print("\n=== 今日新增来源分布 ===")
    cur.execute(
        "SELECT discovered_from, COUNT(*) FROM biz.doc_source_entry "
        "WHERE deep_crawled_at >= CURRENT_DATE "
        "GROUP BY discovered_from ORDER BY COUNT(*) DESC LIMIT 20"
    )
    for src, cnt in cur.fetchall():
        print(f"  {cnt:>6}  {src}")

    # 7. 评估
    print("\n=== 噪声评估 ===")
    if noise_total == 0:
        print("  ✅ 今日无已知噪声域名，AI 噪声清理可能已经处理过了")
    elif deep_today >= 100 and noise_total / deep_today > 0.3:
        print(f"  ⚠️ 噪声占比较高 ({noise_total/deep_today*100:.1f}%)，建议运行 AI 噪声清理")
    else:
        print(f"  噪声占比可接受，继续观察")

    conn.close()
    print("\n诊断完成。")


if __name__ == "__main__":
    run()
