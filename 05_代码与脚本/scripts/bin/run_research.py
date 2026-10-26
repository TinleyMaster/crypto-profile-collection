#!/usr/bin/env python3
"""加密投研助手：投研数据采集器（只收集数据，不做投研判断）。

职责边界（重要）：
    - 本脚本只负责**收集数据**：查询系统 DB、Crawl4AI 抓取官方文档、
      firecrawl 搜索外部情报、数据体检、内容质检。
    - 输出一份**纯数据清单**（research_data.md）+ 完整数据包（research.json），
      只陈述事实，**不含任何评分/解读/结论**。
    - 投研判断（多空、评分、解读、叙事、风险、结论）由 AI 层完成：
      AI 读取数据清单 → 自行分析 → 生成最终投研报告。

用法：
    python run_research.py --symbol BTC                     # 默认输出数据清单+JSON
    python run_research.py --symbol UNI --crawl 3           # 补抓官方文档
    python run_research.py --symbol UNI --search 2          # firecrawl 搜索补外部情报
    python run_research.py --symbol BTC --dq                # 附带内容质量检查
    python run_research.py --symbol BTC --json-only         # 只出 JSON 数据包

输出目录（默认 scripts/data/research/）：
    {symbol}_{date}/research_data.md  （AI 可读数据清单，仅事实）
    {symbol}_{date}/research.json     （完整结构化数据包）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunsplit

# ── 路径约定 ───────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
_scripts_src = os.path.join(_code_root, "scripts", "src")
for cand in (_scripts_src, os.path.join(_code_root, "workbench"), "/app"):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 复用同目录 data_health 的资产解析与体检
from data_health import check_data_health, render_health_md, resolve_asset  # noqa: E402

DEFAULT_OUT = Path(_here).parent / "data" / "research"


# ════════════════════════════════════════════════════════════════════
# 格式化工具（纯展示，不改变语义）
# ════════════════════════════════════════════════════════════════════
def _fmt_usd(v, digits: int = 2) -> str:
    """数字 → 千分位美元字符串；None/NaN → '—'。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:  # NaN
        return "—"
    if abs(f) >= 1e12:
        return f"{f / 1e12:.{digits}f}T"
    if abs(f) >= 1e9:
        return f"{f / 1e9:.{digits}f}B"
    if abs(f) >= 1e6:
        return f"{f / 1e6:.{digits}f}M"
    if abs(f) >= 1e3:
        return f"{f / 1e3:.{digits}f}K"
    return f"{f:.{digits}f}"


def _fmt_pct(v, digits: int = 2, signed: bool = True) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    s = "+" if (signed and f > 0) else ""
    return f"{s}{f:.{digits}f}%"


def _fmt_int(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


# ════════════════════════════════════════════════════════════════════
# P0 资产定位
# ════════════════════════════════════════════════════════════════════
def stage_identity(conn, asset: dict) -> dict:
    """标的画像：coin_basic + core.asset 主字段（避开被污染的 description_short）。"""
    aid = asset["asset_id"]
    out = {"asset_id": aid, "symbol": asset["canonical_symbol"], "name": asset["canonical_name"]}
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM biz.coin_basic WHERE asset_id=%s", (aid,))
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            cb = dict(zip(cols, row))
            out["cmc_id"] = cb.get("cmc_id")
            out["official_website"] = cb.get("official_website")
            out["main_chain"] = cb.get("main_chain")
            out["defillama_slug"] = cb.get("defillama_slug")
            out["mapping_status"] = cb.get("mapping_status")

        cur.execute("SELECT * FROM core.asset WHERE asset_id=%s", (aid,))
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            ca = dict(zip(cols, row))
            out["launch_date"] = ca.get("launch_date")
            out["primary_sector"] = ca.get("primary_sector")
            out["market_cap_rank"] = ca.get("market_cap_rank")
            out["categories"] = (ca.get("categories") or [])[:8]
            out["circulating_supply"] = ca.get("circulating_supply")
            out["total_supply"] = ca.get("total_supply")
            out["ath_usd"] = ca.get("ath_usd")
            out["asset_status"] = ca.get("status")

        # 赛道（asset_sector 主赛道）
        cur.execute(
            "SELECT sector, source, confidence, is_primary FROM biz.asset_sector "
            "WHERE asset_id=%s ORDER BY is_primary DESC, confidence DESC LIMIT 4",
            (aid,),
        )
        out["sectors"] = [dict(zip(["sector", "source", "confidence", "is_primary"], r)) for r in cur.fetchall()]
    return out


# ════════════════════════════════════════════════════════════════════
# P2 文档基本面（数据采集）
# ════════════════════════════════════════════════════════════════════
def stage_docs(conn, asset: dict) -> dict:
    """文档资产盘点：入口链接 + 已下载文档 + 白皮书摘要 + GitHub 活跃。"""
    aid = asset["asset_id"]
    out = {"doc_entries": [], "doc_assets": [], "whitepaper": None, "github": []}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT entry_type, COUNT(*) FROM biz.doc_source_entry WHERE asset_id=%s "
            "GROUP BY entry_type ORDER BY COUNT(*) DESC",
            (aid,),
        )
        out["entry_counts"] = {r[0]: r[1] for r in cur.fetchall()}

        # 外部抓取候选 URL（白皮书/docs 页面优先）
        cur.execute(
            "SELECT entry_type, entry_url FROM biz.doc_source_entry WHERE asset_id=%s "
            "AND entry_type IN ('whitepaper_page','docs','docs_portal','official_website') "
            "ORDER BY CASE entry_type WHEN 'whitepaper_page' THEN 0 WHEN 'docs' THEN 1 "
            "WHEN 'docs_portal' THEN 2 ELSE 3 END LIMIT 8",
            (aid,),
        )
        out["crawl_candidates"] = [{"entry_type": r[0], "entry_url": r[1]} for r in cur.fetchall()]

        cur.execute(
            "SELECT entry_type, entry_url, is_primary, content_topics, classify_confidence, source_code "
            "FROM biz.doc_source_entry WHERE asset_id=%s "
            "AND entry_type IN ('official_website','docs','docs_portal','whitepaper_page','github','medium') "
            "ORDER BY is_primary DESC, entry_id LIMIT 30",
            (aid,),
        )
        cols = [d.name for d in cur.description]
        out["doc_entries"] = [dict(zip(cols, r)) for r in cur.fetchall()]

        # 已下载文档资产
        cur.execute(
            "SELECT doc_id, doc_type, source_url, file_name, mime_type, parse_status, sync_status, "
            "       language, drive_file_url, ima_doc_url "
            "FROM biz.doc_asset WHERE asset_id=%s ORDER BY doc_id LIMIT 30",
            (aid,),
        )
        cols = [d.name for d in cur.description]
        out["doc_assets"] = [dict(zip(cols, r)) for r in cur.fetchall()]

        # 白皮书结构化摘要
        cur.execute(
            "SELECT one_liner, summary_short, summary_long, problem_statement, solution, "
            "       core_mechanism, token_utility, tokenomics_notes, team_info, investors, "
            "       funding_info, roadmap, risks, confidence, source_pages "
            "FROM biz.doc_whitepaper_summary WHERE asset_id=%s LIMIT 1",
            (aid,),
        )
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            out["whitepaper"] = dict(zip(cols, row))

        # GitHub 仓库 + 活跃度（经 asset_github_repo 关联）
        cur.execute(
            "SELECT r.owner_login, r.repo_name, r.stars_count, r.forks_count, r.open_issues_count, "
            "       r.language, r.total_commits_52w, r.contributor_count_52w, r.pushed_at, r.fetched_at "
            "FROM biz.asset_github_repo g "
            "JOIN biz.github_repo_activity r ON r.owner_login=g.owner_login AND r.repo_name=g.repo_name "
            "WHERE g.asset_id=%s AND r.total_commits_52w IS NOT NULL "
            "ORDER BY r.total_commits_52w DESC LIMIT 5",
            (aid,),
        )
        cols = [d.name for d in cur.description]
        out["github"] = [dict(zip(cols, r)) for r in cur.fetchall()]

        # 代币经济学
        cur.execute(
            "SELECT total_supply, max_supply, circulating_supply, buy_tax_pct, sell_tax_pct, "
            "       contract_renounced, lp_locked, allocation_json, emission_schedule, utility_info, "
            "       extract_status, confidence "
            "FROM biz.asset_tokenomics WHERE asset_id=%s LIMIT 1",
            (aid,),
        )
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            out["tokenomics"] = dict(zip(cols, row))

        # 解锁事件（未来 90 天 + 近期）
        today = datetime.now(timezone.utc).date()
        horizon = today + timedelta(days=90)
        cur.execute(
            "SELECT unlock_date, unlock_type, unlock_value_usd, unlock_ratio_circulating, "
            "       beneficiary_type, risk_level, source_code "
            "FROM biz.asset_unlock_event WHERE asset_id=%s "
            "ORDER BY unlock_date LIMIT 30",
            (aid,),
        )
        cols = [d.name for d in cur.description]
        all_unlocks = [dict(zip(cols, r)) for r in cur.fetchall()]
        # 合理性校验：解锁金额相对 FDV 过大，或已全流通币出现大额解锁 → 标疑似脏数据
        # （BTC 等全流通资产本不该有解锁事件；仅供 AI 判断时注意，不作结论）
        cur.execute(
            "SELECT market_cap, circulating_supply, total_supply FROM core.asset WHERE asset_id=%s",
            (aid,),
        )
        _supply_row = cur.fetchone()
        fdv = float(_supply_row[0]) if _supply_row and _supply_row[0] else None
        _circ = float(_supply_row[1]) if _supply_row and _supply_row[1] else None
        _total = float(_supply_row[2]) if _supply_row and _supply_row[2] else None
        fully_circulated = bool(_circ and _total and _circ >= _total * 0.99)
        for u in all_unlocks:
            uv = u.get("unlock_value_usd")
            u["suspicious"] = bool(
                (uv and fdv and float(uv) > float(fdv) * 0.1)
                or (fully_circulated and uv and float(uv) > 0)
            )
        out["unlocks_all"] = all_unlocks
        # 未来 90 天（含今天起）；历史解锁单独保留在 all_unlocks
        out["unlocks_90d"] = [
            u for u in all_unlocks
            if u["unlock_date"] and today <= u["unlock_date"] <= horizon
        ]

        # 融资
        cur.execute(
            "SELECT round, raise_date, amount, lead_investors, other_investors, valuation, source "
            "FROM biz.asset_raises WHERE asset_id=%s ORDER BY raise_date DESC LIMIT 10",
            (aid,),
        )
        cols = [d.name for d in cur.description]
        out["raises"] = [dict(zip(cols, r)) for r in cur.fetchall()]
    return out


