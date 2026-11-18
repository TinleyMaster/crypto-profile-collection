-- =====================================================================
-- β 因子落库：每个有 1h K 线的资产相对于 BTC 的 β
-- 三种口径：全周期比值 β / 牛市比值 β / 回归 β
-- 编号：fix_112
-- 日期：2026-10-20
-- =====================================================================

CREATE TABLE IF NOT EXISTS core.asset_beta (
    beta_id         BIGSERIAL PRIMARY KEY,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    symbol          TEXT NOT NULL,

    -- 三种 β 口径（全部用 7d 滚动收益率，entry → +7d）
    beta_full       NUMERIC(12,4),    -- mean(r_a/r_b)，|r_b| > 0.1% 过滤极端分母
    beta_med_full   NUMERIC(12,4),    -- 全周期比值中位数（过滤均值被极端样本拉爆）
    beta_bull       NUMERIC(12,4),    -- mean(r_a/r_b) WHERE r_b > 1%（牛市放大倍数）
    beta_med_bull   NUMERIC(12,4),    -- 牛市比值中位数
    beta_regression NUMERIC(12,4),    -- Cov(r_a, r_b) / Var(r_b)（线性回归斜率）

    -- 质量指标
    n_samples       INTEGER NOT NULL, -- 7d 滚动样本数（≥50 才可信）
    n_bull_samples  INTEGER,          -- 牛市样本数（r_b > 1%）
    mean_excess     NUMERIC(10,4),    -- 全周期平均超额（资产 7d 收益 - BTC 7d 收益）
    med_excess      NUMERIC(10,4),    -- 全周期超额中位数
    bull_excess     NUMERIC(10,4),    -- 牛市平均超额
    bear_excess     NUMERIC(10,4),    -- 熊市平均超额（r_b < -1%）

    -- 元信息
    beta_bucket     TEXT,             -- β<1 / 1≤β<1.5 / 1.5≤β<2.5 / β≥2.5
    kline_min       TIMESTAMPTZ,      -- K 线时间范围起点
    kline_max       TIMESTAMPTZ,      -- K 线时间范围终点

    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (asset_id)
);

COMMENT ON TABLE core.asset_beta
    IS '每个有 1h K 线资产的 β 因子（vs BTC，7d 滚动收益）';
COMMENT ON COLUMN core.asset_beta.beta_full
    IS '全周期 β = mean(资产7d收益 / BTC7d收益), |r_b|>0.1% 过滤极端分母';
COMMENT ON COLUMN core.asset_beta.beta_bull
    IS '牛市 β = mean(资产7d收益 / BTC7d收益) WHERE BTC7d涨 > 1%';
COMMENT ON COLUMN core.asset_beta.beta_regression
    IS '回归 β = Cov(r_a, r_b)/Var(r_b)，线性拟合斜率';
COMMENT ON COLUMN core.asset_beta.beta_med_full
    IS 'β 中位数，过滤极端样本对均值的污染';
COMMENT ON COLUMN core.asset_beta.beta_bucket
    IS '按 beta_full 分桶：β<1 跑输 / 1~1.5 轻度 / 1.5~2.5 高 / ≥2.5 极高';

CREATE INDEX IF NOT EXISTS idx_asset_beta_bucket ON core.asset_beta (beta_bucket);
CREATE INDEX IF NOT EXISTS idx_asset_beta_full ON core.asset_beta (beta_full DESC);
CREATE INDEX IF NOT EXISTS idx_asset_beta_bull ON core.asset_beta (beta_bull DESC);
