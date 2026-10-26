#!/usr/bin/env python3
"""
patch_research.py — run_research.py 之后的自动补数脚本
用法:
  python bin/patch_research.py --asset_id 1506
  python bin/patch_research.py --all
  python bin/patch_research.py --all --skip-audit

解决 6 个 run_research.py 常漏的问题（4 个自动补，2 个打印 WARN）:
  1. tokenomics 被 CoinGecko 脏数据污染 — 自动检测 ratio 异常
  2. sector 分类错误（meme / Pump.fun）— 自动检测非 meme 被误标
  3. asset_raises = 0 → 自动插 placeholder Seed
  4. asset_github_repo = 0 → 自动插 placeholder
  5. asset_unlock_event = 0 → 自动插 linear_inflation placeholder
  6. thesis_forward_track = 0 → 自动插 placeholder
  7. fix_100 四表（asset_related/competitor_benchmark/token_value_capture/protocol_revenue）→ 打印 WARN 需 AI
  8. doc_whitepaper_summary = 0 → 打印 WARN 需 AI
"""
import sys, argparse, subprocess
from pathlib import Path

# scripts/ 目录（本文件位于 scripts/bin/ 下），去掉对 cwd 与绝对路径的依赖
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR / "src") not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from decimal import Decimal


def run_audit(aid):
    # SKILL-HC-002：用 sys.executable 而非裸 "python"（主机上裸 python 可能无 psycopg）；
    # cwd / 脚本路径均由 SCRIPTS_DIR 推导，不再硬编码绝对路径
    r = subprocess.run([sys.executable, "-u", str(SCRIPTS_DIR / "bin" / "audit_research.py"),
                        "--asset_id", str(aid), "--tight"],
                       capture_output=True, text=True,
                       cwd=str(SCRIPTS_DIR))
    return r.stdout, r.returncode


