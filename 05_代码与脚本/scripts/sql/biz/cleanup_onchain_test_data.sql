-- ============================================================
-- 清理链上转账测试数据（0xtest*）
--
-- 问题：biz.onchain_transfer_log 中有 3 条测试数据（tx_hash 以 0xtest 开头），
--       这些假数据会污染链上告警面板。
--
-- 操作：删除所有 tx_hash 以 '0xtest' 开头的记录。
-- ============================================================

BEGIN;

-- 先统计要删除的数量
SELECT COUNT(*) AS test_data_count
FROM biz.onchain_transfer_log
WHERE tx_hash LIKE '0xtest%';

-- 删除测试数据
DELETE FROM biz.onchain_transfer_log
WHERE tx_hash LIKE '0xtest%';

-- 验证：删除后剩余数量
SELECT COUNT(*) AS remaining_count
FROM biz.onchain_transfer_log;

COMMIT;
