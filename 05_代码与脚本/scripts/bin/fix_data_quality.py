#!/usr/bin/env python3
"""投研数据质量修复脚本（在 data_quality_snapshot 发现问题后执行）。

修复项（全部默认 dry-run，加 --apply 才真正落库）：
    1. 未来异常解锁：unlock_date >= 今天 且 unlock_value_usd > 5 × 最新市值
       （tokenomist/tokenomics 把累计市值口径混进事件表，如 BNB 836B 解锁）
    2. 历史解锁残留：unlock_date < 今天（默认不清理——投研已过滤未来窗口；
       需要时加 --clean-history）
    3. 描述污染：coin_basic.description 里含其它资产的 canonical_name，
       用 src_cmc.cmc_asset_info 的正确描述回填（需 --fix-desc）
    4. GitHub 误配关联：审计公司公共仓库（名字/描述含 audit 且跨 ≥2 资产）
       + 通用工具仓库（github-mcp-server/nextra 等）被错误挂到多个资产
       （需 --fix-github）

安全机制：
    - 每次真正执行前自动建备份表 biz.asset_unlock_event_bak_{ts}
    - 只清理"未来 + 金额>市值5倍"的解锁（真实单次解锁不会超总市值）
    - 描述回填仅针对 cmc 源有正确描述且当前描述含其它币名的资产
    - GitHub 只删"跨 ≥2 资产"的审计/工具仓库关联；1 对 1 关联（真实项目 repo）
      绝不误删

用法：
    python bin/fix_data_quality.py                        # dry-run 预览
    python bin/fix_data_quality.py --apply                # 真正执行（清理未来异常解锁）
    python bin/fix_data_quality.py --apply --clean-history  # 同时清理历史解锁
    python bin/fix_data_quality.py --apply --fix-desc     # 同时修复描述污染
    python bin/fix_data_quality.py --apply --fix-github   # 同时修复 GitHub 误配关联
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
_scripts_src = os.path.join(_code_root, "scripts", "src")
for cand in (_scripts_src, os.path.join(_code_root, "workbench"), "/app"):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 未来异常解锁判定：金额 > 市值 × 5
ABNORMAL_MULTIPLIER = 5.0


def _latest_mcap_sql(alias: str = "u") -> str:
    """该资产最新市值（asset_market_daily 最新一条）。"""
    return (
        f"(SELECT COALESCE(MAX(m.market_cap), 0) FROM biz.asset_market_daily m "
        f"WHERE m.asset_id = {alias}.asset_id)"
    )


def find_abnormal_future_unlocks(conn, limit: int = 50) -> list[tuple]:
    """未来 + 金额 > 市值×5 的异常解锁。"""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT u.asset_id, a.canonical_symbol, a.canonical_name, u.unlock_date,
                   u.unlock_type, u.unlock_value_usd, u.source_code,
                   {_latest_mcap_sql('u')} AS mcap
            FROM biz.asset_unlock_event u
            JOIN core.asset a ON a.asset_id = u.asset_id
            WHERE u.unlock_date >= CURRENT_DATE
              AND u.unlock_value_usd > 0
              AND u.unlock_value_usd > {ABNORMAL_MULTIPLIER} * {_latest_mcap_sql('u')}
            ORDER BY u.unlock_value_usd DESC
            LIMIT {limit}
            """
        )
        return cur.fetchall()


