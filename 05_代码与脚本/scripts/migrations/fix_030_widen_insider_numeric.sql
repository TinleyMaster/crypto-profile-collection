-- MEME-07-P2-B1: widen precision — NUMERIC(6,4) → NUMERIC(20,8)
-- fix_029 CREATE TABLE IF NOT EXISTS 不会改现有列，故走 ALTER 独立 migration。
-- USING 子句保证现有 5 行(0.0000) 无损拓宽。
ALTER TABLE biz.asset_insider_clusters
    ALTER COLUMN insider_dominance TYPE NUMERIC(20,8)
    USING insider_dominance::numeric(20,8);

ALTER TABLE biz.asset_insider_clusters
    ALTER COLUMN insider_account_ratio TYPE NUMERIC(20,8)
    USING insider_account_ratio::numeric(20,8);
