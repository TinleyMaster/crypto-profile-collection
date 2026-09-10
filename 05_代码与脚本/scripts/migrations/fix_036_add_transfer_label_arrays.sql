-- fix_036_add_transfer_label_arrays.sql
-- 为 onchain_transfer_log 添加多标签数组列，支持 exchange / smart_money / mev_bot 等多种标签
-- 老列（from_label / to_label / from_exchange / to_exchange / is_to_exchange）保留不动，向后兼容

ALTER TABLE biz.onchain_transfer_log
    ADD COLUMN IF NOT EXISTS from_labels      TEXT[],
    ADD COLUMN IF NOT EXISTS to_labels        TEXT[],
    ADD COLUMN IF NOT EXISTS from_label_names TEXT[],
    ADD COLUMN IF NOT EXISTS to_label_names   TEXT[];

COMMENT ON COLUMN biz.onchain_transfer_log.from_labels IS
    '发送方标签类型数组（如 {exchange,market_maker}），支持多标签共存';
COMMENT ON COLUMN biz.onchain_transfer_log.to_labels IS
    '接收方标签类型数组（如 {exchange,market_maker}），支持多标签共存';
COMMENT ON COLUMN biz.onchain_transfer_log.from_label_names IS
    '发送方标签名称数组（如 {Binance 5}），与 from_labels 一一对应';
COMMENT ON COLUMN biz.onchain_transfer_log.to_label_names IS
    '接收方标签名称数组（如 {Binance 5}），与 to_labels 一一对应';

-- 索引：按标签类型快速筛选（GIN 索引支持数组包含查询）
CREATE INDEX IF NOT EXISTS idx_transfer_log_from_labels
    ON biz.onchain_transfer_log USING GIN (from_labels);
CREATE INDEX IF NOT EXISTS idx_transfer_log_to_labels
    ON biz.onchain_transfer_log USING GIN (to_labels);

-- 历史数据回填：根据已有 from_label/to_label 字段填充数组列
-- （只有 exchange/unknown 两种，先填进去，后续标签更新了再覆盖）
UPDATE biz.onchain_transfer_log
SET from_labels = ARRAY[from_label]::TEXT[]
WHERE from_labels IS NULL
  AND from_label IS NOT NULL;

UPDATE biz.onchain_transfer_log
SET to_labels = ARRAY[to_label]::TEXT[]
WHERE to_labels IS NULL
  AND to_label IS NOT NULL;

UPDATE biz.onchain_transfer_log
SET from_label_names = ARRAY[from_exchange]::TEXT[]
WHERE from_label_names IS NULL
  AND from_exchange IS NOT NULL;

UPDATE biz.onchain_transfer_log
SET to_label_names = ARRAY[to_exchange]::TEXT[]
WHERE to_label_names IS NULL
  AND to_exchange IS NOT NULL;
