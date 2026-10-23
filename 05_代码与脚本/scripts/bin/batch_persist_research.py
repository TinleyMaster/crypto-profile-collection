"""
批量入库 research_report：扫描所有 *_最终.md → 提取 symbol → 查 asset_id → 提取 report 核心数据 → INSERT。
同时自动补 thesis_forward_track + email_status 标记 sent。
"""
import sys, os, re, json, hashlib, glob
sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.persist import extract_tracking_from_md
from datetime import datetime
from decimal import Decimal

RESEARCH_DIR = r"E:\瞎搞乱搞\web3\加密货币研究报告\05_代码与脚本\scripts\data\research"
s = get_settings(require_database=True)

def parse_report_md(md_text):
    """从报告 markdown 提取核心字段。"""
    info = {}
    # symbol / asset_id (从路径前缀)
    m = re.search(r'标的.*?asset_id[=：]\s*(\d+)', md_text)
    info['asset_id'] = int(m.group(1)) if m else None
    m = re.search(r'标的.*?([A-Z]{2,20})\s*\(', md_text)
    info['symbol'] = m.group(1) if m else None
    
    # stance
    m = re.search(r'方向[：:]\s*(neutral_bullish|neutral_bearish|neutral|bullish|bearish)', md_text)
    info['stance'] = m.group(1) if m else 'neutral'
    
    # score
    m = re.search(r'综合评分[：:]\s*([\d.]+)', md_text)
    info['score'] = Decimal(m.group(1)) if m else Decimal('5.0')
    
    # confidence
    m = re.search(r'置信度[：:]\s*(\d+)%', md_text)
    info['confidence'] = Decimal(f"0.{m.group(1)}") if m else Decimal('0.50')
    
    # title (第一行 # 标题)
    m = re.search(r'^#\s+(.+?)(?:\s*$)', md_text, re.MULTILINE)
    info['title'] = m.group(1)[:120] if m else "研究报告"
    
    # summary (第一段总结)
    m = re.search(r'\*\*一句话结论\*\*[：:]?\s*(.+?)(?:\n\n|$)', md_text, re.DOTALL)
    if m:
        s2 = m.group(1).strip()
        info['summary'] = s2[:500]
    else:
        info['summary'] = ""
    
    # 五维评分卡
    dims = {}
    for dim in ['基本面','代币经济','价值捕获','市场结构','技术安全']:
        m = re.search(r'\|\s*\*\*' + re.escape(dim) + r'\*\*\s*\|.*?\|\s*(\d)', md_text)
        if m: dims[dim] = int(m.group(1))
    info['dimensions'] = json.dumps(dims) if dims else None

    # 跟踪清单（md 跟踪章节 → [{freq, items}]；缺失则落库口子再兜底）
    tracking = extract_tracking_from_md(md_text)
    info['tracking'] = tracking if tracking else None

    return info


def find_asset_id(symbol, name_hint, core_conn):
    """从 symbol + hint 查 asset_id。"""
    with core_conn.cursor() as cur:
        cur.execute("SELECT asset_id, canonical_symbol FROM core.asset WHERE canonical_symbol=%s ORDER BY market_cap_rank NULLS LAST", (symbol,))
        rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if len(rows) > 1 and name_hint:
            for aid, sym in rows:
                pass  # 无法区分，返回 mcap 最小的
            return rows[0][0]
        return rows[0][0] if rows else None


# === 扫描所有最终报告 ===
all_mds = sorted(glob.glob(os.path.join(RESEARCH_DIR, "**", "*最终.md"), recursive=True))
print(f"扫描到 {len(all_mds)} 个 *_最终.md 文件\n")

