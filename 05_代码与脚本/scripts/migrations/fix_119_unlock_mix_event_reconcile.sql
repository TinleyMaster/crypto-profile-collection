-- fix_119_unlock_mix_event_reconcile.sql
-- HC-015（2026-10-27）：mix 与事件表反向对账 —— 把「已抓到但没回写」的事件级构成补回事件表
--
-- 背景（发现路径）
-- -------------
-- HC-014 修完三处叠加缺陷后，reaction 的 next_unlock_beneficiary 已有 326/552 来自
-- tokenomics_hero。但做消费端一致性核对时发现 6 行「reaction 说下次是 community/foundation，
-- 事件表同一行却是 unspecified」——两处对同一事实给了不同答案。
--
-- 根因不是谁算错，而是**写入顺序**：
--   backfill_unlock_recipients 落的 mix 行带 `unlock_date = hero_date or event_unlock_date`。
--   当某天跑的时候，事件表里**还没有**那个未来解锁日的事件行，脚本写 mix 成功、
--   但 `UPDATE ... WHERE asset_id=? AND unlock_date=?` 影响 0 行（且当时无告警）。
--   之后 sync_unlock_events_from_json 才把该日事件补进来，beneficiary_type 为空 /
--   source=unspecified ⇒ 数据其实早在库里（biz.asset_unlock_recipient_mix），只是没回写。
--
-- 实测面（2026-10-27，全库）
-- ------------------------
--   mix 覆盖 (asset_id, unlock_date) 组合            520
--     其中 事件源已是精确值（无需动）                 379
--     事件源 unspecified                            113
--     事件源 asset_level_only                        26
--     无对应事件行（幽灵日期，不处理）                   2
--   ⇒ 可回填目标 pair 139 个、涉及事件行 152 行、129 个资产。
--
-- 语义正确性
-- --------
--   mix 行由 tokenomics.com hero「Unlock Recipients」抓取，描述的就是**该 unlock_date
--   这一次解锁**的构成 ⇒ 把主导桶写回同一天的事件行，是「用该日的证据标该日」，不是外推。
--   因此本 migration 只回填 mix 里**确实存在**的日期，不做任何日期推断。
--
-- 幂等性
-- -----
--   条件里带 `beneficiary_source NOT IN ('event_recipients','label','tokenomics_unlock_hero')`，
--   重复执行只会命中尚未精确标注的行；已标注的不会被反复覆写。
--
-- 权限
-- ----
--   biz.asset_unlock_event 的 owner 是 navicat（可自行 apply）。
--   若用 postgres 执行，仍需确认表 owner 不阻碍 DML。
--
-- 不变式（apply 后用第 3 段校验）
--   ∀ (asset_id, unlock_date) ∈ mix：事件行若存在，则 beneficiary_source 必为精确源。
--   ⇒ 任何一次 backfill 之后都不该出现「mix 有构成、事件行还写 unspecified」。

BEGIN;

-- ── 1) 反向回填：mix 的主导桶 → 同日期事件行（仅当事件侧尚无精确来源）──────────
WITH mx AS (
    SELECT asset_id,
           unlock_date,
           (array_agg(bucket ORDER BY pct DESC, category))[1] AS dom_bucket,
           jsonb_agg(jsonb_build_object('category', category,
                                        'bucket',   bucket,
                                        'pct',      pct)
                     ORDER BY pct DESC, category) AS mix_json
    FROM biz.asset_unlock_recipient_mix
    GROUP BY asset_id, unlock_date
)
UPDATE biz.asset_unlock_event e
   SET beneficiary_type   = mx.dom_bucket,
       beneficiary_source = 'tokenomics_unlock_hero',
       raw_ref            = COALESCE(e.raw_ref, '{}'::jsonb)
                            || jsonb_build_object('tokenomics_recipients', mx.mix_json),
       updated_at         = NOW()
  FROM mx
 WHERE e.asset_id    = mx.asset_id
   AND e.unlock_date = mx.unlock_date
   AND mx.dom_bucket IS NOT NULL
   AND (e.beneficiary_source IS NULL
        OR e.beneficiary_source NOT IN ('event_recipients', 'label', 'tokenomics_unlock_hero'));

-- ── 2) 幽灵日期：mix 有行但事件表没有该日 —— 只登记不改数 ────────────────────
--     这些行不会污染 reaction（reaction 的 next_unlock 只取事件表日期），
--     但会在未来某天事件补进来时自动被第 1 段覆盖，故无需删除。
COMMENT ON TABLE biz.asset_unlock_recipient_mix IS
    '解锁事件级受益人构成（资产 × 解锁日 × 类别）。源 tokenomics.com 解锁页 hero「Unlock Recipients」。'
    ' 不变式：当日事件行若存在，beneficiary_source 必须是精确源（fix_119 反向对账维护）。'
    ' 与 biz.asset_unlock_event 的关系：mix 是「构成明细」，事件表是「事实行」，两者按 (asset_id, unlock_date) 一一对应。';

COMMIT;

-- ── 3) 校验（apply 后应返回 0 行）────────────────────────────────────────────
-- SELECT e.asset_id, e.unlock_date, e.beneficiary_source
--   FROM biz.asset_unlock_recipient_mix m
--   JOIN biz.asset_unlock_event e
--     ON e.asset_id = m.asset_id AND e.unlock_date = m.unlock_date
--  WHERE e.beneficiary_source IS NULL
--     OR e.beneficiary_source NOT IN ('event_recipients', 'label', 'tokenomics_unlock_hero')
--  GROUP BY 1,2,3;
