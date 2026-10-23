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
import re
from datetime import datetime, timezone

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


def persist_contract_security_scan(conn, asset_id: int, chain: str, contract: str,
                                   result: dict) -> None:
    """ContractSecurityClient.scan 结果落库（GoPlus/RugCheck，upsert asset_id）。"""
    if not isinstance(result, dict):
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO biz.asset_contract_security
               (asset_id, chain, contract_addr, source, source_status,
                is_honeypot, is_open_source, is_mintable, can_take_back_ownership,
                hidden_owner, is_blacklisted, freeze_authority, mint_authority,
                buy_tax, sell_tax, lp_locked_pct, top_holders_pct,
                holder_count, creator_percent, risk_score, raw_json, scanned_at)
               VALUES (%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s::jsonb,NOW())
               ON CONFLICT (asset_id) DO UPDATE SET
                chain=EXCLUDED.chain, contract_addr=EXCLUDED.contract_addr,
                source=EXCLUDED.source, source_status=EXCLUDED.source_status,
                is_honeypot=EXCLUDED.is_honeypot, is_open_source=EXCLUDED.is_open_source,
                is_mintable=EXCLUDED.is_mintable, can_take_back_ownership=EXCLUDED.can_take_back_ownership,
                hidden_owner=EXCLUDED.hidden_owner, is_blacklisted=EXCLUDED.is_blacklisted,
                freeze_authority=EXCLUDED.freeze_authority, mint_authority=EXCLUDED.mint_authority,
                buy_tax=EXCLUDED.buy_tax, sell_tax=EXCLUDED.sell_tax,
                lp_locked_pct=EXCLUDED.lp_locked_pct, top_holders_pct=EXCLUDED.top_holders_pct,
                holder_count=EXCLUDED.holder_count, creator_percent=EXCLUDED.creator_percent,
                risk_score=EXCLUDED.risk_score, raw_json=EXCLUDED.raw_json, scanned_at=NOW()""",
            (asset_id, chain, contract,
             result.get("source") or "scan", result.get("source_status") or "hit",
             result.get("is_honeypot"), result.get("is_open_source"), result.get("is_mintable"),
             result.get("can_take_back_ownership"), result.get("hidden_owner"),
             result.get("is_blacklisted"), result.get("freeze_authority"), result.get("mint_authority"),
             result.get("buy_tax"), result.get("sell_tax"), result.get("lp_locked_pct"),
             result.get("top_holders_pct"), result.get("holder_count"), result.get("creator_percent"),
             result.get("risk_score"),
             json.dumps(result.get("raw_json") or result, ensure_ascii=False, default=str).replace("\\u0000", "")))


# ════════════════════════════════════════════════════════════════
# 社交热度（asset_social_heat）
# ════════════════════════════════════════════════════════════════
def persist_social_heat(conn, asset_id: int, symbol: str,
                        twitter_followers: int | None = None,
                        extra: dict | None = None) -> None:
    """社交热度落库（upsert）：X 关注数写入 input_snapshot_json，供后续评分。"""
    if not twitter_followers and not extra:
        return
    snapshot = {}
    if twitter_followers:
        snapshot["twitter_followers"] = twitter_followers
    if extra:
        snapshot.update(extra)
    snapshot["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO biz.asset_social_heat (asset_id, symbol, input_snapshot_json, updated_at)
               VALUES (%s,%s,%s::jsonb,NOW())
               ON CONFLICT (asset_id) DO UPDATE SET
                 symbol=EXCLUDED.symbol, input_snapshot_json=EXCLUDED.input_snapshot_json,
                 updated_at=NOW()""",
            (asset_id, symbol, json.dumps(snapshot, ensure_ascii=False)))


