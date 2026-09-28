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

判定（§6.4）：p90 绝对差异 < T 且资金费率同向率 > 80% ⇒ injectable（可进入阶段二并行注入）；
否则 monitor_only（保留 Binance 主路、CG 仅作异常监测）；样本不足 ⇒ insufficient_data。
⚠️ 「同向率」= **两源同正负占比**（`_same_sign`），**不是**差值 `CG-BN` 的正负主导——
后者会把「两源符号相反」（最该拦下的危险态）误算成同向。
T 只留代码常量（`FUNDING_ABS_DIFF_P90_T`，当前未校准），不在文档留数字。

CLI：`--symbols` / `--limit`（默认全池）、`--hours`（默认 48h）、`--json`、`--probe`。

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

# §6.4 判定阈值：只留代码常量、不在文档留数字。当前值**未校准**（样本不足），
# 仅用于把「可进入阶段二并行注入」与「仅作异常监测」两态区分开，非统计显著结论。
FUNDING_ABS_DIFF_P90_T = 0.0005
SAME_DIRECTION_MIN_PCT = 80.0


def judge(funding_diff: dict, funding_rel: dict,
          t: float = FUNDING_ABS_DIFF_P90_T,
          same_min: float = SAME_DIRECTION_MIN_PCT) -> dict:
    """§6.4 判定规则（纯函数，可离线单测）：

    p90 绝对差异 < T 且资金费率同向率 > same_min ⇒ 可进入阶段二「并行注入」；
    否则 ⇒ 保留 Binance 主路、跨所源仅作异常监测。缺样本（p90/同向率为 None）
    ⇒ insufficient_data（不臆断方向，缺失≠0）。
    """
    p90 = funding_diff.get("p90")
    same = funding_diff.get("same_direction_pct")
    rel_p90 = funding_rel.get("p90")
    if p90 is None or same is None:
        return {
            "verdict": "insufficient_data",
            "reason": "资金费率配对样本不足，无法判定",
            "t": t, "same_direction_min_pct": same_min,
            "p90": p90, "same_direction_pct": same,
        }
    injectable = (p90 < t) and (same > same_min)
    return {
        "verdict": "injectable" if injectable else "monitor_only",
        "reason": (
            "p90 绝对差异 < T 且同向率 > 阈值 ⇒ 差异可接受，可进入阶段二并行注入"
            if injectable else
            "p90 绝对差异或同向率越阈 ⇒ 保留 Binance 主路，跨所源仅作异常监测"
        ),
        "t": t, "same_direction_min_pct": same_min,
        "p90": p90, "same_direction_pct": same, "rel_p90": rel_p90,
    }


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


def _same_sign(a: float, b: float) -> bool:
    """两源是否同正负（任一侧为 0 ⇒ 不算同向）。

    对应 §6.4「符号同向率（即**两源是否同正负**）」——注意**不是**差值 `a-b` 的正负主导：
    两源符号相反（如 CG=+0.0001 / BN=-0.0001）时差值恒为 `2a` 正号，旧算法会把它误算成同向。
    """
    return (a > 0 and b > 0) or (a < 0 and b < 0)


def summarize_pairs(pairs: list[dict], key: str, flag_key: str | None = None) -> dict:
    """对 pairs 中该 key 的差值做分位统计（纯函数，可离线单测）。

    `same_direction_pct` 取 `flag_key` 对应的**布尔同向标志**（两源同正负）占比，
    与差值自身正负无关；无 flag_key/无样本 ⇒ None（缺失≠0）。
    """
    vals = sorted([p[key] for p in pairs if key in p])
    out = {
        "n": len(vals),
        "p50": pctile(vals, 0.5),
        "p90": pctile(vals, 0.9),
        "p95": pctile(vals, 0.95),
        "same_direction_pct": None,
    }
    if flag_key:
        flags = [p[flag_key] for p in pairs if flag_key in p]
        if flags:
            out["same_direction_pct"] = round(sum(flags) / len(flags) * 100, 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CGV4-003 对账：coinglass_derivatives_snapshot vs asset_derivatives（只读）")
    ap.add_argument("--symbols", default="",
                    help="指定对比币（逗号分隔，本库合约码，如 BTCUSDT,1000PEPEUSDT）；默认全池")
    ap.add_argument("--limit", type=int, default=0,
                    help="只对比前 N 个币（按基码字典序，作用在 coinglass 侧；0 = 全池）")
    ap.add_argument("--hours", type=int, default=DEFAULT_LOOKBACK_H,
                    help=f"只看最近 N 小时快照（默认 {DEFAULT_LOOKBACK_H}h，覆盖 2 个日频周期）")
    ap.add_argument("--min-pairs", type=int, default=MIN_PAIRS,
                    help=f"最小配对样本数（默认 {MIN_PAIRS}）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--probe", action="store_true", help="仅检查两张表是否有数据")
    args = ap.parse_args()

    selected_bases = {base_code(s) for s in args.symbols.split(",") if s.strip()}

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

    # §6.4 --symbols / --limit（默认全池）：对「币种基码」维度收敛后再配对
    if selected_bases:
        cg_rows = {k: v for k, v in cg_rows.items() if k in selected_bases}
        bn_rows = {k: v for k, v in bn_rows.items() if k in selected_bases}
    if args.limit and args.limit > 0:
        cg_rows = dict(sorted(cg_rows.items())[: args.limit])

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
            rec["funding_same_sign"] = _same_sign(cg_f, bn_f)
        if cg_oi is not None and bn_oi is not None and bn_oi != 0:
            rec["oi_usd_rel_diff"] = (cg_oi - bn_oi) / bn_oi
            rec["oi_same_sign"] = _same_sign(cg_oi, bn_oi)
        if len(rec) > 1:
            pairs.append(rec)

    # 样本不足由本门槛先以 rc=1 拦下；judge() 的 insufficient_data 只是防御性分支
    # （仅在 summarize 返回 None 时触发，正常路径走不到），两者口径一致：缺失≠0、不臆断。
    if len(pairs) < args.min_pairs:
        print(f"[audit] 配对样本 {len(pairs)} < {args.min_pairs}，暂不输出差异。"
              f"coinglass 最近 {args.hours}h All 行 {len(cg_rows)} 币，"
              f"与 Binance 源重叠 {len(pairs)} 币。", file=sys.stderr)
        return 1

    funding_diff = summarize_pairs(pairs, "funding_rate_abs_diff", "funding_same_sign")
    funding_rel = summarize_pairs(pairs, "funding_rate_rel_diff", "funding_same_sign")
    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": args.hours,
        "symbols_filter": sorted(selected_bases) if selected_bases else None,
        "limit": args.limit or None,
        "pairs": len(pairs),
        "coinglass_all_rows": len(cg_rows),
        "binance_rows": len(bn_rows),
        "funding_rate_diff": funding_diff,
        "funding_rate_rel_diff": funding_rel,
        "oi_usd_rel_diff": summarize_pairs(pairs, "oi_usd_rel_diff", "oi_same_sign"),
        "judgment": judge(funding_diff, funding_rel),
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
        jd = result["judgment"]
        print(f"\n  判定规则：p90 绝对差异 < T({jd['t']}) 且同向率 > "
              f"{jd['same_direction_min_pct']}% ⇒ 可进入并行注入阶段；否则保留 Binance 主路、"
              "CG 仅作异常监测。")
        print(f"  判定结果：{jd['verdict']} —— {jd['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())