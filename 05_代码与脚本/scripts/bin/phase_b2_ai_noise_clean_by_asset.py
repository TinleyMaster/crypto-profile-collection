"""
B2 AI 噪声清理 — 按资产分组版本。

核心思路：
- 将同一资产的所有 deep_crawl 链接按域名聚合后一并发给 AI
- AI 能同时看到该资产的所有域名，判断"这个文档是否在介绍同一个代币"
- 按域名粒度删除（而非逐条 URL），效率远超逐条判断

用法：
  python phase_b2_ai_noise_clean_by_asset.py [--execute] [--limit 20]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg
import psycopg.rows
from crypto_research.config import get_settings
from crypto_research.clients.llm_client import LLMClient

settings = get_settings(require_database=True)

# ── 规则直删域名（不需要 AI 判断） ──
RULE_NOISE_DOMAINS = {
    "paperdigest": "%paperdigest.org%",
    "arxiv": "%arxiv.org%",
    "papers-nips": "%papers.nips.cc%",
    "neurips": "%neurips.cc%",
    "springer": "%link.springer.com%",
    "researchgate": "%researchgate.net%",
    "papermc": "%papermc.io%",
    "paperspace": "%paperspace.com%",
    "ijcai": "%ijcai.org%",
    "digitalocean": "%digitalocean.com%",
    "powershellgallery": "%powershellgallery.com%",
    "rubydoc": "%rubydoc.info%",
    "rubygems": "%rubygems.org%",
    "openai": "%developers.openai.com%",
    "linkedin": "%linkedin.com%",
    "facebook": "%facebook.com%",
    "t-me": "%t.me%",
    "telegram-me": "%telegram.me%",
    "web-archive": "%web.archive.org%",
    "dropbox": "%dropbox.com%",
    "webflow-cdn": "%cdn.prod.website-files.com%",
    "certora-cdn": "%certora.cdn.prismic.io%",
    "docs-rs": "%docs.rs%",
    "diffend": "%my.diffend.io%",
    "elastic": "%elastic.co%",
    "nuget": "%nuget.org%",
    "packagist": "%packagist.org%",
    "manageengine": "%manageengine.com%",
    "twitter": "%twitter.com%",
    "x-com": "%x.com%",
    "reddit": "%reddit.com%",
    "papermc-jd": "%jd.papermc.io%",
    # ── 2026-08-05 新增：从污染溯源发现的噪声域名 ──
    "ubuntu-packages": "%packages.ubuntu.com%",
    "amazon-ca": "%www.amazon.ca%",
    "whitepaper-silicon": "%whitepaper.silicon.co.uk%",
    "linux-audit": "%lists.linux-audit.osci.io%",
    "google-cloud": "%docs.cloud.google.com%",
    "gitee": "%gitee.com%",
    "huggingface": "%huggingface.co%",
    "plusone-google": "%plusone.google.com%",
    "viadeo": "%www.viadeo.com%",
    "launchpad": "%bugs.launchpad.net%",
    "marc-info": "%marc.info%",
    "scrutinizer": "%scrutinizer-ci.com%",
    "rdoc-info": "%rdoc.info%",
    "badge-fury": "%badge.fury.io%",
    "laravel-auditing": "%laravel-auditing.com%",
    "clickgems": "%clickgems.clickhouse.com%",
    "intel-aikido": "%intel.aikido.dev%",
    "laravel-com": "%laravel.com%",
    "groups-google": "%groups.google.com%",
    "postgresql": "%www.postgresql.org%",
    "travis-ci": "%travis-ci.org%",
    "pinterest": "%www.pinterest.com%",
    "baiyuan-tech": "%baiyuan-tech.github.io%",
    "shagunsodhani": "%shagunsodhani.com%",
    # ── 2026-08-06 新增：跨资产噪声域名 ──
    # 代币化平台（非项目文档）
    "pump-fun": "%pump.fun%",
    "socios": "%www.socios.com%",
    "xstocks-fi": "%xstocks.fi%",
    "xstocks-com": "%xstocks.com%",
    "backed-fi": "%assets.backed.fi%",
    "backedassets-fi": "%www.backedassets.fi%",
    "backed-fi-root": "%backed.fi%",
    "reality-finance": "%realityfinance.xyz%",
    # 聚合器/CDN
    "coinmarketcap": "%coinmarketcap.com%",
    "robinhood-cdn": "%cdn.robinhood.com%",
    # 学术论文/原始代码
    "iacr": "%eprint.iacr.org%",
    "raw-github": "%raw.githubusercontent.com%",
    # 审计/安全平台聚合器
    "cyberscope": "%www.cyberscope.io%",
    # 通用代码托管（非审计相关）
    "github-cyberscope": "%github.com/cyberscope-io%",
    "github-quillhash": "%github.com/Quillhash%",
    "github-peckshield": "%github.com/peckshield%",
    "github-verichains": "%github.com/verichains%",
    "github-zokyo": "%github.com/zokyo-sec%",
    "github-bnb-whitepaper": "%github.com/bnb-chain/whitepaper%",
}


def run_rule_delete(conn, execute: bool) -> int:
    """规则直删：删除明显噪声域名下的所有 deep_crawl 链接。"""
    deleted = 0
    for label, pattern in RULE_NOISE_DOMAINS.items():
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT count(*) FROM biz.doc_source_entry "
                "WHERE entry_url LIKE %s AND discovered_from LIKE 'deep_crawl:%%'",
                (pattern,),
            )
            cnt = cur.fetchone()["count"]
        if cnt == 0:
            continue
        if execute:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    "DELETE FROM biz.doc_source_entry "
                    "WHERE entry_url LIKE %s AND discovered_from LIKE 'deep_crawl:%%'",
                    (pattern,),
                )
            conn.commit()
        print(f"  [规则直删] {label}: {cnt} 条{' (dry-run)' if not execute else ' ✓'}")
        deleted += cnt
    if deleted > 0:
        print(f"  规则直删合计: {deleted} 条\n")
    return deleted


# ── 审计/安全平台域名白名单：这些平台的链接是投研材料，不删除 ──
AUDIT_DOMAINS = {
    "audits.sherlock.xyz",
    "halborn.com",
    "openzeppelin.com",
    "certik.com",
    "chainsecurity.com",
    "code4rena.com",
    "consensys.net",
    "hacken.io",
    "immunefi.com",
    "tech-audit.org",
    "quillaudits.com",
    "www.coinfabrik.com",
    "reports.yaudit.dev",
    "guardianaudits.com",
    "sayfer.io",
    "paladinsec.co",
    "softstack.io",
    "wp.hacken.io",
    "trailofbits.com",
    "quantstamp.com",
    "solidified.io",
    "mixbytes.io",
    "arbitraryexecution.com",
    "slowmist.com",
    "peckshield.com",
}


def reset_ai_false_positives(conn, execute: bool) -> int:
    """
    重置 AI 误判的 deep_crawl 条目。
    对于关联 >50 资产的域名（非审计平台），将 ai_noise_checked_at 重置为 NULL，
    让 AI 在资产上下文中重新评估。
    """
    reset_count = 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 找出 deep_crawl 中关联 >50 资产的域名
        cur.execute("""
            SELECT LOWER(SPLIT_PART(REPLACE(REPLACE(entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain,
                   count(DISTINCT asset_id) AS asset_cnt,
                   count(*) AS entry_cnt
            FROM biz.doc_source_entry
            WHERE discovered_from LIKE 'deep_crawl:%%'
              AND ai_noise_checked_at IS NOT NULL
            GROUP BY 1
            HAVING count(DISTINCT asset_id) > 50
            ORDER BY asset_cnt DESC
        """)
        domains = [dict(r) for r in cur.fetchall()]

    for d in domains:
        domain = d["domain"]
        # 跳过审计平台域名
        if domain in AUDIT_DOMAINS:
            continue
        # 跳过通用代码托管平台（github.com 是合法代码托管，不重置）
        if domain in ("github.com", "gitlab.com", "bitbucket.org"):
            continue

        if execute:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    UPDATE biz.doc_source_entry
                    SET ai_noise_checked_at = NULL
                    WHERE entry_url LIKE %s
                      AND discovered_from LIKE 'deep_crawl:%%'
                      AND ai_noise_checked_at IS NOT NULL
                """, (f"%{domain}%",))
                affected = cur.rowcount
            conn.commit()
            print(f"  [AI误判纠正] {domain}: {affected} 条已重置（{d['asset_cnt']} 资产，{d['entry_cnt']} 条）")
        else:
            print(f"  [AI误判纠正] {domain}: {d['entry_cnt']} 条待重置（{d['asset_cnt']} 资产）(dry-run)")
        reset_count += d["entry_cnt"]

    if reset_count > 0:
        print(f"  AI误判纠正合计: {reset_count} 条\n")
    return reset_count


def reset_dense_domains(conn, execute: bool) -> int:
    """
    重置单资产密集域名的 AI 检查状态。
    对于域名在单个资产下链接数 >100 且占比 >90% 的情况，
    将 ai_noise_checked_at 重置为 NULL，让 AI 重新评估。
    排除 github.com/gitlab.com/bitbucket.org（合法代码托管）。
    """
    reset_count = 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            WITH asset_domain_stats AS (
                SELECT
                    dse.asset_id,
                    SUBSTRING(dse.entry_url FROM 'https?://([^/]+)') AS domain,
                    COUNT(*) AS domain_cnt
                FROM biz.doc_source_entry dse
                WHERE dse.entity_type = 'asset'
                  AND dse.discovered_from LIKE 'deep_crawl:%%'
                  AND dse.ai_noise_checked_at IS NOT NULL
                GROUP BY dse.asset_id, domain
            ),
            asset_totals AS (
                SELECT
                    dse.asset_id,
                    COUNT(*) AS total_dc
                FROM biz.doc_source_entry dse
                WHERE dse.entity_type = 'asset'
                  AND dse.discovered_from LIKE 'deep_crawl:%%'
                GROUP BY dse.asset_id
            )
            SELECT ads.asset_id, ads.domain, ads.domain_cnt, at.total_dc,
                   ROUND(ads.domain_cnt::numeric / at.total_dc * 100, 1) AS pct
            FROM asset_domain_stats ads
            INNER JOIN asset_totals at ON at.asset_id = ads.asset_id
            WHERE ads.domain_cnt > 100
              AND ads.domain_cnt::numeric / at.total_dc > 0.9
              AND ads.domain NOT IN ('github.com', 'gitlab.com', 'bitbucket.org')
            ORDER BY ads.domain_cnt DESC
        """)
        dense = [dict(r) for r in cur.fetchall()]

    for d in dense:
        domain = d["domain"]
        aid = d["asset_id"]
        cnt = d["domain_cnt"]
        pct = d["pct"]

        if execute:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    UPDATE biz.doc_source_entry
                    SET ai_noise_checked_at = NULL
                    WHERE asset_id = %s
                      AND entity_type = 'asset'
                      AND discovered_from LIKE 'deep_crawl:%%'
                      AND entry_url LIKE %s
                      AND ai_noise_checked_at IS NOT NULL
                """, (aid, f"%{domain}%"))
                affected = cur.rowcount
            conn.commit()
            print(f"  [密集域名重置] {domain}: {affected} 条已重置（asset_id={aid}, {cnt}条/{pct}%）")
        else:
            print(f"  [密集域名重置] {domain}: {cnt} 条待重置（asset_id={aid}, {pct}%）(dry-run)")
        reset_count += cnt

    if reset_count > 0:
        print(f"  密集域名重置合计: {reset_count} 条\n")
    return reset_count


def get_asset_domain_groups(conn, limit: int) -> list[dict]:
    """
    获取有未检查 deep_crawl 链接的资产，按资产分组，返回每个资产的域名聚合信息。
    优先处理链接数多的资产。
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT
                a.asset_id,
                a.canonical_symbol,
                a.canonical_name,
                SUM(CASE WHEN dse.ai_noise_checked_at IS NULL THEN 1 ELSE 0 END) AS unchecked,
                SUM(CASE WHEN dse.ai_noise_checked_at IS NOT NULL THEN 1 ELSE 0 END) AS checked
            FROM biz.doc_source_entry dse
            JOIN core.asset a ON a.asset_id = dse.asset_id
            WHERE dse.entity_type = 'asset'
              AND dse.discovered_from LIKE 'deep_crawl:%%'
            GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
            HAVING SUM(CASE WHEN dse.ai_noise_checked_at IS NULL THEN 1 ELSE 0 END) > 0
            ORDER BY unchecked DESC
            LIMIT %s
            """,
            (limit,),
        )
        assets = [dict(r) for r in cur.fetchall()]

    result = []
    for asset in assets:
        aid = asset["asset_id"]
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 按域名聚合未检查的 deep_crawl 链接
            cur.execute(
                """
                SELECT
                    SUBSTRING(entry_url FROM 'https?://([^/]+)') AS domain,
                    COUNT(*) AS cnt,
                    array_agg(entry_id ORDER BY entry_id) AS entry_ids,
                    (array_agg(entry_url ORDER BY entry_id))[1:4] AS sample_urls
                FROM biz.doc_source_entry
                WHERE asset_id = %s
                  AND entity_type = 'asset'
                  AND discovered_from LIKE 'deep_crawl:%%'
                  AND ai_noise_checked_at IS NULL
                GROUP BY domain
                ORDER BY cnt DESC
                """,
                (aid,),
            )
            domains = [dict(r) for r in cur.fetchall()]

        if not domains:
            continue

        result.append({
            "asset_id": aid,
            "symbol": asset["canonical_symbol"],
            "name": asset["canonical_name"],
            "unchecked": asset["unchecked"],
            "checked": asset["checked"],
            "domains": domains,
        })

    return result


def mark_checked(conn, entry_ids: list[int]) -> None:
    """批量标记已检查。"""
    if not entry_ids:
        return
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "UPDATE biz.doc_source_entry SET ai_noise_checked_at = NOW() "
            "WHERE entry_id = ANY(%s)",
            (entry_ids,),
        )
    conn.commit()


