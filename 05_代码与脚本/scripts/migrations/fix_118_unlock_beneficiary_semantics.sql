-- fix_118_unlock_beneficiary_semantics.sql
-- HC-014（2026-10-27）：解锁受益人字段的语义澄清 + 口径变更落库说明
--
-- 背景（三处叠加缺陷，导致「内部抛压」对 89% 的标的算不出来）
-- ---------------------------------------------------------------
-- 1) sync_unlock_events_from_json 的 UPSERT 写成
--        beneficiary_type = EXCLUDED.beneficiary_type
--    而源端多为 NULL → 每次同步都把 backfill 已算好的精准值打回 NULL；同时
--        raw_ref = EXCLUDED.raw_ref        （整体替换）
--    又把 backfill 写入 raw_ref 的 tokenomics_recipients 证据一并销毁。
--    实测：source=tokenomics_unlock_hero 的 296 行中，tokenomics_recipients
--    存在率 0/296 —— 标签还在、证据全无。与 HC-012 的 scraped_at 同属
--    「UPSERT 的 SET 语义与字段生命周期不匹配」。
--
-- 2) build_allocation_mix.classify() 词表缺 insider，而 tokenomics hero 区
--    最常用的超级组名恰是 Insiders（官方口径 = Team and Advisors）→
--    classify(Insiders) 曾返回 other，把内部抛压判成无关类别。
--    修复后 label 来源的 typed 行数从 11 → 76（全量命中）。
--
-- 3) 调用处把 allocations 当文本喂给推断函数，而该字段实际是**类别计数**
--    （1 / 2 / 4）→ 推断恒返回 NULL。
--
-- 口径变更（重要）
-- ---------------
-- 原做法：用资产级 dominant_bucket 写事件级 beneficiary_type，
--         标 source=asset_level_inferred。
-- 实测：157 个「资产级主桶 + 事件级精确明细」双全样本中，仅三分之一一致；
--   典型反例
--     ARKM 资产级=community，事件级 Insiders 35.8 + Private Investors 27.2 = 63 insider
--     ATH  资产级=other，    事件级 Insiders 43.3 + Private Investors 42.7 = 86 insider
--   方向性错误会把内部抛压洗成社区解锁（假阴性）。
-- 现做法：资产级只标 source=asset_level_only，**不写事件级 type**；
--   事件级 type 一律由 raw_ref 里的精确明细按 pct 加权回填
--   （bin/backfill_beneficiary_event_level.py，source=event_recipients）。
--
-- 幂等：COMMENT ON 可重复执行。
-- 权限：三张表 owner=navicat，与迁移执行身份一致，无需提权。

COMMENT ON COLUMN biz.asset_unlock_event.beneficiary_type IS $doc$
本次解锁的**主导受益人类别**，归一化到 8 桶之一：
team / investor / community / ecosystem / foundation / public / liquidity / other。

⚠️ 只有当 beneficiary_source 属于「事件级精确来源」时才可信：
    event_recipients         从 raw_ref.recipients 按 pct 加权（首选）
    label                    源站 raw_ref.label 精确类别
    tokenomics_unlock_hero   tokenomics.com hero 区「Unlock Recipients」
    sync_recipients          sync 从事件 JSON 的结构化 recipients 推断
其余来源必须视为「无法判断」，不得当作「非 insider」：
    asset_level_only         仅有该资产全生命周期分配构成，与本轮解锁流向无关
                             （实测三分之二与事件级精确值方向相反）
    unspecified / NULL       无任何受益人信息

下游计算内部抛压时，请显式限定 source 白名单，不要只判断 type IS NOT NULL。
$doc$;

COMMENT ON COLUMN biz.asset_unlock_event.beneficiary_source IS $doc$
beneficiary_type 的取值来源，决定该值可信度（HC-014，2026-10-27 定稿）。

事件级精确（可用于 insider 判定）：
    event_recipients         从 raw_ref 明细按 pct 加权重算
                             （bin/backfill_beneficiary_event_level.py）
    label                    raw_ref.label 精确类别（build_allocation_mix 3a）
    tokenomics_unlock_hero   tokenomics.com hero 快照（backfill_unlock_recipients.py）
    sync_recipients          sync_unlock_events_from_json 的结构化推断
仅资产级（**不得**用于 insider 判定）：
    asset_level_only         该资产有生命周期分配构成，但本次解锁流向未知
无信息：
    unspecified              确认无可用的受益人信息
    NULL                     历史遗留，等价 unspecified

🔴 历史包袱：曾用 asset_level_inferred 表示「资产级粗推断写进事件级 type」。
   该口径已废止（理由见本文件头部）。存量行已在 2026-10-27 统一改为
   asset_level_only 并清空 type；若再见到 asset_level_inferred，
   说明有脚本回退到旧口径。
$doc$;

COMMENT ON COLUMN biz.asset_allocation_mix.insider_pct IS $doc$
**存量**口径：该资产全生命周期代币分配里 team + investor 的占比
（%，来自 allocation_json 分类）。

⚠️ 与 biz.asset_unlock_event 的「本次解锁 insider 占比」是两件事：
   存量回答「内部人手里一共有多少筹码」，
   事件级回答「这一期解锁里有多少流向内部人」。
实测二者方向常常相反（ARKM 存量 40.5 但当期 63；ATH 存量 29 但当期 86），
因此本列只能作背景参考，**不得替代事件级判定**。
$doc$;

COMMENT ON COLUMN biz.asset_allocation_mix.dominant_bucket IS $doc$
该资产全生命周期分配中占比最大的桶（8 桶制）。

⚠️ 只描述「总量结构」，不能推断「某次解锁流向谁」。曾被误用为事件级
beneficiary_type（source=asset_level_inferred），实测一致率仅约三分之一，
已废止该用法（见 fix_118）。
$doc$;
