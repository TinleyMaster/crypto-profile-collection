-- fix_082: 链上大额转账扫描独立水位表
--
-- 背景：原逻辑用 MAX(block_number) of 已入库大额行作为游标，导致
--   1) 价格缺失/低于阈值被丢弃的转账仍让游标前进，形成永久缺口；
--   2) 区间头部有大额入库后游标跳到最新，同区间中段大额被永久跳过。
-- 本表把「扫描覆盖到哪里」与「入库了哪些大额」解耦。
--
-- 设计：
--   - cursor_type='block'：按区块推进的源（etherscan / rpc）。cursor_value 记录
--     该合约已确认扫描到的最大区块号（从旧到新推进）；下次扫描从 cursor_value+1 开始。
--   - cursor_type='timestamp'：blockNumber 恒为 0 的 explorer 源（ethplorer/binplorer）。
--     cursor_value 记录已覆盖到的最旧时间戳（秒级 epoch），仅作审计参考；
--     因该 API 不支持时间/区块范围查询，实际覆盖仍靠高频重访 + 单次 limit=1000。
--
-- 推进规则（写入侧保证）：
--   1) 仅当本次 API 调用成功返回且完成本地处理后才更新水位；
--   2) block 水位 = 本次扫描到的原始转账（未过滤阈值/价格）中的最大 block_number；
--   3) 若本次返回为空，但 API 调用成功，水位推进到请求窗口的 end_block；
--   4) 调用失败、超时、被限流时不更新水位，避免把半截数据当成已覆盖。
--
-- 幂等：CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS。
-- 应用方式：
--   python 05_代码与脚本/scripts/apply_migration.py fix_082_onchain_transfer_scan_cursor.sql

CREATE TABLE IF NOT EXISTS biz.onchain_transfer_scan_cursor (
    chain            TEXT        NOT NULL,
    contract_address TEXT        NOT NULL,
    cursor_type      TEXT        NOT NULL DEFAULT 'block',
    cursor_value     BIGINT      NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain, contract_address, cursor_type)
);

CREATE INDEX IF NOT EXISTS idx_onchain_transfer_scan_cursor_updated
    ON biz.onchain_transfer_scan_cursor (updated_at);

COMMENT ON TABLE biz.onchain_transfer_scan_cursor IS
  '链上大额转账扫描独立水位。与 biz.onchain_transfer_log 解耦，避免「只存大额」导致游标失真。';

COMMENT ON COLUMN biz.onchain_transfer_scan_cursor.cursor_type IS
  'block = 区块推进（etherscan/rpc 等真实 blockNumber 源）；'
  'timestamp = 时间戳推进（ethplorer/binplorer 等 blockNumber 恒为 0 的源，仅作审计）。';

COMMENT ON COLUMN biz.onchain_transfer_scan_cursor.cursor_value IS
  'block 类型 = 已确认扫描到的最大区块号；timestamp 类型 = 已覆盖的最旧时间戳（秒级 epoch）。';