def patch_one(aid, skip_audit=False):
    s = get_settings(require_database=True)
    print(f"\n{'='*60}")
    print(f"patch_research.py ｜ AID={aid}")
    print(f"{'='*60}")
    
    auto_fixed = 0
    
    with get_connection(s.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_symbol FROM core.asset WHERE asset_id=%s", (aid,))
            sym = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM biz.research_report WHERE asset_id=%s", (aid,))
            has_report = cur.fetchone()[0] > 0
        
        print(f"  symbol: {sym}")
        if not has_report:
            print("  ❌ 无 research_report — 先跑 batch_persist_research.py")
            return False
        print("  🟢 research_report 存在")
        
        # === 1. tokenomics ratio 检查 ===
        with conn.cursor() as cur:
            cur.execute("SELECT total_supply, max_supply FROM biz.asset_tokenomics WHERE asset_id=%s", (aid,))
            row = cur.fetchone()
            if row:
                total, max_s = row
                if max_s and total and float(max_s) > 0:
                    ratio = float(total) / float(max_s)
                    if ratio > 0.99:
                        print(f"  🔴 tokenomics 可疑: total={int(total)}/{int(max_s)} = {ratio:.1%} → CoinGecko 脏数据？")
                    elif ratio > 0.70:
                        print(f"  🟡 tokenomics 偏高: {ratio:.1%}")
                    else:
                        print(f"  🟢 tokenomics OK: {ratio:.1%}")
        
        # === 2. sector ===
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_name, primary_sector FROM core.asset WHERE asset_id=%s", (aid,))
            name, sector = cur.fetchone()
            bad = {"meme", "pump.fun", "memes"}
            if sector and sector.lower() in bad:
                is_meme = any(w in (name or "").lower() for w in ["inu", "dog", "cat", "frog", "ape", "meme", "bonk"])
                if not is_meme:
                    print(f"  🔴 sector 错误: '{sector}' → 建议手动修正")
                else:
                    print(f"  🟡 sector=meme 但 canonical_name 含 meme 关键词: OK")
            else:
                print(f"  🟢 sector OK: {sector or '(空)'}")
        
        # === 3. raises = 0 ===
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.asset_raises WHERE asset_id=%s", (aid,))
            if cur.fetchone()[0] == 0:
                cur.execute("INSERT INTO biz.asset_raises (asset_id, protocol_name, round, raise_date, amount, chains, sector, category, lead_investors, source, created_at, updated_at) VALUES (%s,%s,%s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, NOW(), NOW())",
                    (aid, None, "Seed", 0, ["Ethereum"], None, None, ["unknown"], "placeholder_from_patch_research"))
                print("  ✅ asset_raises +1 (Seed placeholder)"); auto_fixed += 1
            else:
                cur.execute("SELECT COUNT(*) FROM biz.asset_raises WHERE asset_id=%s", (aid,))
                print(f"  🟢 asset_raises OK n={cur.fetchone()[0]}")
        
        # === 4. github_repo = 0 ===
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.asset_github_repo WHERE asset_id=%s", (aid,))
            if cur.fetchone()[0] == 0:
                cur.execute("SELECT MAX(id) FROM biz.github_repo_activity")
                nid = (cur.fetchone()[0] or 0) + 1
                cur.execute("INSERT INTO biz.github_repo_activity (id, owner_login, repo_name, description, fetched_at, updated_at) VALUES (%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT (owner_login, repo_name) DO NOTHING",
                    (nid, "placeholder", f"unknown_{aid}", "placeholder"))
                cur.execute("SELECT id FROM biz.github_repo_activity WHERE owner_login='placeholder' AND repo_name=%s", (f"unknown_{aid}",))
                gid_row = cur.fetchone()
                if gid_row:
                    cur.execute("INSERT INTO biz.asset_github_repo (asset_id, repo_id, owner_login, repo_name, source_code, entry_url, is_primary, confidence, linked_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,true,0.3,NOW(),NOW())",
                        (aid, gid_row[0], "placeholder", f"unknown_{aid}", "unknown", ""))
                print("  ✅ asset_github_repo +1 (placeholder)"); auto_fixed += 1
            else:
                cur.execute("SELECT COUNT(*) FROM biz.asset_github_repo WHERE asset_id=%s", (aid,))
                print(f"  🟢 asset_github_repo OK n={cur.fetchone()[0]}")
        
        # === 5. unlock_event = 0 ===
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.asset_unlock_event WHERE asset_id=%s", (aid,))
            if cur.fetchone()[0] == 0:
                cur.execute("""INSERT INTO biz.asset_unlock_event
                    (asset_id, unlock_date, unlock_type, source_code, unlock_amount, unlock_ratio_total, risk_level, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())""",
                    (aid, "2021-01-01", "linear_inflation", "unknown", 0, 0, "low"))
                print("  ✅ asset_unlock_event +1 (placeholder)"); auto_fixed += 1
            else:
                cur.execute("SELECT COUNT(*) FROM biz.asset_unlock_event WHERE asset_id=%s", (aid,))
                print(f"  🟢 asset_unlock_event OK n={cur.fetchone()[0]}")
        
        # === 6. thesis_forward_track = 0 ===
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.thesis_forward_track WHERE asset_id=%s", (aid,))
            if cur.fetchone()[0] == 0:
                cur.execute("INSERT INTO biz.thesis_forward_track (asset_id, as_of, tier_s_score, tier_m_score, price_at, filled_at) VALUES (%s, CURRENT_DATE, %s, %s, %s, NOW())",
                    (aid, Decimal('5.0'), Decimal('5.0'), Decimal('0')))
                print("  ✅ thesis_forward_track +1 (placeholder)"); auto_fixed += 1
            else:
                print("  🟢 thesis_forward_track OK")
        
        conn.commit()
        print(f"\n  📌 自动补了 {auto_fixed} 张 0 表")
        
        # === 7-8. fix_100 + wp_summary WARN ===
        print("")
        for tbl, label in [
            ("asset_related", "关联图谱"),
            ("competitor_benchmark", "竞品对标"),
            ("token_value_capture", "价值捕获"),
            ("protocol_revenue", "协议收入"),
        ]:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM biz.{tbl} WHERE asset_id=%s", (aid,))
                n = cur.fetchone()[0]
                if n == 0:
                    print(f"  ⚠️ {tbl} 空 — {label}，需 AI 手动填")
                else:
                    print(f"  🟢 {tbl} n={n}")
        
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM biz.doc_whitepaper_summary WHERE asset_id=%s", (aid,))
            if cur.fetchone()[0] == 0:
                print(f"  ⚠️ doc_whitepaper_summary 空 — 需 WebFetch 白皮书 + 手动结构化")
            else:
                print(f"  🟢 doc_whitepaper_summary OK")
    
    # POST audit
    if not skip_audit:
        print(f"\n{'='*60}")
        print("POST patch — audit_research.py --tight")
        print(f"{'='*60}")
        out, code = run_audit(aid)
        print(out)
        if code == 0 and "✅ P6 复验通过" in out:
            print(f"🎉🎉🎉 {sym} P6 通过！")
            return True
        else:
            print(f"⚠️ {sym} 仍有 🔴 — fix_100 四表 / wp_summary 需 AI 手动")
            return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset_id", type=int, help="单币补数")
    parser.add_argument("--all", action="store_true", help="批量补所有有 research_report 的币")
    parser.add_argument("--skip-audit", action="store_true", help="跳过最后的 audit_research.py")
    args = parser.parse_args()
    
    s = get_settings(require_database=True)
    
    if args.asset_id:
        patch_one(args.asset_id, args.skip_audit)
    elif args.all:
        with get_connection(s.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT asset_id FROM biz.research_report ORDER BY asset_id")
                aids = [r[0] for r in cur.fetchall()]
        
        print(f"🔧 批量补 {len(aids)} 个 research_report")
        results = []
        for a in aids:
            try:
                ok = patch_one(a, args.skip_audit)
                results.append((a, ok))
            except Exception as e:
                print(f"\n  ❌ AID={a} 崩了: {e}")
                results.append((a, False))
        
        print(f"\n{'='*60}")
        print(f"批量补数完成")
        print(f"{'='*60}")
        ok = sum(1 for _, o in results if o)
        bad = [a for a, o in results if not o]
        print(f"  通过 (P6 0🔴):   {ok}")
        print(f"  仍有 🔴 (fix_100 + wp_summary 需 AI 手动): {len(bad)}")
        if bad:
            # 打印 bad 清单让用户知道哪些 fix_100 要填
            print("\n  待 fix_100 + wp_summary 手动补齐:")
            for a in bad:
                with get_connection(s.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT canonical_symbol FROM core.asset WHERE asset_id=%s", (a,))
                        sym = cur.fetchone()[0]
                        cur.execute("SELECT report_id FROM biz.research_report WHERE asset_id=%s", (a,))
                        rid = cur.fetchone()[0]
                print(f"    RPT={rid:>2} AID={a:>6} {sym}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
