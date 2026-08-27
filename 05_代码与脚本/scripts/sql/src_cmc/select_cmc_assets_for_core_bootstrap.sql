-- Bootstrap core.asset from CMC data.
-- Match CMC assets to existing core.asset by CMC source_map first,
-- then cross-source by symbol + name similarity (so CMC reuses assets
-- already created by CoinGecko/DL instead of creating duplicates),
-- finally fall back to creating new core.asset rows for unmatched assets.
WITH ranked AS (
    SELECT
        m.cmc_id,
        m.symbol,
        m.name,
        m.slug,
        m.platform_name,
        m.token_address,
        i.description,
        i.date_launched,
        i.category_hint,
        i.tags,
        i.urls,
        -- 优先复用 CMC 自身映射，其次跨源 symbol 匹配到的 core.asset
        COALESCE(asm.asset_id, a.asset_id) AS existing_asset_id,
        -- 已存在资产的 categories，用于 asset_type 多信号分类（避免刷新时回退已修正类型）
        ea.categories AS existing_categories,
        ROW_NUMBER() OVER (
            PARTITION BY m.cmc_id
            ORDER BY
                CASE WHEN UPPER(a.canonical_name) = UPPER(m.name) THEN 1 ELSE 2 END,
                a.asset_id
        ) AS rn
    FROM src_cmc.cmc_asset_map AS m
    LEFT JOIN src_cmc.cmc_asset_info AS i
        ON i.cmc_id = m.cmc_id
    LEFT JOIN core.asset_source_map AS asm
        ON asm.source_code = 'cmc'
       AND asm.source_asset_key = m.cmc_id::text
    LEFT JOIN core.asset ea
        ON ea.asset_id = asm.asset_id
    LEFT JOIN core.asset a
        ON UPPER(a.canonical_symbol) = UPPER(m.symbol)
        AND COALESCE(asm.asset_id, -1) != a.asset_id
        -- 防止 symbol 撞名（如 meme 币 "Bullish Trump Coin" 也占 BTC）劫持主流币：
        -- 仅当名称完全一致时才允许跨源复用已有 asset，否则视为新资产，避免覆盖污染。
        AND UPPER(a.canonical_name) = UPPER(m.name)
    WHERE
        (%s::boolean IS TRUE OR asm.asset_id IS NULL)
),
dedup AS (
    SELECT * FROM ranked WHERE rn = 1
)
SELECT
    cmc_id,
    symbol,
    name,
    slug,
    platform_name,
    token_address,
    description,
    date_launched,
    category_hint,
    urls,
    existing_asset_id,
    existing_categories
FROM dedup
ORDER BY cmc_id
LIMIT %s;
