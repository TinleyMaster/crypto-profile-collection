-- Bootstrap core.asset from CoinGecko data.
-- Match CG coins to existing CMC core.asset by symbol + name similarity,
-- then fall back to creating new core.asset rows for unmatched coins.
WITH ranked AS (
    SELECT
        i.coin_id,
        UPPER(i.symbol) AS symbol,
        i.name,
        i.description,
        i.homepage_url,
        i.image,
        i.genesis_date,
        i.categories,
        i.platforms,
        i.links,
        -- Try exact symbol match to existing CMC core.asset
        a.asset_id AS existing_asset_id,
        ROW_NUMBER() OVER (
            PARTITION BY i.coin_id
            ORDER BY
                CASE WHEN UPPER(a.canonical_name) = i.name THEN 1 ELSE 2 END,
                a.asset_id
        ) AS rn
    FROM src_cg.coin_info i
    LEFT JOIN core.asset_source_map asm
        ON asm.source_code = 'cg'
        AND asm.source_asset_key = i.coin_id
    LEFT JOIN core.asset a
        ON UPPER(a.canonical_symbol) = UPPER(i.symbol)
        AND COALESCE(asm.asset_id, -1) != a.asset_id
    WHERE asm.asset_id IS NULL  -- not yet mapped to core
),
dedup AS (
    SELECT * FROM ranked WHERE rn = 1
)
SELECT
    coin_id,
    symbol,
    name,
    description,
    homepage_url,
    image,
    genesis_date,
    categories,
    platforms,
    links,
    existing_asset_id
FROM dedup
LIMIT %s
