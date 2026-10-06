-- ============================================================
-- fix_081_asset_contract_onchain_verify.sql
-- 合约身份校验元数据（链上异动告警可信度修复，2026-10-06）
--
-- 背景（实测 2026-10-06）：
--   core.asset_contract_map 把同一个合约地址挂到了多个链上，
--   其中部分地址上部署的是「同名仿冒合约」——symbol/name 与真实资产一致，
--   但合约自身的 supply 相差几个数量级：
--     DOT  0x8d010bf9… @base/arbitrum/optimism：totalSupply = 4.69e20
--          真实 Polkadot total_supply = 1.70e9        → 差 2.8e11 倍
--     NEX  0x365de036… @bsc                  ：totalSupply = 4.37e12
--          真实 Nash     total_supply = 5.0e7         → 差 8.7e4 倍
--   采集侧把「仿冒合约的数量 × 真实资产的单价」当作美元额入库，
--   凭空造出 $1.16B / $1.32B 的伪额，污染链上异动页的未打标漏报口径。
--
--   core.asset_contract.decimals 此前 100% 为 NULL（16,398 行），
--   说明设计中的「P0-1 精度兜底 + 金额量纲 sanity check」从未真正校验过。
--
-- 本迁移只加两列缓存位，供 phase_chain_transfer_monitor.py 落库链上实测值：
--   onchain_total_supply : 合约自身 totalSupply()（人类单位，已按 decimals 归一）
--   onchain_checked_at   : 校验时间；非 NULL 表示「已取到链上值」，
--                          取不到时保持 NULL 以便下次重试（不把网络抖动固化）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS，可重复执行。
-- ============================================================

ALTER TABLE core.asset_contract
    ADD COLUMN IF NOT EXISTS onchain_total_supply NUMERIC,
    ADD COLUMN IF NOT EXISTS onchain_checked_at   TIMESTAMPTZ;

COMMENT ON COLUMN core.asset_contract.onchain_total_supply IS
    '合约自身 totalSupply()（人类单位）；与 core.asset 权威 supply 相差 >100 倍 → 判定同名仿冒合约';
COMMENT ON COLUMN core.asset_contract.onchain_checked_at IS
    '链上校验时间；NULL 表示尚未取到链上值（需重试）';