# ════════════════════════════════════════════════════════════════
# 融资记录（asset_raises，外部情报补强）
# ════════════════════════════════════════════════════════════════
def persist_raises(conn, asset_id: int, protocol_name: str,
                   raises: list[dict]) -> int:
    """融资记录落库（幂等：按 asset_id+round+source 去重）。返回新增条数。

    raises 元素：{round, amount(USD), raise_date, lead_investors, other_investors, source}
    """
    if not raises:
        return 0
    n = 0
    with conn.cursor() as cur:
        for r in raises:
            src = r.get("source") or "web_search"
            rnd = r.get("round") or "funding_round"
            cur.execute(
                "SELECT 1 FROM biz.asset_raises WHERE asset_id=%s AND round=%s AND source=%s LIMIT 1",
                (asset_id, rnd, src))
            if cur.fetchone():
                continue
            cur.execute(
                """INSERT INTO biz.asset_raises
                   (asset_id, protocol_name, round, raise_date, amount,
                    lead_investors, other_investors, source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (asset_id, protocol_name, rnd,
                 r.get("raise_date"), r.get("amount"),
                 r.get("lead_investors") or [], r.get("other_investors") or [], src))
            n += 1
    return n


# ════════════════════════════════════════════════════════════════
# 关联资产图谱（asset_related，AI 判断 + 落库）
# ════════════════════════════════════════════════════════════════
def persist_related_assets(conn, asset_id: int, relations: list[dict],
                           as_of_date=None) -> int:
    """关联资产图谱落库（幂等：按 asset_id+related_symbol+relation_type upsert）。

    relations 元素：
      {related_symbol, related_asset_id(可选), related_name(可选),
       relation_type, relation_direction(默认 bidirectional), strength(默认 medium),
       confidence(可选), rationale(传导逻辑，含建议观测联动指标)}
    as_of_date: 标注日期（默认今天）。返回 upsert 条数。
    """
    if not relations:
        return 0
    from datetime import date
    d = as_of_date or date.today()
    n = 0
    with conn.cursor() as cur:
        for r in relations:
            sym = (r.get("related_symbol") or "").strip().upper()
            if not sym:
                continue
            cur.execute(
                """INSERT INTO biz.asset_related
                   (asset_id, related_asset_id, related_symbol, related_name,
                    relation_type, relation_direction, strength, confidence,
                    rationale, source, as_of_date)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ai_analysis',%s)
                   ON CONFLICT (asset_id, related_symbol, relation_type) DO UPDATE SET
                     related_asset_id=EXCLUDED.related_asset_id,
                     related_name=EXCLUDED.related_name,
                     relation_direction=EXCLUDED.relation_direction,
                     strength=EXCLUDED.strength,
                     confidence=EXCLUDED.confidence,
                     rationale=EXCLUDED.rationale,
                     as_of_date=EXCLUDED.as_of_date,
                     updated_at=NOW()""",
                (asset_id,
                 r.get("related_asset_id"),
                 sym,
                 r.get("related_name"),
                 r.get("relation_type", "other"),
                 r.get("relation_direction", "bidirectional"),
                 r.get("strength", "medium"),
                 r.get("confidence"),
                 r.get("rationale"),
                 d))
            n += 1
    return n


# ════════════════════════════════════════════════════════════════
# 协议 TVL（protocol_tvl，来自 DefiLlama 补强；让 TVL 可被下游查询）
# ════════════════════════════════════════════════════════════════
def persist_protocol_tvl(conn, asset_id: int, tvl_usd: float, measured_at,
                         *, source: str = "DefiLlama", chains: list | None = None,
                         protocol_slug: str | None = None,
                         protocol_category: str | None = None,
                         notes: str | None = None) -> int:
    """协议 TVL 落库（幂等：按 asset_id+measured_at upsert，每日一行）。返回 upsert 条数。

    Args:
        tvl_usd: TVL 美元数值
        measured_at: 测量日期（date / datetime / str 'YYYY-MM-DD'）
        source: 数据来源，默认 DefiLlama
        chains: 链数组，如 ['Ethereum']
        protocol_slug: DefiLlama slug（如 'cap'）
        protocol_category: 协议分类（如 'Lending/Stablecoin'）
        notes: 备注（如峰值/回撤说明）
    """
    if tvl_usd is None or measured_at is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biz.protocol_tvl
                (asset_id, protocol_slug, tvl_usd, chains, protocol_category,
                 source, measured_at, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (asset_id, measured_at) DO UPDATE SET
                tvl_usd         = EXCLUDED.tvl_usd,
                protocol_slug   = EXCLUDED.protocol_slug,
                chains          = EXCLUDED.chains,
                protocol_category = EXCLUDED.protocol_category,
                source          = EXCLUDED.source,
                notes           = EXCLUDED.notes
            """,
            (asset_id, protocol_slug, tvl_usd, chains, protocol_category,
             source, measured_at, notes))
    return 1


