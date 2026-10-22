#!/usr/bin/env python3
"""单币资金流因子（Flow Factor）——投研 SOP P1.8。

把「利好 + 资金进」做成**双确认**：事件弹性（P1.6）回答「消息值不值钱」，
本指标回答「资金有没有真进来」。两者同向 = 高置信；背离 = 需警惕。

三个源（各以自身 MAX(ts) 为锚，取近 7d/30d，源滞后时自动对齐）
------------------------------------------------------------------
1. **OBI 盘口失衡** = (bids_usd − asks_usd)/(bids_usd + asks_usd)
   源 `biz.orderbook_depth_history`（Binance 深度 ±1%，4h）。
2. **OFI 主动成交失衡** = (taker_buy_usd − taker_sell_usd)/(buy + sell)
   源 `biz.cg_taker_volume_hist`（CoinGlass，4h）。>0 主动买占优。
3. **链上净流** = sum(netflow_usd)，并折算为占市值 %（>0 净流入交易所=潜在抛压；
   交易所口径见源站；此处仅作方向与量级参考）。源 `biz.onchain_netflow_hourly`。

综合输出 **资金流分 0-100** + **评级**（资金流入 / 中性 / 资金流出 / 无数据）。

用法
----
    python bin/asset_flow_factor.py --symbol LINK
    python bin/asset_flow_factor.py --symbol LINK --md
    python bin/asset_flow_factor.py --symbol LINK --persist
    python bin/asset_flow_factor.py --batch          # 全市场落库

口径局限
--------
- 三源时间跨度不一（OBI 6 个月 / OFI 1 年 / 链上 3 个月），以各自 MAX(ts) 对齐。
- 盘口/主动成交为**当下资金压力**，非长期趋势；链上净流为交易所口径，含义随源站定义。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

METHOD_VERSION = "v1"

OBI_SQL = """
WITH m AS (SELECT max(ts) mx FROM biz.orderbook_depth_history)
SELECT symbol,
  avg((bids_usd-asks_usd)/nullif(bids_usd+asks_usd,0))
      FILTER (WHERE ts > (SELECT mx FROM m) - INTERVAL '7 days')  AS v7,
  avg((bids_usd-asks_usd)/nullif(bids_usd+asks_usd,0))
      FILTER (WHERE ts > (SELECT mx FROM m) - INTERVAL '30 days') AS v30,
  count(*) FILTER (WHERE ts > (SELECT mx FROM m) - INTERVAL '7 days') AS n7
FROM biz.orderbook_depth_history
WHERE range_pct = 1.0 AND (bids_usd + asks_usd) > 0
GROUP BY symbol
"""

TAKER_SQL = """
WITH m AS (SELECT max(ts) mx FROM biz.cg_taker_volume_hist WHERE interval='4h'),
t AS (
  SELECT symbol, ts,
         (taker_buy_usd - taker_sell_usd)/nullif(taker_buy_usd + taker_sell_usd, 0) AS imb
  FROM biz.cg_taker_volume_hist WHERE interval='4h'
)
SELECT symbol,
  avg(imb) FILTER (WHERE ts > (SELECT mx FROM m) - INTERVAL '7 days')  AS v7,
  avg(imb) FILTER (WHERE ts > (SELECT mx FROM m) - INTERVAL '30 days') AS v30,
  count(*) FILTER (WHERE ts > (SELECT mx FROM m) - INTERVAL '7 days') AS n7
FROM t GROUP BY symbol
"""

NETFLOW_SQL = """
WITH m AS (SELECT max(bucket_hour) mx FROM biz.onchain_netflow_hourly)
SELECT asset_id,
  sum(netflow_usd) FILTER (WHERE bucket_hour > (SELECT mx FROM m) - INTERVAL '7 days')  AS v7,
  sum(netflow_usd) FILTER (WHERE bucket_hour > (SELECT mx FROM m) - INTERVAL '30 days') AS v30,
  count(*) FILTER (WHERE bucket_hour > (SELECT mx FROM m) - INTERVAL '7 days') AS n7
FROM biz.onchain_netflow_hourly GROUP BY asset_id
"""


FRESH_SQL = """
SELECT
  (SELECT max(ts) FROM biz.orderbook_depth_history)      AS obi_max,
  (SELECT max(ts) FROM biz.cg_taker_volume_hist WHERE interval='4h') AS taker_max,
  (SELECT max(bucket_hour) FROM biz.onchain_netflow_hourly)          AS netflow_max,
  (SELECT now())                                          AS now_ts
