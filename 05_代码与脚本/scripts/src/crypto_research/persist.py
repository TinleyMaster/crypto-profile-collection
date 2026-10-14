"""投研数据统一落库层：各采集结果 → 幂等写回 biz 层事实表。

设计目标（支撑「边投研边落库」与 Alpha 因子挖掘）：
    - 每个函数 = 一个数据域的持久化（幂等：当日快照先删后插 / ON CONFLICT upsert）
    - 事实层落库：只存采集到的事实，不在此处做投研判断
    - 快照表带 asset_id + snapshot_date，可随多次投研累积成时间序列供回测

已实现的数据域：
    - doc_whitepaper_summary      文档结构化摘要（LLM 提炼 + 落库）
    - onchain_holder_snapshot/rank 持有者集中度快照 + Top 明细
    - asset_contract_security     合约安全核查（代理/实现/部署者/信誉）
"""
from __future__ import annotations

import hashlib
import json
import logging

from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 文档结构化摘要（LLM 提炼 → doc_whitepaper_summary）
# ════════════════════════════════════════════════════════════════
_DOC_SUMMARY_SYSTEM_PROMPT = """你是一个加密货币投研专家，擅长从项目官方文档中提取关键信息。
给定项目文档的 Markdown 文本内容，提取结构化的投研摘要。

要求：
1. 只输出 JSON，不要输出其他内容，不要 markdown 代码块包裹
2. 信息必须严格来自文档原文，不要编造；不确定的字段填 null，数组字段为空数组
3. 所有文本字段使用中文（原文为英文则翻译为中文）
4. one_liner 控制在 15 字以内，summary_short 控制在 100 字以内，summary_long 控制在 500 字以内
5. 文档可能包含多个协议版本（如 v1/v2），机制类字段优先综合最新的正式版本

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
  "team_info": "核心团队信息（仅文档中出现的）",
  "investors": ["投资方1", "投资方2"],
  "funding_info": "融资历史/金额（仅文档中出现的）",
  "roadmap": "路线图概述（含版本迭代计划）",
  "key_milestones": ["里程碑1", "里程碑2"],
  "risks": ["风险1", "风险2"],
  "challenges": "面临的挑战",
  "confidence": 0.85,
  "extraction_notes": "提取备注（缺失字段、不确定等）"
}"""


def summarize_docs(llm: LLMClient, md_text: str, symbol: str, name: str) -> dict:
    """从抓取到的项目文档 Markdown 提炼结构化摘要（LLM，纯提取不改写）。"""
    user_prompt = (
        f"项目：{symbol} ({name})\n\n"
        f"官方文档 Markdown（前 {len(md_text)} 字符）：\n"
        f"{'=' * 40}\n"
        f"{md_text}\n"
        f"{'=' * 40}\n\n"
        f"请根据以上文档内容，提取结构化投研摘要。"
    )
    raw = llm.chat(_DOC_SUMMARY_SYSTEM_PROMPT, user_prompt, temperature=0.1,
                   max_tokens=4096, response_format={"type": "json_object"})
    return extract_json_from_llm_response(raw) or {}


