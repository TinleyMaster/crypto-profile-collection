-- W-13：机会清单落表（P2 效果追踪前置）
-- 只新增一张表，不改任何既有表。
-- 背景：机会清单此前只存在于 biz.market_overview_snapshot.payload->'opportunity_list'
-- 的 JSON 里，无独立持久化 →「W-03 改了排序后指导意义是否真的提升」没有任何数据能回答。
-- 本表把每日机会清单落成可回填、可分组统计的行，供 backfill_opportunity_outcome.py
-- 按 calibration_gate 分组做 T+1 / T+7 前向收益验证。
CREATE TABLE IF NOT EXISTS biz.opportunity_snapshot (
    snapshot_date    DATE        NOT NULL,
    target           TEXT        NOT NULL,
    signal_type      TEXT        NOT NULL DEFAULT '',  -- 主键成员，NULL 归一为 ''（PK 不允许 NULL）
    direction        TEXT,
    conviction_score NUMERIC,
    conviction_tier  TEXT,
    calibration_gate TEXT,                              -- 校准门（calibrated_ok / calibrated_low / preliminary / exempt_*）
    sample_count     INTEGER,
    hit_rate         NUMERIC,
    ref_price        NUMERIC,                           -- 建仓基准价（快照日「资产最近可得收盘价」，取不到为 NULL）
    asset_id         BIGINT,                            -- 回填前向收益用（payload 内自带；多标的/无 asset_id 的行为 NULL）
    outcome_1d       NUMERIC,                           -- T+1 前向收益 %（回填脚本写，未到期/无基准价为空）
    outcome_7d       NUMERIC,                           -- T+7 前向收益 %（同上）
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_date, target, signal_type)
);

-- 回填扫描用：只覆盖尚未回填 T+7 的行（部分索引，避免全表扫）
CREATE INDEX IF NOT EXISTS idx_opportunity_snapshot_pending
    ON biz.opportunity_snapshot (snapshot_date)
    WHERE outcome_7d IS NULL;

COMMENT ON TABLE  biz.opportunity_snapshot                IS '每日机会清单快照，供 T+1/T+7 前向收益追踪与校准分组统计（W-13）';
COMMENT ON COLUMN biz.opportunity_snapshot.ref_price      IS '快照日资产最近可得收盘价（asset_market_daily，cmc 优先）；取不到为 NULL，不回填假数';
COMMENT ON COLUMN biz.opportunity_snapshot.calibration_gate IS '该机会信号类型的回测校准门，取自 payload 的 calibration_status.gate';
COMMENT ON COLUMN biz.opportunity_snapshot.outcome_1d     IS 'T+1 前向收益 %（相对 ref_price），由 backfill_opportunity_outcome.py 幂等回填';
COMMENT ON COLUMN biz.opportunity_snapshot.outcome_7d     IS 'T+7 前向收益 %（相对 ref_price），由 backfill_opportunity_outcome.py 幂等回填';