from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_REPO_LIMIT = 100  # 不传 --limit 时的默认处理量


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 doc_source_entry 提取 GitHub 仓库，调用 REST API 采集开发活跃度数据。"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_REPO_LIMIT,
        help=f"最多处理多少个仓库（默认 {DEFAULT_REPO_LIMIT}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制刷新已采集过的仓库（默认跳过已有记录的仓库）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅提取仓库列表并打印，不调用 API 也不写入数据库。",
    )
    return parser


def _extract_github_repos(conn, limit: int, force: bool) -> list[dict[str, str]]:
    """从 doc_source_entry 提取去重的 owner/repo 列表，按市值排序优先主项目仓库。

    Returns list of dicts with keys: owner_login, repo_name, sample_url, entry_count, market_cap_rank
    """
    # 外部用 NOT EXISTS 过滤已采集的仓库
    not_exists_clause = ""
    if not force:
        not_exists_clause = (
            "AND NOT EXISTS ("
            "  SELECT 1 FROM biz.github_repo_activity gra "
            "  WHERE gra.owner_login = repo.owner_login "
            "    AND gra.repo_name = repo.repo_name"
            ")"
        )

    sql = (
        """
        SELECT
            repo.owner_login,
            repo.repo_name,
            repo.sample_url,
            repo.entry_count,
            repo.market_cap_rank
        FROM (
            SELECT
                split_part(
                    regexp_replace(d.entry_url, '^https?://github\\.com/', ''),
                    '/', 1
                ) AS owner_login,
                split_part(
                    regexp_replace(d.entry_url, '^https?://github\\.com/', ''),
                    '/', 2
                ) AS repo_name,
                MAX(d.entry_url) AS sample_url,
                COUNT(*) AS entry_count,
                MIN(a.market_cap_rank) AS market_cap_rank
            FROM biz.doc_source_entry d
            JOIN core.asset a ON a.asset_id = d.asset_id
            WHERE d.entry_url LIKE '%%github.com%%'
              AND d.entry_url NOT LIKE '%%gist.github.com%%'
              -- 过滤审计报告/文档类仓库，优先项目主仓库
              AND d.entry_url NOT LIKE '%%/audit%%'
              AND d.entry_url NOT LIKE '%%/audits%%'
              AND d.entry_url NOT LIKE '%%/audit-reports%%'
              AND d.entry_url NOT LIKE '%%/publications%%'
              AND d.entry_url NOT LIKE '%%/docs%%'
              AND d.entry_url NOT LIKE '%%/documentation%%'
              AND d.entry_url NOT LIKE '%%/whitepaper%%'
              AND d.entry_url NOT LIKE '%%/whitepapers%%'
            GROUP BY 1, 2
        ) repo
        WHERE repo.owner_login != ''
          AND repo.repo_name != ''
          AND repo.market_cap_rank IS NOT NULL
        """
        + not_exists_clause
        + """
        ORDER BY repo.market_cap_rank ASC
        LIMIT %s
        """
    )
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, (limit,))
        return list(cur.fetchall())


