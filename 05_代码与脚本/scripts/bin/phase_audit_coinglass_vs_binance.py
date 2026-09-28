#!/usr/bin/env python3
"""CoinGlass 跨所衍生品快照 vs Binance 单所源 纯只读对账（CGV4-003 消费侧阶段一）。

关联：04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md §6.4

定位：
  - 不改任何既有链路，不写入任何表，只读 `biz.coinglass_derivatives_snapshot` 与
    `biz.asset_derivatives` 并输出差异分布。
  - 回答：跨所聚合/分所衍生品数据与 Binance 单所源偏离多少？能否替代或只能当备用？

输入口径（因两张表 symbol 不同）：
  - coinglass 表：symbol = 币种基码（BTC / 1000PEPE）
  - asset_derivatives 表：symbol = 本库合约码（BTCUSDT / 1000PEPEUSDT）
  对账时用 `REPLACE(symbol, 'USDT', '')` 等逆向映射；1000PEPE 等前缀币需去掉计价后缀后
  再比较。

输出指标：
  - funding_rate_abs_diff  / funding_rate_rel_diff
  - oi_usd_rel_diff（Binance 源取 asset_derivatives.total_oi_usd）
  - 差异 p50/p90/p95 与同向率

退出码：
  0 = 对账完成；1 = 数据不足；2 = 参数/连接错误
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

DEFAULT_LOOKBACK_H = 48
MIN_PAIRS = 30


def base_code(contract_symbol: str) -> str:
    """合约码 → 币种基码（与 ingest_coinglass_derivatives.base_code 同逻辑）。"""
    s = contract_symbol.upper()
    for quote in ("USDT", "USDC", "BUSD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


SQL_COINGLASS_LATEST = """
    SELECT symbol,
           (array_agg(funding_rate ORDER BY ts DESC))[1]   AS funding_rate,
           (array_agg(oi_usd ORDER BY ts DESC))[1]         AS oi_usd,
           (array_agg(exchange ORDER BY ts DESC))[1]        AS exchange,
           max(ts) AS last_ts
    FROM biz.coinglass_derivatives_snapshot
    WHERE ts >= NOW() - make_interval(hours => %s)
      AND exchange = 'All'
    GROUP BY symbol
"""

SQL_BINANCE_LATEST = """
    SELECT symbol, funding_rate, total_oi_usd, fetched_at
    FROM (
        SELECT symbol, funding_rate, total_oi_usd, fetched_at,
               row_number() OVER (PARTITION BY symbol ORDER BY fetched_at DESC) AS rn
        FROM biz.asset_derivatives
        WHERE fetched_at >= NOW() - make_interval(hours => %s)
    ) t
    WHERE rn = 1