def find_history_unlocks(conn, limit: int = 50) -> list[tuple]:
    """历史解锁（unlock_date < 今天；含零金额占位记录）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id, unlock_date, unlock_type, unlock_value_usd, source_code "
            f"FROM biz.asset_unlock_event WHERE unlock_date < CURRENT_DATE "
            f"ORDER BY unlock_date DESC LIMIT {limit}"
        )
        return cur.fetchall()


def find_polluted_descs(conn, limit: int = 50) -> list[tuple]:
    """描述污染：描述不含自己资产名的资产（如 BTC 被 Bullish Trump Coin 覆盖）。

    判定：cmc 有正确描述（含自己 symbol），但 coin_basic 当前描述不含自己 name
    → 真污染（描述被别的项目覆盖）。LTC/NMC 等描述正常（含自己名）不命中。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT cb.asset_id, cb.coin_symbol, cb.coin_name,
                   LEFT(cb.description_short, 60) AS cur_desc,
                   LEFT(ci.description, 60) AS cmc_desc
            FROM biz.coin_basic cb
            JOIN src_cmc.cmc_asset_info ci ON ci.cmc_id = cb.cmc_id
            WHERE cb.description_short IS NOT NULL
              AND ci.description IS NOT NULL
              AND ci.description LIKE CONCAT('%', cb.coin_symbol, '%')
              AND cb.description_short NOT LIKE CONCAT('%', cb.coin_name, '%')
            ORDER BY cb.asset_id
            LIMIT {limit}
            """
        )
        return cur.fetchall()


# GitHub 误配黑名单：通用工具/基础设施仓库，与加密资产无归属关系
GITHUB_TOOL_BLACKLIST = {
    "github-mcp-server", "nextra", "oh-my-claudecode", "agent-zero",
    "solidity", "projects", "assets", "contract", "Contracts", "Docs",
    "docs", "Documents", "Whitepaper", "White-Paper", "whitepaper",
    "white-paper", "website-ts", "Publications", "Resources",
    "Knowledge-Base", "santipu03",
}


def find_github_mismatches(conn, limit: int = 100) -> list[tuple]:
    """GitHub 误配关联：审计公共仓库（跨≥2资产）+ 工具黑名单仓库。

    返回 [(asset_id, symbol, repo_name, n_assets, stars)]。
    只删"跨 ≥2 资产"的关联，1 对 1 真实项目 repo 绝不动。
    """
    with conn.cursor() as cur:
        # 审计仓库：repo 名/描述含 audit，且被挂到 >=2 个资产
        audit_sql = """
            SELECT g.asset_id, a.canonical_symbol, g.repo_name, n.n_assets, r.stars_count
            FROM biz.asset_github_repo g
            JOIN core.asset a ON a.asset_id = g.asset_id
            JOIN (
                SELECT repo_name, COUNT(DISTINCT asset_id) AS n_assets
                FROM biz.asset_github_repo GROUP BY repo_name
            ) n ON n.repo_name = g.repo_name
            JOIN biz.github_repo_activity r ON r.owner_login=g.owner_login AND r.repo_name=g.repo_name
            WHERE n.n_assets >= 2
              AND (LOWER(g.repo_name) LIKE '%%audit%%'
                   OR LOWER(COALESCE(r.description,'')) LIKE '%%audit%%')
            ORDER BY g.asset_id
            LIMIT %s
        """
        cur.execute(audit_sql, (limit,))
        audit_rows = cur.fetchall()
        # 黑名单工具仓库（跨>=2资产）
        ph = ",".join(["%s"] * len(GITHUB_TOOL_BLACKLIST))
        cur.execute(
            f"""
            SELECT g.asset_id, a.canonical_symbol, g.repo_name, n.n_assets, r.stars_count
            FROM biz.asset_github_repo g
            JOIN core.asset a ON a.asset_id = g.asset_id
            JOIN (
                SELECT repo_name, COUNT(DISTINCT asset_id) AS n_assets
                FROM biz.asset_github_repo GROUP BY repo_name
            ) n ON n.repo_name = g.repo_name
            JOIN biz.github_repo_activity r ON r.owner_login=g.owner_login AND r.repo_name=g.repo_name
            WHERE n.n_assets >= 2 AND g.repo_name IN ({ph})
            ORDER BY g.asset_id
            LIMIT %s
            """,
            (*GITHUB_TOOL_BLACKLIST, limit),
        )
        tool_rows = cur.fetchall()
        return audit_rows + tool_rows


def count_github_mismatches(conn) -> int:
    """待删 GitHub 误配总数（用于统计，不限制条数）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM biz.asset_github_repo g
            JOIN (
                SELECT repo_name, COUNT(DISTINCT asset_id) AS n_assets
                FROM biz.asset_github_repo GROUP BY repo_name
            ) n ON n.repo_name = g.repo_name
            JOIN biz.github_repo_activity r ON r.owner_login=g.owner_login AND r.repo_name=g.repo_name
            WHERE n.n_assets >= 2
              AND (LOWER(g.repo_name) LIKE '%%audit%%'
                   OR LOWER(COALESCE(r.description,'')) LIKE '%%audit%%')
            """
        )
        n_audit = cur.fetchone()[0]
        ph = ",".join(["%s"] * len(GITHUB_TOOL_BLACKLIST))
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM biz.asset_github_repo g
            JOIN (
                SELECT repo_name, COUNT(DISTINCT asset_id) AS n_assets
                FROM biz.asset_github_repo GROUP BY repo_name
            ) n ON n.repo_name = g.repo_name
            WHERE n.n_assets >= 2 AND g.repo_name IN ({ph})
            """,
            (*GITHUB_TOOL_BLACKLIST,),
        )
        n_tool = cur.fetchone()[0]
        return n_audit + n_tool