# ════════════════════════════════════════════════════════════════
# 研究型催化剂（asset_catalyst，人工标注的 4 类催化事件，source=research_manual）
# ════════════════════════════════════════════════════════════════
def persist_catalysts(conn, asset_id: int, catalysts: list[dict],
                      *, source_code: str = "research_manual",
                      symbol: str | None = None) -> int:
    """研究型催化剂落库（幂等：按 source_code+source_article_id upsert）。返回 upsert 条数。

    与新闻流（Binance Square 等）区分：人工/AI 提炼的投研催化剂用 source_code='research_manual'，
    不冲击新闻流唯一键。catalysts 元素：
      {article_id(可选，缺省自动生成), title, body_text, published_at,
       event_category(可选), ai_event_type(可选), ai_sentiment(可选),
       ai_summary(可选), related_pairs(可选，默认 [SYMUSDT])}
    symbol: 可选，显式传入用于默认 related_pairs；不传则跳过（避免库内反查吞错导致事务中止）。
    """
    if not catalysts:
        return 0
    sym = (symbol or "").strip().upper()
    default_pair = ["{0}USDT".format(sym)] if sym else None
    n = 0
    with conn.cursor() as cur:
        for i, c in enumerate(catalysts):
            aid = c.get("article_id") or "{0}_{1}_{2}".format(source_code, asset_id, i)
            pairs = c.get("related_pairs") or default_pair
            cur.execute(
                """
                INSERT INTO biz.asset_catalyst
                    (source_code, source_article_id, asset_id, title, body_text,
                     published_at, event_category, related_pairs, source_codes,
                     ai_processed, ai_event_type, ai_sentiment, ai_summary,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        TRUE, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (source_code, source_article_id) DO UPDATE SET
                    asset_id      = EXCLUDED.asset_id,
                    title         = EXCLUDED.title,
                    body_text     = EXCLUDED.body_text,
                    published_at  = EXCLUDED.published_at,
                    event_category= EXCLUDED.event_category,
                    ai_event_type = EXCLUDED.ai_event_type,
                    ai_sentiment  = EXCLUDED.ai_sentiment,
                    ai_summary    = EXCLUDED.ai_summary,
                    updated_at    = NOW()
                """,
                (source_code, aid, asset_id, c.get("title"), c.get("body_text"),
                 c.get("published_at"), c.get("event_category"), pairs,
                 [source_code],
                 c.get("ai_event_type"), c.get("ai_sentiment"), c.get("ai_summary"))
            )
            n += 1
    return n


# ════════════════════════════════════════════════════════════════
# 投研报告归档（research_report，结构化结论 + 原文全文 + 前向跟踪）
# ════════════════════════════════════════════════════════════════
def _jd(v, default=None):
    """安全 JSON 序列化（非 ASCII 直出；不可序列化对象转字符串）。None → default。"""
    if v is None:
        return None if default is None else json.dumps(default, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False, default=str)


