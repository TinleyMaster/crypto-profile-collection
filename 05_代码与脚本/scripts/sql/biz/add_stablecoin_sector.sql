-- 新增 stablecoin 赛道：更新 CHECK 约束
-- 先删旧约束再加新的（PostgreSQL 不支持 ALTER CHECK）

ALTER TABLE biz.asset_sector
    DROP CONSTRAINT IF EXISTS chk_asset_sector_sector;

ALTER TABLE biz.asset_sector
    ADD CONSTRAINT chk_asset_sector_sector CHECK (
        sector IN ('l1','l2','defi','meme','gamefi','rwa','ai','stablecoin',
                   'cex_token','derivatives','depin','infra','other')
    );
