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
    parser.add_argument("--asset-id", type=int, default=None, help="资产 ID（单资产模式）")
    parser.add_argument("--batch", type=int, default=None, metavar="N",
                        help="批量模式：处理市值前 N 个尚无缓存的资产")
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

    def _extract_json(raw: str):
        """从 LLM 返回内容中健壮地提取 JSON。"""
        if not raw or not raw.strip():
            raise ValueError("LLM 返回空内容")

        text = raw.strip()
        cleaned = text
        if cleaned.startswith("```"):
            nl = cleaned.find("\n")
            if nl > 0:
                cleaned = cleaned[nl + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

        fence_start = text.find("```")
        if fence_start >= 0:
            fence_end = text.find("\n", fence_start)
            if fence_end > 0:
                inner = text[fence_end + 1:]
                close_fence = inner.rfind("```")
                if close_fence > 0:
                    return json.loads(inner[:close_fence].strip())

        raise ValueError(f"无法解析 LLM 返回的 JSON，前 200 字符: {text[:200]}")

    # 构建候选列表文本
    if not candidates:
        return []

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
        "5. 社交链接（Twitter/Reddit/Telegram）不参与精选，已在上游过滤\n"
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

    # 解析 JSON（增强提取）
    try:
        result = _extract_json(content)
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


def _curate_single_asset(conn, asset_id: int, top_n: int, force: bool, dry_run: bool) -> dict:
    """为单个资产生成 NotebookLM 精选。返回结果 dict。"""
    import psycopg
    from crypto_research.db.upsert import load_sql

    select_candidates_sql = load_sql("biz/select_notebooklm_candidates.sql")

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 1. 检查缓存
        if not force:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM biz.doc_source_notebooklm WHERE asset_id = %s",
                (asset_id,),
            )
            row = cur.fetchone()
            if row and row["cnt"] > 0:
                cur.execute(
                    "SELECT entry_url FROM biz.doc_source_notebooklm WHERE asset_id = %s ORDER BY ai_rank",
                    (asset_id,),
                )
                urls = [r["entry_url"] for r in cur.fetchall()]
                return {"status": "cache_hit", "asset_id": asset_id, "count": len(urls), "urls": urls}

        # 2. 获取资产信息
        cur.execute(
            "SELECT canonical_symbol, canonical_name FROM core.asset WHERE asset_id = %s",
            (asset_id,),
        )
        asset_row = cur.fetchone()
        if not asset_row:
            return {"status": "error", "asset_id": asset_id, "error": "资产不存在"}
        asset_symbol = asset_row["canonical_symbol"]
        asset_name = asset_row["canonical_name"]

        # 3. 配额粗筛
        # SQL 有 9 个 %s，全部是 asset_id
        params = (asset_id,) * 9
        cur.execute(select_candidates_sql, params)
        candidates = [dict(row) for row in cur.fetchall()]

        if not candidates:
            return {"status": "empty", "asset_id": asset_id, "candidates": 0}

        # 4. AI 排序
        try:
            llm_config = _get_llm_config()
        except RuntimeError as e:
            print(f"  LLM 不可用: {e}，使用降级排序")
            ranked = _fallback_ranking(candidates, top_n)
        else:
            ranked = call_deepseek_ranking(candidates, asset_symbol, asset_name, top_n, llm_config)

        if dry_run:
            return {
                "status": "dry_run",
                "asset_id": asset_id,
                "candidates": len(candidates),
                "ranked": len(ranked),
                "urls": [r["entry_url"] for r in ranked],
            }

        # 5. 写入缓存表（先删后插）
        cur.execute(
            "DELETE FROM biz.doc_source_notebooklm WHERE asset_id = %s",
            (asset_id,),
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

        return {
            "status": "generated",
            "asset_id": asset_id,
            "candidates": len(candidates),
            "ranked": len(ranked),
            "urls": [r["entry_url"] for r in ranked],
        }


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 批量模式
        if args.batch:
            import psycopg
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 找市值前 N 个尚无缓存的资产
                not_exists_clause = "" if args.force else (
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM biz.doc_source_notebooklm n "
                    "  WHERE n.asset_id = a.asset_id"
                    ")"
                )
                cur.execute(f"""
                    SELECT a.asset_id, a.canonical_symbol, a.market_cap_rank
                    FROM core.asset a
                    WHERE a.market_cap_rank IS NOT NULL
                      {not_exists_clause}
                    ORDER BY a.market_cap_rank ASC
                    LIMIT %s
                """, (args.batch,))
                assets = cur.fetchall()

            print(f"批量模式：待处理 {len(assets)} 个资产（市值前 {args.batch}）")
            success = 0
            skipped = 0
            errors = 0
            for i, ast in enumerate(assets, 1):
                print(f"\n[{i}/{len(assets)}] {ast['canonical_symbol']} (asset_id={ast['asset_id']}, rank={ast['market_cap_rank']})")
                try:
                    result = _curate_single_asset(conn, ast["asset_id"], args.top_n, args.force, args.dry_run)
                    print(f"  → {result['status']}: {result.get('ranked', result.get('count', 0))} 条")
                    if result["status"] in ("generated", "cache_hit", "dry_run"):
                        success += 1
                    elif result["status"] == "empty":
                        skipped += 1
                    else:
                        errors += 1
                except Exception as e:
                    print(f"  → ERROR: {e}")
                    errors += 1

            print(f"\n批量完成：成功 {success}，空候选 {skipped}，错误 {errors}")
            return 0 if errors == 0 else 1

        # 单资产模式
        if not args.asset_id:
            print("请指定 --asset-id 或 --batch")
            return 1

        result = _curate_single_asset(conn, args.asset_id, args.top_n, args.force, args.dry_run)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] != "error" else 1


if __name__ == "__main__":
    sys.exit(main())