-- fix_108: CMC 下次解锁快照表（CM-UNLOCK-001）
--
-- 背景
-- ----
-- 现有事件级受益人来自 app.tokenomics.com 解锁页 hero，只覆盖 ~349 资产、且仅「下一次解锁」。
-- CMC 的 token-unlocks 页（https://coinmarketcap.com/token-unlocks/）通过 SSR 内嵌
-- __NEXT_DATA__ 的 token-unlocks-page-table 查询给出 **1326 币**，每币含：
--   nextUnlocked { tokenAmount, tokenAmountUsd, tokenAmountPercentage, date }
--   nextUnlockedDetail[] { allocationName, tokenAmount, tokenAmountUsd, vestingType }  ← 类别构成（事件级）
--   totalUnlockedPercentage / circulatingSupply / totalSupply / maxSupply / quotes ...
-- 覆盖更广（1013 币可用 biz.coin_basic.cmc_id 精确映射到 asset_id），并**新增两个信号**：
--   ① 下次解锁的美元金额 ② 占供应比例（解锁压力定量化）。
--
-- 本表：每资产一行 = 最新一次抓取的「下一次解锁」快照（幂等 upsert on asset_id）。
-- 只读快照，不改动 asset_unlock_event / asset_unlock_recipient_mix。
--
-- 幂等：可重复执行。

CREATE TABLE IF NOT EXISTS biz.asset_unlock_next_cmc (
    asset_id                BIGINT PRIMARY KEY,
    cmc_id                  BIGINT,
    symbol                  VARCHAR(32),
    slug                    VARCHAR(96),
    name                    VARCHAR(160),
    cmc_rank                INTEGER,

    -- 下一次解锁（CMC 口径）
    next_unlock_date        DATE,
    next_unlock_amount      NUMERIC(38, 8),
    next_unlock_usd         NUMERIC(24, 2),
    next_unlock_pct_supply  NUMERIC(14, 6),

    -- 总量口径
    total_unlocked_pct      NUMERIC(14, 6),
    circulating_supply      NUMERIC(38, 8),
    total_supply            NUMERIC(38, 8),
    max_supply              NUMERIC(38, 8),

    -- 类别构成（nextUnlockedDetail 归桶）
    allocations             JSONB,          -- 原始明细 [{allocationName, tokenAmount, tokenAmountUsd, vestingType}]
    vesting_types           TEXT[],         -- 去重后的 vestingType
    bucket_pct              JSONB,          -- {team: 0.53, investor: 0.35, ...}（按 USD 金额计）
    dominant_bucket         VARCHAR(24),    -- 金额占比最大的归一化桶
    insider_pct             NUMERIC(14, 6), -- team+investor 合计占比

    -- 行情快照
    price_usd               NUMERIC(28, 12),
    market_cap_usd          NUMERIC(24, 2),

    self_reported           BOOLEAN,
    as_of                   DATE NOT NULL DEFAULT CURRENT_DATE,
    source                  VARCHAR(32) NOT NULL DEFAULT 'cmc_token_unlocks',
    source_url              TEXT,
    raw                     JSONB,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aunc_next_date
    ON biz.asset_unlock_next_cmc (next_unlock_date);
CREATE INDEX IF NOT EXISTS idx_aunc_dominant
    ON biz.asset_unlock_next_cmc (dominant_bucket);
CREATE INDEX IF NOT EXISTS idx_aunc_insider
    ON biz.asset_unlock_next_cmc (asset_id)
    WHERE insider_pct IS NOT NULL;

COMMENT ON TABLE  biz.asset_unlock_next_cmc IS
    'CMC token-unlocks 页「下一次解锁」快照（含类别构成 + 美元金额 + 占供应比）；源 coinmarketcap.com/token-unlocks，经 biz.coin_basic.cmc_id 映射 asset_id。';
COMMENT ON COLUMN biz.asset_unlock_next_cmc.bucket_pct IS
    '按 USD 金额加权的 8 桶占比；桶词表复用 bin/build_allocation_mix.py 的 BUCKET_RULES。';
COMMENT ON COLUMN biz.asset_unlock_next_cmc.next_unlock_pct_supply IS
    '下次解锁量占流通供应的百分比（CMC tokenAmountPercentage）——解锁压力定量指标。';
