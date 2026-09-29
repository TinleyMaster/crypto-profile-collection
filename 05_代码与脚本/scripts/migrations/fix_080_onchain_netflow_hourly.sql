-- ============================================================
-- fix_080_onchain_netflow_hourly.sql
-- 资产级交易所净流因子表（P0 数据利用工单 2026-09-29，女王授权）
--   biz.onchain_transfer_log（5min/12链 逐笔）→ 资产 × 小时 聚合因子。
--
-- 语义（对齐 CoinGlass）：
--   inflow_usd  = 转入交易所钱包的 USD（充值，潜在抛压）
--   outflow_usd = 提币离场交易所的 USD（提币，吸筹/自托管）
--   netflow_usd = inflow - outflow（正=净流入交易所=抛压偏多；负=净流出=吸筹偏多）
--   同所家族互转（如 Binance→Binance 14）已剔除——无用户行为信号。
--   is_suspect=TRUE 与 0xtest% 测试行已剔除。
--
-- 归因口径 = 读侧 union（与 workbench/onchain_alert.py 完全一致）：
--   biz.onchain_exchange_wallet(confidence='high')
--   UNION biz.onchain_address_label(label_type='exchange', confidence IN high,medium)
--   家族判定 = split_part(split_part(name,':',1),' ',1)
--   （兼容 Binance / Binance 14 / Binance: Hot Wallet 20 三种命名）
--
-- 设计约束：
--   纯读侧聚合，**不碰采集/写入 daemon**；消费者 JOIN core.asset 取 symbol。
--   回填脚本 scripts/bin/backfill_netflow_factor.py 幂等 upsert，可任意重跑。
-- ============================================================

CREATE TABLE IF NOT EXISTS biz.onchain_netflow_hourly (
    bucket_hour   TIMESTAMPTZ NOT NULL,
    asset_id      INTEGER     NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    inflow_usd    NUMERIC     NOT NULL DEFAULT 0,
    outflow_usd   NUMERIC     NOT NULL DEFAULT 0,
    netflow_usd   NUMERIC     NOT NULL DEFAULT 0,
    inflow_cnt    INTEGER     NOT NULL DEFAULT 0,
    outflow_cnt   INTEGER     NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bucket_hour, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_onchain_netflow_hourly_asset_time
    ON biz.onchain_netflow_hourly (asset_id, bucket_hour DESC);
