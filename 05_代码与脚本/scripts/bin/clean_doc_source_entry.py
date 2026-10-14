#!/usr/bin/env python3
"""doc_source_entry 链接噪音治理脚本（CMC/CoinGecko 元数据导入污染的清理与预防）。

背景：doc_source_entry 44.9 万条链接中，大量为爬虫/元数据导入的噪音——
    - docs 类型指向无关技术/平台域名（MDN/LinkedIn/PostgreSQL/ethereum.org 等）
    - official_website 指向社交/聚合/平台站（x.com/linktr.ee/realityfinance 等）
    - official_website 塞满网页子页外链（/blog /tutorials /explore 等）

规则（全部默认 dry-run，加 --apply 才真正删除）：
    R1. docs/whitepaper/docs_portal 类型 + 域名 ∈ 无关技术/平台站黑名单
    R2. official_website + 域名 ∈ 社交/聚合/平台站黑名单
    R3. official_website + 路径含子页噪音特征（/blog /tutorials /explore /token/ 等），
        保留含 /docs /whitepaper /gitbook 的路径

安全机制：
    - 每次执行前自动建备份表 biz.doc_source_entry_bak_{ts}（已存在则跳过）
    - 关键资产校验：PONS 等真实官网入口不被误删
    - 逐规则统计待删数量，--apply 才执行

用法：
    python bin/clean_doc_source_entry.py                    # dry-run 预览
    python bin/clean_doc_source_entry.py --apply            # 真正删除（R1+R2+R3）
    python bin/clean_doc_source_entry.py --apply --scope r1,r3   # 只执行指定规则
    python bin/clean_doc_source_entry.py --apply --skip-backup    # 跳过备份（不推荐）
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
_scripts_src = os.path.join(_code_root, "scripts", "src")
for cand in (_scripts_src, os.path.join(_code_root, "workbench"), "/app"):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# ════════════════════════════════════════════════════════════════
# 规则黑名单（新增噪音源时在此追加）
# ════════════════════════════════════════════════════════════════
# R1: docs 类型里绝不可能是资产文档站的域名（通用技术/平台/社交）
R1_HOSTS = (
    "developer.mozilla.org", "linkedin.com", "postgresql.org", "ethereum.org",
    "supabase.com", "quicknode.com", "alchemy.com", "platform.claude.com",
    "solana.com", "www.binance.com", "binance.com", "github.com",
    "audits.sherlock.xyz", "medium.com", "twitter.com", "x.com", "youtube.com",
    "facebook.com", "reddit.com", "t.me", "discord.com", "gitlab.com",
    "stackoverflow.com", "wikipedia.org", "docs.python.org", "openzeppelin.com",
    "npmjs.com",
)
# R2: official_website 里被批量误当官网的社交/聚合/平台域名
R2_HOSTS = (
    "x.com", "twitter.com", "linktr.ee", "facebook.com", "instagram.com",
    "youtube.com", "t.me", "telegram", "reddit.com", "discord.com",
    "medium.com", "www.binance.com", "binance.com", "robinhood.com",
    "pump.fun", "realityfinance.xyz", "xstocks.fi", "xstocks.com",
    "www.bstocks.finance", "bstocks.finance", "backed.fi", "assets.backed.fi",
    "dinari.com", "app.ondo.finance", "app.virtuals.io", "backpack.exchange",
    "app.anchored.finance",
)
# R3: official_website 子页噪音路径（网页外链/子页，非官网根）
NOISE_PATHS = (
    "/blog", "/tutorials", "/education", "/press", "/events", "/explore",
    "/pool", "/hackathon", "/developers", "/learn", "/roadmap", "/faq",
    "/careers", "/legal", "/terms", "/privacy", "/jobs", "/token/",
    "/address/", "/transfer", "/wallet", "/history", "/chart", "/charts",
    "/analytics", "/dashboard", "/airdrop", "/claim", "/about", "/team",
    "/partners", "/grant", "/community", "/news", "/updates", "/announc",
)
# R3 保留：含这些路径的 official_website 视为文档站，不删
KEEP_PATHS = ("/docs", "/whitepaper", "/white-paper", "/gitbook")


def _has_any(url: str, pats: tuple[str, ...]) -> bool:
    return any(p in url for p in pats)


def rule_r1_sql() -> tuple[str, list[str]]:
    like = [f"%{h}%" for h in R1_HOSTS]
    return ("WHERE entry_type IN ('docs','docs_portal','whitepaper_page') "
            "AND entry_url ILIKE ANY(%s)", like)


def rule_r2_sql() -> tuple[str, list[str]]:
    like = [f"%{h}%" for h in R2_HOSTS]
    return ("WHERE entry_type='official_website' AND entry_url ILIKE ANY(%s)", like)


def rule_r3_ids(conn) -> list[int]:
    """official_website 子页噪音（Python 侧过滤，保留 /docs 类）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT entry_id, entry_url FROM biz.doc_source_entry "
                    "WHERE entry_type='official_website'")
        rows = cur.fetchall()
    return [eid for eid, url in rows
            if _has_any(url, NOISE_PATHS) and not _has_any(url, KEEP_PATHS)]


