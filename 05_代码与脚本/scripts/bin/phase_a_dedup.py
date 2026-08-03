"""Phase A Step 4: Pure SQL dedup - merge duplicate (symbol+name) assets."""

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

DEDUP = """
-- Dedup: for assets where (UPPER(symbol), UPPER(name)) are identical,
-- merge all into the oldest (MIN asset_id).

-- Create temp mapping: old_id -> keep_id
CREATE TEMP TABLE IF NOT EXISTS _dedup_map AS
SELECT
    MIN(asset_id) AS keep_id,
    unnest(array_agg(asset_id ORDER BY asset_id)) AS old_id
FROM core.asset
GROUP BY UPPER(canonical_symbol), UPPER(canonical_name)
HAVING COUNT(*) > 1;

-- Remove rows where old_id = keep_id
DELETE FROM _dedup_map WHERE old_id = keep_id;

-- Delete conflicting source_maps (those that already exist at keep_id)
DELETE FROM core.asset_source_map sm
USING _dedup_map m
WHERE sm.asset_id = m.old_id
  AND EXISTS (
      SELECT 1 FROM core.asset_source_map sm2
      WHERE sm2.asset_id = m.keep_id
        AND sm2.source_code = sm.source_code
        AND sm2.source_asset_key = sm.source_asset_key
  );

-- Move remaining source_maps
UPDATE core.asset_source_map sm
SET asset_id = m.keep_id, updated_at = NOW()
FROM _dedup_map m
WHERE sm.asset_id = m.old_id;

-- Delete conflicting doc_source_entries
DELETE FROM biz.doc_source_entry dse
USING _dedup_map m
WHERE dse.asset_id = m.old_id
  AND EXISTS (
      SELECT 1 FROM biz.doc_source_entry dse2
      WHERE dse2.asset_id = m.keep_id
        AND dse2.entity_type = dse.entity_type
        AND dse2.entry_url = dse.entry_url
  );

-- Move remaining doc_source_entries
UPDATE biz.doc_source_entry dse
SET asset_id = m.keep_id, updated_at = NOW()
FROM _dedup_map m
WHERE dse.asset_id = m.old_id;

-- Delete conflicting asset_contracts
DELETE FROM core.asset_contract ac
USING _dedup_map m
WHERE ac.asset_id = m.old_id
  AND EXISTS (
      SELECT 1 FROM core.asset_contract ac2
      WHERE ac2.asset_id = m.keep_id
        AND ac2.chain = ac.chain
        AND ac2.contract_address = ac.contract_address
  );

-- Move remaining asset_contracts
UPDATE core.asset_contract ac
SET asset_id = m.keep_id, updated_at = NOW()
FROM _dedup_map m
WHERE ac.asset_id = m.old_id;

-- Delete orphan assets
DELETE FROM core.asset a
USING _dedup_map m
WHERE a.asset_id = m.old_id;

-- Cleanup
DROP TABLE IF EXISTS _dedup_map;
"""

with get_connection(settings.database_url) as conn:
    # First, count duplicates
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*), SUM(cnt - 1)
            FROM (SELECT COUNT(*) AS cnt FROM core.asset
                  GROUP BY UPPER(canonical_symbol), UPPER(canonical_name)
                  HAVING COUNT(*) > 1) t
        """)
        groups, to_merge = cur.fetchone()
    print(f"[Step 4] 重复组: {groups}, 待合并: {to_merge}")

    with conn.cursor() as cur:
        cur.execute(DEDUP)
    conn.commit()
    print("[Step 4] 资产去重完成")

    # Verify
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM core.asset")
        print(f"  core.asset: {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(DISTINCT canonical_symbol) FROM core.asset")
        print(f"  唯一 symbol: {cur.fetchone()[0]}")
