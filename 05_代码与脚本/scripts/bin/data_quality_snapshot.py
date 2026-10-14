#!/usr/bin/env python3
"""投研数据质量快照（P1 补充层）：扫描全库内容级脏数据。

与 data_health.py 的分工：
    data_health.py          → 新鲜度/覆盖率（"数据新不新、有没有"）
    data_quality_snapshot   → 内容质量（"数据对不对"）

检查项：
    1. 描述污染（description 含其它资产名）
    2. 空/占位描述
    3. symbol 一致性（coin_basic vs core.asset）
    4. 解锁历史残留（unlock_date < today）
    5. 解锁金额异常（> 最新市值）
    6. ETF 双源不一致（farside vs cryptoetf）
    7. 行情价格异常（price<=0 / |chg24h|>500%）
    8. doc_asset 长期待解析
    9. doc_source_entry 重复 URL
    10. tokenomics supply 异常
    11. github 关联疑似错误（启发式）

注意：所有动态值（asset_id/limit）均为内部 int，直接 f-string 拼接安全；
SQL 中保留 LIKE 字面量 % 不受 psycopg 占位符解析影响。

用法：
    python bin/data_quality_snapshot.py                 # 全库扫描
    python bin/data_quality_snapshot.py --symbol BTC    # 单资产聚焦
    python bin/data_quality_snapshot.py --limit 5       # 每个问题最多显示 N 条样例
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
_scripts_src = os.path.join(_code_root, "scripts", "src")
for cand in (_scripts_src, os.path.join(_code_root, "workbench"), "/app"):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

from data_health import resolve_asset  # noqa: E402

# 问题级别
LEVEL_OK = "🟢"
LEVEL_WARN = "🟡"
LEVEL_BAD = "🔴"


def _q(conn, sql: str) -> list[tuple]:
    with conn.cursor() as cur:
        try:
            cur.execute(sql)
            return cur.fetchall()
        except Exception:
            conn.rollback()  # 单个检查失败不污染事务，其余检查继续
            raise


def _scalar(conn, sql: str) -> int:
    rows = _q(conn, sql)
    return int(rows[0][0]) if rows and rows[0][0] is not None else 0


# ════════════════════════════════════════════════════════════════════
# 各检查项（返回 dict: {key, label, status, count, samples, note})
# ════════════════════════════════════════════════════════════════════
def check_description_pollution(conn, asset_id: int | None = None, limit: int = 5) -> dict:
    """描述污染：coin_basic.description 不含自己资产名，但 cmc 有正确描述。

    严格口径（与 fix_data_quality.py 对齐）：CMC 模板描述天然含其它币名
    （如 LTC 描述含 "cryptocurrency" 等共词），"含其它币名"会误报。
    真实污染 = 当前描述不含自己 name + cmc 描述含自己 symbol → 描述被覆盖。

    只扫描核心资产（有市值排名），避免全库 4 万资产笛卡尔积。
    """
    if asset_id is not None:
        core_filter = f"cb.asset_id = {asset_id}"
    else:
        # 全库模式：只扫描 top 200 核心资产（投研主要对象），避免 4 万资产昂贵扫描
        core_filter = (
            "cb.asset_id IN (SELECT asset_id FROM core.asset "
            "WHERE market_cap_rank IS NOT NULL AND market_cap_rank <= 200)"
        )
    rows = _q(
        conn,
        f"""
        SELECT DISTINCT cb.asset_id, cb.coin_symbol, cb.coin_name,
               LEFT(cb.description_short, 100) AS descr,
               LEFT(ci.description, 100) AS cmc_desc
        FROM biz.coin_basic cb
        JOIN src_cmc.cmc_asset_info ci ON ci.cmc_id = cb.cmc_id
        WHERE {core_filter}
          AND cb.description_short IS NOT NULL
          AND ci.description IS NOT NULL
          AND ci.description LIKE CONCAT('%', cb.coin_symbol, '%')
          AND cb.description_short NOT LIKE CONCAT('%', cb.coin_name, '%')
        ORDER BY cb.asset_id
        LIMIT {limit}
        """,
    )
    n = _scalar(
        conn,
        f"""
        SELECT COUNT(DISTINCT cb.asset_id)
        FROM biz.coin_basic cb
        JOIN src_cmc.cmc_asset_info ci ON ci.cmc_id = cb.cmc_id
        WHERE {core_filter}
          AND cb.description_short IS NOT NULL
          AND ci.description IS NOT NULL
          AND ci.description LIKE CONCAT('%', cb.coin_symbol, '%')
          AND cb.description_short NOT LIKE CONCAT('%', cb.coin_name, '%')
        """,
    )
    status = LEVEL_BAD if n > 0 else LEVEL_OK
    samples = [f"asset_id={r[0]} {r[1]} {r[2]}: {r[3]}…" for r in rows]
    return {"key": "desc_pollution", "label": "描述污染(描述被覆盖)",
            "status": status, "count": n, "samples": samples,
            "note": "当前描述不含自己资产名但 cmc 有正确描述 → 描述被其它项目覆盖"}


def check_empty_desc(conn, asset_id: int | None = None, limit: int = 5) -> dict:
    asset_where = (" AND asset_id = %d" % asset_id) if asset_id else ""
    cond = ("(description_short IS NULL OR TRIM(description_short)='' "
            "OR description_short ILIKE '%暂无%' OR description_short ILIKE '%tbd%' "
            "OR description_short ILIKE '%placeholder%')") + asset_where
    rows = _q(
        conn,
        f"SELECT asset_id, coin_symbol FROM biz.coin_basic WHERE {cond} LIMIT {limit}",
    )
    n = _scalar(conn, f"SELECT COUNT(*) FROM biz.coin_basic WHERE {cond}")
    status = LEVEL_WARN if n > 100 else (LEVEL_OK if n == 0 else LEVEL_BAD)
    return {"key": "empty_desc", "label": "空/占位描述",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]}" for r in rows],
            "note": "描述缺失，基本面文本分析会退化"}


def check_symbol_mismatch(conn, limit: int = 5) -> dict:
    rows = _q(
        conn,
        "SELECT cb.asset_id, cb.coin_symbol, a.canonical_symbol FROM biz.coin_basic cb "
        "JOIN core.asset a ON a.asset_id=cb.asset_id "
        f"WHERE UPPER(cb.coin_symbol) != UPPER(a.canonical_symbol) LIMIT {limit}",
    )
    n = _scalar(
        conn,
        "SELECT COUNT(*) FROM biz.coin_basic cb JOIN core.asset a ON a.asset_id=cb.asset_id "
        "WHERE UPPER(cb.coin_symbol) != UPPER(a.canonical_symbol)",
    )
    status = LEVEL_OK if n == 0 else LEVEL_BAD
    return {"key": "symbol_mismatch", "label": "symbol 不一致",
            "status": status, "count": n,
            "samples": [f"{r[0]} coin_basic={r[1]} core={r[2]}" for r in rows],
            "note": "同一资产在两层表里符号不一致"}


def check_unlock_history(conn, asset_id: int | None = None, limit: int = 5) -> dict:
    where = "unlock_date < CURRENT_DATE" + (f" AND asset_id = {asset_id}" if asset_id else "")
    rows = _q(
        conn,
        f"SELECT asset_id, unlock_date, unlock_type, unlock_value_usd "
        f"FROM biz.asset_unlock_event WHERE {where} AND unlock_value_usd > 0 "
        f"ORDER BY unlock_date DESC LIMIT {limit}",
    )
    n = _scalar(
        conn, f"SELECT COUNT(*) FROM biz.asset_unlock_event WHERE {where} AND unlock_value_usd > 0"
    )
    status = LEVEL_WARN if n > 0 else LEVEL_OK
    return {"key": "unlock_history", "label": "解锁历史残留",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]} {r[2]} {r[3]}" for r in rows],
            "note": "历史解锁未清理，投研若未过滤会混入"}


def check_unlock_abnormal(conn, asset_id: int | None = None, limit: int = 5) -> dict:
    where = "WHERE u.unlock_value_usd > m.market_cap" + (f" AND u.asset_id = {asset_id}" if asset_id else "")
    rows = _q(
        conn,
        f"""
        SELECT u.asset_id, u.unlock_date, u.unlock_value_usd, m.market_cap
        FROM biz.asset_unlock_event u
        JOIN (SELECT DISTINCT ON (asset_id) asset_id, market_cap
              FROM biz.asset_market_daily ORDER BY asset_id, market_date DESC) m USING(asset_id)
        {where} LIMIT {limit}
        """,
    )
    n = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM biz.asset_unlock_event u
        JOIN (SELECT DISTINCT ON (asset_id) asset_id, market_cap
              FROM biz.asset_market_daily ORDER BY asset_id, market_date DESC) m USING(asset_id)
        {where}
        """,
    )
    status = LEVEL_BAD if n > 0 else LEVEL_OK
    return {"key": "unlock_abnormal", "label": "解锁金额异常(>市值)",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]} 解锁{float(r[2]):,.0f} vs 市值{float(r[3]):,.0f}" for r in rows],
            "note": "解锁金额超过最新市值，疑似脏数据"}


