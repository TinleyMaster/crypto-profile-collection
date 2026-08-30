"""RC-1 价格尖刺纠正脚本。

扫描 biz.asset_market_daily（source='cmc'）中 price_usd 偏离近 30 日中位数 >10× 的异常日，
优先用 cmc_historical 同日正确值覆盖，若无则置 NULL（让 latest 回退到最近有效日）。

用法：
    python remediate_price_spikes.py              # 预览（dry-run）
    python remediate_price_spikes.py --execute    # 执行纠正
    python remediate_price_spikes.py --asset-id 4747  # 只处理指定资产
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RC-1 价格尖刺纠正")
    parser.add_argument("--execute", action="store_true", help="执行纠正（默认 dry-run）")
    parser.add_argument("--asset-id", type=int, default=None, help="只处理指定 asset_id")
    parser.add_argument("--threshold", type=float, default=10.0, help="偏离倍数阈值（默认 10×）")
    return parser


def detect_and_fix_spikes(conn, execute: bool, asset_id: int | None, threshold: float) -> dict:
    """检测并修复价格尖刺。"""
    with conn.cursor() as cur:
        # 1. 计算每个资产近 30 天的价格中位数
        asset_filter_medians = ""
        asset_filter_anomalies = ""
        params = []
        if asset_id:
            asset_filter_medians = "AND d.asset_id = %s"
            asset_filter_anomalies = "AND d.asset_id = %s"
            params = [asset_id, asset_id]
        
        cur.execute(f"""
            WITH medians AS (
                SELECT 
                    d.asset_id,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY d.price_usd) AS median_price,
                    COUNT(*) AS total_days
                FROM biz.asset_market_daily d
                WHERE d.source_code = 'cmc'
                  AND d.market_date >= CURRENT_DATE - INTERVAL '30 days'
                  AND d.price_usd > 0
                  AND (d.is_anomaly IS NOT TRUE OR d.is_anomaly IS NULL)
                  {asset_filter_medians}
                GROUP BY d.asset_id
                HAVING COUNT(*) >= 3
            ),
            anomalies AS (
                SELECT 
                    d.asset_id,
                    d.market_date,
                    d.price_usd,
                    m.median_price,
                    (d.price_usd / NULLIF(m.median_price, 0))::NUMERIC(12,2) AS ratio,
                    a.canonical_symbol
                FROM biz.asset_market_daily d
                JOIN medians m ON m.asset_id = d.asset_id
                JOIN core.asset a ON a.asset_id = d.asset_id
                WHERE d.source_code = 'cmc'
                  AND d.price_usd > 0
                  AND m.median_price > 0
                  AND (d.is_anomaly IS NOT TRUE OR d.is_anomaly IS NULL)
                  AND (d.price_usd / m.median_price > %s OR d.price_usd / m.median_price < 1.0/%s)
                  {asset_filter_anomalies}
            )
            SELECT asset_id, market_date, price_usd, median_price, ratio, canonical_symbol
            FROM anomalies
            ORDER BY ratio DESC
        """, [threshold, threshold] + params)
        
        anomalies = cur.fetchall()
        
        if not anomalies:
            return {"detected": 0, "fixed": 0, "skipped": 0}
        
        fixed = 0
        skipped = 0
        
        for row in anomalies:
            a_id, m_date, price, median, ratio, symbol = row
            print(f"[spike] {symbol} (asset_id={a_id}) {m_date}: price=${price:.6f} median=${median:.6f} ratio={ratio:.1f}×")
            
            if not execute:
                continue
            
            # 尝试用 cmc_historical 同日值覆盖
            cur.execute("""
                SELECT price_usd 
                FROM biz.asset_market_daily 
                WHERE asset_id = %s 
                  AND market_date = %s 
                  AND source_code = 'cmc_historical'
                  AND price_usd > 0
                LIMIT 1
            """, (a_id, m_date))
            
            hist_row = cur.fetchone()
            
            if hist_row:
                # 用 historical 值覆盖
                new_price = hist_row[0]
                cur.execute("""
                    UPDATE biz.asset_market_daily
                    SET price_usd = %s,
                        is_anomaly = TRUE,
                        updated_at = NOW()
                    WHERE asset_id = %s
                      AND market_date = %s
                      AND source_code = 'cmc'
                """, (new_price, a_id, m_date))
                print(f"  → 用 historical 值 ${new_price:.6f} 覆盖")
                fixed += 1
            else:
                # 无 historical 值，置 NULL
                cur.execute("""
                    UPDATE biz.asset_market_daily
                    SET price_usd = NULL,
                        is_anomaly = TRUE,
                        updated_at = NOW()
                    WHERE asset_id = %s
                      AND market_date = %s
                      AND source_code = 'cmc'
                """, (a_id, m_date))
                print(f"  → 置 NULL（无 historical 值）")
                fixed += 1
        
        if execute:
            conn.commit()
        
        return {"detected": len(anomalies), "fixed": fixed, "skipped": skipped}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    
    settings = get_settings(require_database=True)
    
    print("=" * 60)
    print("RC-1 价格尖刺纠正")
    print("=" * 60)
    print(f"模式: {'执行' if args.execute else '预览（dry-run）'}")
    print(f"阈值: {args.threshold}×")
    if args.asset_id:
        print(f"目标: asset_id={args.asset_id}")
    print()
    
    with get_connection(settings.database_url) as conn:
        result = detect_and_fix_spikes(
            conn,
            execute=args.execute,
            asset_id=args.asset_id,
            threshold=args.threshold
        )
    
    print()
    print("=" * 60)
    print(f"检测到: {result['detected']} 条异常")
    print(f"已修复: {result['fixed']} 条")
    print("=" * 60)
    
    return 0 if result['detected'] == 0 else (1 if not args.execute else 0)


if __name__ == "__main__":
    sys.exit(main())