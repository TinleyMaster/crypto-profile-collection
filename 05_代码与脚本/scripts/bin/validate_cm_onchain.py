"""验证 Coin Metrics 链上日频指标入库结果。

用法：
    python validate_cm_onchain.py              # 全量验证
    python validate_cm_onchain.py --symbol btc # 仅验证 BTC
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


def validate_overview(conn) -> None:
    """验证总体统计。"""
    print("=" * 60)
    print("总体统计")
    print("=" * 60)

    with conn.cursor() as cur:
        # 总行数
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT asset_id), COUNT(DISTINCT cm_symbol) FROM biz.cm_asset_onchain_daily")
        row = cur.fetchone()
        print(f"总行数: {row[0]:,}")
        print(f"资产数: {row[1]}")
        print(f"币种数: {row[2]}")

        # 日期范围
        cur.execute("SELECT MIN(metric_date), MAX(metric_date), MAX(source_cutoff) FROM biz.cm_asset_onchain_daily")
        row = cur.fetchone()
        print(f"日期范围: {row[0]} ~ {row[1]}")
        print(f"数据截止: {row[2]}")

        # 各币种行数
        print("\n各币种行数:")
        cur.execute("""
            SELECT cm_symbol, COUNT(*) as cnt, MIN(metric_date), MAX(metric_date)
            FROM biz.cm_asset_onchain_daily
            GROUP BY cm_symbol
            ORDER BY cnt DESC
        """)
        for row in cur.fetchall():
            print(f"  {row[0]:8s} {row[1]:,} 行  {row[2]} ~ {row[3]}")


def validate_mvrv_extremes(conn, symbol: str | None = None) -> None:
    """验证 MVRV 极值分位（已知极值抽查）。"""
    print("\n" + "=" * 60)
    print("MVRV 极值抽查")
    print("=" * 60)

    where_clause = f"WHERE cm_symbol = '{symbol}'" if symbol else ""

    # BTC 2021-11 顶部应 ≥ 90 分位
    # BTC 2018-12 / 2022-11 底部应 ≤ 10 分位
    query = f"""
        SELECT cm_symbol, metric_date, cap_mvrv_cur,
               ROUND(100.0 * PERCENT_RANK() OVER (
                   PARTITION BY asset_id ORDER BY cap_mvrv_cur
               ), 2) AS mvrv_pct
        FROM biz.cm_asset_onchain_daily
        {where_clause}
        AND cap_mvrv_cur IS NOT NULL
        ORDER BY metric_date
    """

    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

        if not rows:
            print("  无数据")
            return

        # 找极值
        max_row = max(rows, key=lambda r: r[2] if r[2] else 0)
        min_row = min(rows, key=lambda r: r[2] if r[2] else float("inf"))

        print(f"  最高 MVRV: {max_row[0]} {max_row[1]} = {max_row[2]:.2f} (分位: {max_row[3]:.1f}%)")
        print(f"  最低 MVRV: {min_row[0]} {min_row[1]} = {min_row[2]:.2f} (分位: {min_row[3]:.1f}%)")

        # 检查已知极值
        if max_row[3] and max_row[3] < 90:
            print(f"  ⚠️ 最高 MVRV 分位 {max_row[3]:.1f}% < 90%（预期 ≥ 90%）")
        else:
            print(f"  ✅ 最高 MVRV 分位 {max_row[3]:.1f}% ≥ 90%")

        if min_row[3] and min_row[3] > 10:
            print(f"  ⚠️ 最低 MVRV 分位 {min_row[3]:.1f}% > 10%（预期 ≤ 10%）")
        else:
            print(f"  ✅ 最低 MVRV 分位 {min_row[3]:.1f}% ≤ 10%")


def validate_null_handling(conn) -> None:
    """验证空值处理。"""
    print("\n" + "=" * 60)
    print("空值处理验证")
    print("=" * 60)

    with conn.cursor() as cur:
        # 各列非空率
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(cap_mvrv_cur) as mvrv,
                COUNT(adr_act_cnt) as adr,
                COUNT(tx_tfr_cnt) as tx,
                COUNT(flow_in_ex_usd) as flow_in,
                COUNT(flow_out_ex_usd) as flow_out
            FROM biz.cm_asset_onchain_daily
        """)
        row = cur.fetchone()
        total = row[0]
        if total == 0:
            print("  无数据")
            return

        print(f"  总行数: {total:,}")
        print(f"  MVRV 非空: {row[1]:,} ({row[1]/total*100:.1f}%)")
        print(f"  活跃地址非空: {row[2]:,} ({row[2]/total*100:.1f}%)")
        print(f"  转账笔数非空: {row[3]:,} ({row[3]/total*100:.1f}%)")
        print(f"  交易所流入非空: {row[4]:,} ({row[4]/total*100:.1f}%)")
        print(f"  交易所流出非空: {row[5]:,} ({row[5]/total*100:.1f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(description="验证 CM 链上日频指标入库结果")
    parser.add_argument("--symbol", type=str, default=None, help="仅验证指定币种")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        validate_overview(conn)
        validate_mvrv_extremes(conn, args.symbol)
        validate_null_handling(conn)

    print("\n" + "=" * 60)
    print("验证完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
