-- 为搜索功能添加 pg_trgm 索引，加速 ILIKE '%query%' 模糊搜索
-- 解决 search_assets 全表扫描问题（core.asset 表 17901 条记录）

-- 1. 启用 pg_trgm 扩展（如果尚未启用）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. 为 canonical_symbol 创建 GIN 索引
CREATE INDEX IF NOT EXISTS idx_asset_canonical_symbol_trgm
    ON core.asset USING gin (canonical_symbol gin_trgm_ops);

-- 3. 为 canonical_name 创建 GIN 索引
CREATE INDEX IF NOT EXISTS idx_asset_canonical_name_trgm
    ON core.asset USING gin (canonical_name gin_trgm_ops);