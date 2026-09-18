-- =====================================================================
-- fix_051: onchain_transfer_log 时间戳健全性约束（审计 P1-4，2026-09-18）
-- =====================================================================
-- 背景：历史代码用「跨链共享的最新区块高度缓存」估算时间戳，
-- 导致 1,594 行 block_timestamp 落在 1856/1872 年。代码侧已修复
-- （毫秒归一化 / 上下界 / 缓存键带链维度），此处加 DB 兜底约束。
--
-- 用 NOT VALID：只约束新写入行，不因历史脏行而失败。
-- 历史清理需单独授权后执行（见文件末尾注释）。
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_onchain_transfer_log_block_ts'
    ) THEN
        ALTER TABLE biz.onchain_transfer_log
            ADD CONSTRAINT chk_onchain_transfer_log_block_ts
            CHECK (
                block_timestamp IS NULL
                OR (block_timestamp >= TIMESTAMPTZ '2015-01-01'
                    AND block_timestamp < TIMESTAMPTZ '2100-01-01')
            ) NOT VALID;
    END IF;
END $$;

-- =====================================================================
-- 历史脏行清理（⚠️ 破坏性操作，需用户授权后手动执行，勿自动跑）
-- =====================================================================
-- 建议先把脏行时间戳置为 NULL（而非删除），保留转账记录的其余字段：
--
--   UPDATE biz.onchain_transfer_log
--      SET block_timestamp = NULL
--    WHERE block_timestamp < TIMESTAMPTZ '2015-01-01'
--       OR block_timestamp >= TIMESTAMPTZ '2100-01-01';
--
-- 确认无脏行后再将约束置为有效校验：
--
--   ALTER TABLE biz.onchain_transfer_log
--       VALIDATE CONSTRAINT chk_onchain_transfer_log_block_ts;
