-- 多来源赛道标签归一化（纯 SQL 批量版 v2）
-- 来源：CMC tags/category_hint + CG categories + DL category
-- v2 改进：
--   - 合并同一资产的多个来源映射（多 CMC/CG/DL ID 合并去重），避免取到信息不全的那条
--   - 补充 SocialFi、GambleFi、DeSci、Wrapped-Tokens 等高频标签映射
-- 直接执行，全量重建 biz.asset_sector 和 core.asset.primary_sector
-- 执行时间：秒级

-- ═══════════════════════════════════════════════════════════
-- 0. 清空旧数据
-- ═══════════════════════════════════════════════════════════
DELETE FROM biz.asset_sector WHERE source IN ('cmc', 'cg', 'dl');
UPDATE core.asset SET primary_sector = 'other';

-- ═══════════════════════════════════════════════════════════
-- 1. CMC 来源：tags + category_hint 映射
--    合并同一资产的多个 CMC 映射（tags 合并去重，category_hint 取全部）
-- ═══════════════════════════════════════════════════════════
WITH cmc_tags AS (
    -- 合并所有 CMC 映射的 tags（去重）
    SELECT m.asset_id,
           jsonb_agg(DISTINCT t.tag) as all_tags
    FROM core.asset_source_map m
    JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.source_asset_key::bigint
    CROSS JOIN LATERAL jsonb_array_elements_text(i.tags) AS t(tag)
    WHERE m.source_code = 'cmc'
      AND i.tags IS NOT NULL
    GROUP BY m.asset_id
),
cmc_cats AS (
    -- 合并所有 CMC 映射的 category_hint（去重）
    SELECT m.asset_id,
           array_agg(DISTINCT i.category_hint) as all_categories
    FROM core.asset_source_map m
    JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.source_asset_key::bigint
    WHERE m.source_code = 'cmc'
      AND i.category_hint IS NOT NULL
    GROUP BY m.asset_id
),
-- tag 展开 + 映射
tag_hits AS (
    SELECT d.asset_id, map.sector, map.confidence
    FROM cmc_tags d
    CROSS JOIN LATERAL jsonb_array_elements_text(d.all_tags) AS t(tag)
    JOIN (VALUES
        -- L1/L2
        ('layer-1', 'l1', 0.9), ('layer-2', 'l2', 0.9),
        ('rollups', 'l2', 0.8), ('rollups-as-a-service', 'l2', 0.7),
        ('zero-knowledge-zk', 'l2', 0.6), ('zk-rollups', 'l2', 0.85),
        ('zk-coprocessors', 'l2', 0.6),
        -- DeFi
        ('defi', 'defi', 0.9), ('decentralized-exchange-dex-token', 'defi', 0.8),
        ('yield-farming', 'defi', 0.7), ('lending', 'defi', 0.8),
        ('borrowing', 'defi', 0.8), ('liquid-staking-tokens-lsds', 'defi', 0.8),
        ('liquid-restaking-tokens-lrts', 'defi', 0.8), ('dex-tools', 'defi', 0.7),
        ('launchpad', 'defi', 0.6), ('index-fund', 'defi', 0.7),
        ('insurance', 'defi', 0.7), ('prediction-market', 'defi', 0.7),
        -- 稳定币
        ('stablecoin', 'stablecoin', 0.95), ('asset-backed-stablecoin', 'stablecoin', 0.9),
        ('fiat-stablecoin', 'stablecoin', 0.9), ('usd-stablecoin', 'stablecoin', 0.9),
        ('eur-stablecoin', 'stablecoin', 0.9), ('algorithmic-stablecoin', 'stablecoin', 0.85),
        ('yield-bearing-stablecoin', 'stablecoin', 0.9), ('stablecoin-protocol', 'stablecoin', 0.8),
        -- Meme
        ('memes', 'meme', 0.9), ('animal-memes', 'meme', 0.9),
        ('cat-themed', 'meme', 0.8), ('doggone-doggerel', 'meme', 0.8),
        -- GameFi
        ('gaming', 'gamefi', 0.9), ('play-to-earn', 'gamefi', 0.9),
        ('metaverse', 'gamefi', 0.8), ('collectibles-nfts', 'gamefi', 0.8),
        ('fan-token', 'gamefi', 0.7), ('move-to-earn', 'gamefi', 0.8),
        ('gambling', 'gamefi', 0.6),
        -- RWA
        ('real-world-assets-protocols', 'rwa', 0.9), ('tokenized-assets', 'rwa', 0.8),
        ('tokenized-stock', 'rwa', 0.9), ('tradfi-assets-derivatives', 'rwa', 0.8),
        ('rehypothecated-crypto', 'rwa', 0.8), ('tokenized-etfs', 'rwa', 0.8),
        ('real-estate', 'rwa', 0.8),
        -- AI
        ('ai-big-data', 'ai', 0.9), ('ai-agents', 'ai', 0.9),
        ('ai-applications', 'ai', 0.8), ('distributed-computing', 'ai', 0.7),
        ('ai-meme-coins', 'ai', 0.7),
        -- 平台币
        ('platform', 'cex_token', 0.7), ('centralized-exchange-token', 'cex_token', 0.9),
        ('centralized-exchange', 'cex_token', 0.85), ('discount-token', 'cex_token', 0.8),
        ('exchange-token', 'cex_token', 0.8),
        -- 衍生品
        ('synthetics', 'derivatives', 0.8), ('derivatives', 'derivatives', 0.9),
        ('options', 'derivatives', 0.8), ('perpetual-dex', 'derivatives', 0.85),
        -- DePIN
        ('depin', 'depin', 0.9), ('storage', 'depin', 0.7),
        ('decentralized-storage', 'depin', 0.8), ('dewi', 'depin', 0.8),
        ('wireless', 'depin', 0.7),
        -- 基础设施
        ('infrastructure', 'infra', 0.85), ('oracle', 'infra', 0.75),
        ('bridge', 'infra', 0.7), ('cross-chain', 'infra', 0.7),
        ('node', 'infra', 0.6), ('rpc-node', 'infra', 0.7),
        ('name-service', 'infra', 0.7), ('wallets', 'infra', 0.7),
        ('privacy', 'infra', 0.7), ('data-availability', 'infra', 0.8),
        ('medium-of-exchange', 'infra', 0.6), ('store-of-value', 'infra', 0.6),
        ('mineable', 'infra', 0.5), ('pow', 'infra', 0.5),
        ('pos', 'infra', 0.5), ('payments', 'infra', 0.65),
        ('services', 'infra', 0.6), ('marketplace', 'infra', 0.5),
        -- Social / 其他
        ('communications-social-media', 'infra', 0.5),
        ('dao', 'infra', 0.4),
        ('desci', 'ai', 0.5),
        ('entertainment', 'gamefi', 0.4),
        ('social-token', 'infra', 0.4),
        ('media', 'infra', 0.4),
        ('asset-management', 'defi', 0.5),
        ('governance', 'defi', 0.4),
        ('marketing', 'infra', 0.3),
        ('education', 'infra', 0.3),
        ('health', 'ai', 0.4),
        ('energy', 'depin', 0.4),
        ('hybrid-pow-pos', 'l1', 0.4),
        ('move-vm', 'l1', 0.5),
        ('web3', 'infra', 0.3)
    ) map(tag_name, sector, confidence) ON lower(t.tag) = lower(map.tag_name)
),
-- category_hint 映射
cat_hits AS (
    SELECT d.asset_id, map.sector, map.confidence
    FROM cmc_cats d
    CROSS JOIN LATERAL unnest(d.all_categories) AS c(cat)
    JOIN (VALUES
        ('layer-1', 'l1', 0.8), ('layer-2', 'l2', 0.8),
        ('defi', 'defi', 0.8), ('memes', 'meme', 0.8),
        ('gaming', 'gamefi', 0.7), ('play-to-earn', 'gamefi', 0.7),
        ('collectibles-nfts', 'gamefi', 0.7),
        ('real-world-assets-protocols', 'rwa', 0.8), ('tokenized-assets', 'rwa', 0.7),
        ('tokenized-stock', 'rwa', 0.8),
        ('ai-big-data', 'ai', 0.8), ('ai-agents', 'ai', 0.8),
        ('platform', 'cex_token', 0.6), ('depin', 'depin', 0.8),
        ('synthetics', 'derivatives', 0.7),
        ('stablecoin', 'stablecoin', 0.9), ('asset-backed-stablecoin', 'stablecoin', 0.85),
        ('algorithmic-stablecoin', 'stablecoin', 0.8),
        ('centralized-exchange', 'cex_token', 0.75),
        ('marketplace', 'infra', 0.5), ('services', 'infra', 0.55),
        ('medium-of-exchange', 'infra', 0.55), ('store-of-value', 'infra', 0.55),
        ('mineable', 'infra', 0.5), ('privacy', 'infra', 0.6),
        ('infrastructure', 'infra', 0.75), ('oracle', 'infra', 0.7),
        ('wallets', 'infra', 0.65),
        ('communications-social-media', 'infra', 0.5),
        ('gambling', 'gamefi', 0.6),
        ('dao', 'infra', 0.4),
        ('desci', 'ai', 0.5),
        ('entertainment', 'gamefi', 0.4),
        ('health', 'ai', 0.4),
        ('asset-management', 'defi', 0.5),
        ('education', 'infra', 0.3),
        ('media', 'infra', 0.4),
        ('energy', 'depin', 0.4),
        ('marketing', 'infra', 0.3),
        ('hybrid-pow-pos', 'l1', 0.4),
        ('binance-chain', 'l1', 0.5)
    ) map(cat_name, sector, confidence) ON lower(c.cat) = lower(map.cat_name)
),
-- 合并，取每个赛道最高置信度
cmc_best AS (
    SELECT asset_id, sector, MAX(confidence) as confidence
    FROM (
        SELECT asset_id, sector, confidence FROM tag_hits
        UNION ALL
        SELECT asset_id, sector, confidence FROM cat_hits
    ) all_hits
    GROUP BY asset_id, sector
)
INSERT INTO biz.asset_sector (asset_id, sector, source, confidence, is_primary)
SELECT asset_id, sector, 'cmc', confidence, false
FROM cmc_best;