def delete_noise_ids(conn, entry_ids: list[int]) -> None:
    """批量删除噪声链接。"""
    if not entry_ids:
        return
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "DELETE FROM biz.doc_source_entry WHERE entry_id = ANY(%s)",
            (entry_ids,),
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="B2 AI 噪声清理 — 按资产分组")
    parser.add_argument("--execute", action="store_true", help="实际执行删除（默认 dry-run）")
    parser.add_argument("--limit", type=int, default=20, help="最多处理几个资产（默认 20）")
    parser.add_argument("--skip-rule-delete", action="store_true", help="跳过规则直删")
    args = parser.parse_args()

    print("=" * 70)
    print("  B2 AI 噪声清理 — 按资产分组")
    print(f"  模式: {'执行删除' if args.execute else 'dry-run 预览'}")
    print(f"  资产上限: {args.limit}")
    print("  策略: 按资产聚合域名 → AI 判断 → 批量删除噪声域名")
    print("=" * 70)

    llm = LLMClient(settings)

    with psycopg.connect(settings.database_url) as conn:
        # ── Step 1: 规则直删 ──
        if not args.skip_rule_delete:
            print("\n── 规则直删 ──")
            run_rule_delete(conn, args.execute)

        # ── Step 1.5: AI 误判纠正 ──
        # 对关联 >50 资产的非审计域名，重置 ai_noise_checked_at
        print("\n── AI 误判纠正 ──")
        reset_ai_false_positives(conn, args.execute)

        # ── Step 1.6: 密集域名重置 ──
        # 对单资产下链接数 >100 且占比 >90% 的域名，重置 ai_noise_checked_at
        print("\n── 密集域名重置 ──")
        reset_dense_domains(conn, args.execute)

        # ── Step 2: 按资产分组，AI 判断 ──
        print(f"\n── 按资产 AI 判断（最多 {args.limit} 个资产）──")
        assets = get_asset_domain_groups(conn, args.limit)
        print(f"获取到 {len(assets)} 个有未检查链接的资产\n")

        total_domains_judged = 0
        total_noise_domains = 0
        total_noise_links = 0
        total_kept_links = 0
        total_checked = 0

        for idx, asset in enumerate(assets):
            aid = asset["asset_id"]
            sym = asset["symbol"] or "?"
            name = asset["name"] or "?"
            domains = asset["domains"]
            unchecked = asset["unchecked"]
            total_checked += unchecked

            print(f"[{idx + 1}/{len(assets)}] {sym} ({name})  asset_id={aid}")
            print(f"  未检查: {unchecked:,} 条  |  已检查: {asset['checked']:,} 条")
            print(f"  域名数: {len(domains)}")

            # 构造 domain_groups 传给 AI
            domain_groups = []
            for d in domains:
                domain_groups.append({
                    "domain": d["domain"] or "unknown",
                    "count": d["cnt"],
                    "sample_urls": d.get("sample_urls") or [],
                    "entry_ids": d.get("entry_ids") or [],
                })

            start = time.time()
            try:
                results = llm.batch_check_asset_noise(
                    sym, name, domain_groups, asset_id=aid,
                )
                elapsed = time.time() - start
            except Exception as e:
                print(f"  ❌ AI 调用失败: {str(e)[:120]}")
                continue

            # 处理结果
            noise_domains = [r for r in results if r["noise"]]
            relevant_domains = [r for r in results if not r["noise"]]

            for r in noise_domains:
                domain = r["domain"]
                ids = r.get("affected_ids", [])
                reason = r.get("reason", "")
                if args.execute:
                    delete_noise_ids(conn, ids)
                print(f"  ✗ [{domain}] {len(ids):,} 条 → {reason[:80]}")

            # 标记相关域名的条目为已检查
            for r in relevant_domains:
                ids = r.get("affected_ids", [])
                if args.execute:
                    mark_checked(conn, ids)

            noise_link_count = sum(len(r.get("affected_ids", [])) for r in noise_domains)
            kept_link_count = sum(len(r.get("affected_ids", [])) for r in relevant_domains)

            total_domains_judged += len(domains)
            total_noise_domains += len(noise_domains)
            total_noise_links += noise_link_count
            total_kept_links += kept_link_count

            print(f"  噪声: {len(noise_domains)} 域名 / {noise_link_count:,} 条  |  "
                  f"保留: {len(relevant_domains)} 域名 / {kept_link_count:,} 条  |  "
                  f"耗时: {elapsed:.1f}s")

        # ── 汇总 ──
        print(f"\n{'=' * 70}")
        print(f"  汇总")
        print(f"  处理资产: {len(assets)} 个")
        print(f"  判断域名: {total_domains_judged} 个")
        print(f"  噪声域名: {total_noise_domains} 个")
        print(f"  噪声链接: {total_noise_links:,} 条")
        print(f"  保留链接: {total_kept_links:,} 条")
        print(f"  总检查数: {total_checked:,} 条")
        if not args.execute:
            print(f"  ⚠️  dry-run 模式，未实际执行。加 --execute 执行删除。")
        print(f"{'=' * 70}")


if __name__ == "__main__":
    main()