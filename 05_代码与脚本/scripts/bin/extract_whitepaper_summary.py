"""
白皮书结构化摘要提取：PDF → 文本 → LLM 提取 → 入库 doc_whitepaper_summary。

用法：
    python bin/extract_whitepaper_summary.py --asset_id 1
    python bin/extract_whitepaper_summary.py --doc_id 123
    python bin/extract_whitepaper_summary.py --all            # 提取所有有 PDF 但无摘要的白皮书
    python bin/extract_whitepaper_summary.py --all --force    # 强制重新提取（覆盖已有）
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from PyPDF2 import PdfReader

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

settings = get_settings(require_database=True)

# 文档存储根目录（优先环境变量，兼容容器和本地）
DOCS_STORAGE_ROOT = Path(os.getenv("DOCS_STORAGE_ROOT", "/app/docs_storage"))

# 单次提取最大文本字符数（避免 token 超限）
MAX_TEXT_CHARS = 15000


def extract_pdf_text(pdf_path: Path, max_chars: int = MAX_TEXT_CHARS) -> str:
    """从 PDF 提取文本，截取前 max_chars 字符。"""
    reader = PdfReader(str(pdf_path))
    parts = []
    total = 0
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        parts.append(f"--- Page {i + 1} ---\n{text}")
        total += len(text)
        if total >= max_chars:
            break
    full = "\n\n".join(parts)
    return full[:max_chars]


SYSTEM_PROMPT = """你是一个加密货币投研专家，擅长从项目白皮书中提取关键信息。
给定白皮书的文本内容，提取结构化的投研摘要。

要求：
1. 只输出 JSON，不要输出其他内容，不要 markdown 代码块包裹
2. 信息必须严格来自白皮书原文，不要编造
3. 不确定的字段填 null，数组字段为空数组
4. 所有文本字段使用中文（原文为英文则翻译为中文）
5. one_liner 控制在 15 字以内，summary_short 控制在 100 字以内，summary_long 控制在 500 字以内

