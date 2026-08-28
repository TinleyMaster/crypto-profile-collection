-- fix_017: token unlock 批量采集审计修复
-- 对应 commit: 待定
-- 二狗审计报告 2026-08-28 发现的 6 个问题

BEGIN;

-- P2-1: 补建 unlock_ratio_mcap 列（解锁占市值百分比）
ALTER TABLE biz.asset_token_unlocks
  ADD COLUMN IF NOT EXISTS unlock_ratio_mcap NUMERIC;

-- P2-4: source_name 修正 — app.tokenomics.com 的行应标 tokenomics.com 而非 tokenomist
UPDATE biz.asset_token_unlocks
SET source_name = 'tokenomics.com'
WHERE source_url LIKE '%app.tokenomics.com%'
  AND source_name = 'tokenomist';

-- P1-3: parse_empty 修正 — overview 有信号但事件为空的资产应标 parse_empty
UPDATE biz.asset_token_unlocks
SET crawl_status = 'parse_empty',
    updated_at = NOW()
WHERE crawl_status = 'ok'
  AND (
    unlock_events_json IS NULL
    OR jsonb_array_length(unlock_events_json) = 0
  )
  AND overview_json IS NOT NULL
  AND overview_json != '{}'::jsonb
  AND (
    overview_json ? 'released_pct'
    OR overview_json ? 'next_unlock_date'
    OR overview_json ? 'released_amount_str'
  );

-- 隐患1: 为 fail_timeout 墓碑预留（代码已支持，此处仅确认列兼容）
-- crawl_status VARCHAR(20) 已足够存储 'fail_timeout'

-- 二狗 §5(c): 历史 %MCAP 误存残留 — 事件的 ratio_mcap=True（主源语义）但值落到
-- unlock_ratio_total，迁到 unlock_ratio_mcap；仅处理明确带 ratio_mcap 标记的行，避免误伤。
UPDATE biz.asset_unlock_event
SET unlock_ratio_mcap = unlock_ratio_total,
    unlock_ratio_total = NULL
WHERE unlock_ratio_total > 1
  AND (raw_ref->>'ratio_mcap')::text = 'true';

COMMIT;
