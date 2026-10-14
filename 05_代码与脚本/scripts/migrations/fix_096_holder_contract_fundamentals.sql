-- fix_096: 基本面因子落库扩展（持有者集中度明细 + 合约安全代理核查）
-- 目标：支撑基本面因子回测——每次投研/采集写入按日快照，形成时间序列
-- 幂等：IF NOT EXISTS / ADD COLUMN IF NOT EXISTS，可重复执行

BEGIN;

-- ============================================================
-- 1. onchain_holder_snapshot：补充集中度档位与 burn/流通口径
-- ============================================================
ALTER TABLE biz.onchain_holder_snapshot
    ADD COLUMN IF NOT EXISTS top1_concentration  NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS top5_concentration  NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS top20_concentration NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS burn_pct            NUMERIC(5,2),   -- burn 地址占总供应 %
    ADD COLUMN IF NOT EXISTS burned_amount       NUMERIC(24,2),  -- burn 数量（代币本位）
    ADD COLUMN IF NOT EXISTS circ_supply         NUMERIC(24,2);  -- 扣除 burn 后流通口径

COMMENT ON COLUMN biz.onchain_holder_snapshot.top1_concentration  IS 'Top1 持仓占比（%）';
COMMENT ON COLUMN biz.onchain_holder_snapshot.top5_concentration  IS 'Top5 持仓占比（%）';
COMMENT ON COLUMN biz.onchain_holder_snapshot.top20_concentration IS 'Top20 持仓占比（%）';
COMMENT ON COLUMN biz.onchain_holder_snapshot.burn_pct            IS 'burn 地址占总供应（%）';
COMMENT ON COLUMN biz.onchain_holder_snapshot.burned_amount       IS 'burn 数量（代币本位）';
COMMENT ON COLUMN biz.onchain_holder_snapshot.circ_supply         IS '扣除 burn 后流通口径（代币本位）';

-- 快照日期唯一性（幂等：同日重复采集不重复插入）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_holder_snapshot_asset_chain_contract_date'
    ) THEN
        ALTER TABLE biz.onchain_holder_snapshot
            ADD CONSTRAINT uq_holder_snapshot_asset_chain_contract_date
            UNIQUE (asset_id, chain, contract_address, snapshot_date);
    END IF;
END $$;

-- ============================================================
-- 2. onchain_holder_rank：Top 持有者明细快照（回测可算 HHI/分位因子）
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.onchain_holder_rank (
    rank_id        SERIAL PRIMARY KEY,
    snapshot_id    INTEGER NOT NULL REFERENCES biz.onchain_holder_snapshot(snapshot_id) ON DELETE CASCADE,
    rank           INTEGER NOT NULL,          -- 排名（1=最大持有者，含 burn）
    holder_address TEXT   NOT NULL,           -- 持有者地址
    balance        NUMERIC(24,2),             -- 持仓量（代币本位）
    balance_pct    NUMERIC(8,3),              -- 占总供应 %
    address_type   VARCHAR(16),               -- eoa / contract / burn / zero
    is_burn        BOOLEAN DEFAULT FALSE,
    CONSTRAINT uq_holder_rank UNIQUE (snapshot_id, rank)
);

COMMENT ON TABLE biz.onchain_holder_rank IS 'Top 持有者明细快照：随 onchain_holder_snapshot 按日累积，供集中度/HHI 等因子回测';
COMMENT ON COLUMN biz.onchain_holder_rank.address_type IS '地址类型：eoa/contract/burn/zero（链上 getCode 判定）';

CREATE INDEX IF NOT EXISTS idx_holder_rank_snapshot ON biz.onchain_holder_rank(snapshot_id);

-- ============================================================
-- 3. asset_contract_security：补充代理/合约属性核查字段
-- ============================================================
ALTER TABLE biz.asset_contract_security
    ADD COLUMN IF NOT EXISTS is_proxy           BOOLEAN,      -- 是否可升级代理
    ADD COLUMN IF NOT EXISTS proxy_type         VARCHAR(32),  -- 代理类型（eip1967/oz/transparent/unknown）
    ADD COLUMN IF NOT EXISTS implementation_addr TEXT,        -- EIP1967 implementation 地址
    ADD COLUMN IF NOT EXISTS contract_name      TEXT,         -- 合约名（如 PonsLauncherToken）
    ADD COLUMN IF NOT EXISTS creator_addr       TEXT,         -- 部署者地址
    ADD COLUMN IF NOT EXISTS contract_verified  BOOLEAN,      -- 是否已验证源码
    ADD COLUMN IF NOT EXISTS reputation         VARCHAR(16);  -- 浏览器信誉（ok/suspicious/scam）

COMMENT ON COLUMN biz.asset_contract_security.is_proxy           IS '是否可升级代理（链上 slot/接口核查）';
COMMENT ON COLUMN biz.asset_contract_security.proxy_type         IS '代理类型：eip1967/oz/transparent/unknown';
COMMENT ON COLUMN biz.asset_contract_security.implementation_addr IS 'EIP1967 implementation 地址（若为代理）';
COMMENT ON COLUMN biz.asset_contract_security.contract_name      IS '合约名';
COMMENT ON COLUMN biz.asset_contract_security.creator_addr       IS '部署者地址';
COMMENT ON COLUMN biz.asset_contract_security.contract_verified  IS '浏览器源码是否验证';
COMMENT ON COLUMN biz.asset_contract_security.reputation         IS '浏览器信誉：ok/suspicious/scam';

COMMIT;
