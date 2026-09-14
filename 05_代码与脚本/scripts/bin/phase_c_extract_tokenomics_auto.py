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
# 墓碑冷却期：跳过/失败后 N 天内不再重试该资产，避免同一批内反复处理
TOMBSTONE_COOLDOWN_DAYS = 7
# 墓碑状态值
ST_OK = "ok"
ST_PREFILTER_MISS = "prefilter_miss"
ST_NO_DOCS = "no_docs"
ST_NO_CONTENT = "no_content"

# tokenomics 相关关键词（URL / doc_type 命中即视为候选，不命中直接跳过，不调用 LLM）
TOKENOMICS_KEYWORDS = [
    "tokenomic", "tokenomics", "token-economics", "token_economics",
    "代币经济", "代币分配", "代币供应", "代币模型",
    "whitepaper", "litepaper", "white-paper", "lite-paper",
    "vesting", "unlock", "emission", "inflation", "supply",
    "staking", "governance", "utility", "distribution",
    "economics", "token-distribution", "token-allocation",
    "allocation", "lockup", "cliff",
]

# 直接跳过的 doc_type（社媒/代码仓库/浏览器等，不可能有 tokenomics 内容）
SKIP_DOC_TYPES = {
    "twitter", "x", "telegram", "discord", "reddit", "facebook",
    "github", "linkedin", "medium", "youtube",
    "etherscan", "bscscan", "solscan", "blockchain_explorer",
    "audit_report", "security_assessment",
    "blog", "news", "press_release",
}


def _has_tokenomics_keyword(url: str, doc_type: str | None) -> bool:
    """URL 或 doc_type 含 tokenomics 关键词即返回 True。"""
    if doc_type and doc_type.lower() in SKIP_DOC_TYPES:
        return False
    text = (url or "").lower()
    return any(kw in text for kw in TOKENOMICS_KEYWORDS)


def _filter_tokenomics_links(all_links: list[dict]) -> list[dict]:
    """从所有链接中按关键词预筛 tokenomics 相关链接，返回命中的列表。"""
    hits = []
    for d in all_links:
        url = d.get("source_url") or ""
        doc_type = d.get("doc_type")
        # doc_asset 中的文件（PDF 等）始终保留，可能是白皮书
        if d.get("file_name"):
            hits.append(d)
            continue
        if _has_tokenomics_keyword(url, doc_type):
            hits.append(d)
    return hits


