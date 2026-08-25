"""每日 diff 变化榜生成器。

从 asset_market_daily / asset_unlock_pressure / asset_social_heat / protocol_metric_daily
等表生成每日变化榜，写入 biz.daily_diff_summary，供前端"每日信号"消费。

榜单类型：
- price_change_24h    24h 涨跌幅 TOP（仅 TOP1000 市值）
- volume_surge_24h    24h 成交量异动 TOP（量/市值比）
- market_cap_mover    24h 市值变动绝对值 TOP（大资金进出）
- unlock_7d           7 天解锁抛压 TOP
- social_surge        社交热度日环比增幅 TOP（需有历史数据）
- tvl_surge_24h       24h TVL 增幅 TOP（DeFi 资金流入）

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
WITH upcoming AS (
    SELECT
        e.asset_id,
        SUM(e.unlock_value_usd) AS unlock_value_7d_usd,
        SUM(e.unlock_amount) AS unlock_amount_7d,
        COUNT(*) AS event_count_7d
    FROM biz.asset_unlock_event e
    WHERE e.unlock_date BETWEEN %s::DATE AND %s::DATE + INTERVAL '7 days'
      AND e.unlock_value_usd IS NOT NULL
      AND e.unlock_value_usd > 0
    GROUP BY e.asset_id
)
SELECT
    %s::DATE,
    'unlock_7d',
    a.asset_id,
    COALESCE(u.unlock_value_7d_usd, 0),
    '7 天解锁价值 (USD)',
    ROW_NUMBER() OVER (ORDER BY COALESCE(u.unlock_value_7d_usd, 0) DESC),
    'up',
    jsonb_build_object(
        'unlock_value_7d_usd', u.unlock_value_7d_usd,
        'unlock_amount_7d', u.unlock_amount_7d,
        'event_count_7d', u.event_count_7d,
        'market_cap', a.market_cap,
        'market_cap_rank', a.market_cap_rank
    )
FROM upcoming u
JOIN core.asset a ON a.asset_id = u.asset_id
WHERE a.market_cap_rank <= 3000
ORDER BY u.unlock_value_7d_usd DESC
LIMIT 20
ON CONFLICT (diff_date, category, asset_id, direction) DO NOTHING
"""

MARKET_CAP_MOVER_SQL = """
INSERT INTO biz.daily_diff_summary
    (diff_date, category, asset_id, metric_value, metric_label, rank, direction, detail_json)
SELECT
    %s::DATE,
    'market_cap_mover',
    d.asset_id,
    d.market_cap - prev.market_cap AS market_cap_change_usd,
    '24h 市值变动 (USD)',
    ROW_NUMBER() OVER (ORDER BY ABS(d.market_cap - prev.market_cap) DESC),
    CASE WHEN d.market_cap >= prev.market_cap THEN 'up' ELSE 'down' END,
    jsonb_build_object(
        'market_cap', d.market_cap,
        'market_cap_prev', prev.market_cap,
        'price_usd', d.price_usd,
        'change_24h', d.change_24h,
        'volume_24h', d.volume_24h
    )
FROM biz.asset_market_daily d
JOIN core.asset a ON a.asset_id = d.asset_id
JOIN biz.asset_market_daily prev
    ON prev.asset_id = d.asset_id
    AND prev.source_code = d.source_code
    AND prev.market_date = d.market_date - INTERVAL '1 day'
WHERE d.source_code = 'cmc'
  AND d.market_date = %s::DATE
  AND a.market_cap_rank <= 1000
  AND d.market_cap IS NOT NULL
  AND prev.market_cap IS NOT NULL
  AND prev.market_cap > 0
ORDER BY ABS(d.market_cap - prev.market_cap) DESC
LIMIT 20
ON CONFLICT (diff_date, category, asset_id, direction) DO NOTHING
"""

