-- ============================================================
-- BTC symbol 错连 "Bitcoin Gold AI" 脏数据清理脚本
-- 背景：core.asset 中 BTC symbol 有多个条目，旧代码 LIMIT 1 兜底
--       可能取到 "Bitcoin Gold AI" 而非正确的 BTC 资产
--       导致 catalyst 关联全部连到错误 asset_id
-- 使用：在容器内 psql 执行，建议先开事务验证再 COMMIT
-- ============================================================

BEGIN;

-- ---- Step 1: 识别错误的 BTC 资产（Bitcoin Gold AI）
-- 先查询确认，应返回 1 行
CREATE TEMP TABLE wrong_btc_asset AS
SELECT asset_id, canonical_symbol, canonical_name, asset_type, primary_sector
FROM core.asset
WHERE UPPER(canonical_symbol) = 'BTC'
  AND LOWER(COALESCE(canonical_name, '')) ~ 'gold.*ai|ai.*gold|bitcoin gold';

-- ---- Step 2: 查询受影响的记录数（验证用）
SELECT 'wrong_btc_asset' AS step, COUNT(*) AS cnt FROM wrong_btc_asset
UNION ALL
SELECT 'catalyst_asset_link', COUNT(*) FROM biz.catalyst_asset_link cal
  JOIN wrong_btc_asset w ON cal.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_signal', COUNT(*) FROM biz.catalyst_signal cs
  JOIN wrong_btc_asset w ON cs.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_second_order', COUNT(*) FROM biz.catalyst_second_order cso
  JOIN wrong_btc_asset w ON cso.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_resonance', COUNT(*) FROM biz.catalyst_resonance cr
  JOIN wrong_btc_asset w ON cr.asset_id = w.asset_id
ORDER BY step;

-- ---- Step 3: 删除脏数据（级联顺序：signal → second_order → resonance → asset_link
DELETE FROM biz.catalyst_signal cs
USING wrong_btc_asset w
WHERE cs.asset_id = w.asset_id;

DELETE FROM biz.catalyst_second_order cso
USING wrong_btc_asset w
WHERE cso.asset_id = w.asset_id;

DELETE FROM biz.catalyst_resonance cr
USING wrong_btc_asset w
WHERE cr.asset_id = w.asset_id;

DELETE FROM biz.catalyst_asset_link cal
USING wrong_btc_asset w
WHERE cal.asset_id = w.asset_id;

-- ---- Step 4: 重置相关催化剂的 grade 状态，让管道重新关联
-- 把有 BTC 参与的催化剂标记为需要重新解析（清空 grade 让 slow 通道重跑）
-- 注意：只重置那些因错误资产产生的 grade 记录
-- （更安全的做法是让管道自然重跑最近 7 天内的催化剂）

-- 如果确认删除行数
GET DIAGNOSTICS deleted_ = ROW_COUNT;

-- 提交前再查一次确认清理结果
SELECT 'catalyst_asset_link_after' AS step, COUNT(*) AS cnt FROM biz.catalyst_asset_link cal
  JOIN wrong_btc_asset w ON cal.asset_id = w.asset_id;

-- 提交（确认无误后再 COMMIT）
-- COMMIT;
-- 回滚：ROLLBACK;