-- ═══════════════════════════════════════════════════════════
-- 2. CG 来源：categories 数组映射
--    合并同一资产的多个 CG 映射（categories 合并去重）
-- ═══════════════════════════════════════════════════════════
WITH cg_data AS (
    SELECT m.asset_id,
           jsonb_agg(DISTINCT c.cat) as all_categories
    FROM core.asset_source_map m
    JOIN src_cg.coin_info ci ON ci.coin_id = m.source_asset_key
    CROSS JOIN LATERAL jsonb_array_elements_text(ci.categories) AS c(cat)
    WHERE m.source_code = 'cg'
      AND ci.categories IS NOT NULL
      AND jsonb_array_length(ci.categories) > 0
    GROUP BY m.asset_id
),
cg_hits AS (
    SELECT d.asset_id, map.sector, map.confidence
    FROM cg_data d
    CROSS JOIN LATERAL jsonb_array_elements_text(d.all_categories) AS c(cat)
    JOIN (VALUES
        -- L1/L2
        ('layer 1 (l1)', 'l1', 0.85), ('smart contract platform', 'l1', 0.7),
        ('layer 2 (l2)', 'l2', 0.85), ('zero-knowledge (zk)', 'l2', 0.6),
        ('zk rollups', 'l2', 0.8), ('rollups-as-a-service', 'l2', 0.7),
        ('chain', 'l1', 0.6),
        -- DeFi
        ('decentralized finance (defi)', 'defi', 0.9),
        ('decentralized exchange (dex)', 'defi', 0.8),
        ('dex aggregators', 'defi', 0.75), ('yield farming', 'defi', 0.75),
        ('lending', 'defi', 0.8), ('borrowing', 'defi', 0.8),
        ('liquid staking', 'defi', 0.8), ('liquid staking tokens', 'defi', 0.8),
        ('liquid restaking', 'defi', 0.8), ('restaking', 'defi', 0.75),
        ('yield aggregator', 'defi', 0.75), ('indexes', 'defi', 0.7),
        ('insurance', 'defi', 0.7), ('prediction market', 'defi', 0.7),
        ('prediction markets', 'defi', 0.7),
        ('launchpad', 'defi', 0.65), ('governance', 'defi', 0.5),
        ('synthetics', 'derivatives', 0.8),
        ('synthetic asset', 'derivatives', 0.75),
        ('asset management', 'defi', 0.6),
        ('liquid restaking tokens', 'defi', 0.8),
        ('yield-bearing tokens', 'defi', 0.6),
        -- 稳定币
        ('stablecoins', 'stablecoin', 0.9),
        ('algorithmic stablecoin', 'stablecoin', 0.85),
        ('algo-stables', 'stablecoin', 0.85),
        ('bridged stablecoin', 'stablecoin', 0.8),
        ('stablecoin issuer', 'stablecoin', 0.8),
        ('dual-token stablecoin', 'stablecoin', 0.8),
        ('partially algorithmic stablecoin', 'stablecoin', 0.8),
        ('stablecoin wrapper', 'stablecoin', 0.7),
        ('crypto-backed tokens', 'stablecoin', 0.6),
        ('yield-bearing stablecoin', 'stablecoin', 0.9),
        -- Meme
        ('meme', 'meme', 0.9), ('dog-themed', 'meme', 0.85),
        ('cat-themed', 'meme', 0.85),
        ('ai meme', 'meme', 0.7), ('parody meme', 'meme', 0.7),
        ('desci meme', 'meme', 0.6),
        -- GameFi
        ('gaming (gamefi)', 'gamefi', 0.9), ('play to earn', 'gamefi', 0.85),
        ('metaverse', 'gamefi', 0.8), ('nft', 'gamefi', 0.8),
        ('nft marketplace', 'gamefi', 0.75), ('fan token', 'gamefi', 0.7),
        ('move to earn', 'gamefi', 0.8), ('gamified mining', 'gamefi', 0.7),
        ('luck games', 'gamefi', 0.6), ('physical tcg', 'gamefi', 0.7),
        ('nftfi', 'gamefi', 0.65), ('nft lending', 'gamefi', 0.65),
        ('gambling (gamblefi)', 'gamefi', 0.75),
        ('entertainment', 'gamefi', 0.5),
        -- RWA
        ('real world assets (rwa)', 'rwa', 0.9), ('tokenized assets', 'rwa', 0.8),
        ('tokenized stock', 'rwa', 0.9),
        ('tokenized exchange-traded funds (etfs)', 'rwa', 0.85),
        ('tokenized exchange-traded product (etps)', 'rwa', 0.85),
        ('rwa lending', 'rwa', 0.8),
        ('tokenized btc', 'rwa', 0.7),
        -- AI
        ('artificial intelligence (ai)', 'ai', 0.9),
        ('ai agents', 'ai', 0.9), ('ai applications', 'ai', 0.8),
        ('decentralized ai', 'ai', 0.8),
        ('decentralized science (desci)', 'ai', 0.6),
        ('healthcare', 'ai', 0.5),
        ('analytics', 'ai', 0.5),
        -- 平台币
        ('exchange-based tokens', 'cex_token', 0.85),
        ('cex', 'cex_token', 0.8), ('cedefi', 'cex_token', 0.7),
        -- 衍生品
        ('derivatives', 'derivatives', 0.9), ('options', 'derivatives', 0.8),
        ('perpetual dex', 'derivatives', 0.85), ('options vault', 'derivatives', 0.75),
        ('interest rate derivatives', 'derivatives', 0.75),
        ('exotic options', 'derivatives', 0.7),
        -- DePIN
        ('depin', 'depin', 0.9),
        -- 基础设施
        ('infrastructure', 'infra', 0.85), ('oracle', 'infra', 0.75),
        ('bridge', 'infra', 0.7), ('canonical bridge', 'infra', 0.7),
        ('cross chain bridge', 'infra', 0.7), ('bridge aggregator', 'infra', 0.65),
        ('bridge aggregators', 'infra', 0.65), ('wallets', 'infra', 0.7),
        ('privacy', 'infra', 0.7), ('domains', 'infra', 0.7),
        ('name service', 'infra', 0.7), ('developer tools', 'infra', 0.7),
        ('mev', 'infra', 0.7), ('block builders', 'infra', 0.7),
        ('identity & reputation', 'infra', 0.65),
        ('data availability', 'infra', 0.8), ('interface', 'infra', 0.6),
        ('services', 'infra', 0.6), ('payments', 'infra', 0.65),
        ('payment solutions', 'infra', 0.65),
        ('decentralized identifier (did)', 'infra', 0.6),
        ('socialfi', 'infra', 0.55),
        ('dao', 'infra', 0.4),
        ('media', 'infra', 0.4),
        ('education', 'infra', 0.3),
        ('e-commerce', 'infra', 0.4),
        ('retail', 'infra', 0.3),
        ('charity', 'infra', 0.3),
        ('regenerative finance (refi)', 'defi', 0.5),
        ('telegram apps', 'infra', 0.4),
        ('trading bots', 'infra', 0.5),
        ('music', 'infra', 0.3),
        ('neobank', 'infra', 0.5),
        ('mobile mining', 'depin', 0.5),
        ('proof of work (pow)', 'l1', 0.4),
        ('multiplier denominated tokens', 'defi', 0.4),
        ('btcfi protocol', 'defi', 0.6),
        ('surge launchpad', 'defi', 0.5),
        ('discord bots', 'infra', 0.4)
    ) map(cat_name, sector, confidence) ON lower(c.cat) = lower(map.cat_name)
),
cg_best AS (
    SELECT asset_id, sector, MAX(confidence) as confidence
    FROM cg_hits
    GROUP BY asset_id, sector
)
INSERT INTO biz.asset_sector (asset_id, sector, source, confidence, is_primary)
SELECT asset_id, sector, 'cg', confidence, false
FROM cg_best;

