-- ============================================================
-- P1-3 修复：补齐 biz.onchain_holder_snapshot 缺失的写列
-- ------------------------------------------------------------
-- 现象：phase_chain_holder_scrape.py 的 UPSERT 写入
--   top_holders_json / tier_distribution_json / source_url
-- 但线上表仍停留在旧迁移 schema（alter_add_onchain_monitor.sql），
-- 缺这 3 列，导致每个资产 INSERT 阶段报
--   UndefinedColumn: column "top_holders_json" ... does not exist
-- 整条链上持仓管线（WF_CHAIN_HOLDER_SNAPSHOT）全部失败。
-- 幂等补齐即可恢复写入。
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'biz'
          AND table_name   = 'onchain_holder_snapshot'
          AND column_name  = 'top_holders_json'
    ) THEN
        ALTER TABLE biz.onchain_holder_snapshot ADD COLUMN top_holders_json JSONB;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'biz'
          AND table_name   = 'onchain_holder_snapshot'
          AND column_name  = 'tier_distribution_json'
    ) THEN
        ALTER TABLE biz.onchain_holder_snapshot ADD COLUMN tier_distribution_json JSONB;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'biz'
          AND table_name   = 'onchain_holder_snapshot'
          AND column_name  = 'source_url'
    ) THEN
        ALTER TABLE biz.onchain_holder_snapshot ADD COLUMN source_url TEXT;
    END IF;
END $$;
