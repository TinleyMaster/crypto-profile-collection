"""
NotebookLM 投研精选：配额粗筛 + AI 排序，为指定资产选出 Top 50 投研链接。

流程：
1. 查缓存表 → 有则直接返回
2. 配额粗筛 SQL → 30~80 条候选
3. DeepSeek（思考模式）排序 → Top 50
4. 写入缓存表 → 下次秒出
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import requests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NotebookLM 投研精选")
    parser.add_argument("--asset-id", type=int, required=True, help="资产 ID")
    parser.add_argument("--force", action="store_true", help="强制重新生成，忽略缓存")
    parser.add_argument("--top-n", type=int, default=50, help="输出 Top N 链接，默认 50")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    return parser


def _get_llm_config():
    """获取 DeepSeek LLM 配置，优先 OPENAI 兼容接口，其次 ARK。"""
    import os
    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)

    if settings.openai_api_key and settings.openai_base_url and settings.llm_model:
        return {
            "api_key": settings.openai_api_key,
            "base_url": settings.openai_base_url.rstrip("/"),
            "model": settings.llm_model,
            "provider": "openai",
        }
    if settings.ark_api_key and settings.ark_base_url and settings.ark_model:
        return {
            "api_key": settings.ark_api_key,
            "base_url": settings.ark_base_url.rstrip("/"),
            "model": settings.ark_model,
            "provider": "ark",
        }
    raise RuntimeError("No LLM configured (need OPENAI_API_KEY or ARK_API_KEY)")


def call_deepseek_ranking(candidates: list[dict], asset_symbol: str, asset_name: str,
                          top_n: int, llm_config: dict) -> list[dict]:
    """调用 DeepSeek（思考模式）对候选链接排序。"""
    if not candidates:
        return []

    # 构建候选列表文本
    items_text = []
    for i, c in enumerate(candidates):
        tags = []
        if c.get("is_deep_crawl"):
            tags.append("deep_crawl")
        if c.get("is_primary"):
            tags.append("is_primary")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        items_text.append(
            f"#{i+1} [{c['entry_type']}] {c['entry_url']} "
            f"(来源: {c['source_code']}{tag_str})"
        )

    system_prompt = (
        "你是一个加密货币投研资料筛选专家。你的任务是从给定链接列表中，"
        "为投研人员选出最有价值的链接，按投研价值从高到低排序。\n"
        "\n"
        "排序原则：\n"
        "1. 白皮书、项目文档、官方文档门户 > 官网 > GitHub 主仓库 > 博客 > 社交\n"
        "2. 原始入口（CMC/CG 收录）的可信度高于 deep_crawl 自动发现的\n"
        "3. 同一域名下优先保留最有代表性的链接，避免重复\n"
        "4. 第三方审计报告、独立分析文章有一定价值，但非项目一手信息\n"
        "5. 社交链接（Twitter/Reddit/Telegram）投研价值低，除非没有其他链接才选\n"
        "6. 通用代码仓库（非项目主仓库）和聚合器网站价值极低\n"
        "\n"
        "输出格式：只输出 JSON，不要输出其他内容。\n"
        '{"ranked": [{"index": 数字, "reason": "简短理由"}]}\n'
        "index 是你输入中 #N 的编号，按投研价值从高到低排列。最多输出前 {top_n} 个。"
    )

    user_prompt = (
        f"资产: {asset_symbol} ({asset_name})\n"
        f"请从以下 {len(candidates)} 个链接中选出最有投研价值的前 {top_n} 个：\n\n"
        + "\n".join(items_text)
    )

    # 构建请求
    url = f"{llm_config['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {llm_config['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": llm_config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 16384,  # 大 token 池，给思考链留空间
        "thinking": {"type": "enabled"},  # 排序任务需要深度推理
    }

    print(f"  调用 {llm_config['model']}（思考模式）对 {len(candidates)} 条候选排序...")
    t0 = time.monotonic()

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  AI 调用失败: {e}")
        # 降级：按配额规则排序（whitepaper > docs > docs_portal > official_website > github > medium > other > twitter > reddit）
        return _fallback_ranking(candidates, top_n)

    elapsed = time.monotonic() - t0
    content = data["choices"][0]["message"].get("content") or ""
    # 兜底：如果 content 为空，尝试 reasoning_content
    if not content:
        content = data["choices"][0]["message"].get("reasoning_content") or ""
    print(f"  AI 响应完成，耗时 {elapsed:.1f}s，输出长度 {len(content)}")

    # 解析 JSON
    try:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            first_line_end = cleaned.find("\n")
            if first_line_end > 0:
                cleaned = cleaned[first_line_end + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        result = json.loads(cleaned)
        ranked = result.get("ranked", [])
        if not isinstance(ranked, list):
            raise ValueError(f"ranked 不是列表")

        # 构建排序结果
        index_map = {}
        for r in ranked:
            idx = int(r.get("index", 0))
            if 1 <= idx <= len(candidates):
                index_map[idx] = r.get("reason", "")

        ranked_candidates = []
        for i, c in enumerate(candidates):
            idx = i + 1
            if idx in index_map:
                c["ai_rank"] = len(ranked_candidates) + 1
                c["ai_reason"] = index_map[idx]
                ranked_candidates.append(c)

        print(f"  AI 选出了 {len(ranked_candidates)} 个链接")
        return ranked_candidates[:top_n]

    except (json.JSONDecodeError, ValueError) as e:
        print(f"  AI 响应解析失败: {e}")
        return _fallback_ranking(candidates, top_n)


def _fallback_ranking(candidates: list[dict], top_n: int) -> list[dict]:
    """降级：按配额规则排序。"""
    type_order = {
        "whitepaper_page": 0,
        "docs": 1,
        "docs_portal": 2,
        "official_website": 3,
        "github": 4,
        "medium": 5,
        "other": 6,
        "twitter": 7,
        "reddit": 8,
        "telegram": 9,
    }
    candidates.sort(key=lambda c: (
        type_order.get(c.get("entry_type", "other"), 99),
        0 if c.get("is_primary") else 1,
        0 if not c.get("is_deep_crawl") else 1,
    ))
    for i, c in enumerate(candidates[:top_n]):
        c["ai_rank"] = i + 1
        c["ai_reason"] = "降级排序（AI 不可用）"
    return candidates[:top_n]


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import load_sql

    settings = get_settings(require_database=True)
    select_candidates_sql = load_sql("biz/select_notebooklm_candidates.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 1. 检查缓存
            if not args.force:
                cur.execute(
                    "SELECT COUNT(*) as cnt FROM biz.doc_source_notebooklm WHERE asset_id = %s",
                    (args.asset_id,),
                )
                row = cur.fetchone()
                if row and row["cnt"] > 0:
                    print(f"  缓存命中: {row['cnt']} 条已精选链接")
                    cur.execute(
                        "SELECT entry_url FROM biz.doc_source_notebooklm WHERE asset_id = %s ORDER BY ai_rank",
                        (args.asset_id,),
                    )
                    urls = [r["entry_url"] for r in cur.fetchall()]
                    result = {"status": "cache_hit", "asset_id": args.asset_id, "count": len(urls), "urls": urls}
                    print(json.dumps(result, ensure_ascii=False))
                    return 0

            # 2. 获取资产信息
            cur.execute(
                "SELECT canonical_symbol, canonical_name FROM core.asset WHERE asset_id = %s",
                (args.asset_id,),
            )
            asset_row = cur.fetchone()
            if not asset_row:
                print(f"资产 {args.asset_id} 不存在")
                return 1
            asset_symbol = asset_row["canonical_symbol"]
            asset_name = asset_row["canonical_name"]

            # 3. 配额粗筛
            # SQL 有 11 个 %s，全部是 asset_id
            params = (args.asset_id,) * 11
            cur.execute(select_candidates_sql, params)
            candidates = [dict(row) for row in cur.fetchall()]

            if not candidates:
                msg = {"status": "empty", "asset_id": args.asset_id, "candidates": 0}
                print(json.dumps(msg, ensure_ascii=False))
                return 0

            print(f"  配额粗筛: {len(candidates)} 条候选")

            # 4. AI 排序
            try:
                llm_config = _get_llm_config()
            except RuntimeError as e:
                print(f"  LLM 不可用: {e}，使用降级排序")
                ranked = _fallback_ranking(candidates, args.top_n)
            else:
                ranked = call_deepseek_ranking(candidates, asset_symbol, asset_name, args.top_n, llm_config)

            if args.dry_run:
                result = {
                    "status": "dry_run",
                    "asset_id": args.asset_id,
                    "candidates": len(candidates),
                    "ranked": len(ranked),
                    "urls": [r["entry_url"] for r in ranked],
                }
                print(json.dumps(result, ensure_ascii=False))
                return 0

            # 5. 写入缓存表（先删后插）
            cur.execute(
                "DELETE FROM biz.doc_source_notebooklm WHERE asset_id = %s",
                (args.asset_id,),
            )
            for r in ranked:
                cur.execute(
                    """INSERT INTO biz.doc_source_notebooklm
                       (asset_id, source_entry_id, entry_type, entry_url, source_code, ai_rank, ai_reason)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        r["asset_id"],
                        r["source_entry_id"],
                        r["entry_type"],
                        r["entry_url"],
                        r["source_code"],
                        r["ai_rank"],
                        r.get("ai_reason", ""),
                    ),
                )
            conn.commit()

            result = {
                "status": "generated",
                "asset_id": args.asset_id,
                "candidates": len(candidates),
                "ranked": len(ranked),
                "urls": [r["entry_url"] for r in ranked],
            }
            print(json.dumps(result, ensure_ascii=False))
            return 0


if __name__ == "__main__":
    sys.exit(main())