def persist_doc_summary(conn, asset_id: int, symbol: str, name: str,
                        md_text: str, source_pages: list[str] | None = None,
                        force: bool = False) -> dict | None:
    """LLM 提炼 docs 摘要并落库 doc_whitepaper_summary（幂等，关联 doc_asset）。

    - 自动确保 doc_asset 记录存在（该资产无 docs 类记录时插入一条占位）。
    - source_pages: 抓取来源 URL 列表（写入 source_pages 字段）。
    - force: 已存在摘要时是否强制重新提炼（默认否：已有则跳过）。
    返回摘要 dict（落库成功）或 None（跳过/失败）。
    """
    if not md_text:
        return None

    # 1) 确保 doc_asset 记录存在（doc_id 是摘要表的必填外键）
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id FROM biz.doc_asset "
            "WHERE asset_id=%s AND entity_type='asset' AND doc_type IN ('whitepaper','docs') "
            "ORDER BY doc_id LIMIT 1", (asset_id,))
        row = cur.fetchone()
        if row:
            doc_id = row[0]
        else:
            source_url = (source_pages or ["https://docs."])[0]
            cur.execute(
                """INSERT INTO biz.doc_asset
                   (entity_type, asset_id, doc_type, source_url, file_name, mime_type,
                    parse_status, sync_status, language)
                   VALUES ('asset', %s, 'docs', %s, 'docs_deep.md', 'text/markdown',
                           '已解析', '已同步', 'en')
                   RETURNING doc_id""",
                (asset_id, source_url))
            doc_id = cur.fetchone()[0]

        # 2) 已有摘要则跳过（--force 覆盖）
        if not force:
            cur.execute("SELECT 1 FROM biz.doc_whitepaper_summary WHERE doc_id=%s LIMIT 1", (doc_id,))
            if cur.fetchone():
                return None

    # 3) LLM 提炼
    try:
        from crypto_research.config import get_settings
        llm = LLMClient(get_settings(require_database=False))
    except Exception as e:
        return None
    data = summarize_docs(llm, md_text[:50000], symbol, name)
    if not data:
        return None

    # 4) 落库（先删后插，幂等）
    with conn.cursor() as cur:
        cur.execute("DELETE FROM biz.doc_whitepaper_summary WHERE doc_id=%s", (doc_id,))
        cur.execute(
            """INSERT INTO biz.doc_whitepaper_summary
               (doc_id, asset_id, one_liner, summary_short, summary_long,
                problem_statement, solution, core_mechanism, key_innovations, tech_stack,
                token_utility, tokenomics_notes, team_info, investors, funding_info,
                roadmap, key_milestones, risks, challenges,
                source_pages, raw_text, extracted_by, confidence, extraction_notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'llm',%s,%s)""",
            (doc_id, asset_id,
             data.get("one_liner"), data.get("summary_short"), data.get("summary_long"),
             data.get("problem_statement"), data.get("solution"), data.get("core_mechanism"),
             data.get("key_innovations") or [], data.get("tech_stack") or [],
             data.get("token_utility"), data.get("tokenomics_notes"), data.get("team_info"),
             data.get("investors") or [], data.get("funding_info"),
             data.get("roadmap"), data.get("key_milestones") or [],
             data.get("risks") or [], data.get("challenges"),
             source_pages or [], md_text[:100000], data.get("confidence"),
             data.get("extraction_notes")))
    return data


# ════════════════════════════════════════════════════════════════
# 持有者分布快照（onchain_holder_snapshot + onchain_holder_rank）
# ════════════════════════════════════════════════════════════════
def _addr_type(addr: str, is_contract_map: dict, burn_addr: str) -> str:
    low = addr.lower()
    if low == burn_addr.lower():
        return "burn"
    if low == "0x0000000000000000000000000000000000000000":
        return "zero"
    return "contract" if is_contract_map.get(low) else "eoa"


