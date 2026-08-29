"""CM 范围收缩脚本：排除 bnb/sol，仅保留 btc,eth,doge,xrp,ada。

根据质检结论，sol 仅 8 列全缺、bnb MVRV 80% 空，需显式排除。
本脚本提供：
1. 白名单过滤（仅入库达标币种）
2. 净流列 NULL 标注（doge/xrp/ada 无净流）

用法：
    python cm_range_consolidate.py --dry-run            # 预览，不写入
    python cm_range_consolidate.py --execute            # 执行范围收缩
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

# 白名单：仅保留这些币种
ALLOWED_ASSETS = {"btc", "eth", "doge", "xrp", "ada"}

# 排除列表（显式排除）
EXCLUDED_ASSETS = {"bnb", "sol"}

# 清理 SQL：删除不在白名单中的币种
DELETE_EXCLUDED_SQL = """
DELETE FROM biz.cm_asset_onchain_daily
WHERE cm_symbol NOT IN ('btc', 'eth', 'doge', 'xrp', 'ada')
"""

# 标注 doge/xrp/ada 净流列为 NULL（CM 无此币净流）
UPDATE_NULL_FLOW_SQL = """
UPDATE biz.cm_asset_onchain_daily
SET flow_in_ex_usd = NULL, flow_out_ex_usd = NULL
WHERE cm_symbol IN ('doge', 'xrp', 'ada')
  AND (flow_in_ex_usd IS NOT NULL OR flow_out_ex_usd IS NOT NULL)
"""

# 验证 SQL：检查是否还有 bnb/sol
VERIFY_NO_EXCLUDED_SQL = """
SELECT cm_symbol, COUNT(*) as cnt
FROM biz.cm_asset_onchain_daily
WHERE cm_symbol IN ('bnb', 'sol')
GROUP BY cm_symbol
"""

# 验证 SQL：检查币种分布
VERIFY_DISTRIBUTION_SQL = """
SELECT cm_symbol, COUNT(*) as cnt, MIN(metric_date) as min_date, MAX(metric_date) as max_date
FROM biz.cm_asset_onchain_daily
GROUP BY cm_symbol
ORDER BY cm_symbol
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="CM 范围收缩：排除 bnb/sol")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    parser.add_argument("--execute", action="store_true", help="执行范围收缩")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("请指定 --dry-run 或 --execute", file=sys.stderr)
        sys.exit(1)

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 1. 检查当前状态
        print("=" * 50)
        print("当前币种分布：")
        with conn.cursor() as cur:
            cur.execute(VERIFY_DISTRIBUTION_SQL)
            rows = cur.fetchall()
            for row in rows:
                print(f"  {row[0]}: {row[1]} 行 ({row[2]} ~ {row[3]})")

        # 2. 检查是否有 bnb/sol
        with conn.cursor() as cur:
            cur.execute(VERIFY_NO_EXCLUDED_SQL)
            excluded = cur.fetchall()
            if excluded:
                print(f"\n发现需排除的币种：{[r[0] for r in excluded]}")
            else:
                print("\n未发现 bnb/sol，范围已收缩")

        if args.dry_run:
            print("\n[DRY-RUN] 将执行以下操作：")
            print(f"  1. 删除 bnb/sol 数据")
            print(f"  2. 标注 doge/xrp/ada 净流列为 NULL")
            return

        # 3. 执行范围收缩
        print("\n执行范围收缩...")
        with conn.cursor() as cur:
            # 删除排除的币种
            cur.execute(DELETE_EXCLUDED_SQL)
            deleted = cur.rowcount
            print(f"  删除 bnb/sol 数据：{deleted} 行")

            # 标注净流列为 NULL
            cur.execute(UPDATE_NULL_FLOW_SQL)
            updated = cur.rowcount
            print(f"  标注 doge/xrp/ada 净流列为 NULL：{updated} 行")

        # 4. 验证结果
        print("\n验证结果：")
        with conn.cursor() as cur:
            cur.execute(VERIFY_NO_EXCLUDED_SQL)
            excluded = cur.fetchall()
            if excluded:
                print(f"  [ERROR] 仍有排除币种：{[r[0] for r in excluded]}")
            else:
                print("  [OK] bnb/sol 已排除")

        with conn.cursor() as cur:
            cur.execute(VERIFY_DISTRIBUTION_SQL)
            rows = cur.fetchall()
            print("\n最终币种分布：")
            for row in rows:
                print(f"  {row[0]}: {row[1]} 行 ({row[2]} ~ {row[3]})")

    print("\n范围收缩完成")


if __name__ == "__main__":
    main()
