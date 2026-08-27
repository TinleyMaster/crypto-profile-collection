-- 代币赛道标签表 + core.asset 主赛道字段
-- 由 scripts/bin/refresh_asset_sectors.py 从 CMC tags/category_hint 归一化写入。
-- 赛道枚举对齐 src/crypto_research/mapping/sector.py 的 SECTORS。

-- 1) 多标签赛道表（一个代币可命中多个赛道）
CREATE TABLE IF NOT EXISTS biz.asset_sector (
    asset_id     BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    sector       VARCHAR(32) NOT NULL,
    source       VARCHAR(16) NOT NULL DEFAULT 'cmc',
    confidence   NUMERIC(3,2) NOT NULL DEFAULT 0.5,
    is_primary   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, sector, source),
    CONSTRAINT chk_asset_sector_sector CHECK (
        sector IN ('l1','l2','defi','meme','gamefi','rwa','ai',
                   'cex_token','derivatives','depin','infra','other')
    )
);

CREATE INDEX IF NOT EXISTS idx_asset_sector_asset ON biz.asset_sector (asset_id);
CREATE INDEX IF NOT EXISTS idx_asset_sector_sector ON biz.asset_sector (sector);

-- 2) core.asset 冗余主赛道字段（供高频查询快速 join，避免 UNNEST 多标签表）
ALTER TABLE core.asset ADD COLUMN IF NOT EXISTS primary_sector VARCHAR(32) NOT NULL DEFAULT 'other';

CREATE INDEX IF NOT EXISTS idx_core_asset_primary_sector ON core.asset (primary_sector);
