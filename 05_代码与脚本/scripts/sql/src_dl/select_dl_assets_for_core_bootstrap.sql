-- Bootstrap core.asset from DefiLlama protocol data.
-- Priority 1: Match by cmc_id (exact) - links to existing CMC core.asset
-- Priority 2: Match by symbol + name similarity
WITH ranked AS (
    SELECT
        p.protocol_id,
        p.symbol,
        p.name,
        p.description,
        p.url,
        p.category,
        p.chains,
        p.cmc_id,
        p.gecko_id,
        -- Priority 1: exact CMC match via cmc_id
        a_cmc.asset_id AS existing_asset_id_cmc,
        -- Priority 2: symbol match
        a_sym.asset_id AS existing_asset_id_sym,
        ROW_NUMBER() OVER (
            PARTITION BY p.protocol_id
            ORDER BY
                CASE WHEN a_cmc.asset_id IS NOT NULL THEN 1
                     WHEN a_sym.asset_id IS NOT NULL THEN 2
                     ELSE 3 END,
                CASE WHEN UPPER(a_sym.canonical_name) = p.name THEN 1 ELSE 2 END,
                COALESCE(a_cmc.asset_id, a_sym.asset_id)
        ) AS rn
    FROM src_dl.protocol_list p
    LEFT JOIN core.asset_source_map asm
        ON asm.source_code = 'dl'
        AND asm.source_asset_key = p.protocol_id
    LEFT JOIN core.asset_source_map asm_cmc
        ON asm_cmc.source_code = 'cmc'
        AND asm_cmc.source_asset_key = p.cmc_id
    LEFT JOIN core.asset a_cmc ON a_cmc.asset_id = asm_cmc.asset_id
    LEFT JOIN core.asset a_sym ON UPPER(a_sym.canonical_symbol) = UPPER(p.symbol)
    WHERE asm.asset_id IS NULL  -- not yet mapped to core
),
dedup AS (
    SELECT * FROM ranked WHERE rn = 1
)
SELECT
    protocol_id,
    symbol,
    name,
    description,
    url,
    category,
    chains,
    cmc_id,
    gecko_id,
    COALESCE(existing_asset_id_cmc, existing_asset_id_sym) AS existing_asset_id
FROM dedup
LIMIT %s
