-- MEME-10: 链覆盖状态声明表（covered vs degraded）
CREATE TABLE IF NOT EXISTS biz.chain_coverage (
    chain               text PRIMARY KEY,
    coverage_status     text NOT NULL,          -- 'covered' | 'degraded'
    has_native_snapshot boolean DEFAULT false,
    asset_count         int,
    note                text,
    updated_at          timestamptz DEFAULT now()
);
