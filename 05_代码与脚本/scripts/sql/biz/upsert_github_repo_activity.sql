INSERT INTO biz.github_repo_activity (
    owner_login,
    repo_name,
    description,
    default_branch,
    stars_count,
    forks_count,
    open_issues_count,
    language,
    topics,
    license_name,
    archived,
    disabled,
    pushed_at,
    created_at,
    -- commit activity (last 52 weeks)
    total_commits_52w,
    weekly_commit_counts,
    -- contributors
    contributor_count_52w,
    top_contributors,
    -- raw
    api_response_json,
    fetched_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s,
    %s, %s,
    %s::jsonb, %s::timestamptz
)
ON CONFLICT (owner_login, repo_name) DO UPDATE SET
    description = EXCLUDED.description,
    default_branch = EXCLUDED.default_branch,
    stars_count = EXCLUDED.stars_count,
    forks_count = EXCLUDED.forks_count,
    open_issues_count = EXCLUDED.open_issues_count,
    language = EXCLUDED.language,
    topics = EXCLUDED.topics,
    license_name = EXCLUDED.license_name,
    archived = EXCLUDED.archived,
    disabled = EXCLUDED.disabled,
    pushed_at = EXCLUDED.pushed_at,
    total_commits_52w = EXCLUDED.total_commits_52w,
    weekly_commit_counts = EXCLUDED.weekly_commit_counts,
    contributor_count_52w = EXCLUDED.contributor_count_52w,
    top_contributors = EXCLUDED.top_contributors,
    api_response_json = EXCLUDED.api_response_json,
    fetched_at = EXCLUDED.fetched_at,
    updated_at = NOW()
RETURNING id;
