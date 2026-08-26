-- Upsert asset_github_repo 关联表
-- 从 doc_source_entry 解析出的 GitHub 链接，建立资产与仓库的关联
-- 同一 asset + repo 组合只保留一条，更新时刷新置信度和来源

INSERT INTO biz.asset_github_repo (
    asset_id,
    repo_id,
    owner_login,
    repo_name,
    source_code,
    entry_url,
    is_primary,
    confidence,
    linked_at,
    updated_at
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    NOW(),
    NOW()
)
ON CONFLICT (asset_id, repo_id) DO UPDATE SET
    source_code = EXCLUDED.source_code,
    entry_url = EXCLUDED.entry_url,
    -- 取更高的置信度
    confidence = GREATEST(EXCLUDED.confidence, biz.asset_github_repo.confidence),
    -- 新链接是主仓库的话升级
    is_primary = CASE
        WHEN EXCLUDED.is_primary = TRUE THEN TRUE
        ELSE biz.asset_github_repo.is_primary
    END,
    updated_at = NOW()
RETURNING asset_id;