# ════════════════════════════════════════════════════════════════════
# P3 市场/交易面（数据采集）
# ════════════════════════════════════════════════════════════════════
def stage_market(conn, asset: dict, binance_symbol: str) -> dict:
    """行情 + 技术简版 + 合约衍生品。"""
    aid = asset["asset_id"]
    out = {"quote": {}, "trend": {}, "klines_1d_tail": None, "derivatives": {}}

    with conn.cursor() as cur:
        # 最新行情（CMC 日线）
        cur.execute(
            "SELECT market_date, price_usd, market_cap, fdv, volume_24h, circulating_supply, "
            "       change_24h, change_7d "
            "FROM biz.asset_market_daily WHERE asset_id=%s ORDER BY market_date DESC LIMIT 1",
            (aid,),
        )
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            out["quote"] = dict(zip(cols, row))

        # 近期趋势（30 日价格序列，算 30d 变化 + 高低区间）
        cur.execute(
            "SELECT market_date, price_usd FROM biz.asset_market_daily "
            "WHERE asset_id=%s AND market_date >= %s ORDER BY market_date",
            (aid, today := datetime.now(timezone.utc).date() - timedelta(days=35)),
        )
        rows = cur.fetchall()
        if rows:
            prices = [float(r[1]) for r in rows]
            d30 = prices[-1] - prices[0] if len(prices) > 1 else None
            out["trend"] = {
                "start": rows[0][0].isoformat(), "end": rows[-1][0].isoformat(),
                "n": len(rows),
                "change_30d_pct": (d30 / prices[0] * 100) if prices[0] else None,
                "min": min(prices), "max": max(prices),
                "pos_in_range": ((prices[-1] - min(prices)) / (max(prices) - min(prices)) * 100)
                if max(prices) > min(prices) else 50.0,
            }

        # 1d K 线尾部（算 MA20/MA50/RSI14 简版）
        cur.execute(
            "SELECT open_time, open_px, high_px, low_px, close_px, base_vol, quote_vol "
            "FROM biz.asset_klines WHERE symbol=%s AND interval='1d' "
            "ORDER BY open_time DESC LIMIT 60",
            (binance_symbol,),
        )
        rows = list(reversed(cur.fetchall()))
        if len(rows) >= 20:
            closes = [float(r[4]) for r in rows]
            highs = [float(r[2]) for r in rows]
            lows = [float(r[3]) for r in rows]
            last = closes[-1]
            ma20 = sum(closes[-20:]) / 20
            ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
            # RSI14（Wilder 简化）
            gains, losses = [], []
            for i in range(1, len(closes)):
                d = closes[i] - closes[i - 1]
                gains.append(max(d, 0)); losses.append(max(-d, 0))
            ag = sum(gains[-14:]) / 14
            al = sum(losses[-14:]) / 14
            rsi = 100 - 100 / (1 + ag / al) if al > 0 else (100 if ag > 0 else 50)
            out["klines_1d_tail"] = {
                "n": len(closes),
                "last_close": last, "last_time": rows[-1][0].isoformat(),
                "ma20": ma20, "ma50": ma50,
                "rsi14": round(rsi, 1),
                "above_ma20": last > ma20, "above_ma50": ma50 is not None and last > ma50,
                "high_52w": max(highs), "low_52w": min(lows),
            }

        # 合约衍生品快照（Coinglass）
        cur.execute(
            "SELECT funding_rate_pct, total_oi_usd, oi_change_24h_pct, cvd_24h_usd, cvd_ratio_24h, "
            "       funding_rate_7d_avg, funding_rate_30d_avg, available_exchanges, fetched_at "
            "FROM biz.asset_derivatives WHERE asset_id=%s ORDER BY fetched_at DESC LIMIT 1",
            (aid,),
        )
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            out["derivatives"] = dict(zip(cols, row))

        # OI 跨所聚合（coinglass_derivatives_snapshot 每所一行，取最新 ts 求和）——
        # asset_derivatives.total_oi_usd 只有 Binance 单所（~7.7B），跨所真实 OI 需聚合
        cur.execute(
            "SELECT ts, SUM(oi_usd) FROM biz.coinglass_derivatives_snapshot "
            "WHERE symbol=%s AND oi_usd IS NOT NULL "
            "AND ts = (SELECT MAX(ts) FROM biz.coinglass_derivatives_snapshot WHERE symbol=%s) "
            "GROUP BY ts",
            (binance_symbol.replace("USDT", ""), binance_symbol.replace("USDT", "")),
        )
        row = cur.fetchone()
        if row and row[1]:
            out["oi_cross_exchange"] = {"ts": row[0], "oi_usd": float(row[1])}

        # 资金费率历史（近 24h 均值）
        cur.execute(
            "SELECT AVG(rate) FROM biz.funding_rate_hist "
            "WHERE symbol=%s AND funding_time >= %s",
            (binance_symbol, datetime.now(timezone.utc) - timedelta(hours=24)),
        )
        row = cur.fetchone()
        out["funding_24h_avg"] = float(row[0]) if row and row[0] is not None else None

        # 多空比最新
        cur.execute(
            "SELECT ts, global_ratio, top_position_ratio, period FROM biz.long_short_ratio "
            "WHERE symbol=%s ORDER BY ts DESC LIMIT 1",
            (binance_symbol,),
        )
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            out["lsr"] = dict(zip(cols, row))

        # 爆仓快照最新（24h）
        cur.execute(
            "SELECT ts, liq_usd_24h, long_liq_usd_24h, short_liq_usd_24h "
            "FROM biz.liquidation_snapshot WHERE symbol=%s ORDER BY ts DESC LIMIT 1",
            (binance_symbol,),
        )
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            out["liq"] = dict(zip(cols, row))
    return out


