-- W-13-R2（核验 2026-09-27 复验 R2）：机会清单窗口可审计 + 堵永久空洞
-- 只改 biz.opportunity_snapshot 一张表。
--
-- 背景：
-- ① ref_price 取的是「最近可得收盘价」，其真实日期可能与 snapshot_date 不同
--    （盘中生成时当日 ETL 未落），旧表只有 ref_price、无日期 → 实际窗口不可审计
--    （名义 T+1 ≠ 实际 T+1）。新增 ref_price_date 记录实际取价日。
-- ② 回填扫描原来只覆盖 `outcome_7d IS NULL`：若某行 outcome_7d 先被填上（或历史遗留）、
--    而 outcome_1d 为空，该行的 T+1 格将成为永久空洞。扫描条件改为两列任一为空。
ALTER TABLE biz.opportunity_snapshot
    ADD COLUMN IF NOT EXISTS ref_price_date DATE;

COMMENT ON COLUMN biz.opportunity_snapshot.ref_price_date IS
    'ref_price 对应的实际 market_date（可能早于 snapshot_date；回填窗口据此审计，W-13-R2）';

-- 部分索引随扫描条件同步重建（谓词变更无法 ALTER，须 DROP + CREATE）
DROP INDEX IF EXISTS biz.idx_opportunity_snapshot_pending;
CREATE INDEX IF NOT EXISTS idx_opportunity_snapshot_pending
    ON biz.opportunity_snapshot (snapshot_date)
    WHERE outcome_1d IS NULL OR outcome_7d IS NULL;