-- 为 NotebookLM 投研精选创建缓存表
-- 配额粗筛 + AI 排序后存入此表，下次查询秒出

CREATE TABLE IF NOT EXISTS biz.doc_source_notebooklm (
    asset_id         INTEGER NOT NULL,
    source_entry_id  INTEGER NOT NULL,
    entry_type       TEXT   NOT NULL,
    entry_url        TEXT   NOT NULL,
    source_code      TEXT   NOT NULL,
    ai_rank          INTEGER NOT NULL,  -- 1~50, AI 排序值
    ai_reason        TEXT   DEFAULT '', -- AI 简短理由
    generated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (asset_id, source_entry_id),
    CONSTRAINT fk_notebooklm_asset
        FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_notebooklm_entry
        FOREIGN KEY (source_entry_id) REFERENCES biz.doc_source_entry(entry_id)
        ON DELETE CASCADE
);

COMMENT ON TABLE biz.doc_source_notebooklm IS
    'NotebookLM 投研精选缓存：配额粗筛 + AI 排序后的 Top 50 链接。命中即用，无则按需生成。';

-- 按资产快速查询
CREATE INDEX IF NOT EXISTS idx_notebooklm_asset_id
    ON biz.doc_source_notebooklm (asset_id, ai_rank);