# ════════════════════════════════════════════════════════════════════
# P4 资金/宏观环境（数据采集）
# ════════════════════════════════════════════════════════════════════
def stage_flow(conn, symbol_base: str) -> dict:
    """ETF 资金流 + 恐贪 + 稳定币 + 宏观 + 大盘快照（市场环境上下文）。"""
    out = {}
    with conn.cursor() as cur:
        # ETF 资金流（近 7 日，双源按日期去重：farside 优先，回退 cryptoetf）
        cur.execute(
            "SELECT flow_date, net_flow_usd_m, source_code FROM biz.etf_flow_daily "
            "WHERE symbol ILIKE %s ORDER BY flow_date DESC, "
            "CASE source_code WHEN 'farside' THEN 0 WHEN 'cryptoetf' THEN 1 ELSE 2 END",
            (symbol_base,),
        )
        etf_rows = cur.fetchall()
        etf_by_date: dict[str, dict] = {}
        for fd, nf, sc in etf_rows:
            key = fd.isoformat()
            if key not in etf_by_date:  # 首个出现的 source 优先（farside 已排最前）
                etf_by_date[key] = {"flow_date": fd, "net_flow_usd_m": nf, "source_code": sc}
        out["etf_7d"] = list(etf_by_date.values())[:7]

        # 恐贪指数
        cur.execute(
            "SELECT metric_date, value, value_classification FROM biz.fear_greed_daily "
            "ORDER BY metric_date DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            out["fear_greed"] = dict(zip([d.name for d in cur.description], row))

        # 稳定币供应 + 7d 变化
        cur.execute(
            "SELECT metric_date, total_supply_usd FROM biz.stablecoin_supply_daily "
            "ORDER BY metric_date DESC LIMIT 8"
        )
        rows = cur.fetchall()
        if rows:
            out["stablecoin"] = {"latest": float(rows[0][1]) if rows[0][1] else None}
            if len(rows) >= 2 and rows[0][1] and rows[-1][1]:
                out["stablecoin"]["7d_change_pct"] = (
                    (float(rows[0][1]) - float(rows[-1][1])) / float(rows[-1][1]) * 100
                )

        # 大盘快照（最新）
        cur.execute(
            "SELECT snapshot_date, btc_price, total_market_cap_usd, btc_dominance_pct, "
            "       total_volume_24h_usd, fear_greed_value, altcoin_season_score, "
            "       stablecoin_total_supply_usd, btc_cycle_phase, btc_cycle_label, overall_score "
            "FROM biz.market_snapshot_daily ORDER BY snapshot_date DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            out["market_snapshot"] = dict(zip([d.name for d in cur.description], row))

        # 宏观（DXY/纳指/黄金 近 7d 变化）
        cur.execute(
            "SELECT metric_date, dxy, us10y, nasdaq, gold FROM biz.macro_market_daily "
            "ORDER BY metric_date DESC LIMIT 8"
        )
        rows = cur.fetchall()
        if rows:
            cols = [d.name for d in cur.description]
            out["macro"] = {"latest": dict(zip(cols, rows[0]))}
            if len(rows) >= 2:
                chg = {}
                for k in ("dxy", "nasdaq", "gold", "us10y"):
                    a, b = rows[0], rows[-1]
                    av = a[cols.index(k)]; bv = b[cols.index(k)]
                    if av and bv and float(bv) != 0:
                        chg[k] = (float(av) - float(bv)) / float(bv) * 100
                out["macro"]["7d_change_pct"] = chg
    return out


# ════════════════════════════════════════════════════════════════════
# P5 催化剂/情绪（数据采集）
# ════════════════════════════════════════════════════════════════════
def stage_catalyst(conn, asset: dict) -> dict:
    """近期催化剂新闻 + 社交热度。"""
    aid = asset["asset_id"]
    out = {"catalysts": [], "social_heat": None}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, ai_summary, published_at, event_category, ai_event_type, ai_sentiment, "
            "       source_url, title_cn "
            "FROM biz.asset_catalyst WHERE asset_id=%s "
            "AND published_at >= %s ORDER BY published_at DESC LIMIT 15",
            (aid, datetime.now(timezone.utc) - timedelta(days=7)),
        )
        cols = [d.name for d in cur.description]
        out["catalysts"] = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute(
            "SELECT score, confidence, sentiment_json, fetched_at "
            "FROM biz.asset_social_heat WHERE asset_id=%s ORDER BY fetched_at DESC LIMIT 1",
            (aid,),
        )
        row = cur.fetchone()
        if row:
            cols = [d.name for d in cur.description]
            out["social_heat"] = dict(zip(cols, row))
    return out


# ════════════════════════════════════════════════════════════════════
# P2b 外部抓取补全（Crawl4AI 本地渲染）
# ════════════════════════════════════════════════════════════════════
def stage_external_web(docs: dict, identity: dict, max_crawl: int = 3) -> dict:
    """当 DB 缺白皮书摘要时，用 Crawl4AI 抓取官方文档页补内容摘要。

    仅在 DB 无 whitepaper 摘要时才触发（遵循「体检先行、不覆盖已有数据」）。
    抓取候选来自 doc_source_entry 的 whitepaper/docs 入口 URL。
    返回 {"fetched": [{url, snippet, chars}], "note": str}，失败不抛异常。
    """
    out = {"fetched": [], "note": ""}
    # 已有 DB 摘要则跳过外部抓取
    if docs.get("whitepaper") and docs["whitepaper"].get("summary_short"):
        out["note"] = "DB 已有白皮书摘要，未触发外部抓取"
        return out

    candidates = docs.get("crawl_candidates") or []
    if not candidates:
        out["note"] = "无可用抓取候选 URL"
        return out

    try:
        from crypto_research.clients.crawl4ai_client import get_shared_client
        from crypto_research.config import get_settings

        client = get_shared_client(get_settings(require_database=False))
    except Exception as e:
        out["note"] = f"Crawl4AI 不可用: {str(e)[:80]}"
        return out
    if client is None:
        out["note"] = "Crawl4AI 未启用（依赖缺失）"
        return out

    fetched = 0
    for cand in candidates:
        if fetched >= max_crawl:
            break
        url = (cand.get("entry_url") or "").strip()
        if not url:
            continue
        md = client.crawl(url, max_chars=6000, use_cache=True)
        if not md:
            continue
        # 取正文前 800 字作为摘要片段（去空行）
        snippet = "\n".join(line for line in md.splitlines() if line.strip())[:800]
        out["fetched"].append({
            "entry_type": cand.get("entry_type"), "url": url,
            "snippet": snippet, "chars": len(md),
        })
        fetched += 1

    if fetched == 0:
        out["note"] = "外部抓取未返回内容（页面可能需登录/反爬）"
    else:
        out["note"] = f"外部抓取 {fetched} 个 URL 成功（Crawl4AI 本地渲染）"
    return out


# ════════════════════════════════════════════════════════════════════
# P2b2 文档深度爬取（Crawl4AI BFS：docs 站子页面全量抓取）
# ════════════════════════════════════════════════════════════════════
# markdown 链接 [text](url)
_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^()\s]+?)\s*\)")
# 文档相关路径关键词（正过滤）
_DOC_POSITIVE = (
    "doc", "whitepaper", "white-paper", "tokenomic", "token", "protocol",
    "guide", "manual", "faq", "overview", "mechanism", "launch", "trade",
    "fee", "price", "pricing", "contract", "integration", "developer",
    "api", "audit", "security", "risk", "graduation", "revenue",
    "governance", "staking", "roadmap", "utility", "vision", "network",
    "assets", "support",
)
# 噪音路径（负过滤：法律/登录/资源/外链）
_DOC_EXCLUDE = (
    "/terms", "/privacy", "/legal", "/law-enforcement", "/takedown",
    "login", "sign-in", "signin", "/account", "/careers",
    "/cdn/", "/assets/", "/static/", "/img/", "/css/", "/js/",
    "mailto:", "tel:", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".gif",
    "twitter.com", "x.com/", "github.com", "t.me/", "discord", "youtube.com",
    "linkedin.com", "medium.com", "dexscreener.com", "etherscan.io",
    "blockscout.com", "defillama.com", "coingecko.com", "coinmarketcap.com",
)


