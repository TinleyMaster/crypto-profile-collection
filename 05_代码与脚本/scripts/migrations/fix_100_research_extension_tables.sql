-- fix_100_research_extension_tables.sql
-- P4（2026-10-15）：投研维度新表，承接此前"无法落库"的数据。方案依据：
--   用户明确要求 CHIP 重写报告后缺失的结构化数据必须落库（此前散落在 report_md 里）
--   并作为后续 HYPE/LIT/ONDO 等 RWA/DePIN/DeFi 协议投研的通用扩展表。
--
--   新增 4 张表，覆盖 4 类此前没有对应 core 表的数据：
--
--   1. biz.protocol_revenue        — 协议级收入/费用/APR 指标（period + metric_name 键值对模式）
--                                    与 biz.asset_raises（融资）、biz.asset_factor_daily（因子）不重叠
--   2. biz.protocol_tvl            — 协议级 TVL（DefiLlama protocol 维度，区别于 coin_tvl_timeseries 的币级 TVL）
--                                    slug 直接记 DefiLlama 英文 slug（如 usd-ai），便于反查元数据
--   3. biz.competitor_benchmark    — 竞品对标数据（某 asset_id vs 一组竞品 symbol 的某指标横向对比）
--                                    多资产共享同一份对标结论（同一 measured_at 下比完一组）
--   4. biz.token_value_capture     — 代币价值捕获机制（staking 状态/回购/治理权/收益分配路径等）
--                                    结构化枚举（status = active/inactive/planned），方便后续筛选
--
--   所有表：asset_id BIGINT REFERENCES core.asset(asset_id) ON DELETE CASCADE
--   所有表：created_at TIMESTAMPTZ DEFAULT NOW()
--   幂等：CREATE TABLE IF NOT EXISTS，可重复执行
--
-- 教训：fix_024 曾因为未及时 COMMIT 持 AccessExclusiveLock 阻塞表读，
--        本 migration 用独立 autocommit 执行，DDL 立即 commit。

BEGIN;