def _github_get(session: requests.Session, api_path: str, github_token: str | None) -> dict[str, Any]:
    """调用 GitHub REST API，返回 JSON dict。"""
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    response = session.get(
        f"{GITHUB_API_BASE}{api_path}",
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _check_rate_limit(session: requests.Session, github_token: str | None) -> dict[str, int]:
    """返回 {'remaining': N, 'reset': unix_timestamp}。"""
    data = _github_get(session, "/rate_limit", github_token)
    core = data.get("resources", {}).get("core", {})
    return {
        "remaining": core.get("remaining", 0),
        "reset": core.get("reset", 0),
        "limit": core.get("limit", 60),
    }


def _wait_if_needed(session: requests.Session, github_token: str | None, min_remaining: int = 5) -> None:
    """如果剩余配额不足，等待到 reset 时间。"""
    rl = _check_rate_limit(session, github_token)
    if rl["remaining"] <= min_remaining:
        wait_sec = max(rl["reset"] - int(time.time()), 0) + 3
        print(f"  [rate-limit] remaining={rl['remaining']}/{rl['limit']}, sleeping {wait_sec}s ...")
        time.sleep(wait_sec)


def _collect_single_repo(
    session: requests.Session,
    owner: str,
    repo: str,
    github_token: str | None,
) -> dict[str, Any]:
    """采集单个仓库的完整活动数据。"""
    # 1. 仓库基本信息
    repo_info = _github_get(session, f"/repos/{owner}/{repo}", github_token)

    # 2. 提交活跃度（可能返回 202，需要等待计算）
    commit_activity = _github_get(session, f"/repos/{owner}/{repo}/stats/commit_activity", github_token)
    if isinstance(commit_activity, dict) and commit_activity.get("status") == "202":
        # GitHub 正在计算，等待并重试一次
        time.sleep(2)
        commit_activity = _github_get(session, f"/repos/{owner}/{repo}/stats/commit_activity", github_token)

    # 3. 贡献者统计
    contributors = _github_get(session, f"/repos/{owner}/{repo}/stats/contributors", github_token)
    if isinstance(contributors, dict) and contributors.get("status") == "202":
        time.sleep(2)
        contributors = _github_get(session, f"/repos/{owner}/{repo}/stats/contributors", github_token)

    # 解析
    weekly_counts = []
    total_commits = 0
    if isinstance(commit_activity, list):
        weekly_counts = [w.get("total", 0) for w in commit_activity]
        total_commits = sum(weekly_counts)

    contrib_list = []
    contributor_count = 0
    if isinstance(contributors, list):
        contributor_count = len(contributors)
        contrib_list = sorted(
            (
                {
                    "login": c.get("author", {}).get("login", "unknown"),
                    "commits": c.get("total", 0),
                }
                for c in contributors
            ),
            key=lambda x: -x["commits"],
        )[:20]

    topics = repo_info.get("topics", [])
    license_info = repo_info.get("license")
    license_name = license_info.get("spdx_id") if license_info else None

    return {
        "owner_login": owner,
        "repo_name": repo,
        # 仓库基本信息
        "description": repo_info.get("description"),
        "default_branch": repo_info.get("default_branch"),
        "stars_count": repo_info.get("stargazers_count"),
        "forks_count": repo_info.get("forks_count"),
        "open_issues_count": repo_info.get("open_issues_count"),
        "language": repo_info.get("language"),
        "topics": topics,
        "license_name": license_name,
        "archived": repo_info.get("archived", False),
        "disabled": repo_info.get("disabled", False),
        "pushed_at": repo_info.get("pushed_at"),
        "created_at": repo_info.get("created_at"),
        # 提交活跃度
        "total_commits_52w": total_commits,
        "weekly_commit_counts": weekly_counts,
        # 贡献者
        "contributor_count_52w": contributor_count,
        "top_contributors": json.dumps(contrib_list, ensure_ascii=False),
        # 归档
        "api_response_json": json.dumps(
            {
                "repo": repo_info,
                "commit_activity": commit_activity,
                "contributors": contributors,
            },
            ensure_ascii=False,
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required")

    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import load_sql

    upsert_sql = load_sql("biz/upsert_github_repo_activity.sql")

    with get_connection(settings.database_url) as conn:
        repos = _extract_github_repos(conn, args.limit, args.force)
        print(f"Found {len(repos)} GitHub repos to process (limit={args.limit}, force={args.force})")
        print()

        if args.dry_run:
            for r in repos:
                print(f"  {r['owner_login']}/{r['repo_name']}  "
                      f"(entries: {r['entry_count']})  {r['sample_url'][:80]}")
            print(f"\nTotal: {len(repos)} repos (dry-run, no API calls)")
            return 0

        session = requests.Session()
        session.headers.update({"User-Agent": "crypto-research-github-activity/1.0"})

        github_token = settings.github_token

        success_count = 0
        skip_count = 0
        error_count = 0

        for i, repo in enumerate(repos, 1):
            owner = repo["owner_login"]
            name = repo["repo_name"]
            print(f"[{i}/{len(repos)}] {owner}/{name}  ", end="", flush=True)

            try:
                _wait_if_needed(session, github_token)
                data = _collect_single_repo(session, owner, name, github_token)

                with conn.cursor() as cur:
                    cur.execute(
                        upsert_sql,
                        (
                            data["owner_login"],
                            data["repo_name"],
                            data["description"],
                            data["default_branch"],
                            data["stars_count"],
                            data["forks_count"],
                            data["open_issues_count"],
                            data["language"],
                            data["topics"],
                            data["license_name"],
                            data["archived"],
                            data["disabled"],
                            data["pushed_at"],
                            data["created_at"],
                            data["total_commits_52w"],
                            data["weekly_commit_counts"],
                            data["contributor_count_52w"],
                            data["top_contributors"],
                            data["api_response_json"],
                            data["fetched_at"],
                        ),
                    )

                print(
                    f"stars={data['stars_count']}, "
                    f"commits_52w={data['total_commits_52w']}, "
                    f"contributors={data['contributor_count_52w']}"
                )
                success_count += 1

            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else None
                if status == 404:
                    print("NOT_FOUND (repo deleted/private)")
                    skip_count += 1
                elif status == 403:
                    rl = _check_rate_limit(session, github_token)
                    print(
                        f"RATE_LIMITED "
                        f"(remaining={rl['remaining']}/{rl['limit']}, "
                        f"reset in {max(rl['reset'] - int(time.time()), 0)}s)"
                    )
                    # 等待后重试
                    _wait_if_needed(session, github_token, min_remaining=999)
                    try:
                        data = _collect_single_repo(session, owner, name, github_token)
                        with conn.cursor() as cur:
                            cur.execute(
                                upsert_sql,
                                (
                                    data["owner_login"],
                                    data["repo_name"],
                                    data["description"],
                                    data["default_branch"],
                                    data["stars_count"],
                                    data["forks_count"],
                                    data["open_issues_count"],
                                    data["language"],
                                    data["topics"],
                                    data["license_name"],
                                    data["archived"],
                                    data["disabled"],
                                    data["pushed_at"],
                                    data["created_at"],
                                    data["total_commits_52w"],
                                    data["weekly_commit_counts"],
                                    data["contributor_count_52w"],
                                    data["top_contributors"],
                                    data["api_response_json"],
                                    data["fetched_at"],
                                ),
                            )
                        print(
                            f"stars={data['stars_count']}, "
                            f"commits_52w={data['total_commits_52w']}, "
                            f"contributors={data['contributor_count_52w']}  (retry ok)"
                        )
                        success_count += 1
                    except Exception:
                        print(f"FAIL after retry")
                        error_count += 1
                else:
                    print(f"HTTP {status}: {exc}")
                    error_count += 1

            except requests.ConnectionError as exc:
                print(f"CONNECTION_ERROR (GitHub API unreachable, need proxy?)")
                error_count += 1
            except Exception as exc:
                print(f"ERROR: {exc}")
                error_count += 1

            # 每处理 10 个仓库提交一次
            if i % 10 == 0:
                conn.commit()

        conn.commit()

        print()
        print(
            f"Done: {success_count} succeeded, {skip_count} skipped/not-found, "
            f"{error_count} errors"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
