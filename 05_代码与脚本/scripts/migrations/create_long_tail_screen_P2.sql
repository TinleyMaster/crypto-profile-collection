-- P2-①：长尾轻量初筛结果表
-- 用途：全市场 7000+ 资产的 holder/social/momentum 三轴低保真评分
-- 不污染 daily_recommendation（后者按 symbol 存、承载 conviction HIGH/MED 语义）
-- 执行方式：psql $DATABASE_URL -f 05_代码与脚本/scripts/migrations/create_long_tail_screen_P2.sql

CREATE TABLE IF NOT EXISTS biz.long_tail_screen (
    screen_id SERIAL PRIMARY KEY,
    asset_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    chain TEXT,
    screen_date DATE NOT NULL DEFAULT CURRENT_DATE,
    holder_score NUMERIC(5,2) DEFAULT 0,       -- 集中度+增减+鲸鱼 三因子
    social_score NUMERIC(5,2) DEFAULT 0,       -- 社交热度+DEX热搜
    momentum_score NUMERIC(5,2) DEFAULT 0,     -- 24h 动量归一
    composite_lowfi NUMERIC(5,2) DEFAULT 0,    -- 三轴加权合成
    mvrv_bonus NUMERIC(5,2) DEFAULT 0,         -- MVRV 精筛 bonus（仅 15 币）
    signals_json JSONB,                         -- 各轴明细信号
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_id, screen_date)
);

CREATE INDEX IF NOT EXISTS idx_long_tail_screen_date ON biz.long_tail_screen(screen_date DESC);
CREATE INDEX IF NOT EXISTS idx_long_tail_screen_composite ON biz.long_tail_screen(composite_lowfi DESC);
CREATE INDEX IF NOT EXISTS idx_long_tail_screen_asset ON biz.long_tail_screen(asset_id);

-- 验证
-- SELECT COUNT(*) FROM biz.long_tail_screen;
-- SELECT symbol, composite_lowfi FROM biz.long_tail_screen ORDER BY composite_lowfi DESC LIMIT 10;