def check_etf_conflict(conn, limit: int = 5) -> dict:
    rows = _q(
        conn,
        """
        SELECT f.flow_date, f.symbol, f.net_flow_usd_m AS farside, c.net_flow_usd_m AS cryptoetf,
               ABS(f.net_flow_usd_m - c.net_flow_usd_m) AS diff
        FROM biz.etf_flow_daily f JOIN biz.etf_flow_daily c
          ON c.symbol=f.symbol AND c.flow_date=f.flow_date AND c.source_code='cryptoetf'
        WHERE f.source_code='farside' AND ABS(f.net_flow_usd_m - c.net_flow_usd_m) > 1
        ORDER BY diff DESC LIMIT 5
        """,
    )
    n = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM biz.etf_flow_daily f JOIN biz.etf_flow_daily c
          ON c.symbol=f.symbol AND c.flow_date=f.flow_date AND c.source_code='cryptoetf'
        WHERE f.source_code='farside' AND ABS(f.net_flow_usd_m - c.net_flow_usd_m) > 1
        """,
    )
    status = LEVEL_BAD if n > 0 else LEVEL_OK
    return {"key": "etf_conflict", "label": "ETF 双源不一致",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]}: farside {r[2]} vs cryptoetf {r[3]} (差{r[4]})" for r in rows],
            "note": "同一日期两源净流差异 >1M，投研取数方向可能相反"}


def check_price_anomaly(conn, asset_id: int | None = None, limit: int = 5) -> dict:
    where = "1=1" + (f" AND asset_id = {asset_id}" if asset_id else "")
    rows = _q(
        conn,
        f"SELECT asset_id, market_date, price_usd, change_24h FROM biz.asset_market_daily "
        f"WHERE {where} AND (price_usd <= 0 OR ABS(change_24h) > 500) "
        f"ORDER BY market_date DESC LIMIT {limit}",
    )
    n = _scalar(
        conn,
        f"SELECT COUNT(*) FROM biz.asset_market_daily WHERE {where} AND (price_usd <= 0 OR ABS(change_24h) > 500)",
    )
    status = LEVEL_WARN if n > 0 else LEVEL_OK
    return {"key": "price_anomaly", "label": "行情价格异常",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]} price={r[2]} chg={r[3]}" for r in rows],
            "note": "价格<=0 或 |24h涨跌|>500%，技术面计算会失真"}


def check_doc_stale_parse(conn, limit: int = 5) -> dict:
    rows = _q(
        conn,
        "SELECT asset_id, doc_type, file_name, created_at FROM biz.doc_asset "
        "WHERE parse_status='待解析' AND created_at < NOW() - INTERVAL '30 days' "
        f"ORDER BY created_at LIMIT {limit}",
    )
    n = _scalar(
        conn,
        "SELECT COUNT(*) FROM biz.doc_asset WHERE parse_status='待解析' "
        "AND created_at < NOW() - INTERVAL '30 days'",
    )
    status = LEVEL_WARN if n > 0 else LEVEL_OK
    return {"key": "doc_stale_parse", "label": "文档长期待解析(>30天)",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]} {r[2]} ({r[3]})" for r in rows],
            "note": "这些文档白皮书摘要缺失的根源"}


def check_dup_entries(conn, limit: int = 5) -> dict:
    rows = _q(
        conn,
        "SELECT asset_id, entry_url, COUNT(*) AS c FROM biz.doc_source_entry "
        f"WHERE entity_type='asset' GROUP BY asset_id, entry_url HAVING COUNT(*)>1 LIMIT {limit}",
    )
    n = _scalar(
        conn,
        "SELECT COUNT(*) FROM (SELECT asset_id, entry_url FROM biz.doc_source_entry "
        "WHERE entity_type='asset' GROUP BY asset_id, entry_url HAVING COUNT(*)>1) t",
    )
    status = LEVEL_OK if n == 0 else LEVEL_WARN
    return {"key": "dup_entries", "label": "文档入口重复 URL",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]} x{r[2]}" for r in rows],
            "note": "同一资产同一 URL 多条入口，分类可能重复计数"}


def check_tokenomics_abnormal(conn, limit: int = 5) -> dict:
    rows = _q(
        conn,
        "SELECT asset_id, total_supply, circulating_supply FROM biz.asset_tokenomics "
        "WHERE total_supply IS NOT NULL AND circulating_supply IS NOT NULL "
        f"AND circulating_supply > total_supply * 1.1 LIMIT {limit}",
    )
    n = _scalar(
        conn,
        "SELECT COUNT(*) FROM biz.asset_tokenomics WHERE total_supply IS NOT NULL "
        "AND circulating_supply IS NOT NULL AND circulating_supply > total_supply * 1.1",
    )
    status = LEVEL_WARN if n > 0 else LEVEL_OK
    return {"key": "tokenomics_abnormal", "label": "代币经济供应异常",
            "status": status, "count": n,
            "samples": [f"{r[0]} total={r[1]} circ={r[2]}" for r in rows],
            "note": "流通供应 > 总供应*1.1，数据自相矛盾"}


def check_github_mismatch(conn, asset_id: int | None = None, limit: int = 5) -> dict:
    """github 关联疑似错误：repo 名既不含 symbol 也不含 name 中任一词（启发式，仅供参考）。"""
    where = "1=1" + (f" AND a.asset_id = {asset_id}" if asset_id else "")
    rows = _q(
        conn,
        f"""
        SELECT a.asset_id, a.canonical_symbol, a.canonical_name, g.repo_name, r.stars_count
        FROM biz.asset_github_repo g
        JOIN core.asset a ON a.asset_id=g.asset_id
        JOIN biz.github_repo_activity r ON r.owner_login=g.owner_login AND r.repo_name=g.repo_name
        WHERE {where}
          AND UPPER(g.repo_name) NOT LIKE '%'||UPPER(a.canonical_symbol)||'%'
          AND (a.canonical_name IS NULL OR UPPER(g.repo_name) NOT LIKE '%'||UPPER(a.canonical_name)||'%')
          AND r.stars_count < 5000
        ORDER BY a.asset_id LIMIT {limit}
        """,
    )
    n = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM biz.asset_github_repo g
        JOIN core.asset a ON a.asset_id=g.asset_id
        JOIN biz.github_repo_activity r ON r.owner_login=g.owner_login AND r.repo_name=g.repo_name
        WHERE {where}
          AND UPPER(g.repo_name) NOT LIKE '%'||UPPER(a.canonical_symbol)||'%'
          AND (a.canonical_name IS NULL OR UPPER(g.repo_name) NOT LIKE '%'||UPPER(a.canonical_name)||'%')
          AND r.stars_count < 5000
        """,
    )
    status = LEVEL_WARN if n > 0 else LEVEL_OK
    return {"key": "github_mismatch", "label": "GitHub 关联疑似错误",
            "status": status, "count": n,
            "samples": [f"{r[0]} {r[1]} {r[2]} ← {r[3]} (⭐{r[4]})" for r in rows],
            "note": "repo 名与资产名无关且低星，可能关联错资产"}