SOCIAL_SURGE_SQL = """
INSERT INTO biz.daily_diff_summary
    (diff_date, category, asset_id, metric_value, metric_label, rank, direction, detail_json)
SELECT
    %s::DATE,
    'social_surge',
    sh.asset_id,
    (sh.score - prev.score) / NULLIF(prev.score, 0) * 100 AS heat_change_pct,
    '社交热度日增幅 (%%)',
    ROW_NUMBER() OVER (ORDER BY (sh.score - prev.score) / NULLIF(prev.score, 0) DESC),
    'up',
    jsonb_build_object(
        'heat_score', sh.score,
        'heat_score_prev', prev.score,
        'confidence', sh.confidence,
        'community_json', sh.community_json,
        'trend_json', sh.trend_json
    )
FROM biz.asset_social_heat sh
JOIN core.asset a ON a.asset_id = sh.asset_id
JOIN biz.asset_social_heat prev
    ON prev.asset_id = sh.asset_id
    AND DATE(prev.updated_at) = DATE(sh.updated_at) - INTERVAL '1 day'
WHERE DATE(sh.updated_at) = %s::DATE
  AND a.market_cap_rank <= 1000
  AND sh.score IS NOT NULL
  AND prev.score IS NOT NULL
  AND prev.score > 0
  AND sh.score > prev.score
ORDER BY (sh.score - prev.score) / prev.score DESC
LIMIT 20
ON CONFLICT (diff_date, category, asset_id, direction) DO NOTHING
"""

TVL_SURGE_SQL = """
INSERT INTO biz.daily_diff_summary
    (diff_date, category, asset_id, metric_value, metric_label, rank, direction, detail_json)
SELECT
    %s::DATE,
    'tvl_surge_24h',
    t.asset_id,
    (t.tvl - prev.tvl) / NULLIF(prev.tvl, 0) * 100 AS tvl_change_pct,
    '24h TVL 增幅 (%%)',
    ROW_NUMBER() OVER (ORDER BY (t.tvl - prev.tvl) / NULLIF(prev.tvl, 0) DESC),
    CASE WHEN t.tvl >= prev.tvl THEN 'up' ELSE 'down' END,
    jsonb_build_object(
        'tvl', t.tvl,
        'tvl_prev', prev.tvl,
        'tvl_change_1d', t.tvl_change_1d,
        'tvl_change_7d', t.tvl_change_7d
    )
FROM biz.protocol_metric_daily t
JOIN core.asset a ON a.asset_id = t.asset_id
JOIN biz.protocol_metric_daily prev
    ON prev.asset_id = t.asset_id
    AND prev.source_code = t.source_code
    AND prev.metric_date = t.metric_date - INTERVAL '1 day'
WHERE t.source_code = 'dl'
  AND t.metric_date = %s::DATE
  AND a.market_cap_rank <= 3000
  AND t.tvl IS NOT NULL
  AND prev.tvl IS NOT NULL
  AND prev.tvl > 0
ORDER BY ABS(t.tvl - prev.tvl) / prev.tvl DESC
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

    cur.execute(MARKET_CAP_MOVER_SQL, (date_str, date_str))
    result["market_cap_mover"] = cur.rowcount

    cur.execute("SELECT count(*) FROM biz.asset_unlock_event")
    if cur.fetchone()[0] > 0:
        cur.execute(UNLOCK_SQL, (date_str, date_str, date_str))
        result["unlock_7d"] = cur.rowcount
    else:
        result["unlock_7d"] = 0

    # social_surge：需要前一天有数据，且当天有更新
    cur.execute(SOCIAL_SURGE_SQL, (date_str, date_str))
    result["social_surge"] = cur.rowcount

    # tvl_surge：需要 protocol_metric_daily 有数据
    cur.execute("SELECT count(*) FROM biz.protocol_metric_daily WHERE source_code = 'dl'")
    if cur.fetchone()[0] > 0:
        cur.execute(TVL_SURGE_SQL, (date_str, date_str))
        result["tvl_surge_24h"] = cur.rowcount
    else:
        result["tvl_surge_24h"] = 0

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
