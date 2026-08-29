-- ============================================================
-- 修复 raw.api_response 唯一约束问题
-- 问题：insert_api_response.sql 使用 ON CONFLICT ON CONSTRAINT
--       但 uq_raw_api_response_dedup 是索引而非约束
--       PostgreSQL 不支持在 ALTER TABLE ADD CONSTRAINT 中使用 COALESCE
-- 修复：保持索引，修改 insert_api_response.sql 使用 ON CONFLICT ON INDEX
-- ============================================================

BEGIN;

-- 1. 确保唯一索引存在（如果不存在则创建）
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_api_response_dedup
    ON raw.api_response (platform_code, endpoint_code, COALESCE(request_key, ''), COALESCE(page_key, ''), payload_hash);

-- 2. 验证索引存在
SELECT 
    indexname,
    indexdef
FROM pg_indexes
WHERE indexname = 'uq_raw_api_response_dedup';

COMMIT;