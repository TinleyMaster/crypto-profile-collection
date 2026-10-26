-- fix_116_symbol_collision_dedup.sql
-- 工单：HC-013「撞名检测护栏静默失效」
--
-- 背景（2026-10-26 实测取证）：
--   `_detect_symbol_collision()` 的 UPSERT 语句是
--       INSERT ... SELECT m.asset_id, ... FROM core.asset m JOIN core.asset o
--         ON o.canonical_symbol = m.canonical_symbol AND o.asset_id <> m.asset_id
--        WHERE m.asset_id = %s
--       ON CONFLICT (asset_id) DO UPDATE ...
--   SELECT 未去重 ⇒ 一个资产撞 N 个 symbol 就返回 N 行、且 N 行的 m.asset_id 完全相同
--   ⇒ PostgreSQL 抛 `CardinalityViolation: ON CONFLICT DO UPDATE command cannot
--   affect row a second time`，整条语句回滚。
--
--   severity（实测，不是推断）：
--     撞 1 个的资产 3078 个 → detected_at 跨 7 个不同分钟（02:17~06:44）⇒ 逐资产写入正常
--     撞 >=2 个的资产 2186 个 → detected_at 全部 = 2026-10-26 02:17:35.485399（同一微秒）
--                              ⇒ 来自一次性批量插入，此后永不刷新
--   ⇒ 护栏在 2186/5264（41.5%）资产上是哑的；且每次抓取都在 stdout 打一条
--     `[warn] 撞名检测失败（不影响入库）`，被 except Exception 吞掉，无人察觉。
--
-- 本迁移做三件事（全部幂等）：
--   ① 补 `n_collisions`：表 PK 是 asset_id ⇒ 结构上只能记录「一个」对撞对象，
--      「撞了几个」这个信息原本彻底丢失。补上才能区分「轻微撞名」与「高危撞名」。
--   ② 建表语句收口到这里（HC-008 教训：DDL 不进抓取热路径）。
--   ③ 回填历史：把现已存在的 5264 行的 n_collisions 按真实撞名数修正。
--
-- 执行者：本表 owner = navicat，可用业务连接直接执行（无需 postgres）。

BEGIN;

-- ① 建表（若不存在）+ 补列
CREATE TABLE IF NOT EXISTS biz.asset_symbol_collision (
    asset_id              BIGINT PRIMARY KEY,
    canonical_symbol      VARCHAR(50),
    collided_with_asset_id BIGINT,
    collided_with_name    VARCHAR(200),
    detected_at           TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE biz.asset_symbol_collision
    ADD COLUMN IF NOT EXISTS n_collisions INTEGER NOT NULL DEFAULT 1;

-- ② 语义注释：这张表的 detected_at 是「最后刷新时间」，不是「首次发现时间」
COMMENT ON COLUMN biz.asset_symbol_collision.n_collisions IS
    '该 canonical_symbol 撞名的资产总数（>=1）。因主键仅 asset_id，本表只能记录其中一个对撞对象，其余靠本列体现规模。';

COMMENT ON COLUMN biz.asset_symbol_collision.detected_at IS
    '最后一次刷新时间（每次 UPSERT 置 NOW()），不是首次发现时间。⚠️ 若某资产该值长期不变，说明其写入路径失败——见 HC-013。';

COMMENT ON COLUMN biz.asset_symbol_collision.collided_with_asset_id IS
    '记录的对撞对象之一：优先取市值排名更高（更易被消费端混淆）者，NULL 排名排最后，再按 asset_id 兜底以保证确定性。';

-- ③ 回填真实撞名数（幂等：IS DISTINCT FROM 保证重复执行不产生无谓写入）
UPDATE biz.asset_symbol_collision s
   SET n_collisions = c.n
  FROM (
        SELECT m.asset_id, count(*) AS n
          FROM core.asset m
          JOIN core.asset o
            ON o.canonical_symbol = m.canonical_symbol
           AND o.asset_id <> m.asset_id
         GROUP BY m.asset_id
       ) c
 WHERE c.asset_id = s.asset_id
   AND s.n_collisions IS DISTINCT FROM c.n;

-- ④ 存量对齐：把「记录的对撞对象」按新规则重算（优先市值排名更高者）。
--    理由：历史那批是一次性批量灌入的，对撞对象是任意挑的；若不重算，
--    表里同时存在新旧两套选取规则，消费端无法判断语义。
--    幂等：IS DISTINCT FROM 保证重复执行不产生无谓写入。
UPDATE biz.asset_symbol_collision s
   SET collided_with_asset_id = c.pick_id,
       collided_with_name     = c.pick_name,
       n_collisions           = c.n
  FROM (
        SELECT m.asset_id,
               (array_agg(o.asset_id
                          ORDER BY o.market_cap_rank NULLS LAST, o.asset_id))[1] AS pick_id,
               (array_agg(o.canonical_name
                          ORDER BY o.market_cap_rank NULLS LAST, o.asset_id))[1] AS pick_name,
               count(*) AS n
          FROM core.asset m
          JOIN core.asset o
            ON o.canonical_symbol = m.canonical_symbol
           AND o.asset_id <> m.asset_id
         GROUP BY m.asset_id
       ) c
 WHERE c.asset_id = s.asset_id
   AND (s.collided_with_asset_id IS DISTINCT FROM c.pick_id
        OR s.collided_with_name   IS DISTINCT FROM c.pick_name
        OR s.n_collisions         IS DISTINCT FROM c.n);

COMMIT;

-- 验收（期望：n_collisions>=2 的行数 ≈ 2186，且与 assets_with_collision 一致）
--   SELECT (n_collisions >= 2) AS multi, count(*) FROM biz.asset_symbol_collision GROUP BY 1;
--   WITH coll AS (SELECT m.asset_id, count(*) n FROM core.asset m JOIN core.asset o
--                   ON o.canonical_symbol=m.canonical_symbol AND o.asset_id<>m.asset_id GROUP BY 1)
--   SELECT (n>=2) multi, count(*) FROM coll GROUP BY 1;
