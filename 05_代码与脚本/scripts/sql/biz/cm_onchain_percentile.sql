-- Coin Metrics 链上指标百分位视图
-- 复用 P2-1 极端阈值（>90% HIGH / <10% LOW）
-- 全历史百分位 + 滚动 365d 百分位

-- 全历史百分位视图
CREATE OR REPLACE VIEW biz.cm_onchain_percentile_full AS
SELECT
    asset_id,
    cm_symbol,
    metric_date,
    -- MVRV 百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY cap_mvrv_cur
    ), 2) AS mvrv_pct_full,
    -- 活跃地址百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY adr_act_cnt
    ), 2) AS adr_pct_full,
    -- 转账笔数百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY tx_tfr_cnt
    ), 2) AS tx_pct_full,
    -- 交易所流入百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY flow_in_ex_usd
    ), 2) AS flow_in_pct_full,
    -- 交易所流出百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY flow_out_ex_usd
    ), 2) AS flow_out_pct_full,
    -- ROI 30d 百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY roi_30d
    ), 2) AS roi_30d_pct_full,
    -- ROI 1yr 百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY roi_1yr
    ), 2) AS roi_1yr_pct_full,
    -- 成交量百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id ORDER BY volume_reported_spot_usd_1d
    ), 2) AS vol_pct_full,
    -- 极端标记（MVRV）
    CASE
        WHEN PERCENT_RANK() OVER (PARTITION BY asset_id ORDER BY cap_mvrv_cur) > 0.90 THEN 'HIGH'
        WHEN PERCENT_RANK() OVER (PARTITION BY asset_id ORDER BY cap_mvrv_cur) < 0.10 THEN 'LOW'
        ELSE 'NONE'
    END AS mvrv_extreme
FROM biz.cm_asset_onchain_daily
WHERE cap_mvrv_cur IS NOT NULL;

COMMENT ON VIEW biz.cm_onchain_percentile_full IS 'CM 链上指标全历史百分位（冻结 2026-05-24）';

-- 滚动 365d 百分位视图
CREATE OR REPLACE VIEW biz.cm_onchain_percentile_roll365 AS
SELECT
    asset_id,
    cm_symbol,
    metric_date,
    -- MVRV 滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY cap_mvrv_cur
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS mvrv_pct_roll365,
    -- 活跃地址滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY adr_act_cnt
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS adr_pct_roll365,
    -- 转账笔数滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY tx_tfr_cnt
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS tx_pct_roll365,
    -- 交易所流入滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY flow_in_ex_usd
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS flow_in_pct_roll365,
    -- 交易所流出滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY flow_out_ex_usd
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS flow_out_pct_roll365,
    -- ROI 30d 滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY roi_30d
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS roi_30d_pct_roll365,
    -- ROI 1yr 滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY roi_1yr
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS roi_1yr_pct_roll365,
    -- 成交量滚动百分位
    ROUND(100.0 * PERCENT_RANK() OVER (
        PARTITION BY asset_id
        ORDER BY volume_reported_spot_usd_1d
        ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
    ), 2) AS vol_pct_roll365,
    -- 极端标记（MVRV 滚动）
    CASE
        WHEN PERCENT_RANK() OVER (
            PARTITION BY asset_id ORDER BY cap_mvrv_cur
            ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
        ) > 0.90 THEN 'HIGH'
        WHEN PERCENT_RANK() OVER (
            PARTITION BY asset_id ORDER BY cap_mvrv_cur
            ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
        ) < 0.10 THEN 'LOW'
        ELSE 'NONE'
    END AS mvrv_extreme_roll365
FROM biz.cm_asset_onchain_daily
WHERE cap_mvrv_cur IS NOT NULL;

COMMENT ON VIEW biz.cm_onchain_percentile_roll365 IS 'CM 链上指标滚动 365d 百分位（冻结 2026-05-24）';
