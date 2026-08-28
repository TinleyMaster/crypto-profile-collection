-- FIX-016: 代币解锁爬取 - 状态列 + %MCAP 语义列
-- 日期：2026-08-28
-- 背景：555dc85 修复 P1-1(负缓存)/P1-2(value_usd)/P2-1(%MCAP 语义)，需新增状态列。
-- 警告：必须先执行本迁移再部署新 binary，否则 INSERT 引用不存在列会直接 SQL 报错。

-- ============================================================
-- 第一步：asset_token_unlocks 加爬取状态列（P1-1 负缓存墓碑 + 冷却）
-- ============================================================

ALTER TABLE biz.asset_token_unlocks
    ADD COLUMN IF NOT EXISTS crawl_status     VARCHAR(20) NOT NULL DEFAULT 'ok';

ALTER TABLE biz.asset_token_unlocks
    ADD COLUMN IF NOT EXISTS last_attempt_at  TIMESTAMPTZ DEFAULT NOW();

COMMENT ON COLUMN biz.asset_token_unlocks.crawl_status
    IS '爬取状态：ok=成功 / not_found=站点未收录(墓碑) / parse_empty=overview有信号但事件空(疑似解析失败)';
COMMENT ON COLUMN biz.asset_token_unlocks.last_attempt_at
    IS '最近一次爬取尝试时间（not_found 冷却 30 天用）';

-- 回填存量：现有 62 行 tokenomist 数据标记为 ok（避免被误判为 pending 重爬）
UPDATE biz.asset_token_unlocks
SET crawl_status = 'ok', last_attempt_at = COALESCE(last_attempt_at, scraped_at)
WHERE crawl_status = 'ok';

-- ============================================================
-- 第二步：asset_unlock_event 加 %MCAP 语义列（P2-1）
-- ============================================================

ALTER TABLE biz.asset_unlock_event
    ADD COLUMN IF NOT EXISTS unlock_ratio_mcap NUMERIC;

COMMENT ON COLUMN biz.asset_unlock_event.unlock_ratio_mcap
    IS '解锁占市值比例（新站 tokenomics.com 的 % of MCAP）';

-- ============================================================
-- 验证
-- ============================================================

SELECT
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema='biz' AND table_name='asset_token_unlocks'
       AND column_name IN ('crawl_status','last_attempt_at')) AS token_unlocks_status_cols,
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema='biz' AND table_name='asset_unlock_event'
       AND column_name='unlock_ratio_mcap') AS unlock_event_mcap_col;