def _norm_url(url: str, base: str) -> str | None:
    """规范化 URL：相对路径按 base 拼接；去锚点/尾斜杠/UTM 追踪参数。"""
    url = (url or "").strip().strip("\"')\n")
    if not url or url.startswith("#"):
        return None  # 同页锚点
    try:
        if url.startswith(("http://", "https://")):
            p = urlparse(url)
        elif url.startswith("/"):
            bp = urlparse(base)
            p = urlparse(f"{bp.scheme}://{bp.netloc}{url}")
        else:
            return None  # 协议相对/裸域名一律跳过，避免噪音
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    qs = [(k, v) for k, v in parse_qsl(p.query or "")
          if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc.lower(), (p.path or "/").rstrip("/") or "/",
                       urlencode(qs), ""))


def _same_site(url: str, seed_host: str) -> bool:
    """url 与种子站点同域（含子域与 www 等价）。"""
    host = (urlparse(url).netloc or "").lower()
    seed = seed_host.lower()
    if host == seed:
        return True
    if host.startswith("www.") and host[4:] == seed:
        return True
    if seed.startswith("www.") and seed[4:] == host:
        return True
    return host.endswith("." + seed) or seed.endswith("." + host)


def _is_doc_url(url: str) -> bool:
    """文档 URL 过滤：路径含文档关键词且不含噪音路径。"""
    p = urlparse(url)
    path = (p.path or "").lower()
    full = url.lower()
    if any(k in full for k in _DOC_EXCLUDE):
        return False
    if path in ("", "/"):
        return True  # 站点根页（docs 站首页）
    return any(k in path for k in _DOC_POSITIVE)


