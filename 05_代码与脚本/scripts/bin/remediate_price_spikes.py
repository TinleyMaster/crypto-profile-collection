"""RC-1 价格尖刺纠正脚本（修复 Defect C 自锁问题）。

扫描 biz.asset_market_daily（source='cmc'）中 price_usd 偏离近 90 日 10 分位锚定 >10× 的异常日，
优先用 cmc_historical 同日正确值覆盖，若无则置 NULL（让 latest 回退到最近有效日）。

Defect C 修复：
- 基线 CTE 不再过滤 is_anomaly（避免自锁）
- 新增 --reset-flags 清除上一版误标
- 基线锚定从"中位数"改"10 分位"（对向上尖刺更稳健）

用法：
    python remediate_price_spikes.py                    # 预览（dry-run）
    python remediate_price_spikes.py --execute          # 执行纠正
    python remediate_price_spikes.py --reset-flags --execute  # 先清标再纠正
    python remediate_price_spikes.py --asset-id 4747    # 只处理指定资产
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
    parser = argparse.ArgumentParser(description="RC-1 价格尖刺纠正（修复 Defect C）")
    parser.add_argument("--execute", action="store_true", help="执行纠正（默认 dry-run）")
    parser.add_argument("--reset-flags", action="store_true", help="检测前清除所有 is_anomaly 标志")
    parser.add_argument("--asset-id", type=int, default=None, help="只处理指定 asset_id")
    parser.add_argument("--threshold", type=float, default=10.0, help="偏离倍数阈值（默认 10×）")
    return parser


def reset_anomaly_flags(conn, asset_id: int | None) -> int:
    """清除 is_anomaly 标志，让正常日回正、尖刺重判。"""
    with conn.cursor() as cur:
        if asset_id:
            cur.execute("""
                UPDATE biz.asset_market_daily
                SET is_anomaly = NULL
                WHERE source_code = 'cmc'
                  AND asset_id = %s
                  AND is_anomaly IS NOT NULL
            """, (asset_id,))
        else:
            cur.execute("""
                UPDATE biz.asset_market_daily
                SET is_anomaly = NULL
                WHERE source_code = 'cmc'
                  AND is_anomaly IS NOT NULL
            """)
        affected = cur.rowcount
        conn.commit()
        return affected


def detect_and_fix_spikes(conn, execute: bool, asset_id: int | None, threshold: float) -> dict:
    """检测并修复价格尖刺。使用 10 分位锚定（对向上尖刺更稳健）。"""
    with conn.cursor() as cur:
        # 1. 计算每个资产近 90 天的 10 分位锚定价格
        #    不再过滤 is_anomaly（避免 Defect C 自锁）
        asset_filter = ""
        params = []
        if asset_id:
            asset_filter = "AND d.asset_id = %s"
            params = [asset_id, asset_id]
        
        cur.execute(f"""
            WITH p10 AS (
                -- 10 分位锚定：向上尖刺几乎不抬高低分位
                SELECT 
                    d.asset_id,
                    PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY d.price_usd) AS anchor_price,
                    COUNT(*) AS total_days
                FROM biz.asset_market_daily d
                WHERE d.source_code = 'cmc'
                  AND d.market_date >= CURRENT_DATE - INTERVAL '90 days'
                  AND d.price_usd > 0
                  {asset_filter}
                GROUP BY d.asset_id
                HAVING COUNT(*) >= 5
            ),
            anomalies AS (
                SELECT 
                    d.asset_id,
                    d.market_date,
                    d.price_usd,
                    p.anchor_price,
                    (d.price_usd / NULLIF(p.anchor_price, 0))::NUMERIC(12,2) AS ratio,
                    a.canonical_symbol
                FROM biz.asset_market_daily d
                JOIN p10 p ON p.asset_id = d.asset_id
                JOIN core.asset a ON a.asset_id = d.asset_id
                WHERE d.source_code = 'cmc'
                  AND d.price_usd > 0
                  AND p.anchor_price > 0
                  AND (d.price_usd / p.anchor_price > %s OR d.price_usd / p.anchor_price < 1.0/%s)
                  {asset_filter}
            )
            SELECT asset_id, market_date, price_usd, anchor_price, ratio, canonical_symbol
            FROM anomalies
            ORDER BY ratio DESC
        """, [threshold, threshold] + params)
        
        anomalies = cur.fetchall()
        
        if not anomalies:
            return {"detected": 0, "fixed": 0, "skipped": 0}
        
        fixed = 0
        skipped = 0
        
        for row in anomalies:
            a_id, m_date, price, anchor, ratio, symbol = row
            print(f"[spike] {symbol} (asset_id={a_id}) {m_date}: price=${price:.6f} anchor=${anchor:.6f} ratio={ratio:.1f}×")
            
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
    print("RC-1 价格尖刺纠正（修复 Defect C）")
    print("=" * 60)
    print(f"模式: {'执行' if args.execute else '预览（dry-run）'}")
    print(f"阈值: {args.threshold}×")
    print(f"锚定: 10 分位（对向上尖刺更稳健）")
    if args.asset_id:
        print(f"目标: asset_id={args.asset_id}")
    if args.reset_flags:
        print("重置: 将清除所有 is_anomaly 标志")
    print()
    
    with get_connection(settings.database_url) as conn:
        # 先清除误标（如果指定）
        if args.reset_flags and args.execute:
            print("[reset] 清除 is_anomaly 标志...")
            reset_count = reset_anomaly_flags(conn, args.asset_id)
            print(f"[reset] 已清除 {reset_count} 条")
            print()
        
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