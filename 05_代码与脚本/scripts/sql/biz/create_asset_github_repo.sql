-- 资产 ↔ GitHub 仓库多对多关联表
-- 建立 core.asset 与 biz.github_repo_activity 的直接关联，
-- 解决 GitHub 数据资产级覆盖率为 0% 的问题。
--
-- 关联来源：从 biz.doc_source_entry 中 entry_type='github' 的链接解析而来
-- 一个资产可以有多个 GitHub 仓库（主仓、合约仓、SDK 等）
-- 一个仓库也可以被多个资产引用（如公共库）

CREATE TABLE IF NOT EXISTS biz.asset_github_repo (
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    repo_id        BIGINT NOT NULL REFERENCES biz.github_repo_activity(id) ON DELETE CASCADE,
    owner_login    VARCHAR(256) NOT NULL,
    repo_name      VARCHAR(256) NOT NULL,
    source_code    VARCHAR(32) NOT NULL,  -- 来源：cmc / cg / dl
    entry_url       TEXT,                   -- 原始 doc_source_entry 的 URL（用于溯源）
    is_primary    BOOLEAN NOT NULL DEFAULT FALSE,  -- 是否为该资产的主代码仓库
    confidence    FLOAT NOT NULL DEFAULT 0.8,     -- 关联置信度
    linked_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, repo_id)
);

-- 加速按资产查仓库
CREATE INDEX IF NOT EXISTS idx_asset_github_repo_asset
    ON biz.asset_github_repo (asset_id);

-- 加速按仓库查资产
CREATE INDEX IF NOT EXISTS idx_asset_github_repo_repo
    ON biz.asset_github_repo (repo_id);

-- 加速按 owner/repo 查
CREATE INDEX IF NOT EXISTS idx_asset_github_repo_owner_repo
    ON biz.asset_github_repo (owner_login, repo_name);

COMMENT ON TABLE biz.asset_github_repo IS '资产与 GitHub 仓库的多对多关联，从 doc_source_entry 的 GitHub 链接解析而来';
COMMENT ON COLUMN biz.asset_github_repo.is_primary IS '是否为该资产的主代码仓库（按市值排名+链接类型推断）';
