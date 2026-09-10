-- ============================================================
-- KOL 模块扩展：支持 kol_type 分类 + 帖子关联代币
--
-- 变更：
--   1. kol_profile 新增 kol_type 字段（普通 KOL / 催化剂账号 / ...）
--   2. kol_post 新增 related_coins JSONB 字段（帖子下方关联的代币标签）
--   3. kol_post 新增 related_pairs TEXT[] 字段（关联交易对，便于索引查询）
-- ============================================================

-- 1. kol_profile 增加 kol_type
ALTER TABLE biz.kol_profile
    ADD COLUMN IF NOT EXISTS kol_type VARCHAR(32) NOT NULL DEFAULT 'kol'
        CONSTRAINT chk_kol_profile_type CHECK (kol_type IN ('kol', 'catalyst', 'news_media'));

COMMENT ON COLUMN biz.kol_profile.kol_type
    IS 'KOL 类型：kol=普通交易型KOL（做信号分析）/ catalyst=催化剂账号（写入催化剂表）/ news_media=新闻媒体（暂用同catalyst流程）';

CREATE INDEX IF NOT EXISTS idx_kol_profile_type_active
    ON biz.kol_profile (kol_type, is_active) WHERE is_active = TRUE;

-- 2. kol_post 增加 related_coins（详细代币信息，JSONB 数组）
ALTER TABLE biz.kol_post
    ADD COLUMN IF NOT EXISTS related_coins JSONB;

COMMENT ON COLUMN biz.kol_post.related_coins
    IS '帖子关联的代币列表（来自平台 tradingPairsV2 等字段），每项含 symbol/price/priceChange/contractAddress 等';

-- 3. kol_post 增加 related_pairs（交易对数组，便于快速查询和索引）
ALTER TABLE biz.kol_post
    ADD COLUMN IF NOT EXISTS related_pairs TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN biz.kol_post.related_pairs
    IS '帖子关联的交易对数组（如 {BTCUSDT, ETHUSDT}），从 related_coins 提取，便于 GIN 索引查询';

CREATE INDEX IF NOT EXISTS idx_kol_post_related_pairs
    ON biz.kol_post USING GIN (related_pairs);
