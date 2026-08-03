-- Bootstrap core.asset from CoinGecko coin_list only (no coin_info required).
-- Match by symbol to existing CMC core.asset, then fall back to creating new rows.
WITH ranked AS (
    SELECT
        l.coin_id,
        UPPER(l.symbol) AS symbol,
        l.name,
        l.platforms,
        -- Try exact symbol match to existing core.asset (via CMC)
        a.asset_id AS existing_asset_id,
        ROW_NUMBER() OVER (
            PARTITION BY l.coin_id
            ORDER BY
                CASE WHEN UPPER(a.canonical_name) = l.name THEN 1 ELSE 2 END,
                a.asset_id
        ) AS rn
    FROM src_cg.coin_list l
    LEFT JOIN core.asset_source_map asm
        ON asm.source_code = 'cg'
        AND asm.source_asset_key = l.coin_id
    LEFT JOIN core.asset a
        ON UPPER(a.canonical_symbol) = UPPER(l.symbol)
    WHERE asm.asset_id IS NULL  -- not yet mapped to core
),
dedup AS (
    SELECT * FROM ranked WHERE rn = 1
)
SELECT
    coin_id,
    symbol,
    name,
    platforms,
    existing_asset_id
FROM dedup
LIMIT %s
