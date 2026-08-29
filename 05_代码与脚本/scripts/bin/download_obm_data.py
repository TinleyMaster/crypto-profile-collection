"""从 GitHub 下载 OBM BTC 链上指标数据。

从 github.com/diegorllanos/open-bitcoin-metrics 下载 23 项 BTC 链上指标 CSV。

用法：
    python download_obm_data.py                       # 下载到当前目录
    python download_obm_data.py --out /path/to/dir    # 指定输出目录
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.request import urlopen, Request

# OBM GitHub 基础 URL
OBM_BASE_URL = "https://raw.githubusercontent.com/diegorllanos/open-bitcoin-metrics/main/metrics"

# 23 个指标名
METRICS = [
    "obm_block_count_daily",
    "obm_block_weight_wu_daily",
    "obm_cdd_age_band_btcxdays_daily",
    "obm_cdd_btcxdays_daily",
    "obm_cdd_per_supply_days_daily",
    "obm_difficulty_eod_daily",
    "obm_dormancy_days_daily",
    "obm_est7d_hashrate_ehs_daily",
    "obm_fee_share_revenue_ratio_daily",
    "obm_fees_btc_daily",
    "obm_issuance_btc_daily",
    "obm_liveliness_ratio_daily",
    "obm_miner_revenue_btc_daily",
    "obm_raw_output_value_btc_daily",
    "obm_spent_output_count_daily",
    "obm_spent_value_age_band_btc_daily",
    "obm_spent_value_btc_daily",
    "obm_spent_value_ge155d_btc_daily",
    "obm_spent_value_ge365d_btc_daily",
    "obm_spent_value_lt155d_btc_daily",
    "obm_supply_btc_daily",
    "obm_tx_count_daily",
    "obm_utxo_eod_count_daily",
]


def download_metric(metric_name: str, out_dir: Path) -> bool:
    """下载单个指标 CSV。成功返回 True。"""
    url = f"{OBM_BASE_URL}/{metric_name}/{metric_name}.csv"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=60) as resp:
            content = resp.read()
        
        out_file = out_dir / f"{metric_name}.csv"
        out_file.write_bytes(content)
        return True
    except Exception as e:
        print(f"  [ERROR] {metric_name}: {e}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="从 GitHub 下载 OBM BTC 链上指标数据")
    parser.add_argument("--out", type=str, default=".", help="输出目录")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"下载 {len(METRICS)} 个 OBM 指标到 {out_dir}...")
    
    success = 0
    for i, metric in enumerate(METRICS, 1):
        print(f"[{i}/{len(METRICS)}] {metric}...", end=" ", flush=True)
        if download_metric(metric, out_dir):
            # 读取行数
            csv_file = out_dir / f"{metric}.csv"
            lines = csv_file.read_text().count("\n")
            print(f"OK ({lines} 行)")
            success += 1
        else:
            print("FAIL")

    print(f"\n下载完成：{success}/{len(METRICS)} 成功")
    
    if success < len(METRICS):
        sys.exit(1)


if __name__ == "__main__":
    main()
