"""
催化剂 AI 预处理脚本。

对 biz.asset_catalyst 中 ai_processed=false 的记录调用 LLM，提取：
  - ai_event_type: 事件类型（上新/下架/销毁/合作/监管/技术升级/融资/其他）
  - ai_sentiment: 情感倾向（bullish / bearish / neutral）
  - ai_summary: 一句话摘要（≤100字）
  - ai_keywords: 关键词数组

用法：
    python scripts/bin/process_catalyst_ai.py [--batch-size 50] [--max-items 1000]
    python scripts/bin/process_catalyst_ai.py --catalyst-id 123
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

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402
from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


SYSTEM_PROMPT = """你是一名加密货币事件分析助手。请根据给定的新闻标题和正文，输出结构化分析结果。

只输出 JSON，不要输出其他内容。JSON 格式：
{
  "event_type": "listing|delisting|burn|partnership|regulation|tech_upgrade|funding|market_update|other",
  "sentiment": "bullish|bearish|neutral",
  "summary": "一句话摘要（不超过100字）",
  "keywords": ["关键词1", "关键词2", "关键词3"]
}

事件类型说明：
- listing: 上新/上线/新币发行
- delisting: 下架/退市/停止交易
- burn: 销毁/回购销毁
- partnership: 合作/集成/生态扩展
- regulation: 监管/合规/政策
- tech_upgrade: 技术升级/主网升级/硬分叉
- funding: 融资/投资/募资
- market_update: 市场动态/行情更新/产品更新
- other: 其他

情感判断说明：
- bullish: 明显利好（上新、合作、融资、销毁等）
- bearish: 明显利空（下架、监管处罚、安全事件等）
- neutral: 中性（技术升级、常规公告、无法判断）
"""


def process_one(llm: LLMClient, catalyst: dict) -> dict:
    """处理单条催化剂，返回 ai_* 字段 dict。"""
    title = catalyst.get("title") or ""
    body = catalyst.get("body_text") or catalyst.get("body_html") or ""
    event_category = catalyst.get("event_category") or ""
    related_pairs = catalyst.get("related_pairs") or []

    # 截断正文，避免 token 浪费
    if len(body) > 1000:
        body = body[:1000] + "..."

    user_prompt = (
        f"标题：{title}\n"
        f"分类：{event_category}\n"
        f"关联交易对：{', '.join(related_pairs) if related_pairs else '无'}\n"
        f"正文：\n{body}\n\n"
        f"请分析这条新闻。"
    )

    raw = llm.chat(SYSTEM_PROMPT, user_prompt, temperature=0.1, max_tokens=512)

    # 解析 JSON
    result = extract_json_from_llm_response(raw)

    # 字段校验 + 归一化
    event_type = (result.get("event_type") or "other").lower()
    valid_types = {"listing", "delisting", "burn", "partnership", "regulation",
                   "tech_upgrade", "funding", "market_update", "other"}
    if event_type not in valid_types:
        event_type = "other"

    sentiment = (result.get("sentiment") or "neutral").lower()
    if sentiment not in {"bullish", "bearish", "neutral"}:
        sentiment = "neutral"

    summary = (result.get("summary") or "").strip()
    if len(summary) > 200:
        summary = summary[:200]

    keywords = result.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    keywords = [str(k).strip() for k in keywords if k][:10]

    return {
        "ai_event_type": event_type,
        "ai_sentiment": sentiment,
        "ai_summary": summary,
        "ai_keywords": keywords,
    }


def fetch_pending(conn, batch_size: int = 50, force: bool = False, offset: int = 0) -> list[dict]:
    """获取待处理的催化剂记录。

    force=True 时忽略 ai_processed 状态，按 published_at 倒序取 batch_size 条。
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if force:
            cur.execute(
                """
                SELECT catalyst_id, source_code, source_article_id, source_article_code,
                       title, body_text, body_html, event_category, related_pairs,
                       published_at, source_url
                FROM biz.asset_catalyst
                ORDER BY published_at DESC
                LIMIT %s OFFSET %s
                """,
                (batch_size, offset),
            )
        else:
            cur.execute(
                """
                SELECT catalyst_id, source_code, source_article_id, source_article_code,
                       title, body_text, body_html, event_category, related_pairs,
                       published_at, source_url
                FROM biz.asset_catalyst
                WHERE ai_processed = FALSE
                   OR ai_processed IS NULL
                ORDER BY published_at DESC
                LIMIT %s
                """,
                (batch_size,),
            )
        rows = cur.fetchall()

    result = []
    for row in rows:
        d = dict(row)
        # related_pairs 可能是 list 或 str
        if isinstance(d["related_pairs"], str):
            try:
                d["related_pairs"] = json.loads(d["related_pairs"])
            except Exception:
                d["related_pairs"] = []
        result.append(d)
    return result


