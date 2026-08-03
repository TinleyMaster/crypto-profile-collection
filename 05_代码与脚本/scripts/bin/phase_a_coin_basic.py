"""Phase A Step 5: Build biz.coin_basic consumption table."""

from __future__ import annotations
import io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)

with get_connection(settings.database_url) as conn:
    print("[Step 5] 创建 biz.coin_basic...")

    # Drop and recreate
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS biz.coin_basic CASCADE")
        cur.execute("""
            CREATE TABLE biz.coin_basic (
                asset_id BIGINT PRIMARY KEY REFERENCES core.asset(asset_id),
                cmc_id BIGINT,
                defillama_slug TEXT,
                coin_symbol TEXT NOT NULL,
                coin_name TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                main_chain TEXT,
                primary_contract_address TEXT,
                official_website TEXT,
                description_short TEXT,
                logo_url TEXT,
                mapping_status TEXT NOT NULL DEFAULT 'active',
                last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_coin_basic_symbol ON biz.coin_basic(coin_symbol)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_coin_basic_cmc_id ON biz.coin_basic(cmc_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_coin_basic_type ON biz.coin_basic(asset_type)"
        )
    conn.commit()

    # Populate
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO biz.coin_basic (
                asset_id, cmc_id, defillama_slug,
                coin_symbol, coin_name, asset_type,
                main_chain, primary_contract_address,
                official_website, description_short, logo_url,
                mapping_status, last_refreshed_at
            )
            SELECT
                a.asset_id,
                (SELECT asm.source_asset_key::bigint FROM core.asset_source_map asm
                 WHERE asm.asset_id = a.asset_id AND asm.source_code = 'cmc' LIMIT 1),
                (SELECT asm.source_asset_key FROM core.asset_source_map asm
                 WHERE asm.asset_id = a.asset_id AND asm.source_code = 'dl' LIMIT 1),
                a.canonical_symbol,
                a.canonical_name,
                a.asset_type,
                (SELECT ac.chain FROM core.asset_contract ac
                 WHERE ac.asset_id = a.asset_id ORDER BY ac.is_primary DESC, ac.contract_id LIMIT 1),
                (SELECT ac.contract_address FROM core.asset_contract ac
                 WHERE ac.asset_id = a.asset_id ORDER BY ac.is_primary DESC, ac.contract_id LIMIT 1),
                (SELECT dse.entry_url FROM biz.doc_source_entry dse
                 WHERE dse.asset_id = a.asset_id AND dse.entry_type = 'official_website'
                   AND dse.is_primary = TRUE LIMIT 1),
                a.description_short,
                (SELECT info.logo FROM src_cmc.cmc_asset_info info
                 WHERE info.cmc_id = (
                     SELECT asm.source_asset_key::bigint FROM core.asset_source_map asm
                     WHERE asm.asset_id = a.asset_id AND asm.source_code = 'cmc' LIMIT 1
                 )),
                'active',
                NOW()
            FROM core.asset a
            WHERE a.status = 'active'
            ON CONFLICT (asset_id) DO UPDATE SET
                cmc_id = EXCLUDED.cmc_id,
                defillama_slug = EXCLUDED.defillama_slug,
                coin_symbol = EXCLUDED.coin_symbol,
                coin_name = EXCLUDED.coin_name,
                asset_type = EXCLUDED.asset_type,
                main_chain = EXCLUDED.main_chain,
                primary_contract_address = EXCLUDED.primary_contract_address,
                official_website = EXCLUDED.official_website,
                description_short = EXCLUDED.description_short,
                logo_url = EXCLUDED.logo_url,
                last_refreshed_at = NOW()
        """)
        cnt = cur.rowcount
    conn.commit()

    print(f"[Step 5] biz.coin_basic 构建完成: {cnt} 条")

    # Stats
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT coin_symbol) FROM biz.coin_basic")
        total, syms = cur.fetchone()
        cur.execute(
            "SELECT asset_type, COUNT(*) FROM biz.coin_basic GROUP BY asset_type ORDER BY COUNT(*) DESC"
        )
        types = cur.fetchall()

    print(f"  biz.coin_basic: {total} 条 ({syms} 唯一 symbol)")
    for t, c in types:
        print(f"    {t}: {c}")
