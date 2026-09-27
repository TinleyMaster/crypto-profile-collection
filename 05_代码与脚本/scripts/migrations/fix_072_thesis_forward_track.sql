-- fix_072_thesis_forward_track.sql
-- P4（2026-09-27）：投研结论前向跟踪表。方案依据：
--   04_架构与代码方案/投研页三档确定性可落地方案_2026-09-27.md §1.3 / §5
--
-- 背景：原方案把「上线前用 backtest_opportunities.py 回测钉死三档闸门阈值」列为前置，
--   但该框架 (a) 最长持有期 30 天、快照全史仅 ~25 天，(b) 回测对象是 market_overview_snapshot
--   里的 opportunity，**不是** biz.research_thesis —— 投研页结论此前没有任何前向收益链路，
--   中/长期档阈值客观上无数据可校准。
--
-- 本表补上这条链路：结论生成日写一行（filled_at 为空），日级任务在 as_of+7/30/90 到期后
--   从 biz.asset_market_daily 取价回填前向收益；积累后可按 gate_s_open / gate_m_open 分组
--   比较 ret_t30_pct，验证闸门区分度（S 档需 ≥3 个月、M 档 ≥6 个月、L 档 ≥1 年才有样本）。
--   在此之前所有闸门阈值仍标注 uncalibrated（不可宣称已校准）。
--
-- 幂等：CREATE TABLE / INDEX IF NOT EXISTS，可重复执行。
-- 注意：DDL 后须立即 commit（项目教训：未及时 commit 会持 AccessExclusiveLock 阻塞表读）。

CREATE TABLE IF NOT EXISTS biz.thesis_forward_track (
    track_id        SERIAL PRIMARY KEY,
    thesis_id       INTEGER,            -- biz.research_thesis.thesis_id（0 = 降级结论）
    asset_id        INTEGER NOT NULL,
    as_of           DATE NOT NULL,      -- 结论生成日（北京时区）
    tier_s_score    NUMERIC(5,1),       -- 生成时刻短期档分数
    tier_m_score    NUMERIC(5,1),       -- 生成时刻中期档分数
    tier_l_evaluable BOOLEAN,           -- 长期档存在性门槛是否通过（lenient：False=不可评估）
    gate_s_open     BOOLEAN,            -- 短期档闸门是否开
    gate_m_open     BOOLEAN,            -- 中期档闸门是否开
    price_at        NUMERIC,            -- as_of 基准收盘价（建仓价）
    ret_t7_pct      NUMERIC,            -- T+7 前向收益（%）
    ret_t30_pct     NUMERIC,            -- T+30 前向收益（%）
    ret_t90_pct     NUMERIC,            -- T+90 前向收益（%）
    filled_at       TIMESTAMPTZ,        -- 三期全部回填完成时刻（NULL = 仍在跟踪）
    CONSTRAINT uq_thesis_forward UNIQUE (asset_id, as_of)
);

-- 到期候选扫描（partial index：只覆盖仍在跟踪的行，表长期增长后仍保持轻量）
CREATE INDEX IF NOT EXISTS idx_thesis_forward_due
    ON biz.thesis_forward_track (as_of) WHERE filled_at IS NULL;

COMMENT ON TABLE biz.thesis_forward_track
    IS '投研结论前向跟踪：结论生成日建仓基准 → T+7/30/90 收益回填，用于三档闸门区分度的后续校准（阈值当前 uncalibrated）';
COMMENT ON COLUMN biz.thesis_forward_track.price_at
    IS 'as_of 基准价：取 biz.asset_market_daily 中 market_date <= as_of 的最近收盘价（当日 ETL 未完成时回退前一日，避免基准价永久为空）';
COMMENT ON COLUMN biz.thesis_forward_track.filled_at
    IS 'T+7/30/90 三期全部回填后置位；NULL 表示仍在跟踪（到期候选由 idx_thesis_forward_due 扫描）';
COMMENT ON COLUMN biz.thesis_forward_track.tier_l_evaluable
    IS '长期档存在性硬门槛（审计/公开代码库/治理机制三项全为 1）是否通过；False → 长期档不可评估，不参与收益归因';