def _ensure_tombstone_columns(conn) -> None:
    """确保 biz.asset_tokenomics 存在墓碑列（extract_status / next_retry_at），幂等。"""
    with conn.cursor() as cur:
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'biz' AND table_name = 'asset_tokenomics'
                    AND column_name = 'extract_status'
                ) THEN
                    ALTER TABLE biz.asset_tokenomics ADD COLUMN extract_status VARCHAR(32) DEFAULT 'ok';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'biz' AND table_name = 'asset_tokenomics'
                    AND column_name = 'next_retry_at'
                ) THEN
                    ALTER TABLE biz.asset_tokenomics ADD COLUMN next_retry_at TIMESTAMPTZ;
                END IF;
            END $$;
        """)
    conn.commit()


def write_tombstone(conn, asset_id: int, status: str, cooldown_days: int = TOMBSTONE_COOLDOWN_DAYS,
                    note: str | None = None) -> None:
    """写 tokenomics 墓碑（跳过/失败标记），避免同一资产被无限重选。

    墓碑行拥有 extract_status != 'ok' + next_retry_at（冷却期），
    get_candidates 会在冷却期过后重新纳入候选。
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO biz.asset_tokenomics (asset_id, extract_status, next_retry_at, extraction_notes, source_urls)
            VALUES (%s, %s, NOW() + %s * INTERVAL '1 day', %s, '{}')
            ON CONFLICT (asset_id) DO UPDATE SET
                extract_status = EXCLUDED.extract_status,
                next_retry_at = EXCLUDED.next_retry_at,
                extraction_notes = EXCLUDED.extraction_notes,
                updated_at = NOW()
        """, (asset_id, status, cooldown_days, note or f"tombstone: {status}"))
    conn.commit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="代币经济学批量提取（自动循环）")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="每批处理资产数")
    p.add_argument("--max-rounds", type=int, default=MAX_ROUNDS, help="最大轮次")
    p.add_argument("--force", action="store_true", help="强制覆盖已有数据")
    return p


def get_candidates(conn, batch_size: int, force: bool) -> list[int]:
    """获取尚未提取 tokenomics 的资产（有文档入口的优先）。

    - 无记录（asset_id IS NULL）或墓碑冷却期已过（next_retry_at <= NOW()）都算候选
    - 已成功入库（extract_status='ok'）且未 force 时排除
    排序策略：优先处理 CMC 排名靠前的主流币，避免小币连续失败导致任务提前终止。
    """
    where = (
        "TRUE"
        if force
        else "tok.asset_id IS NULL "
             "OR (tok.extract_status IS DISTINCT FROM 'ok' "
             "    AND (tok.next_retry_at IS NULL OR tok.next_retry_at <= NOW()))"
    )

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


def process_one(conn, llm: LLMClient, asset_id: int, force: bool) -> str:
    """处理单个资产，返回三态：

      "success" - 已入库（含"已有数据"跳过，视为成功）
      "skipped" - 正常跳过（无文档/无关键词链接/无内容），不计入连续失败，写墓碑
      "failed"  - 真实失败（LLM 异常/入库异常），计入连续失败
    """
    asset = resolve_asset(conn, asset_id, None)
    if not asset:
        print(f"  SKIP: 资产不存在 asset_id={asset_id}")
        return "skipped"

    symbol = asset["symbol"]
    name = asset["name"]
    print(f"\n--- {symbol} ({name}) [asset_id={asset_id}] ---")

    # 检查已有数据
    if not force:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM biz.asset_tokenomics WHERE asset_id = %s", (asset_id,))
            if cur.fetchone():
                print(f"  跳过（已有数据）")
                return "success"

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
            return "success"
        except Exception as e:
            print(f"  tokenomics.com 入库失败，回退到文档+LLM路径: {e}")

    # 收集所有文档链接
    all_links = collect_all_links(conn, asset_id)
    if not all_links:
        print(f"  SKIP: 无可用文档链接")
        write_tombstone(conn, asset_id, ST_NO_DOCS, note="无可用文档链接")
        return "skipped"

    print(f"  收集到 {len(all_links)} 个文档链接")

    # 关键词预筛 + 预筛盲区放宽：
    # 预筛未命中时，取非社媒/浏览器类文档前 5 条交给 LLM 判相关性，而非直接放弃。
    # （1965/7514/10121 类只有官网/社媒/explorer、无 tokenomics 关键词页的资产可借此补）
    prefiltered = _filter_tokenomics_links(all_links)
    if not prefiltered:
        relaxed = [
            d for d in all_links
            if d.get("doc_type") not in SKIP_DOC_TYPES and (d.get("source_url") or "").strip()
        ][:5]
        if not relaxed:
            print(f"  SKIP: 无 tokenomics 相关链接（含放宽），跳过，不调用 LLM")
            write_tombstone(conn, asset_id, ST_PREFILTER_MISS, note="无 tokenomics 关键词链接且无可用文档")
            return "skipped"
        prefiltered = relaxed
        print(f"  关键词预筛未命中，放宽预筛取 {len(relaxed)} 条候选交给 LLM 判断")

    print(f"  关键词预筛命中 {len(prefiltered)} 个链接")

    # AI 筛选相关链接（只在预筛后链接数 > MAX_PAGES 时才调用 LLM）
    try:
        relevant_urls = select_relevant_links(llm, asset, prefiltered)
    except Exception as e:
        print(f"  AI 链接筛选失败: {e}")
        # 降级：取前 10 个
        relevant_urls = [l["source_url"] for l in prefiltered[:10]]

    print(f"  AI 筛选出 {len(relevant_urls)} 个相关链接")

    if not relevant_urls:
        print(f"  SKIP: 无相关链接")
        write_tombstone(conn, asset_id, ST_NO_CONTENT, note="AI 筛选无相关链接")
        return "skipped"

    # 抓取页面内容
    doc_contents = []
    for url in relevant_urls:
        print(f"  抓取: {url[:80]}")
        # 查找原始 doc_type（用于日志标记）
        doc_type = "unknown"
        for d in prefiltered:
            if d["source_url"] == url:
                doc_type = d["doc_type"]
                break

        content = fetch_page_content(url)
        if content:
            doc_contents.append({
                "doc_type": doc_type,
                "source_url": url,
                "content": content,
            })
        else:
            print(f"    -> 失败")

    if not doc_contents:
        print(f"  SKIP: 无成功抓取的页面")
        write_tombstone(conn, asset_id, ST_NO_CONTENT, note="无成功抓取页面")
        return "skipped"

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
        return "failed"

    if not result:
        print(f"  LLM 提取失败")
        return "failed"

    print(f"  置信度: {result.get('confidence')}")

    # 入库
    source_urls = [d["source_url"] for d in doc_contents]
    try:
        save_tokenomics(conn, asset_id, source_urls, result)
        print(f"  已入库")
        return "success"
    except Exception as e:
        print(f"  入库失败: {e}")
        return "failed"


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

    # 确保墓碑列存在（幂等，兼容首次运行旧表）
    with get_connection(settings.database_url) as conn:
        _ensure_tombstone_columns(conn)

    total_done = 0
    total_skipped = 0
    total_failed = 0
    consecutive_failures = 0

    try:
        for round_num in range(1, max_rounds + 1):
            print()
            print("=" * 60)
            print(f"  Round {round_num} / max {max_rounds}  |  batch={batch_size}  累计成功={total_done}  跳过={total_skipped}  失败={total_failed}")
            print("=" * 60)

            with get_connection(settings.database_url) as conn:
                candidates = get_candidates(conn, batch_size, force)

            if not candidates:
                print("无更多候选资产，全部完成。")
                break

            round_done = 0
            round_skipped = 0
            round_failed = 0
            round_start = time.monotonic()

            for asset_id in candidates:
                try:
                    with get_connection(settings.database_url) as conn:
                        result = process_one(conn, llm, asset_id, force)
                except Exception as e:
                    print(f"  [ERROR] asset_id={asset_id}: {e}")
                    traceback.print_exc()
                    result = "failed"

                if result == "success":
                    round_done += 1
                    consecutive_failures = 0
                elif result == "skipped":
                    # 正常跳过（无文档/无关键词链接/无内容）不计入连续失败，
                    # 避免候选池头部连续几个小币就把任务熔断截断
                    round_skipped += 1
                else:  # "failed"
                    round_failed += 1
                    consecutive_failures += 1

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"\n连续失败 {consecutive_failures} 次，停止。")
                    break

            total_done += round_done
            total_skipped += round_skipped
            total_failed += round_failed

            elapsed = time.monotonic() - round_start
            print(f"\n本轮: {round_done} 成功, {round_skipped} 跳过, {round_failed} 失败 | {elapsed:.1f}s")

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break

    except KeyboardInterrupt:
        print("\n用户中断。")

    print(f"\n全部完成。累计: {total_done} 成功, {total_skipped} 跳过, {total_failed} 失败")


if __name__ == "__main__":
    main()
