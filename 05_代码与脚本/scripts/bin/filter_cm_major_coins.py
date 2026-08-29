"""筛选 Coin Metrics Community 档达标主流币。

扫描 github.com/coinmetrics/data 仓库的 CSV 目录，筛选出：
- 历史 ≥ 730 交易日（约 2 年）
- 关键链上列非空率 ≥ 80%

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

# 关键链上列（非空率阈值检查用）
KEY_ONCHAIN_COLS = [
    "AdrActCnt",      # 活跃地址
    "TxTfrCnt",       # 转账笔数
    "CapMVRVCur",     # MVRV
    "FlowInExUSD",    # 交易所流入
    "FlowOutExUSD",   # 交易所流出
]

# 最小历史天数阈值
MIN_HISTORY_DAYS = 730

# 关键列非空率阈值
MIN_NONNULL_RATIO = 0.80

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

    # 检查关键列非空率
    col_stats = {}
    for col in KEY_ONCHAIN_COLS:
        if col not in rows[0]:
            col_stats[col] = {"nonnull": 0, "total": total_rows, "ratio": 0.0}
            continue
        nonnull_count = sum(1 for row in rows if row.get(col, "") not in ("", "null", "NaN"))
        ratio = nonnull_count / total_rows if total_rows > 0 else 0.0
        col_stats[col] = {"nonnull": nonnull_count, "total": total_rows, "ratio": ratio}

    # 检查非空率是否达标
    for col, stats in col_stats.items():
        if stats["ratio"] < MIN_NONNULL_RATIO:
            return None

    # 提取日期范围
    first_date = rows[0].get("time", "")[:10]
    last_date = rows[-1].get("time", "")[:10]

    return {
        "symbol": symbol,
        "history_days": total_rows,
        "first_date": first_date,
        "last_date": last_date,
        "col_stats": {k: {"nonnull": v["nonnull"], "ratio": round(v["ratio"], 3)} for k, v in col_stats.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="筛选 CM Community 档达标主流币")
    parser.add_argument("--top", type=int, default=None, help="仅扫描 top N 候选币种")
    parser.add_argument("--output", type=str, default="cm_major_coins.json", help="输出文件路径")
    args = parser.parse_args()

    candidates = CANDIDATE_COINS[:args.top] if args.top else CANDIDATE_COINS

    print(f"扫描 {len(candidates)} 个候选币种...")
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

        print(f"OK ({result['history_days']}d, {result['first_date']}~{result['last_date']})")
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
