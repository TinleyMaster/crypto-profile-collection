-- ============================================================================
-- fix_115_unlock_scraped_at_semantics.sql
-- 目的：给 biz.asset_token_unlocks 的两个时间列加 COMMENT，消除「字段名骗人」
--       导致的误判（HC-012 沉淀）。
--
-- 背景（2026-10-26 真实踩坑）：
--   审计时按 scraped_at 统计「数据新鲜度」，得到「全库 1383 行无一 7 天内更新，
--   ARB 已 7 周未更新」的结论，差点据此判定整条解锁链停摆。
--   实读 phase_chain_token_unlocks.py::save_to_db() 后推翻：
--       INSERT ... VALUES (..., NOW(), NOW(), ...)         -- 首次写入两列同值
--       ON CONFLICT (asset_id) DO UPDATE SET
--           ... last_attempt_at = NOW(), updated_at = NOW()  -- ← 没有 scraped_at
--   ⇒ scraped_at 只在**首次 INSERT** 写一次，实质是「首次收录时间」，
--     与「最后一次成功抓取时间」无关。真正可信的新鲜度字段是 updated_at。
--
--   实测（按 updated_at 口径）：ok 类 607 行中 598 行今日更新；
--   「有事件数据但 updated_at 超 7 天」= 0 行 ⇒ 数据面健康。
--
-- 本迁移**只加注释，不改语义、不写数据**。
-- 为什么不直接把 scraped_at 改成 NOW()：
--   1) 那样 scraped_at 与 updated_at 完全同义，两列冗余，纯增维护成本；
--   2) "首次收录时间" 本身有价值（可回答「这个资产何时纳入解锁覆盖」）；
--   3) 真正的缺陷是「字段语义未被记录」，不是「值不对」——补文档即可，
--      动 UPSERT 等于为一个误读去改一个正确运行的写路径。
--
-- 幂等：COMMENT ON 可重复执行。
-- 属主：navicat 非表 owner（owner=postgres），但 COMMENT ON COLUMN 需 owner 权限，
--       故本文件需以 owner 身份执行（或由 postgres 侧 apply）。
-- ============================================================================

COMMENT ON COLUMN biz.asset_token_unlocks.scraped_at IS
    '首次收录时间（≈ first_seen）。⚠️ 不是最后抓取时间：UPSERT 的 ON CONFLICT DO UPDATE 不更新本列。判断数据新鲜度请用 updated_at，勿用本列。';

COMMENT ON COLUMN biz.asset_token_unlocks.updated_at IS
    '最后写入时间。判断解锁数据新鲜度的唯一可信字段（每次 upsert 都会刷新为 NOW()）。';

COMMENT ON COLUMN biz.asset_token_unlocks.last_attempt_at IS
    '最后一次抓取尝试时间（含失败尝试）。与 updated_at 配合可区分「尝试了但没写成」与「写成了」。';

COMMENT ON COLUMN biz.asset_token_unlocks.crawl_status IS
    '抓取状态：ok=成功 / not_found=站点无该币（墓碑，batch 不重试）/ parse_empty=页面解析为空（HC-007 护栏，不覆盖已有非空数据）/ fail_timeout=失败超时。⚠️ crawl_status=ok 本身不证明数据新，需配 updated_at 判断。';
