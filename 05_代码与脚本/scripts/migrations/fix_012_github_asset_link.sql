-- FIX-012: GitHub 仓库-资产关联链路修复
-- 审计日期：2026-08-26
-- 问题：biz.github_repo_activity 有 1,267 行数据，但无 asset_id 外键，
--       core.asset_source_map 中也没有 github 源，导致资产级 GitHub 覆盖率 0%
--
-- 修复：
--   1. 创建 biz.asset_github_repo 多对多关联表
--   2. 从 doc_source_entry 回溯，为已有 github_repo_activity 数据建立关联
--   3. 标记每个资产的主仓库

-- ============================================================
-- 第一步：创建关联表
-- ============================================================

CREATE TABLE IF NOT EXISTS biz.asset_github_repo (
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    repo_id        BIGINT NOT NULL REFERENCES biz.github_repo_activity(id) ON DELETE CASCADE,
    owner_login    VARCHAR(256) NOT NULL,
    repo_name      VARCHAR(256) NOT NULL,
    source_code    VARCHAR(32) NOT NULL,
    entry_url       TEXT,
    is_primary    BOOLEAN NOT NULL DEFAULT FALSE,
    confidence    FLOAT NOT NULL DEFAULT 0.8,
    linked_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, repo_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_github_repo_asset
    ON biz.asset_github_repo (asset_id);

CREATE INDEX IF NOT EXISTS idx_asset_github_repo_repo
    ON biz.asset_github_repo (repo_id);

CREATE INDEX IF NOT EXISTS idx_asset_github_repo_owner_repo
    ON biz.asset_github_repo (owner_login, repo_name);

-- ============================================================
-- 第二步：从 doc_source_entry 回溯建立关联
-- ============================================================

INSERT INTO biz.asset_github_repo (
    asset_id, repo_id, owner_login, repo_name,
    source_code, entry_url, is_primary, confidence
)
WITH repo_assets AS (
    -- 从 doc_source_entry 解析出每个 GitHub 仓库对应的资产
    SELECT DISTINCT ON (d.asset_id, gra.id)
        d.asset_id,
        gra.id AS repo_id,
        gra.owner_login,
        gra.repo_name,
        d.source_code,
        d.entry_url,
        -- 按链接类型计算置信度
        CASE
            WHEN d.entry_type = 'source_code' THEN 1.0
            WHEN d.entry_url LIKE '%/tree/%' THEN 0.7
            WHEN d.entry_url LIKE '%/wiki%' THEN 0.5
            ELSE 0.8
        END AS confidence,
        -- 用于排序选主仓库
        ROW_NUMBER() OVER (
            PARTITION BY d.asset_id
            ORDER BY
                CASE d.entry_type WHEN 'source_code' THEN 1 ELSE 2 END,
                a.market_cap_rank ASC NULLS LAST
        ) AS rn
    FROM biz.github_repo_activity gra
    JOIN biz.doc_source_entry d
      ON d.entry_url LIKE 'https://github.com/' || gra.owner_login || '/' || gra.repo_name || '%'
      OR d.entry_url = 'https://github.com/' || gra.owner_login || '/' || gra.repo_name
    JOIN core.asset a ON a.asset_id = d.asset_id
    WHERE d.entry_url NOT LIKE '%gist.github.com%'
)
SELECT
    asset_id,
    repo_id,
    owner_login,
    repo_name,
    source_code,
    entry_url,
    CASE WHEN rn = 1 THEN TRUE ELSE FALSE END AS is_primary,
    confidence
FROM repo_assets
ON CONFLICT (asset_id, repo_id) DO NOTHING;

-- ============================================================
-- 验证
-- ============================================================

-- 验证 1：关联表行数
SELECT COUNT(*) AS total_links FROM biz.asset_github_repo;

-- 验证 2：有 GitHub 数据的资产数（覆盖率）
SELECT COUNT(DISTINCT asset_id) AS assets_with_github
FROM biz.asset_github_repo;

-- 验证 3：覆盖率百分比
SELECT
    COUNT(DISTINCT agr.asset_id) AS covered,
    (SELECT COUNT(*) FROM core.asset) AS total,
    ROUND(
        COUNT(DISTINCT agr.asset_id)::numeric
        / (SELECT COUNT(*) FROM core.asset)::numeric * 100,
        2
    ) AS coverage_pct
FROM biz.asset_github_repo agr;
