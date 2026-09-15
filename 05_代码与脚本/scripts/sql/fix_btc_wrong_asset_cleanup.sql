-- ============================================================
-- 催化剂错连资产脏数据清理脚本（v5.1 — CAT-CLEANUP 修复 + 白名单分支）
-- 背景：linker.py 旧代码 symbol→asset_id 兜底无 CMC 排名排序，
--       导致 BTC/XRP/BNB/ETH/SOL 等主流币被映射到同名小市值仿盘
--       （Bitcoin Base / XRP AI / BNBTiger Inu / NEAR Intents Bridged ETH / Sol The Trophy Tomato 等）
-- v5 修复点（对齐 CAT-FIX-ROLLUP-001）：
--   1. 去掉 market_cap_rank IS NOT NULL 限制 —— 无排名仿盘（Bitcoin Base 等）也要能删
--   2. 修复原逻辑缺陷：脏词资产被排除出 ranked → 永远不进 wrong_assets
--      改为「全部资产参与排名，rn>1 且命中条件才判脏」
-- v5.1 新增：
--   3. 白名单符号分支：主流币（BTC/ETH/SOL/…）同名多资产除最主流外全判脏，
--      覆盖 Bitcoin Base / XRP AI / Bridged ETH 等「无仿盘词但确为仿盘」的脏资产
--      （带安全守卫：该 symbol 至少一条有 CMC 排名，保证 rn=1 保留的是真币）
-- 删除范围 = 每 symbol 非最主流(rn>1) 且命中「白名单同名 / 仿盘标记 / tokenized 商品」三者之一
--
-- 使用步骤（数据库 GUI 工具）：
--   ┌─ 第一步：验证（只读，不会删数据）───────────┐
--   │ 选中 Step 0~2 全部内容，点「运行」          │
--   │ 查看结果里的 wrong_assets_total 和          │
--   │ catalyst_signal 数量是否合理                │
--   └───────────────────────────────────────────┘
--   ┌─ 第二步：执行删除（确认无误后再操作）──────┐
--   │ 选中 Step 3 全部内容，点「运行」            │
--   │ 再选中 Step 4 验证是否清零                  │
--   └───────────────────────────────────────────┘
--   ┌─ 第三步：重建关联 + 重跑管道回灌 ──────────┐
--   │ 容器内执行：                                 │
--   │ python scripts/bin/backfill_catalyst_links.py
--   │ python scripts/bin/phase_catalyst_pipeline.py --fast --slow --no-alert
--   │   （--fast 走 run_signal 重生成直连信号，--slow 补二阶/G3-G5/AI，--no-alert 免邮件轰炸）
--   └───────────────────────────────────────────┘
-- ============================================================

-- ============================================================
--  Step 0: 创建临时表（同一会话内多次执行先删重建）
-- ============================================================
DROP TABLE IF EXISTS wrong_assets;

CREATE TEMP TABLE wrong_assets AS
WITH ranked AS (
    SELECT asset_id, canonical_symbol, canonical_name, market_cap_rank,
           ROW_NUMBER() OVER (
               PARTITION BY UPPER(canonical_symbol)
               ORDER BY market_cap_rank ASC NULLS LAST, asset_id
           ) AS rn
    FROM core.asset
    WHERE canonical_symbol IS NOT NULL
      AND TRIM(canonical_symbol) <> ''
)
SELECT r.asset_id, r.canonical_symbol, r.canonical_name, r.market_cap_rank
FROM ranked r
WHERE r.rn > 1
  AND (
      -- ① 白名单符号：主流币同名多资产除最主流外全判脏
      --    （覆盖 Bitcoin Base / XRP AI / Bridged ETH / Trophy Tomato 等无仿盘词的仿盘）
      (UPPER(r.canonical_symbol) IN (
          'BTC','ETH','SOL','XRP','BNB','DOGE','PEPE','SHIB','ADA',
          'DOT','AVAX','LINK','LTC','UNI','AAVE','MATIC','POL','TRX',
          'XLM','FIL','NEAR','ARB','OP','SUI','TON','APT','INJ','SEI',
          'WIF','BONK','FLOKI','ENA','PENDLE','JUP','RENDER','FET'
       )
       -- 安全守卫：该 symbol 至少有一条有 CMC 排名的资产（保证 rn=1 保留的是真币）
       AND EXISTS (
           SELECT 1 FROM core.asset a2
           WHERE UPPER(a2.canonical_symbol) = UPPER(r.canonical_symbol)
             AND a2.market_cap_rank IS NOT NULL
       ))
      -- ② 非白名单符号：命中明显仿盘标记才判脏（保守，避免误删）
      OR LOWER(COALESCE(r.canonical_name, '')) ~ 'bridged|wrapped|intents|trophy|tomato|second[[:space:]]+chance|base[[:space:]]+coin'
      -- ③ tokenized / 商品衍生（不该出现在 crypto 信号中的错连）
      OR LOWER(COALESCE(r.canonical_name, '')) ~ 'tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ|crude[[:space:]]+oil|brent'
  )
ORDER BY UPPER(r.canonical_symbol), r.market_cap_rank DESC;

CREATE INDEX IF NOT EXISTS idx_wrong_assets_id ON wrong_assets (asset_id);

-- ============================================================
--  Step 1: 查看错连资产明细（前 30 条，确认是不是真的脏资产）
-- ============================================================
SELECT canonical_symbol, canonical_name, market_cap_rank, asset_id
FROM wrong_assets
ORDER BY market_cap_rank DESC
LIMIT 30;

-- ============================================================
--  Step 2: 查询各表受影响的记录数（验证用，务必先看结果）
-- ============================================================
SELECT 'wrong_assets_total' AS step, COUNT(*) AS cnt FROM wrong_assets
UNION ALL
SELECT 'catalyst_asset_link', COUNT(*)
FROM biz.catalyst_asset_link cal
JOIN wrong_assets w ON cal.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_signal', COUNT(*)
FROM biz.catalyst_signal cs
JOIN wrong_assets w ON cs.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_second_order', COUNT(*)
FROM biz.catalyst_second_order cso
JOIN wrong_assets w ON cso.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_resonance', COUNT(*)
FROM biz.catalyst_resonance cr
JOIN wrong_assets w ON cr.asset_id = w.asset_id
ORDER BY step;

-- ============================================================
--  Step 3: 删除脏数据（确认无误后再执行！）
--          级联顺序：signal → second_order → resonance → asset_link
-- ============================================================
DELETE FROM biz.catalyst_signal cs
USING wrong_assets w
WHERE cs.asset_id = w.asset_id;

DELETE FROM biz.catalyst_second_order cso
USING wrong_assets w
WHERE cso.asset_id = w.asset_id;

DELETE FROM biz.catalyst_resonance cr
USING wrong_assets w
WHERE cr.asset_id = w.asset_id;

DELETE FROM biz.catalyst_asset_link cal
USING wrong_assets w
WHERE cal.asset_id = w.asset_id;

-- ============================================================
--  Step 4: 清理后验证（应该全为 0）
-- ============================================================
SELECT 'catalyst_asset_link_after' AS step, COUNT(*) AS cnt
FROM biz.catalyst_asset_link cal
JOIN wrong_assets w ON cal.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_signal_after', COUNT(*)
FROM biz.catalyst_signal cs
JOIN wrong_assets w ON cs.asset_id = w.asset_id
ORDER BY step;

-- ============================================================
--  清理完成后，在容器内重跑慢通道回灌：
--    python scripts/bin/phase_catalyst_pipeline.py --slow
-- ============================================================
