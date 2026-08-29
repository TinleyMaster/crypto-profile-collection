"""筛选 Coin Metrics Community 档达标主流币（放宽版）。

扫描 github.com/coinmetrics/data 仓库的 CSV 目录，筛选出：
- 历史 ≥ 730 交易日（约 2 年）
- 核心链上列（MVRV + 活跃地址）非空率 ≥ 70%
- 交易所净流为可选维度（缺失不阻断）

输出：达标主流币清单 JSON 文件。

用法：
    python filter_cm_major_coins.py                    # 全量扫描
    python filter_cm_major_coins.py --top 20           # 仅扫描 top 20 币种
    python filter_cm_major_coins.py --output list.json # 指定输出文件
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from urllib.request import urlopen, Request

# Coin Metrics CSV 基础 URL（raw GitHub）
CM_CSV_BASE = "https://raw.githubusercontent.com/coinmetrics/data/master/csv"

# 核心链上列（必须达标，否则整币 SKIP）
CORE_ONCHAIN_COLS = [
    "CapMVRVCur",     # MVRV
    "AdrActCnt",      # 活跃地址
]

# 可选链上列（缺失不阻断，入库时写 NULL）
OPTIONAL_ONCHAIN_COLS = [
    "FlowInExUSD",    # 交易所流入
    "FlowOutExUSD",   # 交易所流出
]

# 最小历史天数阈值
MIN_HISTORY_DAYS = 730

# 核心列非空率阈值（放宽至 0.70）
MIN_NONNULL_RATIO = 0.70

# 候选币种列表（主流币优先，可扩展）
CANDIDATE_COINS = [
    "btc", "eth", "ada", "xrp", "sol", "dot", "avax", "matic", "link",
    "uni", "aave", "atom", "ltc", "bch", "etc", "xlm", "algo", "near",
    "apt", "arb", "op", "fil", "icp", "sand", "mana", "axs", "gala",
    "enj", "chz", "shib", "doge", "pepe", "bonk", "ordi",
]


def fetch_csv_content(symbol: str) -> str | None:
    """下载指定币种的 CSV 内容。失败返回 None。"""
    url = f"{CM_CSV_BASE}/{symbol}.csv"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  [WARN] {symbol}: 下载失败 - {e}", file=sys.stderr)
        return None


def analyze_csv(csv_content: str, symbol: str) -> dict | None:
    """分析 CSV 内容，返回达标信息。不达标返回 None。"""
    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)

    if not rows:
        return None

    total_rows = len(rows)

    # 检查历史天数
    if total_rows < MIN_HISTORY_DAYS:
        return None

    # 检查核心列非空率
    core_stats = {}
    for col in CORE_ONCHAIN_COLS:
        if col not in rows[0]:
            # 核心列缺失，整币 SKIP
            return None
        nonnull_count = sum(1 for row in rows if row.get(col, "") not in ("", "null", "NaN"))
        ratio = nonnull_count / total_rows if total_rows > 0 else 0.0
        core_stats[col] = {"nonnull": nonnull_count, "total": total_rows, "ratio": ratio}

    # 检查核心列非空率是否达标
    for col, stats in core_stats.items():
        if stats["ratio"] < MIN_NONNULL_RATIO:
            return None

    # 检查可选列（统计但不阻断）
    optional_stats = {}
    has_flow = False
    for col in OPTIONAL_ONCHAIN_COLS:
        if col not in rows[0]:
            optional_stats[col] = {"nonnull": 0, "total": total_rows, "ratio": 0.0}
            continue
        nonnull_count = sum(1 for row in rows if row.get(col, "") not in ("", "null", "NaN"))
        ratio = nonnull_count / total_rows if total_rows > 0 else 0.0
        optional_stats[col] = {"nonnull": nonnull_count, "total": total_rows, "ratio": ratio}
        if ratio > 0.5:
            has_flow = True

    # 提取日期范围
    first_date = rows[0].get("time", "")[:10]
    last_date = rows[-1].get("time", "")[:10]

    return {
        "symbol": symbol,
        "history_days": total_rows,
        "first_date": first_date,
        "last_date": last_date,
        "has_flow": has_flow,
        "core_stats": {k: {"nonnull": v["nonnull"], "ratio": round(v["ratio"], 3)} for k, v in core_stats.items()},
        "optional_stats": {k: {"nonnull": v["nonnull"], "ratio": round(v["ratio"], 3)} for k, v in optional_stats.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="筛选 CM Community 档达标主流币（放宽版）")
    parser.add_argument("--top", type=int, default=None, help="仅扫描 top N 候选币种")
    parser.add_argument("--output", type=str, default="cm_major_coins.json", help="输出文件路径")
    args = parser.parse_args()

    candidates = CANDIDATE_COINS[:args.top] if args.top else CANDIDATE_COINS

    print(f"扫描 {len(candidates)} 个候选币种...")
    print(f"达标条件：历史 ≥ {MIN_HISTORY_DAYS}d，核心列(MVRV+活跃地址)非空率 ≥ {MIN_NONNULL_RATIO}")
    print(f"可选列：交易所净流（缺失不阻断）\n")

    qualified = []
    for i, symbol in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {symbol}...", end=" ", flush=True)
        csv_content = fetch_csv_content(symbol)
        if csv_content is None:
            print("SKIP (下载失败)")
            continue

        result = analyze_csv(csv_content, symbol)
        if result is None:
            print("SKIP (不达标)")
            continue

        flow_str = "✅有净流" if result["has_flow"] else "❌无净流"
        mvrv_pct = result["core_stats"].get("CapMVRVCur", {}).get("ratio", 0) * 100
        adr_pct = result["core_stats"].get("AdrActCnt", {}).get("ratio", 0) * 100
        print(f"OK ({result['history_days']}d, MVRV={mvrv_pct:.0f}%, 活跃={adr_pct:.0f}%, {flow_str})")
        qualified.append(result)

    # 输出结果
    output = {
        "total_candidates": len(candidates),
        "qualified_count": len(qualified),
        "min_history_days": MIN_HISTORY_DAYS,
        "min_nonnull_ratio": MIN_NONNULL_RATIO,
        "coins": qualified,
    }

    output_path = Path(args.output)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n筛选完成：{len(qualified)}/{len(candidates)} 达标")
    print(f"结果已写入：{output_path}")


if __name__ == "__main__":
    main()
