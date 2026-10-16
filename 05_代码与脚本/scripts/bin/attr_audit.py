#!/usr/bin/env python3
"""P3-A · 催化剂归属度量审计（**只读，绝不写库**）。

背景（工单 P3-ATTR-001）
-----------------------
「67.6% NULL」这个 KPI 是失真的 —— 它把三件性质完全不同的事混成了一个数：

    无 pairs（源未给交易对）   ← 需文本/项目名抽取（主病灶）
    有 pairs 但全 TradFi      ← 正确地不连，应从分母剔除
    有 pairs 且含真加密对      ← linker 漏，低风险可救

本脚本把归属率重新拆成上述三桶，并给出「真加密归属率」，另附三个死通道探针
与「指向 TradFi 伪资产」的误连清单。

分类口径（可辩护版）
--------------------
- **Non-Crypto 基元(NC)**：满足任一即 NC
    a) 命中 `catalyst.linker._COMMODITY_AMBIGUOUS_SYMBOLS`（商品同名）
    b) 命中 fiat/macro 集合（EUR/GBP/IEF/SPX…）
    c) `core.asset` 中该 symbol 的**全部**行都带合成名（tokenized/bStocks/derivatives/
       Robinhood Token/Dinari/Backpack/xStock）—— 如 `NVDAB`/`MSFTB`
- **Crypto 基元(CR)**：该 symbol 在 `core.asset` 有带 CMC 排名的行，且**不**是纯合成资产，
  且不在 NC 的商品/fiat 集里。
- 行级分桶：`tradfi_only`(全 NC) / `crypto_bearing`(≥1 CR) / `unknown_mixed`(其余)

用法
----
    python attr_audit.py            # 人读表格
    python attr_audit.py --json     # 机器可读（CI / 看板）
    python attr_audit.py --mislinks # 附「指向伪资产」的 catalyst_id 清单
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from catalyst.db import get_conn  # noqa: E402
from catalyst.linker import _COMMODITY_AMBIGUOUS_SYMBOLS  # noqa: E402

# 法币 / 宏观 / 股指（币安广场常打 EURUSDT / IEFUSDT / SPYUSDT 这类标签）
FIAT_MACRO = frozenset({
    "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF", "DXY",
    "IEF", "SPX", "SPY", "NDX", "QQQ", "MSCI", "EWY", "TLT", "VIX",
})
# 合成资产名（伪资产识别）
SYNTH_NAME_RE = (
    r"tokeniz|bstock|prestock|derivativ|robinhood token|dinari|backpack|xstock"
)


def _scalar(cur):
    """取单行单列的值（连接使用 dict_row 行工厂）。"""
    r = cur.fetchone()
    return next(iter(r.values())) if r else None


def _audit(conn) -> dict:
    cur = conn.cursor()

    # 0. 总量
    cur.execute(
        """select count(*) total,
                  count(*) filter (where asset_id is not null) linked,
                  count(*) filter (where asset_id is null) nul,
                  count(*) filter (where cardinality(coalesce(related_pairs,'{}'))=0) no_pairs_total
           from biz.asset_catalyst"""
    )
    _r0 = cur.fetchone()
    total, linked, nul = _r0["total"], _r0["linked"], _r0["nul"]

    # 1. 行级三分（per-base 分类）
    cur.execute(
        f"""
        with sym as (
          select upper(canonical_symbol) s,
                 bool_or(market_cap_rank is not null) has_rank,
                 bool_and(lower(canonical_name) ~ '{SYNTH_NAME_RE}') all_synth
          from core.asset group by 1
        ),
        b as (
          select c.catalyst_id, c.asset_id,
                 upper(regexp_replace(upper(p),'(USDT|USDC|BUSD|FDUSD|TUSD|USDP)$','')) base
          from biz.asset_catalyst c, unnest(c.related_pairs) p
          where cardinality(c.related_pairs) > 0
        ),
        cb as (
          select b.catalyst_id, b.asset_id,
                 -- 基元分类
                 ((b.base = any(%(comm)s)) or (b.base = any(%(fiat)s))
                  or coalesce(sym.all_synth, false)) as nc,
                 (coalesce(sym.has_rank,false) and not coalesce(sym.all_synth,false)
                  and b.base <> all(%(comm)s) and b.base <> all(%(fiat)s)) as cr
          from b left join sym on sym.s = b.base
        ),
        agg as (
          select catalyst_id, max(asset_id) asset_id,
                 count(*) np,
                 count(*) filter (where nc) n_nc,
                 count(*) filter (where cr) n_cr
          from cb group by 1
        )
        select
          count(*) filter (where n_nc = np and n_cr = 0) as tradfi_only,
          count(*) filter (where n_nc = np and n_cr = 0 and asset_id is null) as tradfi_only_null,
          count(*) filter (where n_cr >= 1) as crypto_bearing,
          count(*) filter (where n_cr >= 1 and asset_id is null) as crypto_gap,
          count(*) filter (where n_cr = 0 and n_nc < np) as unknown_mixed,
          count(*) filter (where n_cr = 0 and n_nc < np and asset_id is null) as unknown_mixed_null
        from agg
        """,
        {
            "comm": list(_COMMODITY_AMBIGUOUS_SYMBOLS),
            "fiat": list(FIAT_MACRO),
        },
    )
    _r1 = cur.fetchone()
    tradfi_only = _r1["tradfi_only"]
    tradfi_only_null = _r1["tradfi_only_null"]
    crypto_bearing = _r1["crypto_bearing"]
    crypto_gap = _r1["crypto_gap"]
    unknown_mixed = _r1["unknown_mixed"]
    unknown_mixed_null = _r1["unknown_mixed_null"]

    # 2. 死通道探针
    cur.execute("select count(*) as n from core.asset_source_map where source_code='binance'")
    srcmap_binance = _scalar(cur)
    cur.execute("""select coalesce(sum(n) filter (where link_source='project_name'),0)::int as pn,
                          coalesce(sum(n) filter (where link_source='trading_pairs'),0)::int as tp
                   from (select link_source, count(*) n from biz.catalyst_asset_link group by 1) q""")
    _r2 = cur.fetchone()
    pn_links, tp_links = _r2["pn"], _r2["tp"]

    # 3. 伪资产误连
    cur.execute(
        f"""select count(*) as n from biz.asset_catalyst c join core.asset a on a.asset_id=c.asset_id
            where lower(a.canonical_name) ~ '{SYNTH_NAME_RE}'"""
    )
    pseudo_primary = _scalar(cur)
    cur.execute(
        f"""select count(*) as n from biz.catalyst_asset_link l join core.asset a on a.asset_id=l.asset_id
            where lower(a.canonical_name) ~ '{SYNTH_NAME_RE}'"""
    )
    pseudo_link = _scalar(cur)
    cur.execute(
        f"""select a.canonical_symbol as symbol, count(*) as n
            from biz.asset_catalyst c join core.asset a on a.asset_id=c.asset_id
            where lower(a.canonical_name) ~ '{SYNTH_NAME_RE}'
            group by 1 order by 2 desc limit 15"""
    )
    pseudo_top = [(r["symbol"], r["n"]) for r in cur.fetchall()]

    # 4. core.asset 合成资产规模
    cur.execute(
        f"""select count(*) filter (where market_cap_rank is not null) as ranked,
                   count(*) as all_n
            from core.asset where lower(canonical_name) ~ '{SYNTH_NAME_RE}'"""
    )
    _r3 = cur.fetchone()
    synth_rank, synth_all = _r3["ranked"], _r3["all_n"]

    # 真加密归属率
    denom = total - tradfi_only
    linked_crypto = linked - (tradfi_only - tradfi_only_null)
    real_rate = (100.0 * linked_crypto / denom) if denom else 0.0

    out = {
        "total": total, "linked": linked, "null": nul,
        "null_pct": round(100.0 * nul / total, 1) if total else 0.0,
        "baseline_rate": round(100.0 * linked / total, 1) if total else 0.0,
        "buckets": {
            "no_pairs_null": nul - (tradfi_only_null + crypto_gap + unknown_mixed_null),
            "tradfi_only": tradfi_only,
            "tradfi_only_null": tradfi_only_null,
            "crypto_bearing": crypto_bearing,
            "crypto_gap": crypto_gap,
            "unknown_mixed": unknown_mixed,
            "unknown_mixed_null": unknown_mixed_null,
        },
        "real_crypto_rate": round(real_rate, 1),
        "dead_channels": {
            "asset_source_map_binance_rows": srcmap_binance,
            "links_project_name": pn_links,
            "links_trading_pairs": tp_links,
        },
        "pseudo_pollution": {
            "core_asset_synth_ranked": synth_rank,
            "core_asset_synth_total": synth_all,
            "catalyst_primary_to_synth": pseudo_primary,
            "catalyst_link_to_synth": pseudo_link,
            "top": [{"symbol": s, "n": n} for s, n in pseudo_top],
        },
    }
    return out


def _render(a: dict) -> None:
    b = a["buckets"]
    L = a["dead_channels"]
    P = a["pseudo_pollution"]
    print("=" * 68)
    print("P3-A 催化剂归属度量审计（只读）")
    print("=" * 68)
    print(f"总量 {a['total']}  |  已归属 {a['linked']} ({a['baseline_rate']}%)  |  "
          f"NULL {a['null']} ({a['null_pct']}%)")
    print("-" * 68)
    print("NULL 三分（可行动口径）：")
    print(f"  无 pairs（源未给交易对）   {b['no_pairs_null']:>7}")
    print(f"  有 pairs 但全 TradFi       {b['tradfi_only_null']:>7}   ← 正确地不连")
    print(f"  有 pairs 且含真加密对       {b['crypto_gap']:>7}   ← linker 漏（可救）")
    print(f"  其他/混合                  {b['unknown_mixed_null']:>7}")
    print("-" * 68)
    print(f"真加密归属率 {a['real_crypto_rate']}%   （原口径 {a['baseline_rate']}%）")
    print("-" * 68)
    print("死通道探针：")
    print(f"  asset_source_map 里 source_code='binance' 行数 : {L['asset_source_map_binance_rows']}  "
          f"{'← 死（应为 >0）' if L['asset_source_map_binance_rows'] == 0 else ''}")
    print(f"  catalyst_asset_link.link_source='project_name' : {L['links_project_name']}")
    print(f"  catalyst_asset_link.link_source='trading_pairs': {L['links_trading_pairs']}")
    print("-" * 68)
    print("伪资产污染：")
    print(f"  core.asset 合成资产（带 rank/总）              : {P['core_asset_synth_ranked']}/{P['core_asset_synth_total']}")
    print(f"  asset_catalyst.asset_id 指向合成资产           : {P['catalyst_primary_to_synth']}")
    print(f"  catalyst_asset_link 指向合成资产               : {P['catalyst_link_to_synth']}")
    if P["top"]:
        print("  误连 top：" + ", ".join(f"{t['symbol']}×{t['n']}" for t in P["top"][:10]))
    print("=" * 68)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--mislinks", action="store_true", help="附指向伪资产的 catalyst_id 清单")
    args = ap.parse_args()

    with get_conn() as conn:
        a = _audit(conn)
        if args.mislinks:
            cur = conn.cursor()
            cur.execute(
                f"""select c.catalyst_id as cid, c.source_code as sc,
                           coalesce(a.canonical_symbol,'?') as sym, left(c.title,70) as t
                    from biz.asset_catalyst c join core.asset a on a.asset_id=c.asset_id
                    where lower(a.canonical_name) ~ '{SYNTH_NAME_RE}'
                    order by c.published_at desc limit 200"""
            )
            a["mislink_rows"] = [
                {"catalyst_id": r["cid"], "source_code": r["sc"], "symbol": r["sym"], "title": r["t"]}
                for r in cur.fetchall()
            ]

    if args.json:
        print(json.dumps(a, ensure_ascii=False, indent=2))
    else:
        _render(a)
        if args.mislinks:
            print("\n指向伪资产的催化剂（前 200）：")
            for r in a.get("mislink_rows", []):
                print(f"  {r['catalyst_id']:>7} {r['source_code']:<34} {r['symbol']:<10} {r['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