JSON 格式：
{
  "one_liner": "一句话简介",
  "summary_short": "简短摘要（100字内）",
  "summary_long": "详细摘要（500字内）",
  "problem_statement": "项目要解决的核心问题",
  "solution": "解决方案概述",
  "core_mechanism": "核心技术/经济机制",
  "key_innovations": ["创新点1", "创新点2"],
  "tech_stack": ["技术栈1", "技术栈2"],
  "token_utility": "代币用途/价值捕获",
  "tokenomics_notes": "代币经济补充说明",
  "team_info": "核心团队信息",
  "investors": ["投资方1", "投资方2"],
  "funding_info": "融资历史/金额",
  "roadmap": "路线图概述",
  "key_milestones": ["里程碑1", "里程碑2"],
  "risks": ["风险1", "风险2"],
  "challenges": "面临的挑战",
  "confidence": 0.85,
  "extraction_notes": "提取备注（缺失字段、不确定等）"
}"""


def extract_whitepaper_summary(llm: LLMClient, pdf_text: str, symbol: str, name: str) -> dict:
    """调用 LLM 从白皮书文本提取结构化摘要。"""
    user_prompt = (
        f"项目：{symbol} ({name})\n\n"
        f"白皮书文本（前 {len(pdf_text)} 字符）：\n"
        f"{'=' * 40}\n"
        f"{pdf_text}\n"
        f"{'=' * 40}\n\n"
        f"请根据以上白皮书内容，提取结构化投研摘要。"
    )

    raw = llm.chat(SYSTEM_PROMPT, user_prompt, temperature=0.1, max_tokens=4096, response_format={"type": "json_object"})
    data = extract_json_from_llm_response(raw)
    return data


def get_whitepapers_to_process(conn, asset_id=None, doc_id=None, all_flag=False, force=False):
    """获取待处理的白皮书列表。"""
    conditions = ["d.doc_type = 'whitepaper'", "d.storage_path IS NOT NULL"]
    params = []

    if doc_id:
        conditions.append("d.doc_id = %s")
        params.append(doc_id)
    elif asset_id:
        conditions.append("d.asset_id = %s")
        params.append(asset_id)
    elif not all_flag:
        raise ValueError("必须指定 --asset_id, --doc_id 或 --all")

    if not force:
        conditions.append("s.id IS NULL")  # 没有已有摘要

    sql = f"""
        SELECT d.doc_id, d.asset_id, d.storage_path, d.file_name,
               a.canonical_symbol, a.canonical_name
        FROM biz.doc_asset d
        JOIN core.asset a ON a.asset_id = d.asset_id
        LEFT JOIN biz.doc_whitepaper_summary s ON s.doc_id = d.doc_id
        WHERE {' AND '.join(conditions)}
        ORDER BY d.doc_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def save_summary(conn, doc_id: int, asset_id: int, data: dict, raw_text: str):
    """保存摘要到数据库。"""
    sql = """
        INSERT INTO biz.doc_whitepaper_summary (
            doc_id, asset_id, one_liner, summary_short, summary_long,
            problem_statement, solution, core_mechanism, key_innovations, tech_stack,
            token_utility, tokenomics_notes, team_info, investors, funding_info,
            roadmap, key_milestones, risks, challenges,
            raw_text, extracted_by, confidence, extraction_notes
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, 'llm', %s, %s
        )
        ON CONFLICT (doc_id) DO UPDATE SET
            one_liner = EXCLUDED.one_liner,
            summary_short = EXCLUDED.summary_short,
            summary_long = EXCLUDED.summary_long,
            problem_statement = EXCLUDED.problem_statement,
            solution = EXCLUDED.solution,
            core_mechanism = EXCLUDED.core_mechanism,
            key_innovations = EXCLUDED.key_innovations,
            tech_stack = EXCLUDED.tech_stack,
            token_utility = EXCLUDED.token_utility,
            tokenomics_notes = EXCLUDED.tokenomics_notes,
            team_info = EXCLUDED.team_info,
            investors = EXCLUDED.investors,
            funding_info = EXCLUDED.funding_info,
            roadmap = EXCLUDED.roadmap,
            key_milestones = EXCLUDED.key_milestones,
            risks = EXCLUDED.risks,
            challenges = EXCLUDED.challenges,
            raw_text = EXCLUDED.raw_text,
            extracted_by = EXCLUDED.extracted_by,
            confidence = EXCLUDED.confidence,
            extraction_notes = EXCLUDED.extraction_notes,
            updated_at = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            doc_id, asset_id,
            data.get("one_liner"),
            data.get("summary_short"),
            data.get("summary_long"),
            data.get("problem_statement"),
            data.get("solution"),
            data.get("core_mechanism"),
            data.get("key_innovations") or [],
            data.get("tech_stack") or [],
            data.get("token_utility"),
            data.get("tokenomics_notes"),
            data.get("team_info"),
            data.get("investors") or [],
            data.get("funding_info"),
            data.get("roadmap"),
            data.get("key_milestones") or [],
            data.get("risks") or [],
            data.get("challenges"),
            raw_text[:10000],  # 截断保存原始文本
            data.get("confidence", 0.5),
            data.get("extraction_notes"),
        ))


def main():
    parser = argparse.ArgumentParser(description="白皮书结构化摘要提取")
    parser.add_argument("--asset_id", type=int, help="指定资产 ID")
    parser.add_argument("--doc_id", type=int, help="指定文档 ID")
    parser.add_argument("--all", action="store_true", help="处理所有有 PDF 但无摘要的白皮书")
    parser.add_argument("--force", action="store_true", help="强制重新提取（覆盖已有）")
    parser.add_argument("--limit", type=int, help="最多处理数量")
    args = parser.parse_args()

    llm = LLMClient(settings, rpm=30)
    if not llm.is_available():
        print("错误：未配置 LLM")
        sys.exit(1)

    with get_connection(settings.database_url) as conn:
        rows = get_whitepapers_to_process(
            conn,
            asset_id=args.asset_id,
            doc_id=args.doc_id,
            all_flag=args.all,
            force=args.force,
        )

        if args.limit:
            rows = rows[:args.limit]

        print(f"待处理白皮书: {len(rows)} 份")
        print()

        success = 0
        failed = 0

        for i, (doc_id, asset_id, storage_path, file_name, symbol, name) in enumerate(rows, 1):
            pdf_path = DOCS_STORAGE_ROOT / storage_path
            print(f"[{i}/{len(rows)}] doc_id={doc_id}, {symbol}/{name}")
            print(f"  文件: {storage_path}")

            if not pdf_path.exists():
                print(f"  [跳过] 文件不存在: {pdf_path}")
                failed += 1
                continue

            # 提取 PDF 文本
            try:
                pdf_text = extract_pdf_text(pdf_path)
                print(f"  提取文本: {len(pdf_text)} 字符")
            except Exception as e:
                print(f"  [失败] PDF 解析错误: {e}")
                failed += 1
                continue

            if len(pdf_text.strip()) < 200:
                print(f"  [跳过] 文本内容太少 ({len(pdf_text.strip())} 字符)，可能是扫描版或图片 PDF")
                failed += 1
                continue

            # LLM 提取
            try:
                t0 = time.time()
                data = extract_whitepaper_summary(llm, pdf_text, symbol, name)
                elapsed = time.time() - t0
                print(f"  LLM 提取完成，耗时 {elapsed:.1f}s，置信度 {data.get('confidence', 'N/A')}")
            except Exception as e:
                print(f"  [失败] LLM 提取错误: {e}")
                failed += 1
                continue

            # 保存
            try:
                save_summary(conn, doc_id, asset_id, data, pdf_text)
                print(f"  [OK] 已保存")
                success += 1
            except Exception as e:
                print(f"  [失败] 保存错误: {e}")
                failed += 1

            print()

        print(f"=== 完成 ===")
        print(f"  成功: {success}")
        print(f"  失败: {failed}")
        print(f"  总计: {len(rows)}")


if __name__ == "__main__":
    main()