"""

# 断更阈值（小时）：超过即判定该源过期。OBI 4h 采集 → 24h 宽松；OFI 4h → 24h；
# 链上净流小时级 → 48h（部分资产无逐小时数据，留余量）。
STALE_HOURS = {"OBI": 24, "OFI": 24, "onchain": 48}


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _f(v):
    return float(v) if v is not None else None


def flow_score(obi, taker, nf_pct):
    """三源映射 0-100 加权：OBI .4 / taker .4 / 链上净流 .2。"""
    parts = []
    if obi is not None:
        parts.append((clamp(50 + 200 * obi), 0.40))
    if taker is not None:
        parts.append((clamp(50 + 200 * taker), 0.40))
    if nf_pct is not None:
        parts.append((clamp(50 + 500 * nf_pct), 0.20))
    if not parts:
        return None
    w = sum(w for _, w in parts)
    return round(sum(v * wt for v, wt in parts) / w, 2)


def grade_of(score):
    if score is None:
        return "无数据"
    if score >= 60:
        return "资金流入"
    if score <= 40:
        return "资金流出"
    return "中性"


# ═══════════════════════════════════════════════════════════════
#  数据加载（批量：一次拉全市场聚合）
# ═══════════════════════════════════════════════════════════════

def load_flow(conn):
    cur = conn.cursor()
    cur.execute(OBI_SQL)
    obi = {r[0]: ({"v7": _f(r[1]), "v30": _f(r[2]), "n7": r[3]}) for r in cur.fetchall()}
    cur.execute(TAKER_SQL)
    taker = {r[0]: ({"v7": _f(r[1]), "v30": _f(r[2]), "n7": r[3]}) for r in cur.fetchall()}
    cur.execute(NETFLOW_SQL)
    nf = {r[0]: ({"v7": _f(r[1]), "v30": _f(r[2]), "n7": r[3]}) for r in cur.fetchall()}

    # 各源最新时点 → 断更检测（防止把两周前的数据当成今天的资金流）
    cur.execute(FRESH_SQL)
    row = cur.fetchone()
    now_ts = row[3]
    fresh = {}
    for label, ts in (("OBI", row[0]), ("OFI", row[1]), ("onchain", row[2])):
        if ts is None:
            fresh[label] = {"max_ts": None, "lag_hours": None, "stale": True,
                            "reason": "无数据"}
        else:
            lag = (now_ts - ts).total_seconds() / 3600.0
            stale = lag > STALE_HOURS[label]
            fresh[label] = {"max_ts": ts, "lag_hours": round(lag, 1), "stale": stale,
                            "reason": ("断更 %.1f 天" % (lag / 24)) if stale else ""}
    return obi, taker, nf, fresh


def load_assets(conn):
    """symbol(USDT) → {asset_id, symbol, name, market_cap, rank}（每 symbol 取市值最大）"""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (upper(canonical_symbol))
               asset_id, canonical_symbol, canonical_name, market_cap, market_cap_rank
        FROM core.asset
        WHERE canonical_symbol IS NOT NULL
        ORDER BY upper(canonical_symbol), market_cap DESC NULLS LAST
    """)
    return {r[1].upper() + "USDT": {"asset_id": r[0], "symbol": r[1], "name": r[2],
                                    "market_cap": _f(r[3]), "rank": r[4]}
            for r in cur.fetchall()}


