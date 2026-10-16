-- fix_106: 解锁事件级受益人构成 biz.asset_unlock_recipient_mix（幂等）
--
-- 动机（投研 SOP P1.7 补齐）：上一轮把 unlock_type / beneficiary_type 从「全空」补到
--   「资产级推断」（biz.asset_allocation_mix.dominant_bucket），但仍是**整条代币生命周期**
--   的分配口径，不是「这一次解锁到底谁在卖」。SOP 最关心的恰恰是**下一次**解锁的受益人。
--
-- 数据源实测（2026-10-16）：
--   ✅ app.tokenomics.com/tokenomics/{slug}/unlocks 的 hero「Unlock Recipients」区块
--      —— 纯 HTTP 可取（gzip），含**下一次解锁**的类别构成（类别名 + 占比%），
--         以及 Next Unlock 日期 / USD 值 / 代币量。实测 ARB / ONDO / STRK / ZRO 全部解析成功。
--   ❌ tokenomist.ai 被 Cloudflare 拦截（"Just a moment..."，headless Playwright 亦不可过），
--        其事件级「类别」列暂不可得 → 历史事件仍只能沿用资产级推断，本表只覆盖**下一期**。
--
-- 与既有表的关系：
--   biz.asset_unlock_event.beneficiary_type  —— 事件行上的**主受益人桶**（本脚本回填，
--       beneficiary_source='tokenomics_unlock_hero'；build_allocation_mix.py 只覆盖
--       source IS NULL/'unspecified' 的行，故不会被反向冲掉）
--   本表                                      —— 完整构成（多行，可审计、可算 insider 占比）

BEGIN;

CREATE TABLE IF NOT EXISTS biz.asset_unlock_recipient_mix (
    asset_id     BIGINT      NOT NULL,
    unlock_date  DATE        NOT NULL,   -- 该次解锁日（来自 tokenomics hero「Next Unlock」）
    category     VARCHAR(64) NOT NULL,   -- tokenomics 原始类别名，如 'Insiders' / 'Private Investors'
    bucket       VARCHAR(24),            -- 归一化桶：team/investor/community/ecosystem/foundation/public/liquidity/other
    pct          NUMERIC(7,3),           -- 该类别占本次解锁的百分比
    source_code  VARCHAR(32) NOT NULL DEFAULT 'tokenomics_unlock_hero',
    source_url   TEXT,
    scraped_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, unlock_date, category)
);

CREATE INDEX IF NOT EXISTS idx_unlock_recipient_mix_date
    ON biz.asset_unlock_recipient_mix (unlock_date);
CREATE INDEX IF NOT EXISTS idx_unlock_recipient_mix_bucket
    ON biz.asset_unlock_recipient_mix (bucket);

COMMENT ON TABLE biz.asset_unlock_recipient_mix IS
    '解锁事件级受益人构成（下一期，源 app.tokenomics.com hero Unlock Recipients）；补 biz.asset_unlock_event 的资产级推断';

COMMIT;
