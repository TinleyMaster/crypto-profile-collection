-- FEAT-SECTOR-002: 叙事板块→资产映射表
-- 从 src_cmc.cmc_category_member + core.asset_source_map 派生，打通「叙事板块 → 代币」下钻路径
-- 日级快照，支持按日期回溯成分

CREATE TABLE IF NOT EXISTS biz.sector_narrative_asset (
    narrative           TEXT NOT NULL,         -- 叙事名（统一用 NARRATIVE_WATCHLIST 中的标准名，如 'Zero Knowledge'）
    cmc_category_id     TEXT NOT NULL,         -- CMC 分类 ID
    cmc_category_name   TEXT NOT NULL,         -- CMC 分类显示名（可能与 narrative 标准名不同）
    asset_id            BIGINT,                -- 关联 core.asset.asset_id（未匹配到则 NULL）
    cmc_id              BIGINT NOT NULL,       -- CMC 代币 ID
    symbol              TEXT,                  -- CMC 代币符号
    name                TEXT,                  -- CMC 代币名称
    rank_in_category    INTEGER,               -- 在分类内的市值排名
    market_cap          NUMERIC(20,2),         -- 代币市值
    weight_pct          NUMERIC(8,4),          -- 在板块内的市值占比（相对于该分类总市值）
    percent_change_24h  NUMERIC(12,6),         -- 24h 涨跌幅
    as_of_date          DATE NOT NULL,         -- 快照日期
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (narrative, as_of_date, cmc_id)
);

CREATE INDEX IF NOT EXISTS idx_sna_date ON biz.sector_narrative_asset(as_of_date);
CREATE INDEX IF NOT EXISTS idx_sna_asset ON biz.sector_narrative_asset(asset_id);
CREATE INDEX IF NOT EXISTS idx_sna_narr_date ON biz.sector_narrative_asset(narrative, as_of_date);

-- 注释
COMMENT ON TABLE biz.sector_narrative_asset IS '叙事板块→资产映射（FEAT-SECTOR-002）。从 src_cmc.cmc_category_member 派生，关联 core.asset，打通叙事榜下钻路径。';
COMMENT ON COLUMN biz.sector_narrative_asset.narrative IS '叙事标准名（与 NARRATIVE_WATCHLIST 对齐）';
COMMENT ON COLUMN biz.sector_narrative_asset.weight_pct IS '该代币在板块内的市值占比，总市值为所有成员市值之和';
