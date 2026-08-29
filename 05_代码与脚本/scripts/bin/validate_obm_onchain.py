"""验证脚本：OBM + CM 链上指标数据质量检查。

验证内容：
1. OBM supply 单调不减
2. BTC MVRV 锚点验证（2021-11=2.721, 2018-12=0.690, 2022-11=0.778）
3. sol/bnb 不在 CM 表
4. dropna 后分位非空率

用法：
    python validate_obm_onchain.py                     # 运行所有验证
    python validate_obm_onchain.py --check obm         # 仅验证 OBM
    python validate_obm_onchain.py --check cm          # 仅验证 CM
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

# 验证 SQL
VALIDATE_OBM_SUPPLY_MONOTONIC_SQL = """
WITH supply_data AS (
    SELECT metric_date, value,
           LAG(value) OVER (ORDER BY metric_date) as prev_value
    FROM biz.obm_btc_daily
    WHERE metric_name = 'obm_supply_btc_daily'
      AND value IS NOT NULL
)
SELECT COUNT(*) as violations
FROM supply_data
WHERE value < prev_value
"""

VALIDATE_OBM_METRIC_COUNT_SQL = """
SELECT COUNT(DISTINCT metric_name) as metric_count
FROM biz.obm_btc_daily
"""

VALIDATE_OBM_MAX_DATE_SQL = """
SELECT MAX(metric_date) as max_date
FROM biz.obm_btc_daily
"""

VALIDATE_BTC_MVRV_ANCHORS_SQL = """
SELECT metric_date, value
FROM biz.obm_btc_daily
WHERE metric_name = 'obm_mvrv_btc_daily'
  AND metric_date IN ('2021-11-10', '2018-12-15', '2022-11-21')
ORDER BY metric_date
"""

VALIDATE_CM_NO_EXCLUDED_SQL = """
SELECT cm_symbol, COUNT(*) as cnt
FROM biz.cm_asset_onchain_daily
WHERE cm_symbol IN ('bnb', 'sol')
GROUP BY cm_symbol
"""

VALIDATE_CM_DISTRIBUTION_SQL = """
SELECT cm_symbol, COUNT(*) as cnt
FROM biz.cm_asset_onchain_daily
GROUP BY cm_symbol
ORDER BY cm_symbol
"""

VALIDATE_NULL_RATE_SQL = """
SELECT
    metric_name,
    COUNT(*) as total_rows,
    COUNT(value) as non_null_rows,
    ROUND(100.0 * COUNT(value) / NULLIF(COUNT(*), 0), 2) as non_null_pct
FROM biz.obm_btc_daily
GROUP BY metric_name
ORDER BY metric_name
"""


def validate_obm(conn) -> bool:
    """验证 OBM 数据质量。"""
    print("=" * 50)
    print("OBM 验证")
    print("=" * 50)

    all_passed = True

    # 1. 检查指标数量
    with conn.cursor() as cur:
        cur.execute(VALIDATE_OBM_METRIC_COUNT_SQL)
        row = cur.fetchone()
        count = row[0] if row else 0
        if count == 23:
            print(f"  [OK] 指标数量：{count}/23")
        else:
            print(f"  [WARN] 指标数量：{count}/23（预期 23）")
            all_passed = False

    # 2. 检查最大日期
    with conn.cursor() as cur:
        cur.execute(VALIDATE_OBM_MAX_DATE_SQL)
        row = cur.fetchone()
        max_date = row[0] if row else None
        if max_date and str(max_date) == "2026-08-24":
            print(f"  [OK] 最大日期：{max_date}")
        else:
            print(f"  [WARN] 最大日期：{max_date}（预期 2026-08-24）")
            all_passed = False

    # 3. 检查 supply 单调性
    with conn.cursor() as cur:
        cur.execute(VALIDATE_OBM_SUPPLY_MONOTONIC_SQL)
        row = cur.fetchone()
        violations = row[0] if row else 0
        if violations == 0:
            print(f"  [OK] Supply 单调不减：无违规")
        else:
            print(f"  [ERROR] Supply 单调性：{violations} 处违规")
            all_passed = False

    # 4. 检查 MVRV 锚点
    with conn.cursor() as cur:
        cur.execute(VALIDATE_BTC_MVRV_ANCHORS_SQL)
        rows = cur.fetchall()
        anchors = {str(r[0]): r[1] for r in rows}

        expected = {
            "2021-11-10": (2.7, 2.8),  # 预期 2.721，允许误差
            "2018-12-15": (0.6, 0.8),  # 预期 0.690
            "2022-11-21": (0.7, 0.9),  # 预期 0.778
        }

        for date, (low, high) in expected.items():
            if date in anchors:
                val = float(anchors[date])
                if low <= val <= high:
                    print(f"  [OK] MVRV 锚点 {date}：{val:.3f}")
                else:
                    print(f"  [WARN] MVRV 锚点 {date}：{val:.3f}（预期 {low}-{high}）")
                    all_passed = False
            else:
                print(f"  [WARN] MVRV 锚点 {date}：缺失")
                all_passed = False

    # 5. 检查空值率
    with conn.cursor() as cur:
        cur.execute(VALIDATE_NULL_RATE_SQL)
        rows = cur.fetchall()
        print("\n  空值率统计：")
        for row in rows:
            metric, total, non_null, pct = row
            if pct and float(pct) >= 90:
                print(f"    {metric}: {pct}%")
            else:
                print(f"    {metric}: {pct}% (空值较多)")

    return all_passed


def validate_cm(conn) -> bool:
    """验证 CM 数据质量。"""
    print("\n" + "=" * 50)
    print("CM 验证")
    print("=" * 50)

    all_passed = True

    # 1. 检查排除的币种
    with conn.cursor() as cur:
        cur.execute(VALIDATE_CM_NO_EXCLUDED_SQL)
        rows = cur.fetchall()
        if not rows:
            print(f"  [OK] bnb/sol 已排除")
        else:
            print(f"  [ERROR] 仍有排除币种：{[r[0] for r in rows]}")
            all_passed = False

    # 2. 检查币种分布
    with conn.cursor() as cur:
        cur.execute(VALIDATE_CM_DISTRIBUTION_SQL)
        rows = cur.fetchall()
        print("\n  币种分布：")
        for row in rows:
            print(f"    {row[0]}: {row[1]} 行")

    return all_passed


def main() -> None:
    parser = argparse.ArgumentParser(description="验证 OBM + CM 数据质量")
    parser.add_argument("--check", type=str, choices=["obm", "cm", "all"], default="all",
                       help="验证范围")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        if args.check in ("obm", "all"):
            obm_ok = validate_obm(conn)
        else:
            obm_ok = True

        if args.check in ("cm", "all"):
            cm_ok = validate_cm(conn)
        else:
            cm_ok = True

    print("\n" + "=" * 50)
    if obm_ok and cm_ok:
        print("✅ 所有验证通过")
        sys.exit(0)
    else:
        print("❌ 部分验证失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
