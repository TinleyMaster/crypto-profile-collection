-- fix_065: 存量越界持仓集中度清理（top10/50/100）
--
-- 来源：复验_早报P1_F1_1c84693_2026-09-23「2,416 行历史脏 concentration 未物理清理」延伸。
-- 根因：部分链解析出的集中度越界（>100）。prod 只读实测：top10 2,416 / top50 4,402 /
--       top100 5,215 行越界，最新快照(2026-09-22) top10 越界 163 行；会经 web 资产页
--       (index.html 直显 top10/50/100_concentration)、most_concentrated 榜单等读取路径暴露
--       （如 SHRUB 927.96% / REZ 885.52%）。
--
-- 处置：逐字段把越界值置 NULL（保留同行其它合法字段）；备份受影响整行。
--       代码侧已修：生产者 _valid_conc 堵新写入；读取侧 most_concentrated BETWEEN 0 AND 100、
--       db_stats 抛压分 clamp [0,100]。
--
-- 幂等：备份表 IF NOT EXISTS；三条 UPDATE 的 WHERE 保证重复执行 0 行。
-- 已对 prod 执行（2026-09-23）。

BEGIN;

CREATE TABLE IF NOT EXISTS biz.onchain_holder_snapshot_conc_backup_20260923 AS
SELECT * FROM biz.onchain_holder_snapshot
WHERE (top10_concentration  IS NOT NULL AND (top10_concentration  < 0 OR top10_concentration  > 100))
   OR (top50_concentration  IS NOT NULL AND (top50_concentration  < 0 OR top50_concentration  > 100))
   OR (top100_concentration IS NOT NULL AND (top100_concentration < 0 OR top100_concentration > 100));

UPDATE biz.onchain_holder_snapshot SET top10_concentration = NULL
 WHERE top10_concentration IS NOT NULL AND (top10_concentration < 0 OR top10_concentration > 100);

UPDATE biz.onchain_holder_snapshot SET top50_concentration = NULL
 WHERE top50_concentration IS NOT NULL AND (top50_concentration < 0 OR top50_concentration > 100);

UPDATE biz.onchain_holder_snapshot SET top100_concentration = NULL
 WHERE top100_concentration IS NOT NULL AND (top100_concentration < 0 OR top100_concentration > 100);

COMMIT;

-- 校验（三项均应返回 0）：
-- SELECT count(*) FROM biz.onchain_holder_snapshot WHERE top10_concentration  NOT BETWEEN 0 AND 100;
-- SELECT count(*) FROM biz.onchain_holder_snapshot WHERE top50_concentration  NOT BETWEEN 0 AND 100;
-- SELECT count(*) FROM biz.onchain_holder_snapshot WHERE top100_concentration NOT BETWEEN 0 AND 100;
