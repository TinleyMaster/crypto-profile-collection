-- fix_102: 单币解锁表现快照表（投研 SOP 新增指标 P1.7）
--
-- 背景：解锁的默认叙事是「抛压 → 跌」，但实测有币解锁照样暴涨。
--       本表把「同一件解锁事件、不同币反应完全相反」这一分化量化。
--
-- 口径（method_version=v1）：
--   锚点 = 解锁日 D（biz.asset_unlock_event.unlock_date，已发生且 unlock_value_usd>0）
--   解锁前抢跑  pre7    = close(D)/close(D-7) − 1
--   解锁后反应  post_k  = close(D+k)/close(D) − 1   （k = 1,3,7,14,30，日线来自 biz.asset_klines interval='1d'）
--   超额        excess_k = post_k − 当日全市场截面 post_k 中位（剔市场普涨 β）
--   事件去重    按 (symbol, unlock_date) 去重（同名多 asset_id 只算一次；同日多次取最大 unlock_value_usd）
--   分布        up_rate_7（post7>0 比例）/ pump_rate（excess7>+5%）/ dump_rate（excess7<−5%）
--   前瞻压力    并入 biz.asset_unlock_pressure（未来 7d/30d 解锁占比 + pressure_score）
--   综合        解锁反应分 0-100 + 评级（抗解禁型/偏抗解禁/中性/偏解禁即崩/解禁即崩/样本不足/无数据）
--
-- 幂等：CREATE TABLE IF NOT EXISTS；唯一键 (asset_id, as_of)

CREATE TABLE IF NOT EXISTS biz.asset_unlock_reaction (
    reaction_id        bigserial   PRIMARY KEY,
    asset_id           bigint      NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    as_of              date        NOT NULL,
    method_version     text        NOT NULL DEFAULT 'v1',

    -- ── 样本 ──
    n_events           integer,          -- 已发生且有价格可算的解锁事件数（去重后）
    n_seen             integer,          -- 已发生解锁事件总数（含无价格）
    event_from         date,             -- 最早可算事件
    event_to           date,             -- 最近可算事件
    next_unlock        date,             -- 未来最近一次解锁日（as_of 之后）

    -- ── 解锁前抢跑（D-7 → D） ──
    pre7_mean          numeric(12,4),
    pre7_median        numeric(12,4),
    pre7_excess_mean   numeric(12,4),

    -- ── 解锁后各窗口（D → D+k） ──
    post1_mean         numeric(12,4),
    post3_mean         numeric(12,4),
    post7_mean         numeric(12,4),
    post7_median       numeric(12,4),    -- ★ 解锁表现主指标（抗极端）
    post14_mean        numeric(12,4),
    post30_mean        numeric(12,4),
    excess1_mean       numeric(12,4),
    excess7_mean       numeric(12,4),    -- ★ 解锁超额主指标
    excess7_median     numeric(12,4),
    excess14_mean      numeric(12,4),
    excess30_mean      numeric(12,4),

    -- ── 分布 ──
    up_rate_7          numeric(6,4),     -- post7 > 0 比例
    pump_rate          numeric(6,4),     -- excess7 > +5% 比例（抗解禁）
    dump_rate          numeric(6,4),     -- excess7 < −5% 比例（解禁即崩）

    -- ── 前瞻压力（源自 biz.asset_unlock_pressure） ──
    unlock_pct_7d      numeric(12,6),
    unlock_pct_30d     numeric(12,6),
    pressure_score     numeric(8,2),
    pressure_level     text,

    -- ── 综合 ──
    reaction_grade     text,             -- 抗解禁型/偏抗解禁/中性/偏解禁即崩/解禁即崩/样本不足/无数据
    reaction_score     numeric(5,2),     -- 0-100
    by_horizon         jsonb,            -- 各窗口 mean/median/excess 明细

    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_asset_unlock_reaction UNIQUE (asset_id, as_of)
);

CREATE INDEX IF NOT EXISTS idx_asset_unlock_reaction_asof
    ON biz.asset_unlock_reaction (as_of DESC);

CREATE INDEX IF NOT EXISTS idx_asset_unlock_reaction_grade
    ON biz.asset_unlock_reaction (reaction_grade);

COMMENT ON TABLE biz.asset_unlock_reaction IS
  '单币解锁表现快照（投研 SOP P1.7）。锚定解锁日 D，算解锁前抢跑 pre7 + 解锁后 post1/3/7/14/30 及相对当日全市场截面中位超额；评级抗解禁型↔解禁即崩。幂等键 asset_id+as_of。';
COMMENT ON COLUMN biz.asset_unlock_reaction.excess7_mean IS '解锁表现主指标：解锁后7天相对当日全市场截面中位的平均超额(%)';
COMMENT ON COLUMN biz.asset_unlock_reaction.pump_rate IS '抗解禁率：excess7>+5% 的事件占比';
COMMENT ON COLUMN biz.asset_unlock_reaction.dump_rate IS '解禁即崩率：excess7<−5% 的事件占比';
COMMENT ON COLUMN biz.asset_unlock_reaction.next_unlock IS '未来最近一次解锁日（as_of 之后），配合历史反应倾向判断下一个解锁日风险';
COMMENT ON COLUMN biz.asset_unlock_reaction.reaction_grade IS '解锁评级：抗解禁型/偏抗解禁/中性/偏解禁即崩/解禁即崩/样本不足/无数据';
COMMENT ON COLUMN biz.asset_unlock_reaction.method_version IS '口径版本（v1=2026-10-16 首版）';
