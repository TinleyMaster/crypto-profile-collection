-- 催化剂 thesis 重生游标表
-- 用途：存储 catalyst_thesis_regen.py 的复合游标 (last_ts, last_asset_id)
-- 确保 LIMIT 分批处理时不遗漏、不重复

CREATE TABLE IF NOT EXISTS biz.catalyst_regen_cursor (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    last_ts         TIMESTAMPTZ,          -- 上次处理到的 ai_processed_at
    last_asset_id   BIGINT,               -- 上次处理到的 asset_id（同 ts 内的偏移）
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_count INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT single_row CHECK (id = 1)  -- 永远只有 1 行
);

COMMENT ON TABLE biz.catalyst_regen_cursor IS '催化剂 thesis 重生游标（复合键：ai_processed_at + asset_id）';
COMMENT ON COLUMN biz.catalyst_regen_cursor.last_ts IS '上次处理到的最大 ai_processed_at';
COMMENT ON COLUMN biz.catalyst_regen_cursor.last_asset_id IS '同 ts 内最后处理的 asset_id（复合游标第二维）';