-- ═══════════════════════════════════════════════════════════
-- 3. DL 来源：category 单值映射
--    合并同一资产的多个 DL 映射（去重）
-- ═══════════════════════════════════════════════════════════
WITH dl_data AS (
    SELECT m.asset_id,
           array_agg(DISTINCT pl.category) as all_categories
    FROM core.asset_source_map m
    JOIN src_dl.protocol_list pl ON pl.protocol_id = m.source_asset_key
    WHERE m.source_code = 'dl'
      AND pl.category IS NOT NULL
    GROUP BY m.asset_id
),
dl_hits AS (
    SELECT d.asset_id, map.sector, MAX(map.confidence) as confidence
    FROM dl_data d
    CROSS JOIN LATERAL unnest(d.all_categories) AS c(cat)
    JOIN (VALUES
        ('dexs', 'defi', 0.9), ('yield', 'defi', 0.85),
        ('lending', 'defi', 0.85), ('derivatives', 'derivatives', 0.9),
        ('liquid staking', 'defi', 0.8), ('farm', 'defi', 0.75),
        ('launchpad', 'defi', 0.65), ('cdp', 'defi', 0.8),
        ('yield aggregator', 'defi', 0.8), ('dex aggregator', 'defi', 0.75),
        ('indexes', 'defi', 0.7), ('prediction market', 'defi', 0.7),
        ('insurance', 'defi', 0.7), ('liquid restaking', 'defi', 0.8),
        ('restaking', 'defi', 0.75), ('reserve currency', 'defi', 0.7),
        ('leveraged farming', 'defi', 0.7), ('liquidity manager', 'defi', 0.7),
        ('liquidations', 'defi', 0.65), ('cdp manager', 'defi', 0.65),
        ('staking pool', 'defi', 0.7), ('staking rental', 'defi', 0.6),
        ('basis trading', 'defi', 0.7),
        ('options', 'derivatives', 0.8), ('synthetics', 'derivatives', 0.8),
        ('options vault', 'derivatives', 0.75),
        ('interest rate derivatives', 'derivatives', 0.7),
        ('exotic options', 'derivatives', 0.65),
        ('algo-stables', 'stablecoin', 0.85),
        ('stablecoin issuer', 'stablecoin', 0.8),
        ('dual-token stablecoin', 'stablecoin', 0.8),
        ('partially algorithmic stablecoin', 'stablecoin', 0.8),
        ('stablecoin wrapper', 'stablecoin', 0.7),
        ('rwa', 'rwa', 0.85), ('rwa lending', 'rwa', 0.8),
        ('gaming', 'gamefi', 0.8), ('nft marketplace', 'gamefi', 0.75),
        ('gamified mining', 'gamefi', 0.7), ('luck games', 'gamefi', 0.6),
        ('physical tcg', 'gamefi', 0.7), ('nftfi', 'gamefi', 0.65),
        ('nft lending', 'gamefi', 0.65), ('nft automated strategies', 'gamefi', 0.6),
        ('nft launchpad', 'gamefi', 0.6),
        ('ai agents', 'ai', 0.85), ('decentralized ai', 'ai', 0.8),
        ('cex', 'cex_token', 0.8), ('cedefi', 'cex_token', 0.7),
        ('depin', 'depin', 0.85),
        ('bridge', 'infra', 0.7), ('canonical bridge', 'infra', 0.7),
        ('cross chain bridge', 'infra', 0.7), ('bridge aggregator', 'infra', 0.65),
        ('bridge aggregators', 'infra', 0.65), ('oracle', 'infra', 0.75),
        ('wallets', 'infra', 0.7), ('privacy', 'infra', 0.7),
        ('domains', 'infra', 0.7), ('developer tools', 'infra', 0.7),
        ('mev', 'infra', 0.7), ('block builders', 'infra', 0.7),
        ('identity & reputation', 'infra', 0.65),
        ('interface', 'infra', 0.6), ('services', 'infra', 0.6),
        ('payments', 'infra', 0.65), ('chain', 'l1', 0.6),
        ('sofi', 'defi', 0.7), ('dao service provider', 'infra', 0.6),
        ('foundation', 'infra', 0.5), ('governance incentives', 'defi', 0.5),
        ('ponzi', 'meme', 0.5), ('meme', 'meme', 0.8),
        ('trading app', 'infra', 0.5),
        ('telegram bot', 'infra', 0.5),
        ('onchain capital allocator', 'defi', 0.6),
        ('yield lottery', 'defi', 0.5),
        ('dca tools', 'defi', 0.5),
        ('charity fundraising', 'infra', 0.3),
        ('crypto card issuer', 'infra', 0.5),
        ('token locker', 'infra', 0.5),
        ('coins tracker', 'infra', 0.4),
        ('uncollateralized lending', 'defi', 0.7),
        ('ve-incentive automator', 'defi', 0.5),
        ('risk curators', 'defi', 0.5)
    ) map(cat_name, sector, confidence) ON lower(c.cat) = lower(map.cat_name)
    GROUP BY d.asset_id, map.sector
)
INSERT INTO biz.asset_sector (asset_id, sector, source, confidence, is_primary)
SELECT asset_id, sector, 'dl', confidence, false
FROM dl_hits;