def compute_one(asset, obi, taker, nf_by_id, fresh=None):
    """计算单币资金流因子。

    fresh: 各源新鲜度（load_flow 第 4 个返回值）。**已断更的源不参与打分** ——
    否则会把两周前的数据当成今天的资金流，输出看似正常的"中性"评级。
    """
    fresh = fresh or {}
    mcap = asset.get("market_cap")
    o = obi.get(asset["symbol"].upper() + "USDT")
    t = taker.get(asset["symbol"].upper() + "USDT")
    n = nf_by_id.get(asset["asset_id"])
    out = {"obi_7d": None, "obi_30d": None, "taker_imb_7d": None, "taker_imb_30d": None,
           "netflow_7d_usd": None, "netflow_30d_usd": None, "netflow_7d_pct_mcap": None,
           "n_obi_7d": 0, "n_taker_7d": 0, "n_netflow_7d": 0}
    if o:
        out.update(obi_7d=o["v7"], obi_30d=o["v30"], n_obi_7d=o["n7"] or 0)
    if t:
        out.update(taker_imb_7d=t["v7"], taker_imb_30d=t["v30"], n_taker_7d=t["n7"] or 0)
    if n:
        out.update(netflow_7d_usd=n["v7"], netflow_30d_usd=n["v30"], n_netflow_7d=n["n7"] or 0)
        if n["v7"] is not None and mcap and mcap > 0:
            out["netflow_7d_pct_mcap"] = round(n["v7"] / mcap * 100, 6)
    # 断更源剔除：不参与加权，避免"用旧数据算出今天的中性"
    stale = []
    def _usable(label, value):
        if fresh.get(label, {}).get("stale"):
            if value is not None or fresh.get(label, {}).get("max_ts") is not None:
                stale.append(label)
            return None
        return value

    use_obi = _usable("OBI", out["obi_7d"])
    use_taker = _usable("OFI", out["taker_imb_7d"])
    use_nf = _usable("onchain", out["netflow_7d_pct_mcap"])

    out["flow_score"] = flow_score(use_obi, use_taker, use_nf)
    out["flow_grade"] = grade_of(out["flow_score"])
    out["freshness"] = fresh
    out["stale_sources"] = stale

    src = []
    if use_obi is not None:
        src.append("OBI")
    if use_taker is not None:
        src.append("taker")
    if use_nf is not None:
        src.append("onchain")
    out["sources"] = ",".join(src) or "none"
    if stale:
        out["sources"] += " | 已剔除断更源: " + ",".join(stale)
    return out