# === 查 DB 已有 asset_id 集合 ===
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT asset_id FROM biz.research_report")
        existing_aids = set(r[0] for r in cur.fetchall())
        print(f"DB 已有 research_report 的 asset_id: {len(existing_aids)} 个")
    
    newly_inserted = []
    skipped_existing = []
    failed = []
    
    for md_path in all_mds:
        rel = os.path.relpath(md_path, RESEARCH_DIR)
        folder = rel.split(os.sep)[0]  # e.g. "chip_20261015"
        sym = folder.split("_")[0].upper()
        
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
        
        parsed = parse_report_md(md_text)
        
        # 先尝试从文件本身提取 asset_id
        asset_id = parsed['asset_id']
        if not asset_id:
            # 从 symbol 查 DB
            asset_id = find_asset_id(sym, None, conn)
        
        if not asset_id:
            failed.append((sym, folder, "asset_id not found"))
            print(f"  ❌ {sym} ({folder}) — asset_id not found")
            continue
        
        sha = hashlib.sha256(md_text.encode()).hexdigest()
        
        # 查 DB
        with conn.cursor() as cur:
            cur.execute("SELECT report_id FROM biz.research_report WHERE asset_id=%s AND report_sha256=%s", (asset_id, sha))
            if cur.fetchone():
                skipped_existing.append((asset_id, sym))
                continue
            
            # 插入
            try:
                cur.execute("""INSERT INTO biz.research_report
                    (asset_id, symbol, name, title, generated_at, engine, stance, score, confidence,
                     summary, dimensions_json, report_path, report_md, report_sha256, subject, email_status, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,NOW(),'CAP11_v1',%s,%s,%s,%s,%s,%s,%s,%s,%s,'sent',NOW(),NOW())
                    RETURNING report_id""",
                    (asset_id, sym, parsed.get('symbol') or sym, parsed.get('title', sym),
                     parsed.get('stance','neutral'), parsed.get('score', Decimal('5.0')), parsed.get('confidence', Decimal('0.50')),
                     parsed.get('summary',''), parsed.get('dimensions'),
                     f"{folder}/{os.path.basename(md_path)}", md_text, sha,
                     parsed.get('title', sym)[:120]))
                rid = cur.fetchone()[0]
                newly_inserted.append((rid, asset_id, sym))
                print(f"  ✅ INSERT report_id={rid} AID={asset_id} {sym}")
            except Exception as e:
                failed.append((sym, folder, str(e)[:80]))
                print(f"  ❌ {sym} ({folder}) — {e}")
        
        conn.commit()  # 每个独立 commit
    
    # thesis_forward_track — 给新插入的也补上
    for rid, aid, sym in newly_inserted:
        with conn.cursor() as cur:
            cur.execute("SELECT track_id FROM biz.thesis_forward_track WHERE asset_id=%s", (aid,))
            if not cur.fetchone():
                cur.execute("INSERT INTO biz.thesis_forward_track (asset_id, as_of, tier_s_score, tier_m_score, price_at, filled_at) VALUES (%s, CURRENT_DATE, %s, %s, %s, NOW())",
                    (aid, Decimal('5.0'), Decimal('5.0'), Decimal('0')))
                print(f"  ✅ thesis_forward_track {sym}")
        conn.commit()

print(f"\n{'='*50}")
print(f"📊 统计")
print(f"{'='*50}")
print(f"  扫描到 *_最终.md:     {len(all_mds)}")
print(f"  新入库 research_report: {len(newly_inserted)} 🟢")
print(f"  已存在跳过:             {len(skipped_existing)} 🟡")
print(f"  失败:                   {len(failed)} 🔴")

if failed:
    print(f"\n  失败详情:")
    for sym, folder, err in failed:
        print(f"    {sym} ({folder}): {err}")

# === 最终 DB 状态 ===
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT report_id, asset_id, symbol, stance, score, email_status FROM biz.research_report ORDER BY report_id")
        rows = cur.fetchall()
        print(f"\n=== DB research_report 最终 ({len(rows)} 个)===")
        for r in rows:
            print(f"  RPT={r[0]:>2} AID={r[1]:>6} sym={r[2]:<8} stance={r[3]:<18} score={r[4]} status={r[5]}")
