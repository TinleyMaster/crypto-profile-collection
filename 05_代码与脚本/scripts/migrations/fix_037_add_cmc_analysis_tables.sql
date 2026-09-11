-- fix_037_add_cmc_analysis_tables.sql
-- 新增 CMC 分析接口的持久化表：
--   biz.global_metric_daily      全球市场指标（总市值/总成交/主导率/稳定币市值/币种数）
--   biz.fear_greed_daily         恐贪指数
--   biz.altcoin_season_daily     山寨季指数
--   biz.asset_trending           趋势榜（gainers / losers / trending / most_visited / new）
--   biz.airdrop_event            空投事件
--   src_cmc.cmc_asset_ohlcv      历史 OHLCV（K线）
--   src_cmc.cmc_asset_perf_stats 价格表现统计（ATH/ATL / 多周期涨跌幅）
--   src_cmc.cmc_asset_perf_daily 价格表现日快照（周期快照，便于按日回看）

-- ============================================================
-- 1. 全球市场指标（/v1/global-metrics/quotes/latest）
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.global_metric_daily (
    metric_date        DATE NOT NULL,
    total_market_cap   NUMERIC(38, 2),
    total_volume_24h   NUMERIC(38, 2),
    btc_dominance      NUMERIC(18, 8),
    eth_dominance      NUMERIC(18, 8),
    stablecoin_market_cap NUMERIC(38, 2),
    total_cryptocurrencies BIGINT,
    active_cryptocurrencies BIGINT,
    raw_ref            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (metric_date)
);

CREATE INDEX IF NOT EXISTS idx_global_metric_daily_date
    ON biz.global_metric_daily (metric_date DESC);

-- ============================================================
-- 2. 恐贪指数（/v3/fear-and-greed）
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.fear_greed_daily (
    metric_date        DATE NOT NULL,
    value              NUMERIC(8, 2),
    value_classification VARCHAR(64),
    raw_ref            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (metric_date)
);

CREATE INDEX IF NOT EXISTS idx_fear_greed_daily_date
    ON biz.fear_greed_daily (metric_date DESC);

-- ============================================================
-- 3. 山寨季指数（/v1/altcoin-season-index）
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.altcoin_season_daily (
    metric_date        DATE NOT NULL,
    value              NUMERIC(8, 2),
    raw_ref            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (metric_date)
);

CREATE INDEX IF NOT EXISTS idx_altcoin_season_daily_date
    ON biz.altcoin_season_daily (metric_date DESC);

-- ============================================================
-- 4. 趋势榜（/v1/cryptocurrency/trending/* 和 /v1/cryptocurrency/listings/new）
--    trend_type: gainers / losers / trending / most_visited / new
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.asset_trending (
    snapshot_date      DATE NOT NULL,
    trend_type         VARCHAR(32) NOT NULL,
    time_period        VARCHAR(16) NOT NULL DEFAULT '24h',
    cmc_id             BIGINT NOT NULL,
    symbol             VARCHAR(128),
    name               VARCHAR(256),
    slug               VARCHAR(256),
    rank_num           INTEGER,
    price_usd          NUMERIC(38, 18),
    market_cap         NUMERIC(38, 2),
    volume_24h         NUMERIC(38, 2),
    percent_change_24h NUMERIC(18, 8),
    raw_ref            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_date, trend_type, time_period, cmc_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_trending_type_date
    ON biz.asset_trending (trend_type, snapshot_date DESC, rank_num);

-- ============================================================
-- 5. 空投事件（/v1/cryptocurrency/airdrops + /v1/cryptocurrency/airdrop）
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.airdrop_event (
    airdrop_id         VARCHAR(128) PRIMARY KEY,
    project_name       VARCHAR(256),
    description        TEXT,
    status             VARCHAR(32),
    coin_id            BIGINT,
    coin_symbol        VARCHAR(128),
    coin_name          VARCHAR(256),
    coin_slug          VARCHAR(256),
    start_date         TIMESTAMPTZ,
    end_date           TIMESTAMPTZ,
    total_prize        NUMERIC(38, 2),
    winner_count       BIGINT,
    link               TEXT,
    raw_ref            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_airdrop_event_status
    ON biz.airdrop_event (status, end_date DESC);
CREATE INDEX IF NOT EXISTS idx_airdrop_event_coin
    ON biz.airdrop_event (coin_id);

-- ============================================================
-- 6. 历史 OHLCV（/v2/cryptocurrency/ohlcv/historical）
-- ============================================================
CREATE TABLE IF NOT EXISTS src_cmc.cmc_asset_ohlcv (
    cmc_id             BIGINT NOT NULL REFERENCES src_cmc.cmc_asset_map(cmc_id) ON DELETE CASCADE,
    time_open          TIMESTAMPTZ NOT NULL,
    time_period        VARCHAR(16) NOT NULL DEFAULT 'daily',
    open               NUMERIC(38, 18),
    high               NUMERIC(38, 18),
    low                NUMERIC(38, 18),
    close              NUMERIC(38, 18),
    volume             NUMERIC(38, 2),
    market_cap         NUMERIC(38, 2),
    raw_response_id    BIGINT REFERENCES raw.api_response(response_id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cmc_id, time_open, time_period)
);

CREATE INDEX IF NOT EXISTS idx_cmc_asset_ohlcv_time
    ON src_cmc.cmc_asset_ohlcv (time_open DESC);

-- ============================================================
-- 7. 价格表现统计（/v2/cryptocurrency/price-performance-stats/latest）
--    单次快照：all_time + 各滚动周期，按 (cmc_id, snapshot_time, time_period) 存储
-- ============================================================
CREATE TABLE IF NOT EXISTS src_cmc.cmc_asset_perf_stats (
    cmc_id             BIGINT NOT NULL REFERENCES src_cmc.cmc_asset_map(cmc_id) ON DELETE CASCADE,
    snapshot_time      TIMESTAMPTZ NOT NULL,
    time_period        VARCHAR(32) NOT NULL DEFAULT 'all_time',
    open               NUMERIC(38, 18),
    high               NUMERIC(38, 18),
    low                NUMERIC(38, 18),
    close              NUMERIC(38, 18),
    percent_change     NUMERIC(18, 8),
    price_change       NUMERIC(38, 18),
    open_timestamp     TIMESTAMPTZ,
    high_timestamp     TIMESTAMPTZ,
    low_timestamp      TIMESTAMPTZ,
    close_timestamp    TIMESTAMPTZ,
    raw_response_id    BIGINT REFERENCES raw.api_response(response_id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cmc_id, snapshot_time, time_period)
);

CREATE INDEX IF NOT EXISTS idx_cmc_asset_perf_stats_time
    ON src_cmc.cmc_asset_perf_stats (snapshot_time DESC);

-- ============================================================
-- 8. 价格表现日快照（汇总表：把每个币的 all_time ATH/ATL 抽出成按日一行，便于分析回撤）
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.asset_perf_daily (
    asset_id           BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    perf_date          DATE NOT NULL,
    source_code        VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    ath_price          NUMERIC(38, 18),
    ath_timestamp      TIMESTAMPTZ,
    atl_price          NUMERIC(38, 18),
    atl_timestamp      TIMESTAMPTZ,
    drawdown_from_ath  NUMERIC(18, 8),
    raw_ref            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, perf_date, source_code)
);

CREATE INDEX IF NOT EXISTS idx_asset_perf_daily_date
    ON biz.asset_perf_daily (perf_date DESC);