-- ═══════════════════════════════════════════════════════════
-- 4. 计算 is_primary（每个资产置信度最高的赛道）
--    置信度相同时，来源优先级: cmc > cg > dl
-- ═══════════════════════════════════════════════════════════
WITH ranked AS (
    SELECT asset_id, sector, source,
        ROW_NUMBER() OVER (
            PARTITION BY asset_id
            ORDER BY confidence DESC,
                CASE source
                    WHEN 'cmc' THEN 3
                    WHEN 'cg' THEN 2
                    WHEN 'dl' THEN 1
                    ELSE 0
                END DESC
        ) as rn
    FROM biz.asset_sector
)
UPDATE biz.asset_sector s
SET is_primary = (r.rn = 1),
    updated_at = NOW()
FROM ranked r
WHERE s.asset_id = r.asset_id
  AND s.sector = r.sector
  AND s.source = r.source;

-- ═══════════════════════════════════════════════════════════
-- 5. 更新 core.asset.primary_sector 冗余字段
-- ═══════════════════════════════════════════════════════════
WITH primary_sectors AS (
    SELECT DISTINCT ON (asset_id)
           asset_id, sector
    FROM biz.asset_sector
    WHERE is_primary = true
    ORDER BY asset_id, confidence DESC
)
UPDATE core.asset a
SET primary_sector = p.sector,
    updated_at = NOW()
FROM primary_sectors p
WHERE a.asset_id = p.asset_id;

-- ═══════════════════════════════════════════════════════════
-- 6. 统计结果
-- ═══════════════════════════════════════════════════════════
SELECT primary_sector AS sector,
       COUNT(*) AS cnt,
       ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER () * 100, 1) AS pct
FROM core.asset
GROUP BY primary_sector
ORDER BY cnt DESC;
