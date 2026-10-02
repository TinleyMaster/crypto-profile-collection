-- 资产对级联动因子表（截面联动挖掘 · 第 3 步落库）
--
-- 背景（审计 2026-10-02）：相关矩阵 + 领先-滞后事件研究证明约 53% 高联动对
-- （ρ≥0.6）存在方向性（A 异动后 B 次日显著跟涨）。本表落库**验证通过**的
-- 资产对，供下游消费：
--   1. catalyst_second_order：二阶受益从「板块静态映射」升级为「实证联动对」
--   2. 盘面联动告警：A 异动 → 预警同赛道 B 可能跟随
--
-- 数据来源：scan_lead_lag_validation.py --write
-- 口径：日收益扣 BTC 超额；lag1 显著 = 滞后1d 同向率 > 基线 +8pp 且 > 55%

CREATE TABLE IF NOT EXISTS biz.asset_linkage_factor (
    asset_id_a    bigint NOT NULL,
    asset_id_b    bigint NOT NULL,
    correlation   double precision,   -- 同期相关（扣 BTC 超额）
    lag1_rate     double precision,   -- A 异动后 B 次日同向率（0-1）
    lag2_rate     double precision,   -- A 异动后 B 隔日同向率（0-1）
    base_rate     double precision,   -- 基线：无条件 B 正收益比例（0-1）
    n_move        integer,            -- A 异动事件数（样本量）
    verified_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id_a, asset_id_b)
);

COMMENT ON TABLE biz.asset_linkage_factor IS
    '资产对级实证联动因子（日频，扣BTC超额；lag1显著=滞后1d同向率>基线+8pp且>55%）';