def persist(conn, asset, as_of, f) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO biz.asset_flow_factor (
            asset_id, as_of, obi_7d, obi_30d, taker_imb_7d, taker_imb_30d,
            netflow_7d_usd, netflow_30d_usd, netflow_7d_pct_mcap,
            n_obi_7d, n_taker_7d, n_netflow_7d, flow_score, flow_grade,
            sources, method_version, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s, %s,%s, NOW())
        ON CONFLICT (asset_id, as_of) DO UPDATE SET
            obi_7d=EXCLUDED.obi_7d, obi_30d=EXCLUDED.obi_30d,
            taker_imb_7d=EXCLUDED.taker_imb_7d, taker_imb_30d=EXCLUDED.taker_imb_30d,
            netflow_7d_usd=EXCLUDED.netflow_7d_usd, netflow_30d_usd=EXCLUDED.netflow_30d_usd,
            netflow_7d_pct_mcap=EXCLUDED.netflow_7d_pct_mcap,
            n_obi_7d=EXCLUDED.n_obi_7d, n_taker_7d=EXCLUDED.n_taker_7d,
            n_netflow_7d=EXCLUDED.n_netflow_7d, flow_score=EXCLUDED.flow_score,
            flow_grade=EXCLUDED.flow_grade, sources=EXCLUDED.sources,
            method_version=EXCLUDED.method_version, updated_at=NOW()
    """, (
        asset["asset_id"], as_of, f["obi_7d"], f["obi_30d"], f["taker_imb_7d"], f["taker_imb_30d"],
        f["netflow_7d_usd"], f["netflow_30d_usd"], f["netflow_7d_pct_mcap"],
        f["n_obi_7d"], f["n_taker_7d"], f["n_netflow_7d"], f["flow_score"], f["flow_grade"],
        f["sources"], METHOD_VERSION,
    ))


# ═══════════════════════════════════════════════════════════════
#  渲染
# ═══════════════════════════════════════════════════════════════

def f2(v, suffix=""):
    return f"{v:+.4f}{suffix}" if isinstance(v, (int, float)) else "-"


def f0(v, suffix=""):
    return f"{v:.0f}{suffix}" if isinstance(v, (int, float)) else "-"


def pct1(v):
    return f"{v*100:+.2f}%" if isinstance(v, (int, float)) else "-"


def render_console(asset, as_of, f):
    print("=" * 72)
    print(f"单币资金流因子 · {asset['name']}({asset['symbol']})  asset_id={asset['asset_id']}  as_of={as_of}")
    print("=" * 72)
    print(f"  资金流分: {f['flow_score']}    评级: {f['flow_grade']}    源: {f['sources']}")
    if f.get("stale_sources"):
        print("  ⚠️ 断更源(已剔除): " + ", ".join(f["stale_sources"]))
    print(f"  OBI 盘口失衡:  7d {f2(f['obi_7d'])} (n={f['n_obi_7d']}) | 30d {f2(f['obi_30d'])}")
    print(f"  OFI 主动成交:  7d {f2(f['taker_imb_7d'])} (n={f['n_taker_7d']}) | 30d {f2(f['taker_imb_30d'])}")
    print(f"  链上净流: 7d {f0(f['netflow_7d_usd'],' USD') if f['netflow_7d_usd'] is not None else '-'}"
          f" (n={f['n_netflow_7d']}) | 占市值 {f2(f['netflow_7d_pct_mcap'],'%') if f['netflow_7d_pct_mcap'] is not None else '-'}")


def render_md(asset, as_of, f):
    return "\n".join([
        f"### 资金流因子（{asset['name']}({asset['symbol']})）", "",
        f"> 口径: OBI 盘口失衡 (bids−asks)/(bids+asks) + OFI 主动成交失衡 + 链上净流；"
        f"各源以自身最新时点为锚取近 7d/30d ｜ method={METHOD_VERSION}",
        "",
        f"**资金流评级：{f['flow_grade']}（资金流分 {f['flow_score']}/100）**", "",
        *(["> ⚠️ **数据断更告警**：" + "；".join(
            "%s 最新 %s（滞后 %.1f 天，阈值 %dh）" % (
                k, v["max_ts"].strftime("%Y-%m-%d %H:%M") if v.get("max_ts") else "无数据",
                (v.get("lag_hours") or 0) / 24, STALE_HOURS[k])
            for k, v in (f.get("freshness") or {}).items() if v.get("stale")
        ) + "。**该源已从评分中剔除**，评级仅由剩余源得出，置信度相应下调。", ""]
          if f.get("stale_sources") else []),
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
        f"| OBI 盘口失衡 7d | {f2(f['obi_7d'])}"
        f"{'（**已剔除**）' if 'OBI' in (f.get('stale_sources') or []) else ''}"
        f" | >0 买盘占优（源 {f['n_obi_7d']} 点）|",
        f"| OFI 主动成交失衡 7d | {f2(f['taker_imb_7d'])}"
        f"{'（**已剔除**）' if 'OFI' in (f.get('stale_sources') or []) else ''}"
        f" | >0 主动买占优（源 {f['n_taker_7d']} 点）|",
        f"| 链上净流 7d | {f0(f['netflow_7d_usd'],' USD') if f['netflow_7d_usd'] is not None else '-'}"
        f" | 占市值 {f2(f['netflow_7d_pct_mcap'],'%') if f['netflow_7d_pct_mcap'] is not None else '-'}"
        f"（源 {f['n_netflow_7d']} 点）"
        f"{'（**已剔除**）' if 'onchain' in (f.get('stale_sources') or []) else ''}|",
        "",
        "> 局限: 三源时间跨度不一，以各自最新时点对齐；盘口/主动成交反映**当下**资金压力，非长期趋势。", "",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="单币资金流因子（投研 SOP P1.8）")
    ap.add_argument("--symbol")
    ap.add_argument("--asset-id", type=int)
    ap.add_argument("--as-of")
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--batch", action="store_true", help="全市场批量落库")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    with get_connection(get_settings(require_database=True).database_url) as conn:
        obi, taker, nf, fresh = load_flow(conn)
        assets = load_assets(conn)

        if args.batch:
            n_done = 0
            for sym_usdt, asset in assets.items():
                f = compute_one(asset, obi, taker, nf, fresh)
                if f["sources"] == "none":
                    continue
                persist(conn, asset, as_of, f)
                n_done += 1
                if n_done % 200 == 0:
                    conn.commit()
            conn.commit()
            print(json.dumps({"as_of": str(as_of), "n_persisted": n_done,
                              "n_obi_syms": len(obi), "n_taker_syms": len(taker),
                              "n_netflow_assets": len(nf)}, ensure_ascii=False, indent=2))
            return 0

        # 单币
        asset = None
        if args.asset_id:
            for a in assets.values():
                if a["asset_id"] == args.asset_id:
                    asset = a
                    break
        elif args.symbol:
            want = (args.symbol or "").upper()
            for a in assets.values():
                if (a["symbol"] or "").upper() == want:
                    asset = a
                    break
        if not asset:
            print(f"未找到标的: {args.symbol or args.asset_id}")
            return 1
        f = compute_one(asset, obi, taker, nf, fresh)
        if args.json:
            print(json.dumps({"asset": asset, "as_of": str(as_of), "flow": f},
                             ensure_ascii=False, indent=2, default=str))
        elif args.md:
            print(render_md(asset, as_of, f))
        else:
            render_console(asset, as_of, f)
        if args.persist:
            persist(conn, asset, as_of, f)
            print(f"\n[persist] 已写 biz.asset_flow_factor: asset_id={asset['asset_id']}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
