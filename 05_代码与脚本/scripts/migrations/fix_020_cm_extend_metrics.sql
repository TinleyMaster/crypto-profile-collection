-- ============================================================
-- CM 扩展指标：基表加 10 列
-- 工单：CM扩展指标接入与doge映射修复_2026-08-30
-- ============================================================

ALTER TABLE biz.cm_asset_onchain_daily
  ADD COLUMN IF NOT EXISTS sply_cur            numeric,
  ADD COLUMN IF NOT EXISTS cap_mrkt_cur_usd    numeric,
  ADD COLUMN IF NOT EXISTS cap_mrkt_est_usd    numeric,
  ADD COLUMN IF NOT EXISTS sply_ex_usd         numeric,
  ADD COLUMN IF NOT EXISTS tx_cnt              bigint,
  ADD COLUMN IF NOT EXISTS adr_bal_cnt         bigint,
  ADD COLUMN IF NOT EXISTS fee_tot_native      numeric,
  ADD COLUMN IF NOT EXISTS iss_tot_native      numeric,
  ADD COLUMN IF NOT EXISTS iss_tot_usd         numeric,
  ADD COLUMN IF NOT EXISTS hash_rate           numeric;