def fetch_by_id(conn, catalyst_id: int) -> dict | None:
    """按 ID 获取单条催化剂。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT catalyst_id, source_code, source_article_id, source_article_code,
                   title, body_text, body_html, event_category, related_pairs,
                   published_at, source_url
            FROM biz.asset_catalyst
            WHERE catalyst_id = %s
            """,
            (catalyst_id,),
        )
        row = cur.fetchone()

    if not row:
        return None
    d = dict(row)
    if isinstance(d["related_pairs"], str):
        try:
            d["related_pairs"] = json.loads(d["related_pairs"])
        except Exception:
            d["related_pairs"] = []
    return d


def update_result(conn, catalyst_id: int, ai_data: dict) -> None:
    """更新 AI 处理结果到数据库。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE biz.asset_catalyst
            SET ai_event_type = %s,
                ai_sentiment = %s,
                ai_summary = %s,
                ai_keywords = %s,
                ai_processed = TRUE,
                ai_processed_at = NOW()
            WHERE catalyst_id = %s
            """,
            (
                ai_data["ai_event_type"],
                ai_data["ai_sentiment"],
                ai_data["ai_summary"],
                ai_data["ai_keywords"],  # TEXT[] 直接传 list
                catalyst_id,
            ),
        )


def main():
    parser = argparse.ArgumentParser(description="催化剂 AI 预处理（事件分类/情感/摘要）")
    parser.add_argument("--batch-size", type=int, default=50, help="每批处理数量")
    parser.add_argument("--max-items", type=int, default=0, help="最多处理条数（0=全部）")
    parser.add_argument("--catalyst-id", type=int, help="只处理指定 catalyst_id")
    parser.add_argument("--force", action="store_true", help="忽略 ai_processed 状态强制重跑")
    parser.add_argument("--sleep", type=float, default=0.5, help="每条之间的间隔秒数")
    args = parser.parse_args()

    settings = get_settings()
    llm = LLMClient(settings)

    if not llm.is_available():
        print("错误：未配置 LLM 提供商（需要 OPENAI_API_KEY/BASE_URL/MODEL 或 ARK_* 环境变量）")
        return 1

    with get_connection(settings.database_url) as conn:
        if args.catalyst_id:
            # 单条模式
            cat = fetch_by_id(conn, args.catalyst_id)
            if not cat:
                print(f"未找到 catalyst_id={args.catalyst_id}")
                return 1
            print(f"处理: [{cat['catalyst_id']}] {cat['title'][:60]}")
            result = process_one(llm, cat)
            update_result(conn, cat["catalyst_id"], result)
            print(f"  → event_type={result['ai_event_type']}, "
                  f"sentiment={result['ai_sentiment']}, "
                  f"summary={result['ai_summary'][:50]}")
            return 0

        # 批量模式
        processed = 0
        failed = 0
        offset = 0

        while True:
            pending = fetch_pending(conn, args.batch_size, force=args.force, offset=offset)
            if not pending:
                print("没有待处理的催化剂，完成。")
                break

            print(f"\n获取到 {len(pending)} 条待处理催化剂")

            for cat in pending:
                if args.max_items and processed >= args.max_items:
                    print(f"已达到 max_items={args.max_items}，停止。")
                    break

                cid = cat["catalyst_id"]
                title_preview = (cat.get("title") or "(无标题)")[:60]
                print(f"[{processed + 1}] 处理 catalyst_id={cid}: {title_preview}")

                try:
                    result = process_one(llm, cat)
                    update_result(conn, cid, result)
                    processed += 1
                    print(f"     ✓ event_type={result['ai_event_type']}, "
                          f"sentiment={result['ai_sentiment']}")
                except Exception as e:
                    failed += 1
                    print(f"     ✗ 失败: {e}")

                time.sleep(args.sleep)

            if args.max_items and processed >= args.max_items:
                break

            # force 模式用 offset 推进；非 force 模式靠 ai_processed 状态推进
            if args.force:
                offset += len(pending)

            # 如果这一批不满 batch_size，说明没有更多了
            if len(pending) < args.batch_size:
                break

    print(f"\n完成：成功 {processed} 条，失败 {failed} 条")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
