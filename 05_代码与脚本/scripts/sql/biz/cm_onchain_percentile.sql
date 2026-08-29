-- CM 链上指标历史极值分位视图（宽表 unpivot → 长表）
-- 复用 P2-1 极端阈值（>=90% HIGH / <=10% LOW）
-- 全历史百分位 + 滚动 365d 百分位 + 极端标记
-- 数据冻结 2026-05-24，纯历史分位用
-- flow_in/flow_out 仅 btc/eth 非空，其余币种自动跳过（WHERE value IS NOT NULL）

CREATE OR REPLACE VIEW biz.cm_onchain_percentile AS
WITH long AS (
  SELECT asset_id, metric_date, 'mvrv'     AS metric, cap_mvrv_cur          AS value FROM biz.cm_asset_onchain_daily
  UNION ALL SELECT asset_id, metric_date, 'adr_act',  adr_act_cnt          FROM biz.cm_asset_onchain_daily
  UNION ALL SELECT asset_id, metric_date, 'tx_tfr',   tx_tfr_cnt           FROM biz.cm_asset_onchain_daily
  UNION ALL SELECT asset_id, metric_date, 'flow_in',  flow_in_ex_usd       FROM biz.cm_asset_onchain_daily
  UNION ALL SELECT asset_id, metric_date, 'flow_out', flow_out_ex_usd      FROM biz.cm_asset_onchain_daily
  UNION ALL SELECT asset_id, metric_date, 'roi1yr',   roi_1yr              FROM biz.cm_asset_onchain_daily
  UNION ALL SELECT asset_id, metric_date, 'roi30d',   roi_30d              FROM biz.cm_asset_onchain_daily
),
base AS (
  SELECT asset_id,
         metric,
         metric_date,
         value,
         percent_rank() OVER (PARTITION BY asset_id, metric ORDER BY value) AS pr_full,
         percent_rank() OVER (PARTITION BY asset_id, metric ORDER BY value
                              ROWS BETWEEN 364 PRECEDING AND CURRENT ROW)   AS pr_roll
  FROM long
  WHERE value IS NOT NULL
)
SELECT asset_id,
       metric,
       metric_date,
       value,
       round(100 * pr_full, 2) AS pct_full,
       round(100 * pr_roll, 2) AS pct_roll_365d,
       CASE
         WHEN pr_full >= 0.90 THEN 'HIGH'
         WHEN pr_full <= 0.10 THEN 'LOW'
         ELSE 'NONE'
       END                      AS flag_full
FROM base;

COMMENT ON VIEW biz.cm_onchain_percentile IS 'CM 链上指标历史极值分位（长表，全历史+滚动365d，冻结 2026-05-24）';
