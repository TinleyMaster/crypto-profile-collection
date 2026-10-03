-- fix_087: 爆仓极值事件新闻归因标注表（BTC/ETH 日频回测配套）
--
-- 背景（2026-10-03）：回测框架新增两个能力：
--   1. GDELT 2.0（免费全历史新闻）接入 —— 极值日自动查当日新闻量/情感；
--   2. 结合既有 catalyst 管线（biz.asset_catalyst）做自动化归因。
-- 本表是归因结果落点（对标 biz.scan_depth_log 的「事件时刻实测标注」定位）。
--
-- ⚠️ 用途限制：
--   - **仅供标定/回测消费**（backfill_liq_event_attribution.py 写入，分析侧读取）；
--     **禁止接入任何实时判定链**（日频粒度不匹配 5m 判定窗口）。
--   - GDELT 共享 IP 会持久 429（实测 2026-10-03）⇒ 全量回填需在**正常网络 IP**
--     执行；本机跑会落 status='gdelt_unavailable' 占位（缺失≠0，回填不中断）。
--
-- 幂等：CREATE TABLE IF NOT EXISTS，可重复执行。
-- 应用：python 05_代码与脚本/scripts/apply_migration.py fix_087_liq_event_attribution.sql

CREATE TABLE IF NOT EXISTS biz.liq_event_attribution (
    symbol        text          NOT NULL,   -- 合约码（BTCUSDT/ETHUSDT）
    event_date    date          NOT NULL,   -- 极值日（UTC，与回测同日）
    scope         text          NOT NULL DEFAULT 'binance',  -- 爆仓口径 binance/all
    bucket        text          NOT NULL,   -- EXT-LONG / EXT-SHORT / EXT-MIX
    liq_total     numeric(24,2),            -- 当日爆仓额（USD）
    liq_ratio     numeric(16,8),            -- 爆仓/当日成交额
    pct           numeric(8,4),             -- 近 90 日相对分位
    long_share    numeric(8,4),             -- 多单占比
    d1            numeric(12,6),            -- 当日价格变动
    fwd7          numeric(12,6),            -- 后续 7 日收益
    fwd14         numeric(12,6),            -- 后续 14 日收益
    -- GDELT 维度
    gdelt_matched integer,                  -- 当日匹配文章数（query=bitcoin/ethereum）
    gdelt_total   integer,                  -- 当日新闻总量（TotalArticles）
    gdelt_avg_tone numeric(12,4),           -- 当日匹配文章平均情感（-100~100）
    gdelt_tag     text,                     -- news_bullish/news_bearish/news_neutral/no_news/gdelt_unavailable
    gdelt_articles jsonb,                   -- top 文章快照 [{url,title,domain}]
    -- catalyst 管线维度
    catalyst_n    integer       NOT NULL DEFAULT 0,  -- biz.asset_catalyst 当日条数
    -- 综合归因（交互框架）：{bucket}:{news_tag}
    attribution   text,
    status        text          NOT NULL DEFAULT 'ok',  -- ok / gdelt_unavailable / error
    err           text,
    fetched_at    timestamptz   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, event_date, scope)
);
CREATE INDEX IF NOT EXISTS idx_liq_ev_attr_bucket
    ON biz.liq_event_attribution (bucket, event_date DESC);
CREATE INDEX IF NOT EXISTS idx_liq_ev_attr_status
    ON biz.liq_event_attribution (status, event_date DESC);

COMMENT ON TABLE biz.liq_event_attribution IS
  'BTC/ETH 爆仓极值日新闻归因标注表（GDELT + catalyst 管线）。'
  '⚠️ 仅供标定/回测消费，禁止接入实时判定；GDELT 需正常 IP 回填，429 落 gdelt_unavailable。';
COMMENT ON COLUMN biz.liq_event_attribution.gdelt_avg_tone IS
  'GDELT AvgTone（-100~100，>0 偏正面）。news_bullish 阈值 +1 / news_bearish 阈值 -1（含匹配=0 视为 no_news）。';
COMMENT ON COLUMN biz.liq_event_attribution.attribution IS
  '综合归因：{bucket}:{news_tag}，如 EXT-SHORT:news_bullish（轧空+利好→延续）';