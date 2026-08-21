"""
代币经济学批量提取（自动循环）。

遍历所有有文档但尚未提取 tokenomics 的资产，逐批调用 LLM 提取并入库。
自动终止条件：无更多候选资产或连续失败过多。

用法:
    python phase_c_extract_tokenomics_auto.py
    python phase_c_extract_tokenomics_auto.py --batch-size 5 --max-rounds 20
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# 导入单币提取的核心函数
from phase_c_extract_tokenomics import (
    resolve_asset,
    scrape_tokenomics_com,
    save_tokenomist_full,
    collect_all_links,
    select_relevant_links,
    fetch_page_content,
    get_cmc_supply,
    get_cg_supply,
    extract_with_llm,
    save_tokenomics,
)

from crypto_research.clients.llm_client import LLMClient

BATCH_SIZE = 10
MAX_ROUNDS = 100
MAX_CONSECUTIVE_FAILURES = 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="代币经济学批量提取（自动循环）")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="每批处理资产数")
    p.add_argument("--max-rounds", type=int, default=MAX_ROUNDS, help="最大轮次")
    p.add_argument("--force", action="store_true", help="强制覆盖已有数据")
    return p


def get_candidates(conn, batch_size: int, force: bool) -> list[int]:
    """获取尚未提取 tokenomics 的资产（有文档入口的优先）。

    排序策略：优先处理 CMC 排名靠前的主流币，避免小币连续失败导致任务提前终止。
    """
    where = "TRUE" if force else "tok.asset_id IS NULL"

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            SELECT a.asset_id
            FROM core.asset a
            LEFT JOIN biz.asset_tokenomics tok ON tok.asset_id = a.asset_id
            LEFT JOIN biz.coin_basic cb ON cb.asset_id = a.asset_id
            LEFT JOIN src_cmc.cmc_asset_map cam ON cam.cmc_id = cb.cmc_id
            WHERE {where}
              AND EXISTS (
                  SELECT 1 FROM biz.doc_source_entry dse
                  WHERE dse.asset_id = a.asset_id
                    AND dse.entity_type = 'asset'
                    AND dse.entry_url IS NOT NULL
              )
            ORDER BY
                -- CMC 有排名的优先，按排名升序
                CASE WHEN cam.rank_num IS NOT NULL THEN 0 ELSE 1 END,
                COALESCE(cam.rank_num, 999999),
                a.asset_id
            LIMIT %s
            """,
            (batch_size,),
        )
        return [row["asset_id"] for row in cur.fetchall()]


