-- ============================================================
-- BACKFILL-1（TXQUAL-001 P0）：存量 onchain_transfer_log 标脏
-- 对已知脏数据 UPDATE is_suspect = TRUE，使下游聚合
-- `WHERE (is_suspect IS NOT TRUE OR is_suspect IS NULL)` 生效。
--
-- 复验（2026-09-07）发现：DDL 默认 False 使 77107 行存量全为
-- False（"未检查"被误判"已确认干净"），下游仍纳入 9913 行脏数据。
-- 本脚本补齐存量标脏，与采集端增量 sanity check 配套闭环。
--
-- 铁律：先 SELECT 预估行数，再在维护窗口执行 UPDATE；
--       每步 UPDATE 独立事务，可单独回滚。
-- ============================================================

-- 0) 预估：各类脏数据行数（复验基线：去重后约 9913 行）
SELECT 'P0-2 脏时间戳' AS cls, COUNT(*) FROM biz.onchain_transfer_log
  WHERE block_timestamp < '2009-01-01' OR block_timestamp > NOW() + INTERVAL '1 day'
UNION ALL
SELECT 'P0-1 极值(value_usd>1e12)', COUNT(*) FROM biz.onchain_transfer_log
  WHERE value_usd > 1e12
UNION ALL
SELECT 'P1-1 NULL价格(value_usd IS NULL)', COUNT(*) FROM biz.onchain_transfer_log
  WHERE value_usd IS NULL
UNION ALL
SELECT 'P1-2 历史阈值漂移(value_usd<50000)', COUNT(*) FROM biz.onchain_transfer_log
  WHERE value_usd IS NOT NULL AND value_usd < 50000;

-- ============================================================
-- 1) P0-2 脏时间戳（年份 < 2009 或未来时间）：标脏
-- ============================================================
BEGIN;
UPDATE biz.onchain_transfer_log SET is_suspect = TRUE
WHERE block_timestamp < '2009-01-01' OR block_timestamp > NOW() + INTERVAL '1 day';
COMMIT;

-- ============================================================
-- 2) P0-1 极值（value_usd > 1e12，物理不可能）：标脏
-- ============================================================
BEGIN;
UPDATE biz.onchain_transfer_log SET is_suspect = TRUE
WHERE value_usd > 1e12;
COMMIT;

-- ============================================================
-- 3) P1-1 价格缺失（value_usd IS NULL，按 P1-1 决策隔离）：标脏
-- ============================================================
BEGIN;
UPDATE biz.onchain_transfer_log SET is_suspect = TRUE
WHERE value_usd IS NULL;
COMMIT;

-- ============================================================
-- 4) P1-2 历史阈值漂移（value_usd < 50000 旧阈值残留）：标脏
--    （若已回填 threshold_used，可用 threshold_used < 50000 精确区分）
-- ============================================================
BEGIN;
UPDATE biz.onchain_transfer_log SET is_suspect = TRUE
WHERE value_usd IS NOT NULL AND value_usd < 50000;
COMMIT;

-- ============================================================
-- 5) 回填 threshold_used（P1-2 口径区分）
-- ============================================================
BEGIN;
UPDATE biz.onchain_transfer_log SET threshold_used = 5000
WHERE value_usd IS NOT NULL AND value_usd < 50000
  AND (threshold_used IS NULL OR threshold_used != 5000);
UPDATE biz.onchain_transfer_log SET threshold_used = 50000
WHERE value_usd IS NOT NULL AND value_usd >= 50000
  AND (threshold_used IS NULL OR threshold_used != 50000);
COMMIT;

-- ============================================================
-- 6) 复验：确认无遗漏（应返回 0 行已知脏数据）
-- ============================================================
SELECT '残留脏时间戳' AS cls, COUNT(*) FROM biz.onchain_transfer_log
  WHERE is_suspect IS NOT TRUE AND (block_timestamp < '2009-01-01' OR block_timestamp > NOW() + INTERVAL '1 day')
UNION ALL
SELECT '残留极值', COUNT(*) FROM biz.onchain_transfer_log
  WHERE is_suspect IS NOT TRUE AND value_usd > 1e12
UNION ALL
SELECT '残留NULL价格', COUNT(*) FROM biz.onchain_transfer_log
  WHERE is_suspect IS NOT TRUE AND value_usd IS NULL;