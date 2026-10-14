-- fix_099_research_report_archive.sql
-- 投研报告归档表（结构化结论 + 原文全文）。
--
-- 背景：加密投研助手（CLI/Skill 流程）此前只把报告落盘为 Markdown + 邮件发送，
--   数据库里没有任何留痕，导致：
--     (a) 报告无法跨标的检索、无法被其他系统（邮件/看板/告警）消费；
--     (b) 结论无法进入前向收益回测（biz.thesis_forward_track 需要一条按资产/日期的建仓行）；
--     (c) 同一标的多次投研无法 diff（催化剂、解锁、评分趋势变化）。
--
-- 设计取舍：
--   - 不复用 biz.research_thesis —— 该表与 NotebookLM 对话流深度耦合
--     （source_notebook_id + research_notebook 的 UNIQUE(asset_id) + research_message
--     citations），CLI 报告没有 notebook/message，硬塞会污染既有消费端（投研页列表计数）。
--   - 本表独立承载「结构化结论列 + 原文全文」，一次写入同时满足归档与检索。
--   - 前向跟踪仍复用既有 biz.thesis_forward_track（见 fix_072）——只写一条建仓行，
--     由既有日级任务回填 T+7/30/90 收益，不新增任务。
--
-- 幂等：CREATE TABLE / INDEX IF NOT EXISTS，可重复执行。
-- 注意：DDL 后须立即 commit（项目教训：未及时 commit 会持 AccessExclusiveLock 阻塞表读）。

CREATE TABLE IF NOT EXISTS biz.research_report (
    report_id         SERIAL PRIMARY KEY,
    asset_id          INTEGER NOT NULL,
    symbol            TEXT NOT NULL,
    name              TEXT,
    title             TEXT,
    generated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    engine            TEXT,                          -- 产出引擎（如：加密投研助手 SOP）
    stance            TEXT,                          -- bullish / bearish / neutral
    score             NUMERIC(4,1),                  -- 综合评分 0-10
    confidence        NUMERIC(4,3),                  -- 置信度 0-1
    summary           TEXT,                          -- 核心判断（一句话）
    thesis_json       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 核心论点（带证据链）
    drivers_up_json   JSONB,                         -- 上行驱动因素（报告第 9 节）
    drivers_down_json JSONB,                         -- 下行风险因素（报告第 9 节）
    tracking_json     JSONB,                         -- 后续跟踪清单 + 节奏（报告第 9 节）
    key_metrics_json  JSONB,                         -- 关键指标快照
    risks_json        JSONB,                         -- 关键风险
    catalysts_json    JSONB,                         -- 催化剂
    dimensions_json   JSONB,                         -- 分维度评分
    sources_json      JSONB,                         -- 来源清单（带 URL）
    report_path       TEXT,                          -- 磁盘 Markdown 路径（人类可读交付物）
    report_md         TEXT,                          -- 报告全文（归档主体）
    report_sha256     TEXT,                          -- 全文指纹（幂等键）
    subject           TEXT,                          -- 邮件主题
    email_status      TEXT,                          -- sent / failed / skipped
    email_error       TEXT,
    meta_json         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 数据清单摘要等附属元数据
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_research_report_asset
        FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    CONSTRAINT uq_research_report_sha UNIQUE (asset_id, report_sha256)
);

-- 按资产取最新报告的主查询路径
CREATE INDEX IF NOT EXISTS idx_research_report_asset
    ON biz.research_report (asset_id, generated_at DESC);

COMMENT ON TABLE biz.research_report IS
    '单币投研报告归档：结构化结论列（stance/score/thesis/drivers/tracking/...）+ 原文全文。幂等键 (asset_id, report_sha256)。前向收益见 biz.thesis_forward_track。';
COMMENT ON COLUMN biz.research_report.report_sha256 IS
    '报告全文 sha256；同一资产同内容重跑 upsert 更新邮件状态，不重复建行。';
COMMENT ON COLUMN biz.research_report.tracking_json IS
    '报告第 9 节「后续跟踪清单」：每条含 跟踪指标/在哪看/触发阈值/动作建议。';
