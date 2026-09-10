"""
初始化 Binance News 为 catalyst 类型 KOL。

同时做数据迁移：将旧的 binance_square_news 催化剂源记录的 source_code
更新为新的 kol_catalyst_binance_square_{profile_id} 格式，保持数据连续性。
"""
import sys, os
from pathlib import Path
# 向上找到 scripts/src 目录
BIN_DIR = Path(__file__).resolve().parent
SCRIPTS_SRC = BIN_DIR.parent / "src"
sys.path.insert(0, str(SCRIPTS_SRC))
from crypto_research.config import get_settings
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

url = get_settings(require_database=True).database_url
conn = psycopg.connect(url, row_factory=dict_row)

# 1. 插入 Binance News 博主档案
print("=== 插入 Binance News 博主档案 ===")
# 用 upsert 语义（不存在则插入，存在则更新 kol_type = 'catalyst'）
existing = conn.execute("""
    SELECT profile_id FROM biz.kol_profile 
    WHERE platform_code = 'binance_square' AND platform_user_id = 'Binance_News'
""").fetchone()

if existing:
    profile_id = existing['profile_id']
    print(f"  已存在 profile_id={profile_id}，更新 kol_type = catalyst")
    conn.execute("""
        UPDATE biz.kol_profile 
        SET kol_type = 'catalyst', is_active = TRUE, updated_at = NOW()
        WHERE profile_id = %s
    """, (profile_id,))
else:
    row = conn.execute("""
        INSERT INTO biz.kol_profile 
        (platform_code, platform_user_id, nickname, kol_type, is_active, 
         follower_count, notes, extra_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING profile_id
    """, (
        'binance_square',
        'Binance_News',
        'Binance News',
        'catalyst',
        True,
        2349819,  # 前面探测到的粉丝数
        '币安官方新闻账号 · 催化剂类 KOL',
        Json({
            'source': 'manual_init',
            'squareUid': 'RF5v7JH_6MiIJr-91F-aBA',
            'displayName': 'Binance News',
            'init_date': '2026-09-10',
        })
    )).fetchone()
    profile_id = row['profile_id']
    print(f"  新插入 profile_id={profile_id}")

new_source_code = f"kol_catalyst_binance_square_{profile_id}"
print(f"  新 source_code: {new_source_code}")

# 2. 迁移旧的 binance_square_news 催化剂数据到新 source_code
print("\n=== 迁移旧 binance_square_news 催化剂数据 ===")
old_source = 'binance_square_news'

# 查旧数据量
cnt_row = conn.execute("""
    SELECT COUNT(*) as cnt FROM biz.asset_catalyst 
    WHERE source_code = %s OR %s = ANY(source_codes)
""", (old_source, old_source)).fetchone()
old_count = cnt_row['cnt']
print(f"  旧数据量: {old_count} 条")

if old_count > 0:
    # 把旧 source_code 改成新的（只针对 source_code = 'binance_square_news' 的行）
    # 对于 source_codes 数组，也要替换
    updated = conn.execute("""
        UPDATE biz.asset_catalyst
        SET 
            source_code = CASE 
                WHEN source_code = %s THEN %s 
                ELSE source_code 
            END,
            source_codes = array_replace(source_codes, %s, %s),
            updated_at = NOW()
        WHERE source_code = %s OR %s = ANY(source_codes)
    """, (old_source, new_source_code, old_source, new_source_code, old_source, old_source))
    print(f"  已更新 {updated.rowcount} 条记录的 source_code/source_codes")

    # 验证：再查旧 source 是否还有数据
    remain = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.asset_catalyst 
        WHERE source_code = %s OR %s = ANY(source_codes)
    """, (old_source, old_source)).fetchone()
    print(f"  迁移后旧 source 剩余: {remain['cnt']} 条")

conn.commit()
print("\n✅ 完成")
conn.close()
