-- OBM 链上指标历史极值分位视图（长表，PARTITION BY metric_name）
-- 复用 P2-1 极端阈值（>=90% HIGH / <=10% LOW）
-- 全历史百分位 + 滚动 365d 百分位 + 极端标记
-- 数据截至 2026-08-24，纯历史分位用

CREATE OR REPLACE VIEW biz.obm_percentile AS
WITH base AS (
  SELECT metric_name,
         metric_date,
         value,
         percent_rank() OVER (
             PARTITION BY metric_name ORDER BY value
         ) AS pr_full,
         percent_rank() OVER (
             PARTITION BY metric_name ORDER BY value
             ROWS BETWEEN 364 PRECEDING AND CURRENT ROW
         ) AS pr_roll
  FROM biz.obm_btc_daily
  WHERE value IS NOT NULL
)
SELECT metric_name,
       metric_date,
       value,
       round((100 * pr_full)::numeric, 2)  AS pct_full,
       round((100 * pr_roll)::numeric, 2)  AS pct_roll_365d,
       CASE
         WHEN pr_full >= 0.90 THEN 'HIGH'
         WHEN pr_full <= 0.10 THEN 'LOW'
         ELSE 'NONE'
       END                       AS flag_full
FROM base;

COMMENT ON VIEW biz.obm_percentile IS 'OBM 链上指标历史极值分位（全历史+滚动365d，截至 2026-08-24）';
