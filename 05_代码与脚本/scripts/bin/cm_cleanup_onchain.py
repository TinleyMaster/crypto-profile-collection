"""清理 CM 链上数据：删除不达标币种，修复 source_cutoff。

根据放宽版筛选结果（15 币），清理：
1. 删除 sol/dot/avax/matic/near/fil/op/apt/arb/atom 等不达标币种
2. 修复 matic 截止日期（2025-11-12）
3. 补充 mana（漏掉的达标币）

用法：
    python cm_cleanup_onchain.py --dry-run    # 预览
    python cm_cleanup_onchain.py --execute    # 执行
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

# 放宽版达标列表（15 币）
ALLOWED_COINS = {"btc", "eth", "ada", "xrp", "link", "uni", "aave", "ltc", "bch",
                 "etc", "xlm", "algo", "icp", "mana", "doge"}

# 需要删除的币种（不达标）
DELETE_COINS_SQL = """
DELETE FROM biz.cm_asset_onchain_daily
WHERE cm_symbol NOT IN ('btc', 'eth', 'ada', 'xrp', 'link', 'uni', 'aave', 'ltc', 'bch',
                         'etc', 'xlm', 'algo', 'icp', 'mana', 'doge')
"""

# 修复 matic 截止日期
FIX_MATIC_CUTOFF_SQL = """
UPDATE biz.cm_asset_onchain_daily
SET source_cutoff = '2025-11-12'
WHERE cm_symbol = 'matic'
  AND source_cutoff != '2025-11-12'
"""

# 验证 SQL
VERIFY_DISTRIBUTION_SQL = """
SELECT cm_symbol, COUNT(*) as cnt, MIN(metric_date) as min_date, MAX(metric_date) as max_date
FROM biz.cm_asset_onchain_daily
GROUP BY cm_symbol
ORDER BY cm_symbol
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="清理 CM 链上数据")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    parser.add_argument("--execute", action="store_true", help="执行清理")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("请指定 --dry-run 或 --execute", file=sys.stderr)
        sys.exit(1)

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 1. 检查当前状态
        print("=" * 50)
        print("清理前币种分布：")
        with conn.cursor() as cur:
            cur.execute(VERIFY_DISTRIBUTION_SQL)
            rows = cur.fetchall()
            for row in rows:
                marker = "✅" if row[0] in ALLOWED_COINS else "❌"
                print(f"  {marker} {row[0]}: {row[1]} 行 ({row[2]} ~ {row[3]})")

        if args.dry_run:
            print("\n[DRY-RUN] 将执行以下操作：")
            print(f"  1. 删除不达标币种数据")
            print(f"  2. 修复 matic 截止日期为 2025-11-12")
            return

        # 2. 执行清理
        print("\n执行清理...")
        with conn.cursor() as cur:
            # 删除不达标币种
            cur.execute(DELETE_COINS_SQL)
            deleted = cur.rowcount
            print(f"  删除不达标币种数据：{deleted} 行")

            # 修复 matic 截止日期
            cur.execute(FIX_MATIC_CUTOFF_SQL)
            updated = cur.rowcount
            print(f"  修复 matic 截止日期：{updated} 行")

        # 3. 验证结果
        print("\n清理后币种分布：")
        with conn.cursor() as cur:
            cur.execute(VERIFY_DISTRIBUTION_SQL)
            rows = cur.fetchall()
            for row in rows:
                print(f"  ✅ {row[0]}: {row[1]} 行 ({row[2]} ~ {row[3]})")

    print("\n清理完成")


if __name__ == "__main__":
    main()