"""


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pctile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    k = (n - 1) * p
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CGV4-003 对账：coinglass_derivatives_snapshot vs asset_derivatives（只读）")
    ap.add_argument("--hours", type=int, default=DEFAULT_LOOKBACK_H,
                    help=f"只看最近 N 小时快照（默认 {DEFAULT_LOOKBACK_H}h，覆盖 2 个日频周期）")
    ap.add_argument("--min-pairs", type=int, default=MIN_PAIRS,
                    help=f"最小配对样本数（默认 {MIN_PAIRS}）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--probe", action="store_true", help="仅检查两张表是否有数据")
    args = ap.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT count(*) AS n, max(ts) AS last FROM biz.coinglass_derivatives_snapshot")
            cg_meta = cur.fetchone()
            cur.execute(
                "SELECT count(*) AS n, max(fetched_at) AS last FROM biz.asset_derivatives")
            bn_meta = cur.fetchone()

            if args.probe:
                out = {
                    "coinglass": {"rows": cg_meta["n"], "last_ts": cg_meta["last"]},
                    "binance": {"rows": bn_meta["n"], "last_ts": bn_meta["last"]},
                    "ready": cg_meta["n"] is not None and cg_meta["n"] > 0
                             and bn_meta["n"] is not None and bn_meta["n"] > 0,
                }
                print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
                return 0

            if not cg_meta["n"]:
                print("[audit] biz.coinglass_derivatives_snapshot 为空 ⇒ 无法对账。"
                      "需等调度跑 2–3 天或手动跑一次 ingest_coinglass_derivatives.py", file=sys.stderr)
                return 1
            if not bn_meta["n"]:
                print("[audit] biz.asset_derivatives 为空 ⇒ 无法对账", file=sys.stderr)
                return 1

            cur.execute(SQL_COINGLASS_LATEST, (args.hours,))
            cg_rows = {r["symbol"]: r for r in cur.fetchall()}

            cur.execute(SQL_BINANCE_LATEST, (args.hours,))
            bn_rows = {base_code(r["symbol"]): r for r in cur.fetchall()}

    pairs = []
    for base, cg in cg_rows.items():
        bn = bn_rows.get(base)
        if not bn:
            continue
        cg_f = _num(cg.get("funding_rate"))
        bn_f = _num(bn.get("funding_rate"))
        cg_oi = _num(cg.get("oi_usd"))
        bn_oi = _num(bn.get("total_oi_usd"))
        rec = {"symbol": base}
        if cg_f is not None and bn_f is not None:
            rec["funding_rate_abs_diff"] = cg_f - bn_f
            denom = abs(bn_f) if bn_f != 0 else 1e-12
            rec["funding_rate_rel_diff"] = (cg_f - bn_f) / denom
        if cg_oi is not None and bn_oi is not None and bn_oi != 0:
            rec["oi_usd_rel_diff"] = (cg_oi - bn_oi) / bn_oi
        if len(rec) > 1:
            pairs.append(rec)

    if len(pairs) < args.min_pairs:
        print(f"[audit] 配对样本 {len(pairs)} < {args.min_pairs}，暂不输出差异。"
              f"coinglass 最近 {args.hours}h All 行 {len(cg_rows)} 币，"
              f"与 Binance 源重叠 {len(pairs)} 币。", file=sys.stderr)
        return 1

    def summarize(key: str) -> dict:
        vals = sorted([p[key] for p in pairs if key in p])
        pos = sum(1 for v in vals if v > 0)
        neg = sum(1 for v in vals if v < 0)
        return {
            "n": len(vals),
            "p50": pctile(vals, 0.5),
            "p90": pctile(vals, 0.9),
            "p95": pctile(vals, 0.95),
            "same_direction_pct": round(max(pos, neg) / len(vals) * 100, 1) if vals else None,
        }

    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": args.hours,
        "pairs": len(pairs),
        "coinglass_all_rows": len(cg_rows),
        "binance_rows": len(bn_rows),
        "funding_rate_diff": summarize("funding_rate_abs_diff"),
        "funding_rate_rel_diff": summarize("funding_rate_rel_diff"),
        "oi_usd_rel_diff": summarize("oi_usd_rel_diff"),
        "samples": pairs[:20] if args.json else None,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"\n[cg-audit] 对账时刻 UTC {result['ts']}")
        print(f"  配对样本：{result['pairs']} 币（coinglass All 行 {result['coinglass_all_rows']}，"
              f"Binance 源 {result['binance_rows']}）")
        print(f"  资金费率绝对差（CG - BN）：n={result['funding_rate_diff']['n']}，"
              f"p50={result['funding_rate_diff']['p50']:.6f}，"
              f"p90={result['funding_rate_diff']['p90']:.6f}，"
              f"p95={result['funding_rate_diff']['p95']:.6f}，"
              f"同向率={result['funding_rate_diff']['same_direction_pct']}%")
        print(f"  资金费率相对差：n={result['funding_rate_rel_diff']['n']}，"
              f"p50={result['funding_rate_rel_diff']['p50']:.2%}，"
              f"p90={result['funding_rate_rel_diff']['p90']:.2%}，"
              f"p95={result['funding_rate_rel_diff']['p95']:.2%}")
        print(f"  OI  相对差：n={result['oi_usd_rel_diff']['n']}，"
              f"p50={result['oi_usd_rel_diff']['p50']:.2%}，"
              f"p90={result['oi_usd_rel_diff']['p90']:.2%}，"
              f"p95={result['oi_usd_rel_diff']['p95']:.2%}")
        print("\n  判定规则：p90 绝对差异 < T 且同向率 > 80% ⇒ 可进入并行注入阶段；"
              "否则保留 Binance 主路、CG 仅作异常监测。")
    return 0


if __name__ == "__main__":
    sys.exit(main())