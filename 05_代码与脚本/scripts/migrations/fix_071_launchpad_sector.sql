-- fix_071_launchpad_sector.sql
-- 工单 SECTOR-RECLASS-001：新增 launchpad 赛道（打新 / 发射台），把它从 defi 中折出。
-- 背景：CMC tag「launchpad」/ CMC category_hint「launchpad」/ CG「launchpad」「surge launchpad」
--       / DL「launchpad」此前全部映射到 defi，导致 PONS(11114) 等发射台类资产
--       被 DeFi 蓝筹做估值对标（`get_sector_competitors`），赛道聚合也被稀释。
--
-- 本脚本只做一件事：扩 biz.asset_sector 的 CHECK 白名单。
--   · core.asset.primary_sector 无 CHECK 约束，无需变更；
--   · 必须先执行本脚本，再运行 run_refresh_sectors.py / refresh_asset_sectors.py，
--     否则新规则产出的 'launchpad' 标签行会违反约束、整批刷新失败。
--   · ⚠️ 白名单必须与 mapping/sector.py 的 SECTORS 逐值一致。生产现行约束含
--     'stablecoin'（add_stablecoin_sector.sql 所加，表内现存 1046 行）——本列表
--     **必须保留它**，否则 ADD CONSTRAINT 会因存量行校验失败而整条迁移报错。
-- 幂等：DROP CONSTRAINT IF EXISTS + 重建，可重复执行。
-- ⚠️ DDL 执行后立即 COMMIT（项目硬约束）：DROP/ADD CONSTRAINT 会取
--    AccessExclusiveLock，未及时提交将阻塞 biz.asset_sector 的全部读操作。
--
-- 回滚：把下面 CHECK 列表中的 'launchpad' 去掉重跑一次即可（需先确认表中
--       已无 sector='launchpad' 的行，否则 ADD CONSTRAINT 会校验失败）。

ALTER TABLE biz.asset_sector
    DROP CONSTRAINT IF EXISTS chk_asset_sector_sector;

ALTER TABLE biz.asset_sector
    ADD CONSTRAINT chk_asset_sector_sector CHECK (
        sector IN ('l1','l2','defi','launchpad','meme','gamefi','rwa','ai','stablecoin',
                   'cex_token','derivatives','depin','infra','other')
    );