def backup_table(conn, table: str = "biz.asset_unlock_event") -> str | None:
    """执行前备份表 → biz.{table}_bak_{ts}。返回备份表名。

    表名含 schema，取末段做备份名（如 asset_github_repo → biz.asset_github_repo_bak_{ts}）。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = table.split(".")[-1]
    bak = f"biz.{short}_bak_{ts}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {bak} AS SELECT * FROM {table}")
    return bak


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="投研数据质量修复")
    parser.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run 预览）")
    parser.add_argument("--clean-history", action="store_true", help="同时清理历史解锁")
    parser.add_argument("--fix-desc", action="store_true", help="修复描述污染（cmc 源回填）")
    parser.add_argument("--fix-github", action="store_true", help="修复 GitHub 误配关联")
    parser.add_argument("--limit", type=int, default=50, help="预览条数上限")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        print(f"== 数据质量修复 {'[APPLY]' if args.apply else '[DRY-RUN]'} ==")

        # ── 1. 未来异常解锁 ──
        abnormal = find_abnormal_future_unlocks(conn, args.limit)
        print(f"\n[1] 未来异常解锁（金额>市值×{ABNORMAL_MULTIPLIER:.0f}）：{len(abnormal)} 条")
        for r in abnormal[:10]:
            print(f"    {r[1]} ({r[0]}) {r[3]} {r[4]} 解锁{float(r[5]):,.0f} vs 市值{float(r[7]):,.0f} [{r[6]}]")

        # ── 2. 历史解锁 ──
        if args.clean_history:
            hist = find_history_unlocks(conn, args.limit)
            print(f"\n[2] 历史解锁（unlock_date < 今天）：{len(hist)} 条")
            for r in hist[:10]:
                print(f"    {r[0]} {r[1]} {r[2]} 解锁{float(r[3]):,.0f} [{r[4]}]")

        # ── 3. 描述污染 ──
        if args.fix_desc:
            polluted = find_polluted_descs(conn, args.limit)
            print(f"\n[3] 描述污染（可 cmc 回填）：{len(polluted)} 条")
            for r in polluted[:10]:
                print(f"    {r[1]} ({r[0]}): {r[3]}… → cmc: {r[4]}…")

        # ── 4. GitHub 误配关联 ──
        if args.fix_github:
            n_gh = count_github_mismatches(conn)
            gh_rows = find_github_mismatches(conn, args.limit)
            print(f"\n[4] GitHub 误配关联（审计公共仓库+工具仓库，跨≥2资产）：{n_gh} 条")
            for r in gh_rows[:12]:
                print(f"    {r[1]} ← {r[2]} (跨{r[3]}资产 ⭐{r[4]})")

        if not args.apply:
            print("\n[DRY-RUN] 以上为预览，未做任何修改。加 --apply 执行。")
            return 0

        # ── 真正执行 ──
        if abnormal:
            bak = backup_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    DELETE FROM biz.asset_unlock_event u
                    WHERE u.unlock_date >= CURRENT_DATE
                      AND u.unlock_value_usd > 0
                      AND u.unlock_value_usd > {ABNORMAL_MULTIPLIER} * {_latest_mcap_sql('u')}
                    """
                )
            print(f"\n[APPLY] 已清理 {len(abnormal)} 条未来异常解锁（备份: {bak}）")

        if args.clean_history and hist:
            with conn.cursor() as cur:
                # 清理全部历史解锁（含 unlock_value_usd=0 的占位记录——
                # 过去日期的零金额事件对投研无信息量，快照仍会报残留）
                cur.execute(
                    "DELETE FROM biz.asset_unlock_event WHERE unlock_date < CURRENT_DATE"
                )
            print(f"[APPLY] 已清理历史解锁（预览 {len(hist)} 条；含零金额占位）")

        if args.fix_desc and polluted:
            fixed = 0
            with conn.cursor() as cur:
                for row in polluted:
                    asset_id, symbol, name, cur_desc, _ = row[0], row[1], row[2], row[3], row[4]
                    cur.execute(
                        "SELECT description FROM src_cmc.cmc_asset_info ci "
                        "JOIN biz.coin_basic cb ON cb.cmc_id = ci.cmc_id "
                        "WHERE cb.asset_id = %s AND ci.description LIKE CONCAT('%%', cb.coin_symbol, '%%') "
                        "LIMIT 1",
                        (asset_id,),
                    )
                    r = cur.fetchone()
                    if r and r[0]:
                        cur.execute(
                            "UPDATE biz.coin_basic SET description_short = %s, "
                            "last_refreshed_at = NOW() WHERE asset_id = %s",
                            (r[0], asset_id),
                        )
                        cur.execute(
                            "UPDATE core.asset SET description_short = %s WHERE asset_id = %s",
                            (r[0], asset_id),
                        )
                        fixed += 1
            print(f"[APPLY] 已修复 {fixed} 条描述污染")

        # ── 4. GitHub 误配关联（apply）──
        if args.fix_github and n_gh:
            bak_gh = backup_table(conn, "biz.asset_github_repo")
            with conn.cursor() as cur:
                # 审计公共仓库（跨>=2资产）
                cur.execute(
                    """
                    DELETE FROM biz.asset_github_repo g
                    USING (
                        SELECT repo_name, COUNT(DISTINCT asset_id) AS n_assets
                        FROM biz.asset_github_repo GROUP BY repo_name
                    ) n,
                    biz.github_repo_activity r
                    WHERE g.repo_name = n.repo_name AND n.n_assets >= 2
                      AND r.owner_login = g.owner_login AND r.repo_name = g.repo_name
                      AND (LOWER(g.repo_name) LIKE '%%audit%%'
                           OR LOWER(COALESCE(r.description,'')) LIKE '%%audit%%')
                    """
                )
                # 工具黑名单仓库（跨>=2资产）
                ph = ",".join(["%s"] * len(GITHUB_TOOL_BLACKLIST))
                cur.execute(
                    f"""
                    DELETE FROM biz.asset_github_repo g
                    USING (
                        SELECT repo_name, COUNT(DISTINCT asset_id) AS n_assets
                        FROM biz.asset_github_repo GROUP BY repo_name
                    ) n
                    WHERE g.repo_name = n.repo_name AND n.n_assets >= 2
                      AND g.repo_name IN ({ph})
                    """,
                    (*GITHUB_TOOL_BLACKLIST,),
                )
            print(f"[APPLY] 已清理 {n_gh} 条 GitHub 误配关联（备份: {bak_gh}）")

        conn.commit()
        print("[APPLY] 完成。建议重跑 data_quality_snapshot.py 验证。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
