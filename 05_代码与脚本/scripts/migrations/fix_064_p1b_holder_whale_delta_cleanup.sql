-- fix_064: P1-B 巨鲸变化（Top10 集中度 delta）越界值清理（2026-09-23）
--
-- 来源：审计_加密大盘早报_2026-09-22 P1-B / 复验_早报P1_f35dc1d_2026-09-23 F1
--
-- 根因：上游 top10_concentration 偶发越界(>100)（prod 实测 2,416 行，max 938.74），
--       生产者 whale_balance_change_7d_pct = cur_top10 - prev_top10 随之越界 [-100,100]
--       （prod 实测 17 行，min -531.01 / max 237.26）。
--
-- 处置（本迁移只清物理脏行；根因由代码侧收口）：
--   ① 备份 17 行越界记录；
--   ② 将 whale_balance_change_7d_pct 越界行置 NULL。
--   代码侧已修：读取侧 fetch_holder_concentration_summary 用 BETWEEN 过滤；
--   生产者 phase_chain_holder_scrape 用 _valid_conc/_conc_delta 越界不写。
--
-- 幂等：备份表 IF NOT EXISTS；UPDATE 的 WHERE 保证重复执行 0 行。
-- 已对 prod 执行（2026-09-23）。

BEGIN;

CREATE TABLE IF NOT EXISTS biz.onchain_holder_snapshot_p1b_backup_20260923 AS
SELECT * FROM biz.onchain_holder_snapshot
WHERE whale_balance_change_7d_pct IS NOT NULL
  AND whale_balance_change_7d_pct NOT BETWEEN -100 AND 100;

UPDATE biz.onchain_holder_snapshot
   SET whale_balance_change_7d_pct = NULL
 WHERE whale_balance_change_7d_pct IS NOT NULL
   AND whale_balance_change_7d_pct NOT BETWEEN -100 AND 100;

COMMIT;

-- 校验（应返回 0）：
-- SELECT count(*) FROM biz.onchain_holder_snapshot
--  WHERE whale_balance_change_7d_pct IS NOT NULL
--    AND whale_balance_change_7d_pct NOT BETWEEN -100 AND 100;
-- 备份行数（应为 17）：
-- SELECT count(*) FROM biz.onchain_holder_snapshot_p1b_backup_20260923;
