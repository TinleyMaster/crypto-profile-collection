#!/usr/bin/env python3
"""基本面因子构建器：原始表 → 数值因子 → biz.asset_factor_daily（Alpha 挖掘数据底座）。

因子均为"事实派生"（不做投研判断）：把已落库的原始数据转成可回测的数值因子。

用法：
    python bin/build_factors.py --symbol PONS          # 单资产全量因子
    python bin/build_factors.py --all                  # 全库（有原始数据的资产）
    python bin/build_factors.py --symbol PONS --date 2026-10-13   # 指定因子日期

当前因子族：
    holder_*    持有者集中度（onchain_holder_snapshot，口径：占比含 burn 地址）
    contract_*  合约安全（asset_contract_security）
    doc_*       文档完备度（doc_source_entry + doc_whitepaper_summary）
    unlock_*    解锁压力（asset_unlock_event）
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
for cand in (os.path.join(_code_root, "scripts", "src"),):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from data_health import resolve_asset  # noqa: E402


def _upsert(conn, asset_id: int, fdate: date, code: str, value, source: str, meta: dict | None):
    if value is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO biz.asset_factor_daily
               (asset_id, factor_date, factor_code, factor_value, factor_source, metadata)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (asset_id, factor_date, factor_code) DO UPDATE SET
                 factor_value=EXCLUDED.factor_value,
                 factor_source=EXCLUDED.factor_source,
                 metadata=EXCLUDED.metadata,
                 updated_at=NOW()""",
            (asset_id, fdate, code, value, source,
             __import__("json").dumps(meta or {}, ensure_ascii=False)))


# ════════════════════════════════════════════════════════════════
# 因子族：持有者集中度
# ════════════════════════════════════════════════════════════════
def build_holder_factors(conn, asset_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT snapshot_date, top1_concentration, top5_concentration,
                      top10_concentration, top20_concentration, top50_concentration,
                      top100_concentration, burn_pct, circ_supply, total_holders
               FROM biz.onchain_holder_snapshot WHERE asset_id=%s
               ORDER BY snapshot_date""", (asset_id,))
        rows = cur.fetchall()
    meta = {"口径": "占比含 burn 地址"}
    for (d, t1, t5, t10, t20, t50, t100, burn, circ, tot) in rows:
        for code, v in (("holder_top1_pct", t1), ("holder_top5_pct", t5),
                        ("holder_top10_pct", t10), ("holder_top20_pct", t20),
                        ("holder_top50_pct", t50), ("holder_top100_pct", t100),
                        ("holder_burn_pct", burn), ("holder_circ_supply", circ),
                        ("holder_total_count", tot)):
            _upsert(conn, asset_id, d, code, v, "onchain_holder_snapshot", meta)


# ════════════════════════════════════════════════════════════════
# 因子族：合约安全
# ════════════════════════════════════════════════════════════════
def build_contract_factors(conn, asset_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scanned_at::date, is_proxy, contract_verified, reputation, risk_score "
            "FROM biz.asset_contract_security WHERE asset_id=%s", (asset_id,))
        rows = cur.fetchall()
    for (d, is_proxy, verified, rep, risk) in rows:
        _upsert(conn, asset_id, d, "contract_is_proxy",
                1 if is_proxy else 0, "asset_contract_security", {"布尔": "1=可升级代理"})
        _upsert(conn, asset_id, d, "contract_verified",
                1 if verified else 0, "asset_contract_security", {"布尔": "1=源码已验证"})
        _upsert(conn, asset_id, d, "contract_reputation_ok",
                1 if rep == "ok" else 0, "asset_contract_security", {"布尔": "1=信誉 ok"})
        _upsert(conn, asset_id, d, "contract_risk_score", risk, "asset_contract_security", {})


# ════════════════════════════════════════════════════════════════
# 因子族：文档完备度
# ════════════════════════════════════════════════════════════════
def build_doc_factors(conn, asset_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(updated_at::date) FROM biz.doc_source_entry WHERE asset_id=%s",
                    (asset_id,))
        (cnt, last_ts) = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry "
                    "WHERE asset_id=%s AND entry_type IN ('whitepaper_page','docs','docs_portal')",
                    (asset_id,))
        docs_cnt = cur.fetchone()[0]
        cur.execute("SELECT MAX(updated_at::date) FROM biz.doc_whitepaper_summary WHERE asset_id=%s",
                    (asset_id,))
        sum_ts = cur.fetchone()[0]
        cur.execute("SELECT doc_type FROM biz.doc_asset WHERE asset_id=%s", (asset_id,))
        doc_types = sorted({r[0] for r in cur.fetchall()})

    fdate = max(t for t in (last_ts, sum_ts) if t) or date.today()
    _upsert(conn, asset_id, fdate, "doc_entry_count", cnt, "doc_source_entry",
            {"doc_types": doc_types})
    _upsert(conn, asset_id, fdate, "doc_docs_count", docs_cnt, "doc_source_entry",
            {"类型": "whitepaper_page/docs/docs_portal 数量"})
    _upsert(conn, asset_id, fdate, "doc_has_summary",
            1 if sum_ts else 0, "doc_whitepaper_summary", {"布尔": "1=有结构化摘要"})


# ════════════════════════════════════════════════════════════════
# 因子族：解锁压力（未来 90 天）
# ════════════════════════════════════════════════════════════════
def build_unlock_factors(conn, asset_id: int, today: date | None = None):
    today = today or date.today()
    horizon = today + timedelta(days=90)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT unlock_date, unlock_value_usd, unlock_ratio_circulating
               FROM biz.asset_unlock_event
               WHERE asset_id=%s AND unlock_date>=%s AND unlock_date<=%s
                 AND unlock_value_usd IS NOT NULL
               ORDER BY unlock_date""", (asset_id, today, horizon))
        rows = cur.fetchall()
    total_usd = sum(r[1] for r in rows if r[1] is not None and r[1] > 0)
    _upsert(conn, asset_id, today, "unlock_90d_usd", total_usd, "asset_unlock_event",
            {"窗口": f"{today}~{horizon}", "事件数": len(rows)})
    for (d, val, ratio) in rows:
        if val is None or val <= 0:
            continue
        _upsert(conn, asset_id, d, "unlock_event_usd", val, "asset_unlock_event",
                {"ratio_circulating": ratio})