def persist_holder_snapshot(conn, asset_id: int, chain: str, contract: str, snapshot_date,
                            holders: list[dict], burn_addr: str,
                            is_contract_map: dict, total_supply: float,
                            total_holders: int = 0) -> int:
    """持有者集中度快照 + Top 明细落库（幂等：当日同资产先删后插）。返回 snapshot_id。"""
    rows = sorted(((h["address"].lower(), int(h["value"]) / 1e18) for h in holders),
                  key=lambda x: -x[1])
    if not rows:
        return 0
    burn_amt = next((v for a, v in rows if a == burn_addr.lower()), 0.0)
    circ = total_supply - burn_amt

    def pct(v): return v / total_supply * 100
    def top(n): return sum(v for _, v in rows[:n])

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM biz.onchain_holder_snapshot WHERE asset_id=%s AND chain=%s "
            "AND LOWER(contract_address)=LOWER(%s) AND snapshot_date=%s",
            (asset_id, chain, contract, snapshot_date))
        cur.execute(
            """INSERT INTO biz.onchain_holder_snapshot
               (asset_id, chain, contract_address, snapshot_date,
                top1_concentration, top5_concentration, top10_concentration,
                top20_concentration, top50_concentration, top100_concentration,
                burn_pct, burned_amount, circ_supply, total_holders)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING snapshot_id""",
            (asset_id, chain, contract, snapshot_date,
             pct(top(1)), pct(top(5)), pct(top(10)), pct(top(20)), pct(top(50)), pct(top(100)),
             pct(burn_amt), burn_amt, circ,
             total_holders if total_holders > 0 else len(rows)))
        sid = cur.fetchone()[0]
        for i, (addr, val) in enumerate(rows, 1):
            cur.execute(
                """INSERT INTO biz.onchain_holder_rank
                   (snapshot_id, rank, holder_address, balance, balance_pct, address_type, is_burn)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (sid, i, addr, val, pct(val),
                 _addr_type(addr, is_contract_map, burn_addr), addr.lower() == burn_addr.lower()))
    return sid


# ════════════════════════════════════════════════════════════════
# 合约安全核查（asset_contract_security）
# ════════════════════════════════════════════════════════════════
def persist_contract_security(conn, asset_id: int, chain: str, contract: str,
                              insp: dict, meta: dict) -> None:
    """合约安全核查落库（upsert，source=blockscout_rpc）。"""
    raw = {k: v for k, v in insp.items() if k != "address"}
    raw["blockscout_meta"] = meta
    raw_json_str = json.dumps(raw, ensure_ascii=False, default=str).replace("\\u0000", "")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO biz.asset_contract_security
               (asset_id, chain, contract_addr, source, source_status,
                is_proxy, proxy_type, implementation_addr, contract_name,
                creator_addr, contract_verified, reputation, risk_score, raw_json, scanned_at)
               VALUES (%s,%s,%s,'blockscout_rpc','hit',
                %s,%s,%s,%s,%s,%s,%s, NULL, %s::jsonb, NOW())
               ON CONFLICT (asset_id) DO UPDATE SET
                chain=EXCLUDED.chain, contract_addr=EXCLUDED.contract_addr,
                source=EXCLUDED.source, source_status=EXCLUDED.source_status,
                is_proxy=EXCLUDED.is_proxy, proxy_type=EXCLUDED.proxy_type,
                implementation_addr=EXCLUDED.implementation_addr,
                contract_name=EXCLUDED.contract_name, creator_addr=EXCLUDED.creator_addr,
                contract_verified=EXCLUDED.contract_verified,
                reputation=EXCLUDED.reputation, raw_json=EXCLUDED.raw_json,
                scanned_at=NOW()""",
            (asset_id, chain, contract,
             insp.get("is_proxy"), insp.get("proxy_type"),
             insp.get("eip1967_impl") or insp.get("impl_call"),
             meta.get("contract_name"), meta.get("creator_addr"),
             meta.get("contract_verified"), meta.get("reputation"),
             raw_json_str))


# ════════════════════════════════════════════════════════════════
# 投研报告归档（research_report，结构化结论 + 原文全文 + 前向跟踪）
# ════════════════════════════════════════════════════════════════
def _jd(v, default=None):
    """安全 JSON 序列化（非 ASCII 直出；不可序列化对象转字符串）。None → default。"""
    if v is None:
        return None if default is None else json.dumps(default, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False, default=str)


def _fetch_as_of_price(conn, asset_id: int):
    """取建仓基准价：`market_date <= 北京今日` 的最近可得收盘价（同 thesis_forward_track 口径）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT price_usd FROM biz.asset_market_daily
            WHERE asset_id = %s
              AND source_code IN ('cmc', 'cmc_historical')
              AND market_date <= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
              AND price_usd IS NOT NULL AND price_usd > 0
            ORDER BY market_date DESC,
                     CASE source_code WHEN 'cmc' THEN 0 ELSE 1 END
            LIMIT 1
            """, (asset_id,))
        row = cur.fetchone()
    if not row:
        return None
    val = row["price_usd"] if isinstance(row, dict) else row[0]
    return float(val) if val is not None else None


