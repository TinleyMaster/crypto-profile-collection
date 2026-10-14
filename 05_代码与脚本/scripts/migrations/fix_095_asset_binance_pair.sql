-- 币安可交易池映射：币安合约 symbol → core.asset.asset_id
--
-- 背景：每日变化榜原以 CMC rank<=1000 全市场为池（含大量币安不可交易的币）。
-- 用户只在币安交易，故榜单池改为「币安 USDT-M 永续合约可交易池」，行情口径同步换为
-- biz.asset_klines（interval='1h' 聚合日线）。
--
-- 为什么不直接写 SQL 表达式关联：core.asset.canonical_symbol 存在重复
-- （22648 行 / 19442 个唯一符号），裸 join 会扇出并把行数放大；且币安合约的
-- 1000/1000000 倍数前缀（1000PEPEUSDT）需要归一化，逻辑复杂、多个脚本都要用。
-- 故物化成一张可审计的映射表。
--
-- match_kind 语义：
--   exact      直接命中（BTCUSDT → BTC）
--   strip      去掉 1000/1000000 倍数前缀后命中（1000PEPEUSDT → PEPE）
--   none       币安有合约但 core.asset 无对应资产 —— asset_id 为 NULL，仅作审计用
--
-- 刷新：python bin/refresh_binance_pair.py
CREATE TABLE IF NOT EXISTS biz.asset_binance_pair (
    symbol      TEXT PRIMARY KEY,                       -- 币安合约 symbol，如 1000PEPEUSDT
    asset_id    BIGINT REFERENCES core.asset(asset_id), -- NULL = 未映射（审计用）
    base_symbol TEXT NOT NULL,                          -- 归一化后的基础符号
    match_kind  TEXT NOT NULL,                          -- exact / strip / none
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asset_binance_pair_asset
    ON biz.asset_binance_pair (asset_id) WHERE asset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_asset_binance_pair_kind
    ON biz.asset_binance_pair (match_kind);