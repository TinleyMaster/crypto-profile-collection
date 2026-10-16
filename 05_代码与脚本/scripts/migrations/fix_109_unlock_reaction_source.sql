-- fix_109: 解锁反应表 v3 —— 受益人来源透明化（CM-UNLOCK-001）
--
-- 背景
-- ----
-- fix_107 给 biz.asset_unlock_reaction 加了 next_unlock_beneficiary（源 = tokenomics hero，
-- 事件级、日期精确）。本轮接入 CMC token-unlocks（覆盖更广），故：
--   1. next_unlock_beneficiary 取值改为「tokenomics hero 优先，缺失则退 CMC」；
--   2. 新增 next_unlock_source 明确标注该值来自哪个源，避免两源混淆。
--
-- 取值优先级（v3）
--   tokenomics_hero : biz.asset_unlock_recipient_mix 命中「下一解锁日」→ 事件级精确
--   cmc_next        : biz.asset_unlock_next_cmc 的 nextUnlockedDetail 归桶 → 事件级（另一源）
--   NULL            : 两源都没有
--
-- 幂等：可重复执行。

ALTER TABLE biz.asset_unlock_reaction
    ADD COLUMN IF NOT EXISTS next_unlock_source VARCHAR(24);

COMMENT ON COLUMN biz.asset_unlock_reaction.next_unlock_source IS
    'next_unlock_beneficiary 的来源：tokenomics_hero（app.tokenomics.com 解锁页 hero）| cmc_next（coinmarketcap token-unlocks）；NULL=无。';