def persist_research_report(conn, asset_id: int, symbol: str, report: dict,
                            report_md: str, name: str | None = None,
                            report_path: str | None = None,
                            engine: str = "加密投研助手",
                            subject: str | None = None,
                            email_status: str | None = None,
                            email_error: str | None = None,
                            meta: dict | None = None,
                            as_of_date=None) -> dict:
    """投研报告落库：结构化结论 + 原文全文归档 + 前向跟踪建仓行。

    report 期望字段（缺失一律留空，不臆造）：
      title, stance(bullish/bearish/neutral), score(0-10), confidence(0-1),
      summary(核心判断一句话), thesis(核心论点 list), drivers_up(上行驱动 list),
      drivers_down(下行风险 list), tracking(跟踪清单 list/dict),
      key_metrics(dict), risks(list), catalysts(list), dimensions(dict), sources(list)

    设计：
      - 结构化 + 原文写入 biz.research_report；幂等键 (asset_id, report_sha256)：
        同内容重跑只更新邮件状态/元数据，不重复建行。
      - 前向跟踪：写 biz.thesis_forward_track 建仓行（asset_id, as_of, price_at），
        由既有日级任务回填 T+7/30/90 收益 —— 复用现成链路，不新增任务；
        与既有行冲突时只补空值，不覆盖（COALESCE）。

    返回 {report_id, sha256, archived(bool), forward_track(bool)}。
    """
    md = report_md or ""
    sha = hashlib.sha256(md.encode("utf-8")).hexdigest() if md else None
    out = {"report_id": None, "sha256": sha, "archived": False, "forward_track": False}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biz.research_report
                (asset_id, symbol, name, title, engine, stance, score, confidence,
                 summary, thesis_json, drivers_up_json, drivers_down_json, tracking_json,
                 key_metrics_json, risks_json, catalysts_json, dimensions_json, sources_json,
                 report_path, report_md, report_sha256, subject, email_status, email_error,
                 meta_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,
                    %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,
                    %s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (asset_id, report_sha256) DO UPDATE SET
                engine         = EXCLUDED.engine,
                stance         = EXCLUDED.stance,
                score          = EXCLUDED.score,
                confidence     = EXCLUDED.confidence,
                summary        = EXCLUDED.summary,
                report_path    = EXCLUDED.report_path,
                subject        = EXCLUDED.subject,
                email_status   = EXCLUDED.email_status,
                email_error    = EXCLUDED.email_error,
                meta_json      = EXCLUDED.meta_json,
                updated_at     = NOW()
            RETURNING report_id
            """,
            (asset_id, symbol, name, report.get("title"), engine,
             report.get("stance"), report.get("score"), report.get("confidence"),
             report.get("summary"),
             _jd(report.get("thesis"), []),
             _jd(report.get("drivers_up")), _jd(report.get("drivers_down")),
             _jd(report.get("tracking")),
             _jd(report.get("key_metrics")), _jd(report.get("risks")),
             _jd(report.get("catalysts")), _jd(report.get("dimensions")),
             _jd(report.get("sources")),
             report_path, md, sha, subject, email_status, email_error,
             _jd(meta, {})))
        row = cur.fetchone()
        if row:
            out["report_id"] = row["report_id"] if isinstance(row, dict) else row[0]
            out["archived"] = True

        # 前向跟踪建仓行（复用既有日级回填任务；冲突只补空值不覆盖）
        price_at = _fetch_as_of_price(conn, asset_id)
        cur.execute(
            """
            INSERT INTO biz.thesis_forward_track
                (thesis_id, asset_id, as_of, price_at)
            VALUES (NULL, %s, COALESCE(%s, (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date), %s)
            ON CONFLICT (asset_id, as_of) DO UPDATE SET
                price_at  = COALESCE(biz.thesis_forward_track.price_at, EXCLUDED.price_at)
            """,
            (asset_id, as_of_date, price_at))
        out["forward_track"] = True

    return out