-- ============================================================
-- 1. biz.protocol_revenue
--    协议级收入/费用/APR 等周期性指标。
--    一个资产同一 period_type + period_start + metric_name 唯一定位一行。
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.protocol_revenue (
    revenue_id      SERIAL PRIMARY KEY,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    period_type     VARCHAR(16) NOT NULL,     -- arr / quarterly / monthly / weekly / trailing_30d / trailing_365d / wtd
    period_start    DATE,                     -- 周期起始日（ARR/Trailing 口径下可为空）
    metric_name     VARCHAR(64) NOT NULL,     -- protocol_arr / dao_arr / fee_30d / fee_365d / avg_apr / loan_count / loan_amount
    metric_value    NUMERIC,                  -- 允许 NULL：ARR/fee 等不同资产缺失某些指标是常态
    metric_unit     VARCHAR(8),               -- usd / pct / count
    source          VARCHAR(32) NOT NULL,     -- lighthouse / may_recap / usd.ai / defillama / messari / official
    source_url      TEXT,
    measured_at     DATE NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_protocol_revenue UNIQUE (asset_id, period_type, period_start, metric_name, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_protocol_revenue_asset
    ON biz.protocol_revenue (asset_id);
CREATE INDEX IF NOT EXISTS idx_protocol_revenue_metric
    ON biz.protocol_revenue (metric_name);

COMMENT ON TABLE biz.protocol_revenue
    IS '协议级收入/费用/APR 等周期性指标（period + metric_name 键值对模式），与 biz.asset_raises（融资）区分';
COMMENT ON COLUMN biz.protocol_revenue.period_type
    IS 'ARR = Annual Run Rate（年化）；trailing_30d/365d = 回看窗口；wtd = week-to-date';
COMMENT ON COLUMN biz.protocol_revenue.metric_name
    IS 'protocol_arr（协议年化收入）/ dao_arr（DAO 年化收入）/ fee_30d / fee_365d / avg_apr / loan_count / loan_amount / origination_fee_pct';

-- ============================================================
-- 2. biz.protocol_tvl
--    协议级 TVL（DefiLlama protocol 维度）。
--    区别于 coin_tvl_timeseries（币级 TVL）：这里记录 DefiLlama slug 下的 protocol 总锁仓。
--    同一 asset_id 同一 measured_at 唯一。
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.protocol_tvl (
    tvl_id          SERIAL PRIMARY KEY,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    protocol_slug   VARCHAR(64),              -- DefiLlama 英文 slug（如 usd-ai / lido / aave）
    tvl_usd         NUMERIC NOT NULL,
    chains          TEXT[],                   -- 协议部署链（Ethereum / Arbitrum / Solana ...）
    protocol_category VARCHAR(32),            -- RWA / DePIN / Lending / DEX / Yield ...
    source          VARCHAR(32) NOT NULL,     -- defillama / official / llama_community
    measured_at     DATE NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_protocol_tvl UNIQUE (asset_id, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_protocol_tvl_asset
    ON biz.protocol_tvl (asset_id);
CREATE INDEX IF NOT EXISTS idx_protocol_tvl_slug
    ON biz.protocol_tvl (protocol_slug);

COMMENT ON TABLE biz.protocol_tvl
    IS '协议级 TVL（DefiLlama protocol 维度），与 coin_tvl_timeseries（币级 TVL）区分';
COMMENT ON COLUMN biz.protocol_tvl.protocol_slug
    IS 'DefiLlama 英文 slug；中文 slug 和内部 ID 不要用（已踩过坑：6190 是内部 ID，usd-ai 才是英文 slug）';

-- ============================================================
-- 3. biz.competitor_benchmark
--    竞品对标数据。同一 measured_at 下比完一组竞品，每个 metric_name 横向展开多行。
--    asset_id 是"被研究币"，competitor_symbol 是"对标竞品"。
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.competitor_benchmark (
    benchmark_id    SERIAL PRIMARY KEY,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    competitor_symbol VARCHAR(16) NOT NULL,   -- CFG / ONDO / PENDLE / CPOOL / SYRUP ...
    competitor_name VARCHAR(64),
    competitor_sector VARCHAR(32),             -- RWA / Lending / Yield / DEX
    metric_name     VARCHAR(64) NOT NULL,     -- tvl_mcap_ratio / fdv_tvl_ratio / pe_ratio / mcap / tvl_usd
    metric_value    NUMERIC,                  -- 允许 NULL：竞品对标某些指标缺失（如 SYRUP 无 FDV/tvl）很常见
    metric_unit     VARCHAR(8),               -- x / usd / pct / ratio
    source          VARCHAR(32) NOT NULL,     -- defillama / coingecko / official
    measured_at     DATE NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_competitor_bench UNIQUE (asset_id, competitor_symbol, metric_name, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_competitor_bench_asset
    ON biz.competitor_benchmark (asset_id);
CREATE INDEX IF NOT EXISTS idx_competitor_bench_competitor
    ON biz.competitor_benchmark (competitor_symbol);

COMMENT ON TABLE biz.competitor_benchmark
    IS '竞品对标数据：某 asset_id 与一组 competitor_symbol 在某 metric_name 下横向对比';
COMMENT ON COLUMN biz.competitor_benchmark.metric_name
    IS 'tvl_mcap_ratio（最便宜的常用指标）/ fdv_tvl_ratio / pe_ratio（Protocol ARR 口径）/ mcap / tvl_usd';

-- ============================================================
-- 4. biz.token_value_capture
--    代币价值捕获机制：staking 状态、回购规模、治理权、收益分配路径、sCHIP 等。
--    用 mechanism + status 枚举，方便后续筛"staking 已激活的 RWA 币"。
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.token_value_capture (
    capture_id      SERIAL PRIMARY KEY,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    mechanism       VARCHAR(64) NOT NULL,     -- staking / buyback / governance / first_loss_reserve / yield_reserve / hurdle_rate / fee_share / tokenburn
    status          VARCHAR(16) NOT NULL,     -- active / inactive / planned / partial
    value_text      TEXT,                     -- 机制的关键参数（如 "1 CHIP = 1 vote, no ve" / "3.71% of 10B = 370M CHIP"）
    value_numeric   NUMERIC,                  -- 可量化的部分（如 7.0 for hurdle_rate / 370000000 for buyback_amount）
    value_unit      VARCHAR(16),              -- pct / count / usd / ratio / native_token
    source          VARCHAR(32) NOT NULL,     -- docs.usd.ai / may_recap / official_messari / defillama
    source_url      TEXT,
    measured_at     DATE NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_token_capture UNIQUE (asset_id, mechanism, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_token_capture_asset
    ON biz.token_value_capture (asset_id);
CREATE INDEX IF NOT EXISTS idx_token_capture_status
    ON biz.token_value_capture (status);

COMMENT ON TABLE biz.token_value_capture
    IS '代币价值捕获机制（结构化枚举 staking/buyback/governance 等 + status），补 biz.asset_tokenomics 没有的定性维度';
COMMENT ON COLUMN biz.token_value_capture.status
    IS 'active = 已激活并在运行；inactive = 定义了但未上线（如 CHIP staking docs 标注 "currently not in use"）；planned = 路线图上有';

COMMIT;
