"""每日 diff 变化榜生成器。

从 asset_market_daily / asset_unlock_pressure 等表生成每日变化榜，
写入 biz.daily_diff_summary，供前端"每日信号"消费。

榜单类型：
- price_change_24h    24h 涨跌幅 TOP（仅 TOP1000 市值）
- volume_surge_24h    24h 成交量异动 TOP（量/市值比）
- unlock_7d           解锁抛压 TOP

设计原则：纯 diff，不做 AI 评分。

用法：
    python daily_diff_generator.py              # 生成最新一天
    python daily_diff_generator.py --date 2026-08-20  # 指定日期
    python daily_diff_generator.py --all        # 所有有数据的日期
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS biz.daily_diff_summary (
    diff_date       DATE NOT NULL,
    category        VARCHAR(32) NOT NULL,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id),
    metric_value    NUMERIC,
    metric_label    VARCHAR(64),
    rank            INT NOT NULL,
    direction       VARCHAR(8) NOT NULL,
    detail_json     JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (diff_date, category, asset_id, direction)
);

CREATE INDEX IF NOT EXISTS idx_daily_diff_date_cat ON biz.daily_diff_summary(diff_date, category);
CREATE INDEX IF NOT EXISTS idx_daily_diff_asset ON biz.daily_diff_summary(asset_id);
"""

PRICE_CHANGE_SQL = """
INSERT INTO biz.daily_diff_summary
    (diff_date, category, asset_id, metric_value, metric_label, rank, direction, detail_json)
SELECT
    %s::DATE,
    'price_change_24h',
    d.asset_id,
    d.change_24h,
    '24h 涨跌幅',
    ROW_NUMBER() OVER (ORDER BY d.change_24h DESC),
    CASE WHEN d.change_24h >= 0 THEN 'up' ELSE 'down' END,
    jsonb_build_object(
        'price_usd', d.price_usd,
        'market_cap', d.market_cap,
        'volume_24h', d.volume_24h
    )
FROM biz.asset_market_daily d
JOIN core.asset a ON a.asset_id = d.asset_id
WHERE d.source_code = 'cmc'
  AND d.market_date = %s::DATE
  AND a.market_cap_rank <= 1000
  AND d.change_24h IS NOT NULL
ORDER BY ABS(d.change_24h) DESC
LIMIT 40
ON CONFLICT (diff_date, category, asset_id, direction) DO NOTHING
"""

VOLUME_SURGE_SQL = """
INSERT INTO biz.daily_diff_summary
    (diff_date, category, asset_id, metric_value, metric_label, rank, direction, detail_json)
SELECT
    %s::DATE,
    'volume_surge_24h',
    d.asset_id,
    CASE WHEN d.market_cap > 0 THEN d.volume_24h / d.market_cap * 100 ELSE NULL END,
    '24h 量/市值比 (%%)',
    ROW_NUMBER() OVER (ORDER BY CASE WHEN d.market_cap > 0 THEN d.volume_24h / d.market_cap ELSE 0 END DESC),
    'up',
    jsonb_build_object(
        'price_usd', d.price_usd,
        'market_cap', d.market_cap,
        'volume_24h', d.volume_24h,
        'change_24h', d.change_24h
    )
FROM biz.asset_market_daily d
JOIN core.asset a ON a.asset_id = d.asset_id
WHERE d.source_code = 'cmc'
  AND d.market_date = %s::DATE
  AND a.market_cap_rank <= 1000
  AND d.volume_24h IS NOT NULL
  AND d.market_cap > 0
ORDER BY d.volume_24h / d.market_cap DESC
LIMIT 20
ON CONFLICT (diff_date, category, asset_id, direction) DO NOTHING
"""

UNLOCK_SQL = """
INSERT INTO biz.daily_diff_summary
    (diff_date, category, asset_id, metric_value, metric_label, rank, direction, detail_json)
SELECT
    %s::DATE,
    'unlock_7d',
    a.asset_id,
    COALESCE(ap.pressure_score, 0),
    '抛压评分 (0-100)',
    ROW_NUMBER() OVER (ORDER BY COALESCE(ap.pressure_score, 0) DESC),
    'up',
    jsonb_build_object(
        'risk_level', ap.risk_level,
        'unlock_pct_7d', ap.unlock_pct_7d,
        'unlock_pct_30d', ap.unlock_pct_30d,
        'top10_concentration', ap.top10_concentration
    )
FROM biz.asset_unlock_pressure ap
JOIN core.asset a ON a.asset_id = ap.asset_id
WHERE a.market_cap_rank <= 1000
  AND ap.pressure_score > 0
ORDER BY ap.pressure_score DESC
LIMIT 20
ON CONFLICT (diff_date, category, asset_id, direction) DO NOTHING
"""


def generate_for_date(cur, d: date) -> dict:
    """为指定日期生成所有榜单，返回 {category: count}。"""
    date_str = str(d)
    result = {}

    cur.execute(PRICE_CHANGE_SQL, (date_str, date_str))
    result["price_change_24h"] = cur.rowcount

    cur.execute(VOLUME_SURGE_SQL, (date_str, date_str))
    result["volume_surge_24h"] = cur.rowcount

    cur.execute("SELECT count(*) FROM biz.asset_token_unlocks")
    if cur.fetchone()[0] > 0:
        cur.execute(UNLOCK_SQL, (date_str,))
        result["unlock_7d"] = cur.rowcount
    else:
        result["unlock_7d"] = 0

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日 diff 变化榜生成器")
    parser.add_argument("--date", type=str, default=None, help="指定日期 (YYYY-MM-DD)，默认最新一天")
    parser.add_argument("--all", action="store_true", help="生成所有有数据的日期")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 确保表存在
            cur.execute(CREATE_TABLE_SQL)
            conn.commit()

            # 确定要生成的日期列表
            if args.all:
                cur.execute("""
                    SELECT DISTINCT market_date
                    FROM biz.asset_market_daily
                    WHERE source_code = 'cmc'
                    ORDER BY market_date
                """)
                dates = [r[0] for r in cur.fetchall()]
            elif args.date:
                dates = [date.fromisoformat(args.date)]
            else:
                cur.execute("SELECT max(market_date) FROM biz.asset_market_daily WHERE source_code = 'cmc'")
                latest = cur.fetchone()[0]
                if not latest:
                    print("[diff] 无行情数据，退出")
                    return 0
                dates = [latest]

            if not dates:
                print("[diff] 无日期可生成")
                return 0

            print(f"[diff] 生成 {len(dates)} 天的 diff 榜单")
            total = 0
            for d in dates:
                result = generate_for_date(cur, d)
                day_total = sum(result.values())
                total += day_total
                detail = ", ".join(f"{k}={v}" for k, v in result.items())
                print(f"  {d}: {day_total} 行 ({detail})")

            conn.commit()
            print(f"[diff] 完成，合计 {total} 行")

    return 0


if __name__ == "__main__":
    sys.exit(main())