def process_one(conn, llm: LLMClient, asset_id: int, force: bool) -> bool:
    """处理单个资产，返回是否成功。"""
    asset = resolve_asset(conn, asset_id, None)
    if not asset:
        print(f"  SKIP: 资产不存在 asset_id={asset_id}")
        return False

    symbol = asset["symbol"]
    name = asset["name"]
    print(f"\n--- {symbol} ({name}) [asset_id={asset_id}] ---")

    # 检查已有数据
    if not force:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM biz.asset_tokenomics WHERE asset_id = %s", (asset_id,))
            if cur.fetchone():
                print(f"  跳过（已有数据）")
                return True

    # 优先尝试 tokenomics.com 结构化数据（主流币命中率高，置信度 1.0）
    tokenomics_com_data = scrape_tokenomics_com(asset)
    if tokenomics_com_data:
        print(f"  tokenomics.com 命中，直接使用结构化数据入库")
        # API 数据补充 supply
        api_data = []
        cmc = get_cmc_supply(conn, asset_id)
        if cmc:
            api_data.append(cmc)
        if asset.get("coingecko_id"):
            cg = get_cg_supply(asset["coingecko_id"])
            if cg:
                api_data.append(cg)
        try:
            save_tokenomist_full(conn, asset_id, tokenomics_com_data, api_data=api_data)
            print(f"  已入库（tokenomics.com）")
            return True
        except Exception as e:
            print(f"  tokenomics.com 入库失败，回退到文档+LLM路径: {e}")

    # 收集所有文档链接
    all_links = collect_all_links(conn, asset_id)
    if not all_links:
        print(f"  SKIP: 无可用文档链接")
        return False

    print(f"  收集到 {len(all_links)} 个文档链接")

    # AI 筛选相关链接
    try:
        relevant_urls = select_relevant_links(llm, asset, all_links)
    except Exception as e:
        print(f"  AI 链接筛选失败: {e}")
        # 降级：取前 10 个
        relevant_urls = [l["source_url"] for l in all_links[:10]]

    print(f"  AI 筛选出 {len(relevant_urls)} 个相关链接")

    if not relevant_urls:
        print(f"  SKIP: 无相关链接")
        return False

    # 抓取页面内容
    doc_contents = []
    for url in relevant_urls:
        print(f"  抓取: {url[:80]}")
        content = fetch_page_content(url)
        if content:
            doc_contents.append({
                "source_url": url,
                "content": content,
            })
        else:
            print(f"    -> 失败")

    if not doc_contents:
        print(f"  SKIP: 无成功抓取的页面")
        return False

    print(f"  成功抓取 {len(doc_contents)} 个页面")

    # API 数据
    api_data = []
    cmc = get_cmc_supply(conn, asset_id)
    if cmc:
        api_data.append(cmc)
    if asset.get("coingecko_id"):
        cg = get_cg_supply(asset["coingecko_id"])
        if cg:
            api_data.append(cg)

    # LLM 提取
    print(f"  调用 LLM 提取 tokenomics...")
    try:
        result = extract_with_llm(llm, asset, doc_contents, api_data)
    except Exception as e:
        print(f"  LLM 调用异常: {e}")
        traceback.print_exc()
        return False

    if not result:
        print(f"  LLM 提取失败")
        return False

    print(f"  置信度: {result.get('confidence')}")

    # 入库
    source_urls = [d["source_url"] for d in doc_contents]
    try:
        save_tokenomics(conn, asset_id, source_urls, result)
        print(f"  已入库")
        return True
    except Exception as e:
        print(f"  入库失败: {e}")
        return False


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings(require_database=True)

    llm = LLMClient(settings, rpm=30)
    if not llm.is_available():
        print("ERROR: LLM 未配置")
        sys.exit(1)

    batch_size = args.batch_size
    max_rounds = args.max_rounds
    force = args.force

    total_done = 0
    total_failed = 0
    consecutive_failures = 0

    try:
        for round_num in range(1, max_rounds + 1):
            print()
            print("=" * 60)
            print(f"  Round {round_num} / max {max_rounds}  |  batch={batch_size}  累计成功={total_done}  累计失败={total_failed}")
            print("=" * 60)

            with get_connection(settings.database_url) as conn:
                candidates = get_candidates(conn, batch_size, force)

            if not candidates:
                print("无更多候选资产，全部完成。")
                break

            round_done = 0
            round_failed = 0
            round_start = time.monotonic()

            for asset_id in candidates:
                try:
                    with get_connection(settings.database_url) as conn:
                        ok = process_one(conn, llm, asset_id, force)
                except Exception as e:
                    print(f"  [ERROR] asset_id={asset_id}: {e}")
                    traceback.print_exc()
                    ok = False

                if ok:
                    round_done += 1
                    consecutive_failures = 0
                else:
                    round_failed += 1
                    consecutive_failures += 1

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"\n连续失败 {consecutive_failures} 次，停止。")
                    break

            total_done += round_done
            total_failed += round_failed

            elapsed = time.monotonic() - round_start
            print(f"\n本轮: {round_done} 成功, {round_failed} 失败 | {elapsed:.1f}s")

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break

    except KeyboardInterrupt:
        print("\n用户中断。")

    print(f"\n全部完成。累计: {total_done} 成功, {total_failed} 失败")


if __name__ == "__main__":
    main()
