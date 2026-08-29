-- OBM 链上指标百分位视图（长表）
-- 复用 P2-1 极端阈值（>90% HIGH / <10% LOW）
-- 全历史百分位 + 滚动 365d 百分位

-- 全历史百分位视图
CREATE OR REPLACE VIEW biz.obm_percentile_full AS
SELECT
    metric_name,
    metric_date,
    value,
    -- 全历史百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY metric_name ORDER BY value
    ), 2) AS pct_full,
    -- 极端标记
    CASE
        WHEN PERCENT_RANK() OVER (PARTITION BY metric_name ORDER BY value) > 0.90 THEN 'HIGH'
        WHEN PERCENT_RANK() OVER (PARTITION BY metric_name ORDER BY value) < 0.10 THEN 'LOW'
        ELSE 'NONE'
    END AS extreme
FROM biz.obm_btc_daily
WHERE value IS NOT NULL;

COMMENT ON VIEW biz.obm_percentile_full IS 'OBM 链上指标全历史百分位（截至 2026-08-24）';

-- 滚动 365d 百分位视图
CREATE OR REPLACE VIEW biz.obm_percentile_roll365 AS
SELECT
    metric_name,
    metric_date,
    value,
    -- 滚动 365d 百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY metric_name
        ORDER BY value
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS pct_roll365,
    -- 极端标记（滚动）
    CASE
        WHEN PERCENT_RANK() OVER (
            PARTITION BY metric_name ORDER BY value
            ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
        ) > 0.90 THEN 'HIGH'
        WHEN PERCENT_RANK() OVER (
            PARTITION BY metric_name ORDER BY value
            ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
        ) < 0.10 THEN 'LOW'
        ELSE 'NONE'
    END AS extreme_roll365
FROM biz.obm_btc_daily
WHERE value IS NOT NULL;

COMMENT ON VIEW biz.obm_percentile_roll365 IS 'OBM 链上指标滚动 365d 百分位（截至 2026-08-24）';

-- 综合视图：合并全历史和滚动百分位
CREATE OR REPLACE VIEW biz.obm_percentile_combined AS
SELECT
    f.metric_name,
    f.metric_date,
    f.value,
    f.pct_full,
    f.extreme,
    r.pct_roll365,
    r.extreme_roll365
FROM biz.obm_percentile_full f
LEFT JOIN biz.obm_percentile_roll365 r
    ON f.metric_name = r.metric_name AND f.metric_date = r.metric_date;

COMMENT ON VIEW biz.obm_percentile_combined IS 'OBM 链上指标综合百分位（全历史 + 滚动 365d）';
