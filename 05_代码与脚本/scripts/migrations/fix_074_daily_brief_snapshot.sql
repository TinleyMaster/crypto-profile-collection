-- W-06：落库早报 brief（P2 全系前置：变更日志 / 事后复盘）
-- 只新增一张表，不改任何既有表。
-- 背景：全库此前无任何表存早报 brief / AI 结论；build_daily_brief 只 save_snapshot
-- （存 overview），已发出的邮件永远无法事后复盘。
CREATE TABLE IF NOT EXISTS biz.daily_brief_snapshot (
    brief_date  DATE        PRIMARY KEY,
    payload     JSONB       NOT NULL,          -- 完整 brief（含 M0_ai_summary / data_quality / DIFF / 各模块 status）
    app_commit  TEXT,                          -- 生成时代码版本，便于归因
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  biz.daily_brief_snapshot           IS '每日早报完整 brief，供变更日志与事后复盘';
COMMENT ON COLUMN biz.daily_brief_snapshot.payload   IS '完整 brief JSON（不裁剪；含 M0_ai_summary 与顶层 data_quality）';
COMMENT ON COLUMN biz.daily_brief_snapshot.app_commit IS '生成时代码版本（env GIT_COMMIT 或 /app/.git_head），读不到为 NULL';