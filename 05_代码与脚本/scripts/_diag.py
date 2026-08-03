import sys

sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
import json

s = get_settings(require_database=True)

queries = {
    "map_with_info_no_core": """
        SELECT COUNT(*) FROM src_cmc.cmc_asset_map m
        JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.cmc_id
        LEFT JOIN core.asset_source_map asm ON asm.source_code='cmc' AND asm.source_asset_key=m.cmc_id::text
        WHERE asm.asset_id IS NULL
    """,
    "map_no_info_no_core": """
        SELECT COUNT(*) FROM src_cmc.cmc_asset_map m
        LEFT JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.cmc_id
        LEFT JOIN core.asset_source_map asm ON asm.source_code='cmc' AND asm.source_asset_key=m.cmc_id::text
        WHERE asm.asset_id IS NULL AND i.cmc_id IS NULL
    """,
    "core_assets_without_cmc": """
        SELECT COUNT(*) FROM core.asset a
        LEFT JOIN core.asset_source_map asm ON asm.asset_id = a.asset_id AND asm.source_code='cmc'
        WHERE asm.source_asset_key IS NULL
    """,
}

out = {}
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        for k, sql in queries.items():
            cur.execute(sql)
            out[k] = cur.fetchone()[0]
print(json.dumps(out, ensure_ascii=False))
