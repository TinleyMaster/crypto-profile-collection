-- FEAT-SECTOR-003: 板块资金流日频快照表
-- 把叙事榜 + 链榜的计算结果日频固化，支持历史回溯 + 快速读取

CREATE TABLE IF NOT EXISTS biz.sector_flow_daily (
    sector_type     VARCHAR(20)  NOT NULL,    -- 'narrative' | 'chain'
    sector_key      VARCHAR(100) NOT NULL,    -- 叙事名 / 链名
    sector_label    VARCHAR(100),             -- 展示名（CMC 分类名 / 链全名）
    metric_date     DATE         NOT NULL,

    -- 市值腿
    market_cap      NUMERIC,                  -- 总市值（USD）
    mcap_change_1d_pct  NUMERIC,              -- 24h 市值变化%
    mcap_change_7d_pct  NUMERIC,              -- 7d 市值变化%
    mcap_change_30d_pct NUMERIC,              -- 30d 市值变化%
    coin_count      INTEGER,                  -- 成分币数量
    mcap_period     VARCHAR(20),              -- 数据来源: '7d' | '24h_fallback' | 'db_only'

    -- TVL 腿
    tvl             NUMERIC,                  -- 总 TVL（USD）
    tvl_change_1d_pct   NUMERIC,              -- 24h TVL 变化%
    tvl_change_7d_pct   NUMERIC,              -- 7d TVL 变化%
    tvl_change_30d_pct  NUMERIC,              -- 30d TVL 变化%
    protocol_count  INTEGER,                  -- 协议数量

    -- 合成
    flow_7d_usd     NUMERIC,                  -- 7d 净流入估算（USD）
    flow_7d_pct     NUMERIC,                  -- 7d 净流入%
    composite_score NUMERIC,                  -- 合成得分
    mode            VARCHAR(20),              -- 'blended' | 'mcap_only'

    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (sector_type, sector_key, metric_date)
);

-- 索引：按日期查所有板块
CREATE INDEX IF NOT EXISTS idx_sector_flow_daily_date
    ON biz.sector_flow_daily (metric_date);

-- 索引：按板块类型 + 日期查排行
CREATE INDEX IF NOT EXISTS idx_sector_flow_daily_type_date
    ON biz.sector_flow_daily (sector_type, metric_date DESC);

COMMENT ON TABLE biz.sector_flow_daily IS '板块资金流日频快照（叙事+链）';
COMMENT ON COLUMN biz.sector_flow_daily.sector_type IS '板块类型: narrative=叙事, chain=公链';
COMMENT ON COLUMN biz.sector_flow_daily.sector_key IS '板块唯一键（叙事名或链名）';