def _page_title(md: str, url: str) -> str:
    """取 markdown 首个 # 标题；无则用 URL 末段。"""
    for line in (md or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()[:80]
    seg = [s for s in (urlparse(url).path or "").split("/") if s]
    return seg[-1] if seg else (urlparse(url).netloc or url)


def stage_docs_deep(docs: dict, max_pages: int = 30) -> dict:
    """文档深度爬取：从 doc_source_entry 文档入口出发，BFS 抓取 docs 站全部相关子页。

    只收集文档原文（事实），不做任何投研判断。
    - 种子：whitepaper_page / docs / docs_portal（官网仅当 URL 含 docs）
    - 对每个种子站 BFS（深度 ≤2，每站上限 per_site，全局上限 max_pages）
      ：抓页 → 提取同站文档链接 → 继续抓；SPA 站点首页渲染后即含全文。
    返回 {"sites": [{host, seed, pages: [{url, title, chars, content}]}], "note": str}。
    """
    out = {"sites": [], "note": ""}
    candidates = docs.get("crawl_candidates") or []
    if not candidates:
        out["note"] = "无文档入口 URL（doc_source_entry 缺 docs 链接）"
        return out

    # 过滤出文档站种子（去重）
    uniq_seeds, seen = [], set()
    for c in candidates:
        et = (c.get("entry_type") or "").lower()
        url = (c.get("entry_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        is_doc_et = et in ("whitepaper_page", "docs", "docs_portal")
        if not is_doc_et and not (
            et == "official_website"
            and ("docs" in urlparse(url).netloc or "docs" in (urlparse(url).path or ""))
        ):
            continue
        n = _norm_url(url, url)
        if n and n not in seen:
            seen.add(n)
            uniq_seeds.append(n)
    if not uniq_seeds:
        out["note"] = "无文档站种子 URL（doc_source_entry 仅有官网，无 docs 入口）"
        return out

    try:
        from crypto_research.clients.crawl4ai_client import get_shared_client
        from crypto_research.config import get_settings

        client = get_shared_client(get_settings(require_database=False))
    except Exception as e:
        out["note"] = f"Crawl4AI 不可用: {str(e)[:80]}"
        return out
    if client is None:
        out["note"] = "Crawl4AI 未启用（依赖缺失）"
        return out

    per_site = max(8, max_pages // 2)
    max_depth = 2
    done = 0
    for seed in uniq_seeds:
        if done >= max_pages:
            break
        host = urlparse(seed).netloc.lower()
        site = {"host": host, "seed": seed, "pages": []}
        queue = [(seed, 0)]
        visited = {seed}
        while queue and done < max_pages and len(site["pages"]) < per_site:
            url, depth = queue.pop(0)
            md = client.crawl(url, max_chars=20000, use_cache=True)
            if not md:
                continue
            site["pages"].append({"url": url, "title": _page_title(md, url),
                                  "chars": len(md), "content": md})
            done += 1
            if depth >= max_depth:
                continue
            for raw in _LINK_RE.findall(md):
                norm = _norm_url(raw, seed)
                if not norm or norm in visited or not _same_site(norm, host) or not _is_doc_url(norm):
                    continue
                visited.add(norm)
                queue.append((norm, depth + 1))
        if site["pages"]:
            out["sites"].append(site)

    if not out["sites"]:
        out["note"] = "文档深度爬取未返回任何页面（可能需登录/反爬）"
    else:
        total = sum(len(s["pages"]) for s in out["sites"])
        out["note"] = f"文档深度爬取 {len(out['sites'])} 站 / {total} 页（Crawl4AI 本地渲染）"
    return out


def render_docs_deep_md(docs_deep: dict) -> str:
    """把文档深度爬取结果渲染成 markdown（仅原文事实，供 AI 阅读）。"""
    L = ["# 文档深度爬取（docs deep crawl）", ""]
    L.append(f"> 生成时间：{datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M}"
             f" ｜ 来源：Crawl4AI 本地渲染 ｜ 仅文档原文，供投研判断")
    L.append(f"> {docs_deep.get('note', '')}")
    L.append("")
    for site in docs_deep.get("sites") or []:
        L.append(f"## 站点：{site['host']}")
        L.append("")
        L.append(f"> 种子入口：{site['seed']} ｜ 抓取 {len(site['pages'])} 页")
        L.append("")
        for p in site["pages"]:
            L.append(f"### {p['title']}")
            L.append("")
            L.append(f"- 来源：{p['url']}（{p['chars']} 字）")
            L.append("")
            L.append(p["content"])
            L.append("")
            L.append("---")
            L.append("")
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════════
# P2/P5 外部搜索补全（firecrawl search → Crawl4AI 抓取详情）
# ════════════════════════════════════════════════════════════════════
# 搜索维度定义：(key, 中文标签, 查询模板)
SEARCH_DIMENSIONS = [
    ("team_funding", "团队与融资", "{name} {symbol} team funding investors round"),
    ("audit_security", "审计与安全", "{name} {symbol} audit security report"),
    ("latest_news", "最新动态", "{name} {symbol} news announcement"),
]


def _parse_funding(results: list[dict]) -> list[dict]:
    """从团队/融资搜索结果尽力解析结构化融资（正则提取金额/轮次，解析不到返回空）。

    返回 [{round, amount(USD), source='web_search'}]，供 persist_raises 落库。
    防误判：必须含融资语义关键词 + 金额 >= $100K（避免把币价/ROI 等小额误当融资）。
    """
    raises: list[dict] = []
    for r in results:
        text = " ".join(str(r.get(k) or "") for k in ("title", "desc", "description"))
        # 1) 融资语义闸门：无融资关键词则跳过（排除币价/ROI/普通数字）
        if not re.search(r"raised|funding|fund|round|raise|seed|series\s*[abcd]|investor|valuation",
                         text, re.I):
            continue
        # 2) 金额（支持 $ / €）
        m = re.search(r"[€$]\s*([\d.]+)\s*([Mm]illion|M|K|k)?", text)
        if not m:
            continue
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit.startswith("m"):
            amount = val * 1_000_000
        elif unit.startswith("k"):
            amount = val * 1_000
        else:
            amount = val
        # 3) 金额阈值：融资一般 >= $100K，过滤价格/手续费等小额
        if amount < 100_000:
            continue
        rm = re.search(r"\b(pre-seed|seed|series\s*[abcd]|private\s*(?:sale|round)|ico|ido)\b", text, re.I)
        raises.append({
            "round": (rm.group(1).lower().replace(" ", "_") if rm else "funding_round"),
            "amount": amount,
            "source": "web_search",
        })
    # 简单去重（round+amount）
    seen = set()
    uniq = []
    for r in raises:
        key = (r["round"], round(r["amount"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


def stage_external_search(identity: dict, per_dim: int = 2, crawl_details: int = 0) -> dict:
    """firecrawl 搜索 3 个维度补外部情报（P2 团队/融资/审计 + P5 动态）。

    返回 {"dimensions": [{key,label,results:[{title,url,desc}]}], "note": str}。
    搜索失败（无 key/配额/网络）不抛异常，note 说明原因。
    crawl_details: 对每个结果前 N 个 URL 用 Crawl4AI 抓正文片段（0=不抓，仅搜索摘要）。
    """
    out = {"dimensions": [], "note": ""}
    try:
        from crypto_research.clients.firecrawl_search_client import get_shared_client
        from crypto_research.config import get_settings

        fc = get_shared_client(get_settings(require_database=False))
    except Exception as e:
        out["note"] = f"firecrawl 不可用: {str(e)[:80]}"
        return out
    if fc is None or not fc.is_available():
        out["note"] = "firecrawl 未配置 API Key"
        return out

    name = identity.get("name") or identity.get("symbol") or ""
    symbol = identity.get("symbol") or ""
    if not name:
        out["note"] = "无资产名可用于搜索"
        return out

    # Crawl4AI 客户端（可选，仅 crawl_details>0 时用）
    crawl_client = None
    if crawl_details > 0:
        try:
            from crypto_research.clients.crawl4ai_client import get_shared_client as get_c4
            crawl_client = get_c4(get_settings(require_database=False))
        except Exception:
            crawl_client = None

    for key, label, tpl in SEARCH_DIMENSIONS:
        query = tpl.format(name=name, symbol=symbol)
        try:
            results = fc.search(query, limit=per_dim + 1)
        except Exception as e:
            out["dimensions"].append({"key": key, "label": label, "results": [],
                                      "error": str(e)[:80]})
            continue
        dim_results = []
        for i, r in enumerate(results[:per_dim]):
            item = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
                "snippet": "",
            }
            # 抓正文片段（可选）
            if crawl_client is not None and i < crawl_details and item["url"]:
                md = crawl_client.crawl(item["url"], max_chars=3000, use_cache=True)
                if md:
                    item["snippet"] = "\n".join(l for l in md.splitlines() if l.strip())[:600]
            dim_results.append(item)
        out["dimensions"].append({"key": key, "label": label, "results": dim_results})

    n_total = sum(len(d["results"]) for d in out["dimensions"])
    out["note"] = f"外部搜索 {len(out['dimensions'])} 维度共 {n_total} 条结果（firecrawl）"
    return out


def render_external_search_md(ext: dict) -> str:
    """外部搜索补全 → Markdown 块（仅事实：标题/来源/描述）。"""
    if not ext or not ext.get("dimensions"):
        return ""
    lines = ["### 外部情报（Web 搜索补全）", ""]
    for d in ext["dimensions"]:
        results = d.get("results") or []
        if not results:
            continue
        lines.append(f"**{d['label']}**：")
        for r in results:
            title = (r.get("title") or "—")[:80]
            lines.append(f"- {title}")
            if r.get("url"):
                lines.append(f"  - 来源：{r['url']}")
            if r.get("description"):
                lines.append(f"  - {r['description'][:150]}")
            if r.get("snippet"):
                lines.append(f"  - 摘要：{r['snippet'][:200]}")
        lines.append("")
    if ext.get("note"):
        lines.append(f"> {ext['note']}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# P1b 内容质量快照（聚焦单资产，内容级脏数据）
# ════════════════════════════════════════════════════════════════════
def stage_data_quality(conn, asset_id: int) -> dict:
    """对目标资产做内容级质量检查（unlock 历史/异常、描述污染、价格异常）。

    仅列出"可疑数据"，不下判断——是否影响投研结论由 AI 决定。
    """
    issues = []
    with conn.cursor() as cur:
        # 1. 未来异常解锁（金额 > 市值×5）
        cur.execute(
            """
            SELECT unlock_date, unlock_type, unlock_value_usd, source_code
            FROM biz.asset_unlock_event u
            WHERE u.asset_id = %s AND u.unlock_date >= CURRENT_DATE
              AND u.unlock_value_usd > 0
              AND u.unlock_value_usd > 5 * (
                  SELECT COALESCE(MAX(m.market_cap),0) FROM biz.asset_market_daily m
                  WHERE m.asset_id = u.asset_id)
            """,
            (asset_id,),
        )
        for r in cur.fetchall():
            issues.append(f"异常解锁 {r[0]} {r[1]} 金额{float(r[2]):,.0f} > 市值×5 [{r[3]}]")

        # 2. 历史解锁残留
        cur.execute(
            "SELECT COUNT(*) FROM biz.asset_unlock_event "
            "WHERE asset_id=%s AND unlock_date < CURRENT_DATE AND unlock_value_usd > 0",
            (asset_id,),
        )
        n_hist = cur.fetchone()[0]
        if n_hist > 0:
            issues.append(f"历史解锁残留 {n_hist} 条（unlock_date<今天）")

        # 3. 描述污染（描述不含自己资产名）
        cur.execute(
            """
            SELECT cb.coin_symbol, LEFT(cb.description_short, 60)
            FROM biz.coin_basic cb
            JOIN src_cmc.cmc_asset_info ci ON ci.cmc_id = cb.cmc_id
            WHERE cb.asset_id = %s AND cb.description_short IS NOT NULL
              AND ci.description IS NOT NULL
              AND ci.description LIKE CONCAT('%%', cb.coin_symbol, '%%')
              AND cb.description_short NOT LIKE CONCAT('%%', cb.coin_name, '%%')
            """,
            (asset_id,),
        )
        for r in cur.fetchall():
            issues.append(f"描述污染: 当前「{r[1]}…」与 {r[0]} 不符")

        # 4. 价格异常
        cur.execute(
            "SELECT COUNT(*) FROM biz.asset_market_daily "
            "WHERE asset_id=%s AND (price_usd <= 0 OR ABS(change_24h) > 500)",
            (asset_id,),
        )
        n_price = cur.fetchone()[0]
        if n_price > 0:
            issues.append(f"行情价格异常 {n_price} 条（price<=0 或 |24h|>500%）")

    return {"asset_id": asset_id, "issues": issues,
            "has_issues": len(issues) > 0}


def render_quality_md(dq: dict) -> str:
    """内容质量 → Markdown 块（仅事实清单）。"""
    if dq is None:
        return ""
    lines = ["### 内容质量检查（可疑数据清单）", ""]
    if not dq["issues"]:
        lines.append("- ✅ 未发现可疑数据（解锁/描述/价格）")
    else:
        for i in dq["issues"]:
            lines.append(f"- ⚠️ {i}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# 数据清单渲染（纯事实，无判断——供 AI 投研判断使用）
# ════════════════════════════════════════════════════════════════════
def _sector_label(sec: str) -> str:
    labels = {"l1": "L1公链", "l2": "L2二层", "defi": "DeFi", "meme": "Meme",
              "ai": "AI", "rwa": "RWA", "stablecoin": "稳定币", "cex_token": "平台币",
              "derivatives": "衍生品", "depin": "DePIN", "gamefi": "GameFi",
              "launchpad": "Launchpad", "infra": "基础设施", "other": "其他"}
    return labels.get(sec, sec)


def render_data_brief(ctx: dict) -> str:
    """纯数据清单 Markdown：只列事实数据与来源，不含任何判断/结论。

    供 AI 读取后自行完成投研分析（多空、评分、解读、结论）。
    """
    iden = ctx["identity"]
    health = ctx["health"]
    docs = ctx["docs"]
    mkt = ctx["market"]
    flow = ctx["flow"]
    cat = ctx["catalyst"]
    gen_time = ctx["gen_time"]

    L = [f"# {iden['name']}（{iden['symbol']}）投研数据清单", ""]
    L.append(f"> 生成时间：{gen_time}（Asia/Shanghai）｜数据采集完成，以下仅为事实数据，供投研判断使用")
    L.append("")
    L.append("## 〇、数据可信度（体检矩阵）")
    L.append("")
    L.append(render_health_md(health))
    L.append("")

    # 内容质量检查（--dq 时）
    dq = ctx.get("dq")
    if dq is not None:
        L.append(render_quality_md(dq))
        L.append("")

    # ── 标的画像 ──
    L.append("## 一、标的画像")
    L.append("")
    L.append(f"- 全名：{iden['name']} ｜ 符号：{iden['symbol']} ｜ 市值排名：#{iden.get('market_cap_rank') or '—'}")
    L.append(f"- 赛道：{_sector_label(iden.get('primary_sector') or 'unknown')}"
             f" ｜ 主链：{iden.get('main_chain') or '—'}")
    if iden.get("categories"):
        L.append(f"- CMC 分类：{', '.join(iden['categories'][:6])}")
    if iden.get("official_website"):
        L.append(f"- 官网：{iden['official_website']}")
    if iden.get("launch_date"):
        L.append(f"- 上线日期：{iden['launch_date']}")
    if iden.get("ath_usd"):
        L.append(f"- 历史最高：{_fmt_usd(iden.get('ath_usd'))}")
    L.append("")

    # ── 基本面数据 ──
    L.append("## 二、基本面数据")
    L.append("")
    if docs.get("whitepaper"):
        wp = docs["whitepaper"]
        if wp.get("one_liner"):
            L.append(f"- 一句话定位：{wp['one_liner']}")
        if wp.get("summary_short"):
            L.append(f"- 白皮书摘要：{wp['summary_short']}")
        for label, key in (("问题", "problem_statement"), ("方案", "solution"),
                           ("核心机制", "core_mechanism"), ("代币用途", "token_utility"),
                           ("团队", "team_info"), ("投资人", "investors"),
                           ("融资", "funding_info"), ("路线图", "roadmap"), ("风险", "risks")):
            if wp.get(key):
                L.append(f"- {label}：{wp[key][:300]}")
        L.append("")
    else:
        L.append("> ⚠️ 无白皮书结构化摘要（数据缺口）。")
        L.append("")

    # 外部抓取补全（Crawl4AI）——仅事实
    ext = ctx.get("external_web") or {}
    if ext.get("fetched"):
        L.append("**外部抓取资料**（Crawl4AI）：")
        L.append("")
        for f in ext["fetched"]:
            L.append(f"- [{f['entry_type']}] {f['url']}（{f['chars']} 字）")
            L.append(f"  - {f['snippet']}")
        L.append(f"> {ext.get('note', '')}")
        L.append("")

    # 文档深度爬取（Crawl4AI BFS）——仅事实
    dde = ctx.get("docs_deep") or {}
    if dde.get("sites"):
        L.append("**文档深度爬取**（docs deep crawl）：")
        L.append("")
        for s in dde["sites"]:
            L.append(f"- [{s['host']}]({s['seed']})：{len(s['pages'])} 页")
            for p in s["pages"][:10]:
                L.append(f"  - {p['title']} —— {p['url']}（{p['chars']} 字）")
            if len(s["pages"]) > 10:
                L.append(f"  - … 另有 {len(s['pages']) - 10} 页（见 docs_deep.md 全文）")
        L.append(f"> {dde.get('note', '')}")
        L.append("")

    # 外部搜索补全（firecrawl）——仅事实
    ext_search = ctx.get("external_search")
    if ext_search:
        search_md = render_external_search_md(ext_search)
        if search_md:
            L.append(search_md)
            L.append("")

    ec = docs.get("entry_counts") or {}
    if ec:
        L.append("**文档入口**（doc_source_entry 按类型）：")
        L.append("")
        for kk, vv in sorted(ec.items(), key=lambda x: -x[1]):
            L.append(f"- {kk}：{vv} 条")
        L.append("")

    if docs.get("doc_assets"):
        L.append("**已下载文档**（doc_asset）：")
        L.append("")
        for d in docs["doc_assets"][:10]:
            status = d.get("parse_status") or "?"
            L.append(f"- [{d.get('doc_type')}] {d.get('file_name') or d.get('source_url')}（{status}）")
        L.append("")

    if docs.get("github"):
        L.append("**开发活跃度**（GitHub）：")
        L.append("")
        L.append("| 仓库 | ⭐ | 近52周提交 | 贡献者 | 语言 |")
        L.append("|---|---|---|---|---|")
        for g in docs["github"][:5]:
            L.append(f"| {g['owner_login']}/{g['repo_name']} | {_fmt_int(g.get('stars_count'))} "
                     f"| {_fmt_int(g.get('total_commits_52w'))} | {_fmt_int(g.get('contributor_count_52w'))} "
                     f"| {g.get('language') or '—'} |")
        L.append("")

    tok = docs.get("tokenomics")
    if tok:
        L.append("**代币经济学**：")
        L.append("")
        L.append(f"- 总供应：{_fmt_int(tok.get('total_supply'))} ｜ 最大供应：{_fmt_int(tok.get('max_supply'))} "
                 f"｜ 流通：{_fmt_int(tok.get('circulating_supply'))}")
        if tok.get("allocation_json"):
            alloc = tok["allocation_json"]
            if isinstance(alloc, dict):
                parts = ", ".join(f"{kk}: {_fmt_pct(vv, 1) if isinstance(vv, (int, float)) else vv}"
                                  for kk, vv in list(alloc.items())[:6])
                L.append(f"- 分配：{parts}")
        if tok.get("buy_tax_pct") is not None or tok.get("sell_tax_pct") is not None:
            L.append(f"- 税：买 {tok.get('buy_tax_pct')}% / 卖 {tok.get('sell_tax_pct')}%")
        L.append("")

    unlocks = docs.get("unlocks_90d") or []
    if unlocks:
        L.append("**未来90天解锁事件**：")
        L.append("")
        L.append("| 日期 | 类型 | 金额 | 流通占比 | 受益人 | 风险 |")
        L.append("|---|---|---|---|---|---|")
        for u in unlocks[:10]:
            note = " ⚠️金额异常" if u.get("suspicious") else ""
            L.append(f"| {u.get('unlock_date')} | {u.get('unlock_type')} "
                     f"| {_fmt_usd(u.get('unlock_value_usd'))} "
                     f"| {_fmt_pct(u.get('unlock_ratio_circulating') or 0, 2, signed=False)} "
                     f"| {u.get('beneficiary_type') or '—'} | {u.get('risk_level') or '—'}{note} |")
        L.append("")

    raises = docs.get("raises") or []
    if raises:
        L.append("**融资记录**（金额为原始数值，单位需人工核验）：")
        L.append("")
        for r in raises[:5]:
            amt = r.get("amount")
            amt_s = f"{amt:,.0f}" if isinstance(amt, (int, float)) else (amt or "—")
            L.append(f"- {r.get('raise_date')} {r.get('round')}：{amt_s}"
                     f" ｜ 领投：{r.get('lead_investors') or '—'}")
        L.append("")

    # ── 市场与衍生品数据 ──
    L.append("## 三、市场与衍生品数据")
    L.append("")
    q = mkt.get("quote") or {}
    if q:
        L.append(f"- 现价：**{_fmt_usd(q.get('price_usd'))}**（{q.get('market_date')}）")
        L.append(f"- 市值：{_fmt_usd(q.get('market_cap'))} ｜ FDV：{_fmt_usd(q.get('fdv'))} "
                 f"｜ 24h 成交：{_fmt_usd(q.get('volume_24h'))}")
        L.append(f"- 24h：{_fmt_pct(q.get('change_24h'))} ｜ 7d：{_fmt_pct(q.get('change_7d'))}")
    k = mkt.get("klines_1d_tail")
    if k:
        L.append(f"- 技术面(1d)：MA20={_fmt_usd(k.get('ma20'))}（现价{'上' if k.get('above_ma20') else '下'}方）"
                 f" ｜ MA50={_fmt_usd(k.get('ma50'))}（现价{'上' if k.get('above_ma50') else '下'}方）"
                 f" ｜ RSI14={k.get('rsi14')}")
    d = mkt.get("derivatives") or {}
    if d:
        oi = mkt.get("oi_cross_exchange")
        if oi and oi.get("oi_usd"):
            oi_txt = f"{_fmt_usd(oi['oi_usd'])}（跨所聚合，{oi['ts']}）"
        else:
            oi_txt = f"{_fmt_usd(d.get('total_oi_usd'))}（单所，24h {_fmt_pct(d.get('oi_change_24h_pct'))}）"
        L.append(f"- 合约(OI)：{oi_txt} ｜ CVD 24h：{_fmt_usd(d.get('cvd_24h_usd'))}")
        L.append(f"- 资金费率：{_fmt_pct(d.get('funding_rate_pct'), 4, signed=False)}/8h"
                 f"（7d均 {_fmt_pct(d.get('funding_rate_7d_avg'), 4, signed=False)}）")
    if mkt.get("funding_24h_avg") is not None:
        L.append(f"- 资金费率(24h均)：{mkt['funding_24h_avg']*100:.4f}%")
    lsr = mkt.get("lsr")
    if lsr and lsr.get("global_ratio") is not None:
        L.append(f"- 多空比：{float(lsr['global_ratio']):.2f}（{lsr.get('period')}，{lsr.get('ts')}）")
    liq = mkt.get("liq")
    if liq:
        L.append(f"- 爆仓24h：{_fmt_usd(liq.get('liq_usd_24h'))}"
                 f"（多 {_fmt_usd(liq.get('long_liq_usd_24h'))} / 空 {_fmt_usd(liq.get('short_liq_usd_24h'))}）")
    L.append("")

    # ── 资金与宏观数据 ──
    L.append("## 四、资金与宏观数据")
    L.append("")
    etf = flow.get("etf_7d") or []
    if etf:
        net7 = sum(float(e["net_flow_usd_m"] or 0) for e in etf)
        L.append(f"- **ETF 近7日净流：{net7:+.0f}M USD**")
        L.append("")
        L.append("| 日期 | 净流(M) | 来源 |")
        L.append("|---|---|---|")
        for e in etf:
            L.append(f"| {e['flow_date']} | {float(e['net_flow_usd_m'] or 0):+.1f} | {e.get('source_code') or '—'} |")
        L.append("")
    fg = flow.get("fear_greed")
    if fg:
        L.append(f"- 恐贪指数：**{fg.get('value')}**（{fg.get('value_classification')}，{fg.get('metric_date')}）")
    sc = flow.get("stablecoin")
    if sc:
        L.append(f"- 稳定币总供应：{_fmt_usd(sc.get('latest'))}"
                 f"（7d {_fmt_pct(sc.get('7d_change_pct'), 2)}）")
    macro = flow.get("macro", {}).get("7d_change_pct") or {}
    if macro:
        L.append(f"- 宏观7d：DXY {_fmt_pct(macro.get('dxy'))} ｜ 纳指 {_fmt_pct(macro.get('nasdaq'))} "
                 f"｜ 黄金 {_fmt_pct(macro.get('gold'))}")
    ms = flow.get("market_snapshot")
    if ms:
        L.append(f"- 大盘：总市值 {_fmt_usd(ms.get('total_market_cap_usd'))} ｜ BTC主导 {ms.get('btc_dominance_pct')}%"
                 f" ｜ 周期 {ms.get('btc_cycle_label') or '—'}")
    L.append("")

    # ── 催化剂与情绪数据 ──
    L.append("## 五、催化剂与情绪数据")
    L.append("")
    catalysts = [c for c in (cat.get("catalysts") or []) if c.get("title")]
    if catalysts:
        L.append("**近7日催化剂**（前10条，含来源/AI情感标注）：")
        L.append("")
        for c in catalysts[:10]:
            sent = c.get("ai_sentiment") or ""
            emoji = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(str(sent).lower(), "⚪")
            pub = c.get("published_at")
            pub_s = pub.strftime("%Y-%m-%d %H:%M") if isinstance(pub, datetime) else str(pub)[:16]
            L.append(f"- {emoji} {pub_s} "
                     f"[{c.get('event_category') or c.get('ai_event_type') or '事件'}] "
                     f"{c.get('title')[:120]}")
            if c.get("ai_summary"):
                L.append(f"  - {c['ai_summary'][:200]}")
        L.append("")
    sh = cat.get("social_heat")
    if sh and sh.get("score") is not None:
        L.append(f"- 社交热度评分：{sh['score']}（{sh.get('fetched_at')}）")
        L.append("")

    # ── 声明 ──
    L.append("---")
    L.append("*数据源：biz/core 层各表（详见体检矩阵）｜ 本清单仅含事实数据，投研判断请由 AI 完成。*")
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════════
def main() -> int:
    parser = argparse.ArgumentParser(description="加密投研助手：投研数据采集器（只收集数据，不做判断）")
    parser.add_argument("--symbol", required=True, help="币种符号/名称/asset_id，如 BTC / Bitcoin / 2")
    parser.add_argument("--out", default="", help=f"输出目录（默认 {DEFAULT_OUT}）")
    parser.add_argument("--json-only", action="store_true", help="只输出 JSON 数据包（不生成数据清单）")
    parser.add_argument("--crawl", type=int, default=0,
                        help="DB 缺白皮书摘要时，用 Crawl4AI 抓取官方文档页补内容（0=关闭，N=最多抓 N 个）")
    parser.add_argument("--dq", action="store_true", help="开启内容质量检查（解锁/描述/价格可疑数据）")
    parser.add_argument("--search", type=int, default=0,
                        help="firecrawl 外部搜索补全（P2团队融资/审计 + P5动态）。0=关闭；N=每维度最多条数")
    parser.add_argument("--search-crawl", type=int, default=0,
                        help="外部搜索结果用 Crawl4AI 抓正文的前 N 条（0=只展示搜索摘要）")
    parser.add_argument("--crawl-docs", type=int, default=0,
                        help="文档深度爬取：从 docs 入口 BFS 抓全部相关子页，N=最多抓 N 页（0=关闭）")
    parser.add_argument("--persist", action=argparse.BooleanOptionalAction, default=True,
                        help="边投研边落库：把本次补强数据写回 DB（docs 摘要等）。默认开启，--no-persist 关闭")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        # P0 资产定位
        raw_query = args.symbol
        q = int(raw_query) if raw_query.isdigit() else raw_query
        candidates = resolve_asset(conn, q)
        if not candidates:
            print(f"[research] 未找到资产: {args.symbol}")
            return 1
        asset = candidates[0]
        print(f"[research] 标的: {asset['asset_id']} {asset['canonical_symbol']} {asset['canonical_name']} "
              f"(rank={asset['market_cap_rank']}, sector={asset['sector']})")

        # 确定 Binance symbol + 赛道
        from data_health import _binance_symbol
        binance_symbol = _binance_symbol(conn, asset["asset_id"])
        sector = asset.get("sector") or "unknown"
        print(f"[research] Binance symbol: {binance_symbol} ｜ sector: {sector}")

        # P1 体检
        health = check_data_health(conn, asset["asset_id"], binance_symbol)

        # P1b 内容质量快照（聚焦当前资产；--dq 开启）
        dq = stage_data_quality(conn, asset["asset_id"]) if args.dq else None
        if dq is not None:
            print(f"[research] 内容质量: {len(dq['issues'])} 项可疑数据")

        # P2-P5 数据采集
        identity = stage_identity(conn, asset)
        docs = stage_docs(conn, asset)
        # P2b 外部抓取补全（Crawl4AI）：DB 缺白皮书摘要时触发
        external_web = stage_external_web(docs, identity, max_crawl=args.crawl)
        # P2b2 文档深度爬取（Crawl4AI BFS）：--crawl-docs 开启
        docs_deep = stage_docs_deep(docs, max_pages=args.crawl_docs) if args.crawl_docs > 0 else None
        if docs_deep is not None:
            print(f"[research] 文档深度爬取: {docs_deep.get('note', '')}")

        # 边投研边落库（P1.5 补强结果写回 DB，供因子回测复用）
        if args.persist:
            try:
                from crypto_research.persist import persist_doc_summary
                docs_md = ""
                source_pages: list[str] = []
                if docs_deep and docs_deep.get("sites"):
                    for site in docs_deep["sites"]:
                        source_pages.append(site["seed"])
                        for p in site["pages"]:
                            source_pages.append(p["url"])
                            docs_md += p["content"] + "\n\n"
                if docs_md:
                    _persisted = persist_doc_summary(
                        conn, asset["asset_id"], identity["symbol"], identity["name"],
                        docs_md, source_pages=source_pages)
                    print(f"[persist] 文档摘要: {'已落库 doc_whitepaper_summary' if _persisted else '跳过（已有摘要或 LLM 失败）'}")
                else:
                    print("[persist] 无文档深度爬取内容，跳过摘要落库")
            except Exception as e:
                conn.rollback()  # 落库失败回滚，避免污染主流程事务
                print(f"[persist] 文档摘要落库异常（不影响采集）: {str(e)[:120]}")

            # ── 合约安全：主链合约扫描（GoPlus/RugCheck）→ asset_contract_security
            try:
                from crypto_research.persist import persist_contract_security_scan
                from crypto_research.clients.contract_security_client import ContractSecurityClient
                with conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT chain, contract_address FROM core.asset_contract "
                        "WHERE asset_id=%s AND contract_address IS NOT NULL "
                        "ORDER BY is_primary DESC, contract_id LIMIT 1", (asset["asset_id"],))
                    _row = _cur.fetchone()
                if _row:
                    _chain, _addr = _row
                    _scan = ContractSecurityClient().scan(asset["asset_id"], _chain, _addr)
                    if isinstance(_scan, dict) and _scan.get("source_status"):
                        persist_contract_security_scan(conn, asset["asset_id"], _chain, _addr, _scan)
                        print(f"[persist] 合约安全: {_scan.get('source')}/{_scan.get('source_status')} "
                              f"risk={_scan.get('risk_score')} 已落库")
                    else:
                        print(f"[persist] 合约安全: 扫描无结果（{_chain} 可能不支持）")
                else:
                    print("[persist] 合约安全: 无合约地址，跳过")
            except Exception as e:
                conn.rollback()
                print(f"[persist] 合约安全落库异常（不影响采集）: {str(e)[:120]}")

            # ── 社交：X 关注数 → asset_social_heat
            try:
                from crypto_research.persist import persist_social_heat
                from crypto_research.clients.crawl4ai_client import Crawl4AIClient
                with conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT entry_url FROM biz.doc_source_entry "
                        "WHERE asset_id=%s AND entry_type='twitter' "
                        "  AND (entry_url ILIKE '%%x.com%%' OR entry_url ILIKE '%%twitter.com%%') "
                        "ORDER BY is_primary DESC LIMIT 1", (asset["asset_id"],))
                    _tw = _cur.fetchone()
                if _tw:
                    _md = Crawl4AIClient(get_settings(require_database=False)).crawl(
                        _tw[0], max_chars=2500, use_cache=True)
                    if _md:
                        _m = re.search(r"(\d+(?:\.\d+)?)\s*([KMB]?)\s*Followers", _md)
                        if _m:
                            _val = float(_m.group(1))
                            _mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(_m.group(2), 1)
                            _followers = int(_val * _mult)
                            persist_social_heat(conn, asset["asset_id"], identity["symbol"],
                                                twitter_followers=_followers)
                            print(f"[persist] 社交: X {_followers:,} 关注 已落库 asset_social_heat")
                        else:
                            print("[persist] 社交: X 页未解析到关注数")
                    else:
                        print("[persist] 社交: X 抓取失败（登录墙/反爬）")
                else:
                    print("[persist] 社交: 无 twitter 入口，跳过")
            except Exception as e:
                conn.rollback()
                print(f"[persist] 社交落库异常（不影响采集）: {str(e)[:120]}")
        # P2/P5 外部搜索补全（firecrawl，--search 开启）
        external_search = stage_external_search(identity, per_dim=args.search,
                                                crawl_details=args.search_crawl) if args.search > 0 else None
        if external_search is not None:
            print(f"[research] 外部搜索: {external_search.get('note', '')}")

        # 外部情报落库（融资 → asset_raises，依赖 external_search 结果）
        if args.persist and external_search:
            try:
                from crypto_research.persist import persist_raises
                funding_results: list[dict] = []
                for _dim in external_search.get("dimensions") or []:
                    if _dim.get("key") == "team_funding":
                        funding_results = _dim.get("results") or []
                        break
                if funding_results:
                    _raises = _parse_funding(funding_results)
                    if _raises:
                        _n = persist_raises(conn, asset["asset_id"], identity["name"], _raises)
                        print(f"[persist] 融资: 解析 {len(_raises)} 条已落库 asset_raises（新增 {_n}）")
                    else:
                        print("[persist] 融资: 搜索结果未解析出结构化融资（建议人工补强）")
                else:
                    print("[persist] 融资: 无 team_funding 搜索结果，跳过")
            except Exception as e:
                conn.rollback()
                print(f"[persist] 融资落库异常（不影响采集）: {str(e)[:120]}")
        market = stage_market(conn, asset, binance_symbol)
        flow = stage_flow(conn, identity.get("symbol", ""))
        catalyst = stage_catalyst(conn, asset)

        # 矩阵防陈旧（2026-10-23）：体检快照算于 P1（采集阶段之前），而本 run 的
        # 落库阶段（docs 摘要/融资，--persist 默认开）与外部作业都可能在此期间写入
        # ⇒ 用入库后的状态重算体检再渲染，避免矩阵漏报本 run 刚补齐/刚可见的数据域
        # （晨间 XMR/ZEC 体检报 CM"0 条"、白皮书"0 条"即此类陈旧/竞态漏报）
        try:
            health = check_data_health(conn, asset["asset_id"], binance_symbol)
            print("[research] 体检矩阵已按入库后状态重算（防陈旧）")
        except Exception as e:
            print(f"[research] 体检重算失败，沿用 P1 快照: {type(e).__name__}: {str(e)[:80]}")

        gen_time = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        data = {
            "identity": identity, "health": health, "docs": docs,
            "external_web": external_web, "docs_deep": docs_deep,
            "external_search": external_search,
            "dq": dq, "market": market,
            "flow": flow, "catalyst": catalyst,
            "gen_time": gen_time,
            "binance_symbol": binance_symbol, "sector": sector,
        }

        # 输出
        out_dir = Path(args.out) if args.out else (DEFAULT_OUT / f"{identity['symbol'].lower()}_{datetime.now():%Y%m%d}")
        out_dir.mkdir(parents=True, exist_ok=True)

        # 文档深度爬取全文（独立文件，方便 AI 阅读全部文档原文）
        if docs_deep is not None and docs_deep.get("sites"):
            (out_dir / "docs_deep.md").write_text(
                render_docs_deep_md(docs_deep), encoding="utf-8")
            print(f"[research] 文档深度爬取: {out_dir / 'docs_deep.md'}")

        if args.json_only:
            (out_dir / "research.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"[research] JSON: {out_dir / 'research.json'}")
            return 0

        brief = render_data_brief(data)
        (out_dir / "research_data.md").write_text(brief, encoding="utf-8")
        (out_dir / "research.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"[research] 数据清单: {out_dir / 'research_data.md'}")
        print(f"[research] JSON: {out_dir / 'research.json'}")
        print("[research] 注：本脚本只收集数据，投研判断由 AI 读取数据清单后完成。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
