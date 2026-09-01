import json, sys

sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
queries = [
    ("cmc_info", "select count(*) from src_cmc.cmc_asset_info"),
    ("cmc_map", "select count(*) from src_cmc.cmc_asset_map"),
    ("core_asset", "select count(*) from core.asset"),
    (
        "cmc_source_map",
        "select count(*) from core.asset_source_map where source_code='cmc'",
    ),
    ("doc_source_entry", "select count(*) from biz.doc_source_entry"),
    ("doc_asset", "select count(*) from biz.doc_asset"),
    (
        "cmc_map_without_asm",
        "select count(*) from src_cmc.cmc_asset_map m left join core.asset_source_map asm on asm.source_code='cmc' and asm.source_asset_key=m.cmc_id::text where asm.asset_id is null",
    ),
    ("cg_coin_list", "select count(*) from src_cg.coin_list"),
    ("cg_coin_info", "select count(*) from src_cg.coin_info"),
    (
        "cg_source_map",
        "select count(*) from core.asset_source_map where source_code='cg'",
    ),
    ("dl_protocol_list", "select count(*) from src_dl.protocol_list"),
    (
        "dl_source_map",
        "select count(*) from core.asset_source_map where source_code='dl'",
    ),
    (
        "dl_doc_source_entries",
        "select count(*) from biz.doc_source_entry where source_code='dl'",
    ),
]
out = {}
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        for k, sql in queries:
            cur.execute(sql)
            out[k] = cur.fetchone()[0]
print(json.dumps(out, ensure_ascii=False))