# ════════════════════════════════════════════════════════════════
def collect_assets(conn, args) -> list[int]:
    """资产集合：只收「有可信基本面数据」的资产，避免被噪音链接污染。

    可信口径：
      - onchain_holder_snapshot（链上真实快照）
      - asset_contract_security（合约安全）
      - doc_source_entry 中 docs/whitepaper 类型且 URL 指向文档站特征（docs/whitepaper/gitbook）
    --all 时按市值排名取前 limit 个（默认全量）。
    """
    if getattr(args, "all", False):
        sql = (
            "SELECT DISTINCT ON (a.asset_id) a.asset_id, a.market_cap_rank FROM core.asset a "
            "WHERE EXISTS (SELECT 1 FROM biz.onchain_holder_snapshot h WHERE h.asset_id=a.asset_id) "
            "   OR EXISTS (SELECT 1 FROM biz.asset_contract_security c WHERE c.asset_id=a.asset_id) "
            "   OR EXISTS (SELECT 1 FROM biz.doc_source_entry d WHERE d.asset_id=a.asset_id "
            "       AND d.entry_type IN ('whitepaper_page','docs','docs_portal') "
            "       AND (d.entry_url ILIKE '%docs%' OR d.entry_url ILIKE '%whitepaper%' "
            "            OR d.entry_url ILIKE '%gitbook%' OR d.entry_url ILIKE '%readme%')) "
            "ORDER BY a.asset_id, a.market_cap_rank ASC NULLS LAST"
        )
        if args.limit:
            sql = f"SELECT asset_id FROM ({sql}) t ORDER BY market_cap_rank ASC NULLS LAST LIMIT {int(args.limit)}"
        with conn.cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]
    q = int(args.symbol) if args.symbol.isdigit() else args.symbol
    candidates = resolve_asset(conn, q)
    return [candidates[0]["asset_id"]] if candidates else []


def main() -> int:
    parser = argparse.ArgumentParser(description="基本面因子构建器（原始表 → asset_factor_daily）")
    parser.add_argument("--symbol", default="", help="币种符号/名称/asset_id")
    parser.add_argument("--all", action="store_true", help="全库有原始数据的资产（按市值排名）")
    parser.add_argument("--limit", type=int, default=0, help="--all 时最多处理前 N 个资产（按市值排名，0=全量）")
    parser.add_argument("--date", default="", help="解锁因子基准日（默认今天，YYYY-MM-DD）")
    args = parser.parse_args()

    if not args.symbol and not args.all:
        print("用法: --symbol PONS 或 --all")
        return 1

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        assets = collect_assets(conn, args)
        if not assets:
            print("[factors] 无可处理资产")
            return 1
        today = date.fromisoformat(args.date) if args.date else None
        total = len(assets)
        for idx, aid in enumerate(assets, 1):
            build_holder_factors(conn, aid)
            build_contract_factors(conn, aid)
            build_doc_factors(conn, aid)
            build_unlock_factors(conn, aid, today)
            # 每 20 资产提交一次：大幅降低事务往返开销，中断最多丢 20 个
            if idx % 20 == 0 or idx == total:
                conn.commit()
                print(f"[factors] 进度 {idx}/{total}（asset_id={aid}）")
        print(f"[factors] 完成：biz.asset_factor_daily 已更新 {total} 个资产")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