# ── 跟踪章节提取（tracking_json 兜底源）──────────────────────────
# 背景：2026-10-23 审计发现 21/63 份研报 tracking_json 为空——各写入方
# 均不解析 md 的跟踪章节。tracking 的唯一权威源是 md 的「跟踪清单」章节，
# 落库时缺失必须自动从 md 派生，杜绝「正文有、库里无」。
_TRACK_HEAD_PAT = re.compile(
    r'#{1,3}\s*(?:\d+(?:\.\d+)?[、.．]?\s*)?(?:十?[一二三四五六七八九十]+、)?[^#\n]*'
    r'(跟踪|情景观察清单|监测|观察清单)')
_TRACK_FREQ_PAT = re.compile(r'(日频|每日|每天|日度|周频|每周|周度|月频|每月|事件|触发|盘前|盘后|实时)')


def extract_tracking_from_md(md_text: str) -> list:
    """从报告 markdown 的跟踪章节提取结构化跟踪清单。

    返回 [{'freq': <分组>, 'items': [<条目>...]}]；找不到章节/无条目返回 []。
    分组规则：章节内子标题归为 freq（命中日频/周频/事件等关键词取关键词，
    否则取子标题前 12 字）；无子标题归「综合」。表格行转条目并滤掉表头。
    """
    if not md_text:
        return []
    lines = md_text.splitlines()
    start = next((i for i, l in enumerate(lines) if _TRACK_HEAD_PAT.search(l)), None)
    if start is None:
        return []

    def _level(line):
        m = re.match(r'^(#+)\s', line)
        return len(m.group(1)) if m else None

    lvl = _level(lines[start])
    end = len(lines)
    for j in range(start + 1, len(lines)):
        lv2 = _level(lines[j])
        if lv2 is not None and lv2 <= lvl:
            end = j
            break

    groups: list[tuple[str, list]] = []
    cur_freq, cur_items = '综合', []
    for raw in lines[start + 1:end]:
        l = raw.strip()
        if not l:
            continue
        if _level(raw) is not None:
            if cur_items:
                groups.append((cur_freq, cur_items))
                cur_items = []
            t = raw.lstrip('#').strip()
            m = _TRACK_FREQ_PAT.search(t)
            cur_freq = m.group(1) if m else (t[:12] if t else '综合')
            continue
        # 加粗伪标题（整行只有 **标签**[：]）→ 视为分组头
        m_bold = re.match(r'^\*\*([^*]{2,14})\*\*\s*[：:]?\s*$', l)
        if m_bold:
            if cur_items:
                groups.append((cur_freq, cur_items))
                cur_items = []
            t = m_bold.group(1).strip()
            m = _TRACK_FREQ_PAT.search(t)
            cur_freq = m.group(1) if m else t[:12]
            continue
        if re.match(r'^\|[\s\-|:]+\|?$', l):  # 表格分隔行
            continue
        if l.startswith('|'):
            l = l.strip('|').replace('|', '｜')
        l = re.sub(r'^[-*+]\s+', '', l)
        l = re.sub(r'^\d+[.、)）]\s*', '', l).strip()
        # 表头行过滤：短行内含 ≥2 个 ｜ 且含典型列名 → 跳过
        if l.count('｜') >= 2 and len(l) < 40 and re.search(r'跟踪项|触发条件|指标|节奏|动作|阈值|频率', l):
            continue
        if len(l) >= 4:
            cur_items.append(l[:200])
    if cur_items:
        groups.append((cur_freq, cur_items))
    return [{'freq': fq, 'items': items} for fq, items in groups]


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

    # tracking 兜底：写入方未传时自动从 md 跟踪章节派生（唯一权威源），
    # 派生事实记入 meta_json 供审计。md 无跟踪章节则留空，不臆造。
    meta = dict(meta) if meta else {}
    if not report.get("tracking"):
        derived = extract_tracking_from_md(md)
        if derived:
            report = {**report, "tracking": derived}
            meta["tracking_auto_extracted"] = True
        else:
            meta["tracking_missing_in_md"] = True

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

