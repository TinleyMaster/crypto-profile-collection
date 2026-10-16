-- fix_107: 解锁反应表增加「下一次解锁的受益人构成」（事件级）
-- ---------------------------------------------------------------------------
-- 背景：UNLOCK-REACTION-001（biz.asset_unlock_reaction）此前只有**资产级推断**的
-- dominant_beneficiary（源自整条生命周期的 allocation_json）。但 SOP 第 9 节真正要
-- 回答的是：**下一次解锁，是谁的筹码在解锁？** —— 资产级口径会把「5 年后才解锁的团队
-- 份额」算进当下风险，误导判断。
--
-- 数据源：biz.asset_unlock_recipient_mix（tokenomics.com hero「Unlock Recipients」，
-- 键 = asset_id + unlock_date），由 backfill_unlock_recipients.py 落库。
--
-- 幂等：ADD COLUMN IF NOT EXISTS，可重复执行。
-- ---------------------------------------------------------------------------

ALTER TABLE biz.asset_unlock_reaction
    ADD COLUMN IF NOT EXISTS next_unlock_beneficiary VARCHAR(24);

ALTER TABLE biz.asset_unlock_reaction
    ADD COLUMN IF NOT EXISTS next_unlock_recipient_json JSONB;

COMMENT ON COLUMN biz.asset_unlock_reaction.next_unlock_beneficiary IS
    '下一次解锁的受益人主导桶（team/investor/community/ecosystem/foundation/public/liquidity/other）；源 biz.asset_unlock_recipient_mix，无事件级数据时为 NULL';

COMMENT ON COLUMN biz.asset_unlock_reaction.next_unlock_recipient_json IS
    '下一次解锁的受益人完整构成 [{"category","bucket","pct"},...]；源 biz.asset_unlock_recipient_mix';

-- 便于按「下一次解锁受益人」筛标的（如：下一次解锁由团队主导 = 潜在抛压）
CREATE INDEX IF NOT EXISTS idx_asset_unlock_reaction_next_beneficiary
    ON biz.asset_unlock_reaction (next_unlock_beneficiary)
    WHERE next_unlock_beneficiary IS NOT NULL;
