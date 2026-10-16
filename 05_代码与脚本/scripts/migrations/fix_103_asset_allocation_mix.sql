-- fix_103: 代币分配构成表 + 解锁事件受益人类别补齐（幂等）
--
-- 动机：biz.asset_unlock_event.beneficiary_type 长期全空 → 无法区分
--       「团队/VC 解锁（通常砸盘）」vs「生态/社区解锁（通常无碍）」，是本指标最大信息损失点。
-- 数据源：biz.asset_tokenomics.allocation_json（777 资产，含 Team/Investors/Ecosystem/Community 等类别）
--         + biz.asset_unlock_event.raw_ref->>'label'（76 条精确标签）。
-- 落点：
--   1) 新表 biz.asset_allocation_mix —— 资产级受益人构成（团队/投资人/社区/生态/基金会/公众/流动性/其他 占比）
--   2) asset_unlock_event 增列 beneficiary_source（标记类别来源：label / asset_level_inferred / unspecified）
--   3) asset_unlock_reaction 增列 insider_pct / dominant_beneficiary / allocation_mix（快照冗余，供看板直接筛）

BEGIN;

-- 1) 资产级受益人构成
CREATE TABLE IF NOT EXISTS biz.asset_allocation_mix (
    asset_id            BIGINT PRIMARY KEY,
    team_pct            NUMERIC,
    investor_pct        NUMERIC,
    insider_pct         NUMERIC,          -- team + investor（内部分配）
    community_pct       NUMERIC,
    ecosystem_pct       NUMERIC,
    foundation_pct      NUMERIC,
    public_pct          NUMERIC,
    liquidity_pct       NUMERIC,
    other_pct           NUMERIC,
    outsider_pct        NUMERIC,          -- community+ecosystem+foundation+public+liquidity
    dominant_bucket     VARCHAR(24),      -- 占比最高的桶
    n_categories        INTEGER,
    confidence          NUMERIC,
    source              VARCHAR(32) NOT NULL DEFAULT 'allocation_json',
    method_version      VARCHAR(8) NOT NULL DEFAULT 'v1',
    mix                 JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 原始类别→pct 明细
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alloc_mix_dominant ON biz.asset_allocation_mix (dominant_bucket);

-- 2) 解锁事件：类别来源标记
ALTER TABLE biz.asset_unlock_event
    ADD COLUMN IF NOT EXISTS beneficiary_source VARCHAR(32);

-- 3) 解锁反应快照：冗余分配构成（下游看板免 JOIN）
ALTER TABLE biz.asset_unlock_reaction
    ADD COLUMN IF NOT EXISTS insider_pct          NUMERIC,
    ADD COLUMN IF NOT EXISTS dominant_beneficiary VARCHAR(24),
    ADD COLUMN IF NOT EXISTS allocation_mix       JSONB;

COMMIT;

-- 幂等说明：全部 IF NOT EXISTS，可重复执行。