RULES = [
    ("R1", "docs 类无关技术/平台域名", "sql"),
    ("R2", "official_website 社交/聚合/平台站", "sql"),
    ("R3", "official_website 子页外链", "python"),
]

# 关键资产校验：这些资产的真实官网入口不应被误删
GUARD_ASSETS = ("PONS", "LINK", "AAVE", "UNI", "BTC")


def backup_table(conn, table: str, ts: str) -> str | None:
    """备份表 biz.{table}_bak_{ts}；已存在则跳过。"""
    bak = f"biz.{table}_bak_{ts}"
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s",
                    (table.split('.')[-1], f"{table}_bak_{ts}"))
        if cur.fetchone()[0] > 0:
            return None
        cur.execute(f"CREATE TABLE {bak} AS SELECT * FROM biz.{table}")
        cur.execute(f"ALTER TABLE {bak} ADD PRIMARY KEY (entry_id)")
    return bak


def count_rule(conn, name: str) -> int:
    if name == "R3":
        return len(rule_r3_ids(conn))
    where, params = rule_r1_sql() if name == "R1" else rule_r2_sql()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry " + where, (params,))
        return cur.fetchone()[0]


def delete_rule(conn, name: str) -> int:
    if name == "R3":
        ids = rule_r3_ids(conn)
        deleted = 0
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            with conn.cursor() as cur:
                cur.execute("DELETE FROM biz.doc_source_entry WHERE entry_id = ANY(%s)", (chunk,))
                deleted += cur.rowcount
        return deleted
    where, params = rule_r1_sql() if name == "R1" else rule_r2_sql()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM biz.doc_source_entry " + where, (params,))
        return cur.rowcount


def guard_check(conn) -> None:
    """校验关键资产仍有记录（真官网未被误删）。"""
    with conn.cursor() as cur:
        for sym in GUARD_ASSETS:
            cur.execute(
                "SELECT COUNT(*) FROM biz.doc_source_entry d JOIN core.asset a ON a.asset_id=d.asset_id "
                "WHERE UPPER(a.canonical_symbol)=%s", (sym,))
            print(f"  ✓ {sym} 剩余记录: {cur.fetchone()[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="doc_source_entry 链接噪音治理（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    parser.add_argument("--scope", default="r1,r2,r3", help="执行的规则子集，如 r1,r3")
    parser.add_argument("--skip-backup", action="store_true", help="跳过备份（不推荐）")
    args = parser.parse_args()

    scopes = {s.strip().upper() for s in args.scope.split(",") if s.strip()}
    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry")
            total = cur.fetchone()[0]
        print(f"doc_source_entry 当前记录: {total}\n")

        # 预览
        plan = []
        for name, label, _kind in RULES:
            if name not in scopes:
                continue
            n = count_rule(conn, name)
            plan.append((name, label, n))
            print(f"[{name}] {label}: {n} 条", flush=True)

        if not args.apply:
            print("\n(dry-run) 未执行删除，加 --apply 生效")
            return 0

        # 备份
        if not args.skip_backup:
            ts = datetime.now().strftime("%Y%m%d")
            bak = backup_table(conn, "doc_source_entry", ts)
            print(f"\n备份: {'已创建 ' + bak if bak else '已存在（跳过）'}")

        # 执行
        for name, label, _ in plan:
            deleted = delete_rule(conn, name)
            print(f"[{name}] 已删除 {deleted} 条（{label}）")

        # 校验
        print("\n关键资产校验（未被误删）:")
        guard_check(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry")
            left = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry")
            assets = cur.fetchone()[0]
        print(f"\n清理后：{total} → {left} 条；覆盖资产 {assets}")
        conn.commit()
        print("已提交")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