CHECKS = [
    check_description_pollution,
    check_empty_desc,
    check_symbol_mismatch,
    check_unlock_history,
    check_unlock_abnormal,
    check_etf_conflict,
    check_price_anomaly,
    check_doc_stale_parse,
    check_dup_entries,
    check_tokenomics_abnormal,
    check_github_mismatch,
]

# 全库检查（不依赖 asset_id）
GLOBAL_ONLY = {check_symbol_mismatch, check_etf_conflict, check_doc_stale_parse,
               check_dup_entries, check_tokenomics_abnormal}


def render_snapshot(results: list[dict]) -> str:
    lines = [
        "### 数据质量快照（内容级脏数据扫描）",
        "",
        "| 检查项 | 状态 | 数量 | 说明 |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['label']} | {r['status']} | {r['count']} | {r['note']} |")
    lines.append("")
    for r in results:
        if r["samples"]:
            lines.append(f"**{r['label']}** 样例：")
            for s in r["samples"][:3]:
                lines.append(f"- {s}")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="投研数据质量快照")
    parser.add_argument("--symbol", default="", help="聚焦单资产（符号/asset_id），默认全库")
    parser.add_argument("--limit", type=int, default=5, help="每项最多显示样例数")
    parser.add_argument("--md", action="store_true", help="输出 Markdown 块")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    asset_id = None
    with get_connection(settings.database_url) as conn:
        if args.symbol:
            q = int(args.symbol) if args.symbol.isdigit() else args.symbol
            candidates = resolve_asset(conn, q)
            if not candidates:
                print(f"[dq] 未找到资产: {args.symbol}")
                return 1
            asset_id = candidates[0]["asset_id"]
            print(f"[dq] 聚焦: {candidates[0]['canonical_symbol']} {candidates[0]['canonical_name']} "
                  f"(asset_id={asset_id})")

        results = []
        for check_fn in CHECKS:
            try:
                if check_fn in GLOBAL_ONLY:
                    r = check_fn(conn, args.limit)
                else:
                    r = check_fn(conn, asset_id, args.limit)
                print(f"[dq] {r['status']} {r['label']}: {r['count']}")
                results.append(r)
            except Exception as e:
                print(f"[dq] ⚠️ {getattr(check_fn, '__name__', '?')} 执行失败: {str(e)[:120]}")

        if args.md:
            print()
            print(render_snapshot(results))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
