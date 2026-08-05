-- 扩展 ingest_run.status 允许 'partial'（部分成功/部分失败）
ALTER TABLE sys.ingest_run DROP CONSTRAINT IF EXISTS chk_ingest_run_status;

ALTER TABLE sys.ingest_run ADD CONSTRAINT chk_ingest_run_status
CHECK (status IN ('running', 'success', 'failed', 'partial'));
