-- Bootstrap core.asset from CoinGecko coin_list only (no coin_info required).
-- Match by symbol to existing CMC core.asset, then fall back to creating new rows.
WITH coin AS (
    SELECT
        l.coin_id,
        UPPER(l.symbol) AS symbol,
        l.name,
        l.platforms,
        -- CG coin 的 homepage 域名，用于区分 symbol/name 撞车的不同项目（如 cap-3 vs cap-4）
        LOWER(REGEXP_REPLACE(
            SPLIT_PART(SPLIT_PART(REGEXP_REPLACE(i.homepage_url, '^https?://', '', 'i'), '/', 1), ':', 1),
            '^www\.', '', 'i'
        )) AS homepage_domain
    FROM src_cg.coin_list l
    LEFT JOIN src_cg.coin_info i ON i.coin_id = l.coin_id
),
asset_site AS (
    -- 每个 core.asset 的 official_website 域名集合（一个资产可能有多个官网）
    SELECT
        d.asset_id,
        LOWER(REGEXP_REPLACE(
            SPLIT_PART(SPLIT_PART(REGEXP_REPLACE(d.entry_url, '^https?://', '', 'i'), '/', 1), ':', 1),
            '^www\.', '', 'i'
        )) AS website_domain
    FROM biz.doc_source_entry d
    WHERE d.entry_type = 'official_website'
      AND d.entry_url IS NOT NULL
      AND d.entry_url <> ''
),
ranked AS (
    SELECT
        c.coin_id,
        c.symbol,
        c.name,
        c.platforms,
        -- 仅当名称精确匹配（大小写敏感）且 homepage 域名一致时才关联现有资产，
        -- 防止 symbol/name 撞车（cap-3=cap.bet 被误关联到 cap-4=cap.app 的 Cap）。
        -- 任一方缺 homepage/官网信息时维持原 name 匹配，避免误伤无官网数据的资产。
        CASE
            WHEN a.canonical_name = c.name
             AND (
                c.homepage_domain IS NULL
                OR NOT EXISTS (SELECT 1 FROM asset_site s WHERE s.asset_id = a.asset_id)
                OR EXISTS (
                    SELECT 1 FROM asset_site s
                    WHERE s.asset_id = a.asset_id
                      AND s.website_domain = c.homepage_domain
                )
             )
            THEN a.asset_id
            ELSE NULL
        END AS existing_asset_id,
        ROW_NUMBER() OVER (
            PARTITION BY c.coin_id
            ORDER BY
                CASE WHEN a.canonical_name = c.name THEN 1 ELSE 2 END,
                a.asset_id
        ) AS rn
    FROM coin c
    LEFT JOIN core.asset_source_map asm
        ON asm.source_code = 'cg'
        AND asm.source_asset_key = c.coin_id
    LEFT JOIN core.asset a
        ON UPPER(a.canonical_symbol) = c.symbol
    WHERE asm.asset_id IS NULL
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
