#!/usr/bin/env python3
"""P3-B · 归属评测集采样器（**只读**，产 gold 候选文件）。

产出 `tests/fixtures/attr_gold.jsonl`。**这是候选集（v0）**：其中
`label_source='auto'` 的标签由确定性规则给出（可当 baseline 直用），
`label_source='review'` 的必须人工确认后才算 gold。

分桶与标注策略
--------------
- `crypto_gap`       有真加密交易对却没连上 → **auto 正样本**（正确 asset=该交易对）
- `tradfi_only`      全 TradFi 交易对         → **auto 负样本**（label=`__non_crypto__`）
- `no_pairs`         源未给交易对             → review（附项目名组织词 hint）
- `unknown_mixed`    全部基元未知             → review

用法
----
    python attr_gold_sample.py                       # 默认 100/150/50/50
    python attr_gold_sample.py --out tests/fixtures/attr_gold.jsonl
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
from catalyst.linker import _COMMODITY_AMBIGUOUS_SYMBOLS, has_crypto_context  # noqa: E402
from catalyst.pipeline import _match_project_name_to_assets  # noqa: E402

FIAT_MACRO = frozenset({
    "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF", "DXY",
    "IEF", "SPX", "SPY", "NDX", "QQQ", "MSCI", "EWY", "TLT", "VIX",
})
SYNTH_NAME_RE = r"tokeniz|bstock|prestock|derivativ|robinhood token|dinari|backpack|xstock"

# 分桶用的 per-base 分类 CTE（与 attr_audit.py 同口径）
_CLS_CTE = f"""
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
  select b.catalyst_id, b.asset_id, b.base,
         ((b.base = any(%(comm)s)) or (b.base = any(%(fiat)s))
          or coalesce(sym.all_synth,false)) as nc,
         (coalesce(sym.has_rank,false) and not coalesce(sym.all_synth,false)
          and b.base <> all(%(comm)s) and b.base <> all(%(fiat)s)) as cr
  from b left join sym on sym.s = b.base
),
agg as (
  select catalyst_id, max(asset_id) asset_id,
         count(*) np, count(*) filter (where nc) n_nc, count(*) filter (where cr) n_cr
  from cb group by 1
)
"""

_ARGS = lambda: {"comm": list(_COMMODITY_AMBIGUOUS_SYMBOLS), "fiat": list(FIAT_MACRO)}


def _fetch_detail(cur, cids: list[int]) -> dict[int, dict]:
    if not cids:
        return {}
    cur.execute(
        """select catalyst_id cid, source_code sc, title t, left(coalesce(body_text,''),300) body,
                  coalesce(related_pairs,'{}') pairs, published_at pub
           from biz.asset_catalyst where catalyst_id = any(%s)""",
        (cids,),
    )
    return {r["cid"]: r for r in cur.fetchall()}


def build(n_gap: int, n_nopair: int, n_tradfi: int, n_unknown: int) -> list[dict]:
    rows: list[dict] = []
    with get_conn() as conn:
        cur = conn.cursor()

        # 1) crypto_gap：有真加密对却 NULL
        cur.execute(
            f"""{_CLS_CTE}
            select aa.catalyst_id cid from agg aa
            where aa.n_cr >= 1 and aa.asset_id is null
            order by random() limit %(n)s""",
            {**_ARGS(), "n": n_gap},
        )
        gap_cids = [r["cid"] for r in cur.fetchall()]

        # 2) tradfi_only：全 TradFi
        cur.execute(
            f"""{_CLS_CTE}
            select aa.catalyst_id cid from agg aa
            where aa.n_nc = aa.np and aa.n_cr = 0
            order by random() limit %(n)s""",
            {**_ARGS(), "n": n_tradfi},
        )
        tradfi_cids = [r["cid"] for r in cur.fetchall()]

        # 3) unknown_mixed：全未知
        cur.execute(
            f"""{_CLS_CTE}
            select aa.catalyst_id cid from agg aa
            where aa.n_cr = 0 and aa.n_nc < aa.np
            order by random() limit %(n)s""",
            {**_ARGS(), "n": n_unknown},
        )
        unknown_cids = [r["cid"] for r in cur.fetchall()]

        # 4) no_pairs：源未给交易对（按 source 分层，取随机）
        cur.execute(
            """select catalyst_id cid from biz.asset_catalyst
               where asset_id is null and cardinality(coalesce(related_pairs,'{}')) = 0
               order by random() limit %s""",
            (n_nopair,),
        )
        nopair_cids = [r["cid"] for r in cur.fetchall()]

        detail = {}
        for lst in (gap_cids, tradfi_cids, unknown_cids, nopair_cids):
            detail.update(_fetch_detail(cur, lst))

        def _rec(cid, bucket, label, label_source, confidence, hint=""):
            d = detail.get(cid)
            if not d:
                return None
            return {
                "cid": cid,
                "bucket": bucket,
                "source_code": d["sc"],
                "published_at": d["pub"].isoformat() if d["pub"] else None,
                "title": d["t"],
                "body_snippet": d["body"],
                "related_pairs": list(d["pairs"] or []),
                "label": label,
                "label_symbols": [],
                "label_source": label_source,
                "confidence": confidence,
                "needs_review": label_source == "review",
                "hint": hint,
            }

        # crypto_gap → auto 正样本（一次性批量取「真加密基元」对应的 asset）
        gap_labels: dict[int, tuple[list[int], list[str]]] = {}
        if gap_cids:
            cur.execute(
                f"""{_CLS_CTE}
                select cb.catalyst_id cid,
                       array_agg(distinct a.asset_id) aids,
                       array_agg(distinct a.canonical_symbol) syms
                from cb join core.asset a on upper(a.canonical_symbol) = cb.base
                where cb.cr and cb.catalyst_id = any(%(cids)s)
                  and a.market_cap_rank is not null
                  and lower(a.canonical_name) !~ '{SYNTH_NAME_RE}'
                group by 1""",
                {**_ARGS(), "cids": gap_cids},
            )
            gap_labels = {r["cid"]: (list(r["aids"]), list(r["syms"])) for r in cur.fetchall()}

        for cid in gap_cids:
            if cid not in gap_labels:
                continue
            aids, syms = gap_labels[cid]
            r = _rec(cid, "crypto_gap", aids, "auto", "high")
            if not r:
                continue
            r["label_symbols"] = syms
            # 护栏：标题/正文无加密语境 ⇒ 很可能是「股票 ticker 撞名加密资产」
            # （实测 cid 9341 = NexGen Energy 美股因 `NEX` 撞名被误标）。
            if not has_crypto_context((r["title"] or "") + " " + (r["body_snippet"] or "")):
                r["label_source"] = "auto_suspect"
                r["confidence"] = "low"
                r["needs_review"] = True
                r["hint"] = "无加密语境 → 疑股票 ticker 撞名，需人工确认"
            rows.append(r)

        # tradfi_only → auto 负样本（正文含加密语境者降级 review，避免误判真 meme）
        for cid in tradfi_cids:
            r = _rec(cid, "tradfi_only", ["__non_crypto__"], "auto", "high")
            if not r:
                continue
            if has_crypto_context((r["title"] or "") + " " + (r["body_snippet"] or "")):
                r["label_source"] = "auto_suspect"
                r["confidence"] = "low"
                r["needs_review"] = True
                r["hint"] = "正文含加密语境 → 可能是商品同名真币，需人工确认"
            rows.append(r)

        # unknown_mixed / no_pairs → review（附组织词 hint，不作标签）
        for cid in unknown_cids:
            r = _rec(cid, "unknown_mixed", None, "review", "low")
            if r:
                hits = _match_project_name_to_assets(conn, r["title"] or "")
                r["hint"] = f"org-word候选: {[h['symbol'] for h in hits]}" if hits else "无组织词命中"
                rows.append(r)
        for cid in nopair_cids:
            r = _rec(cid, "no_pairs", None, "review", "low")
            if r:
                hits = _match_project_name_to_assets(conn, r["title"] or "")
                r["hint"] = f"org-word候选: {[h['symbol'] for h in hits]}" if hits else "无组织词命中"
                rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=int, default=100)
    ap.add_argument("--nopair", type=int, default=150)
    ap.add_argument("--tradfi", type=int, default=50)
    ap.add_argument("--unknown", type=int, default=50)
    default_out = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "attr_gold.jsonl"
    ap.add_argument("--out", default=str(default_out))
    args = ap.parse_args()

    rows = build(args.gap, args.nopair, args.tradfi, args.unknown)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    c = Counter(r["bucket"] for r in rows)
    c2 = Counter(r["label_source"] for r in rows)
    print(f"写出 {len(rows)} 条 → {out}")
    print("  分桶:", dict(c))
    print("  标签来源:", dict(c2), "（auto 可直接当 baseline；review 需人工确认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
