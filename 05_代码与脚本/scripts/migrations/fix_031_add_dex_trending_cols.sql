-- MEME-09: DEX 热搜/趋势列（GeckoTerminal + DexScreener 信号）
-- ALTER 加列，不破坏 CG phase 原 13 列；新列写入时 CG 写入留 NULL。
ALTER TABLE biz.asset_social_heat
    ADD COLUMN IF NOT EXISTS dex_trending_json   jsonb,
    ADD COLUMN IF NOT EXISTS dex_boost_score     numeric,
    ADD COLUMN IF NOT EXISTS dex_source          text,
    ADD COLUMN IF NOT EXISTS last_dex_seen       timestamptz;
