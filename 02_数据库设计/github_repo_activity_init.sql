-- GitHub 仓库开发活跃度追踪表
-- 通过 GitHub REST API 独立采集，用于投研中的「代码提交历史」维度分析
CREATE TABLE IF NOT EXISTS biz.github_repo_activity (
    id              BIGSERIAL PRIMARY KEY,
    owner_login     VARCHAR(256) NOT NULL,
    repo_name       VARCHAR(256) NOT NULL,
    -- 仓库基本信息（来自 /repos/{owner}/{repo}）
    description     TEXT,
    default_branch  VARCHAR(128),
    stars_count     INT,
    forks_count     INT,
    open_issues_count INT,
    language        VARCHAR(64),
    topics          TEXT[],         -- GitHub topics 标签数组
    license_name    VARCHAR(128),
    archived        BOOLEAN,
    disabled        BOOLEAN,
    pushed_at       TIMESTAMPTZ,    -- 最后一次 push 时间
    created_at      TIMESTAMPTZ,    -- 仓库创建时间
    -- 提交活跃度（来自 /stats/commit_activity）
    total_commits_52w  INT,         -- 近 52 周总提交数
    weekly_commit_counts INT[],     -- 近 52 周每周提交数数组
    -- 贡献者统计（来自 /stats/contributors）
    contributor_count_52w INT,      -- 近一年贡献者总数
    top_contributors JSONB,         -- TOP 贡献者 [{login, commits}] 
    -- 原始数据归档
    api_response_json JSONB,        -- 完整 API 响应
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_login, repo_name)
);

-- 加速按 owner/repo 查询
CREATE INDEX IF NOT EXISTS idx_github_repo_activity_owner_repo
    ON biz.github_repo_activity (owner_login, repo_name);

-- 加速按活跃度排序
CREATE INDEX IF NOT EXISTS idx_github_repo_activity_total_commits
    ON biz.github_repo_activity (total_commits_52w DESC NULLS LAST);

-- 加速按获取时间过滤
CREATE INDEX IF NOT EXISTS idx_github_repo_activity_fetched_at
    ON biz.github_repo_activity (fetched_at);

COMMENT ON TABLE biz.github_repo_activity IS 'GitHub 仓库开发活跃度数据，通过 REST API 独立采集，用于投研分析';
COMMENT ON COLUMN biz.github_repo_activity.total_commits_52w IS '近 52 周总提交数，null 表示未采集';
COMMENT ON COLUMN biz.github_repo_activity.weekly_commit_counts IS '近 52 周每周提交数数组 [w1, w2, ... w52]';
COMMENT ON COLUMN biz.github_repo_activity.top_contributors IS 'TOP 贡献者 [{"login":"xxx","commits":N}]';
