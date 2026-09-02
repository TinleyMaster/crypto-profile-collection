-- fix_025: biz.asset_liquidity 主键改复合 (asset_id, chain)（每链一行，方案 A）
-- 背景: 原单列 PK asset_id + ON CONFLICT(asset_id) → 同资产多链合约互相覆盖，
--       仅保留扫描顺序最后一条链（prod 实测 35 资产 51% 多链、极端 25 链只剩 1 行）。
-- 存量数据: 表内 35 行 asset_id 均唯一（无重复）→ ALTER 安全，无需清数。

BEGIN;

ALTER TABLE biz.asset_liquidity DROP CONSTRAINT IF EXISTS asset_liquidity_pkey;
ALTER TABLE biz.asset_liquidity ALTER COLUMN chain SET NOT NULL;
ALTER TABLE biz.asset_liquidity ADD PRIMARY KEY (asset_id, chain);

COMMIT;
