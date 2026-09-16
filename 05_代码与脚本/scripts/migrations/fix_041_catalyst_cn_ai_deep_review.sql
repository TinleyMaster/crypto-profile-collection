-- 催化剂中文化 + A级AI深度评审 字段扩展
-- 执行：psql $DATABASE_URL -f fix_041_catalyst_cn_ai_deep_review.sql

-- 1. 催化剂中文标题（用于邮件、前端展示）
ALTER TABLE biz.asset_catalyst
    ADD COLUMN IF NOT EXISTS title_cn VARCHAR(512);

COMMENT ON COLUMN biz.asset_catalyst.title_cn IS '催化剂中文标题（AI翻译，用于邮件/前端展示）';

-- 2. 信号表：AI深度评审结果（JSON，含多维度分析）
ALTER TABLE biz.catalyst_signal
    ADD COLUMN IF NOT EXISTS ai_deep_review JSONB;

COMMENT ON COLUMN biz.catalyst_signal.ai_deep_review IS 'AI深度评审结果（A级信号快通道即时生成）：包含驱动逻辑、风险点、操作建议、信心度等结构化分析';

-- 3. 索引：加速查找待翻译的催化剂
CREATE INDEX IF NOT EXISTS idx_cat_title_cn_null
    ON biz.asset_catalyst (catalyst_id DESC)
    WHERE title_cn IS NULL;

-- 4. 索引：加速查找待深度评审的A级信号
CREATE INDEX IF NOT EXISTS idx_cat_signal_a_no_deep_review
    ON biz.catalyst_signal (signal_id DESC)
    WHERE tier = 'A' AND status = 'open' AND ai_deep_review IS NULL;
