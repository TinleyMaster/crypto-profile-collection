-- ============================================================
-- P1: 白皮书路径相对化迁移
-- 目的：将 storage_path 从 Windows 绝对路径改为相对于 DOCS_STORAGE_ROOT 的相对路径
-- 背景：当前 52 条白皮书的 storage_path 是 Windows 绝对路径（E:\...\docs_storage\...），
--       云端部署时路径不兼容，需要改为相对路径，由应用层拼接 DOCS_STORAGE_ROOT
-- 格式变化：
--   旧：E:\瞎搞乱搞\web3\加密货币研究报告\docs_storage\xrp_1127\whitepapers\xxx.pdf
--   新：xrp_1127/whitepapers/xxx.pdf
-- ============================================================

-- ---------- 预览：先看有多少条需要迁移 ----------
SELECT COUNT(*) as need_migrate
FROM biz.doc_asset
WHERE storage_path LIKE '%\docs_storage\%'
  AND storage_path LIKE '%\%';  -- 含反斜杠的 Windows 路径

-- ---------- 预览：迁移前的样例 ----------
SELECT doc_id, asset_id, doc_type, storage_path
FROM biz.doc_asset
WHERE storage_path LIKE '%\docs_storage\%'
ORDER BY doc_id
LIMIT 10;

-- ---------- 执行迁移 ----------
-- 思路：截取 \docs_storage\ 之后的部分，将反斜杠替换为正斜杠
-- 注意：先运行上面的预览确认无误后，再取消下面 UPDATE 的注释执行

/*
UPDATE biz.doc_asset
SET storage_path = REPLACE(
        SUBSTRING(
            storage_path
            FROM POSITION('\docs_storage\' IN storage_path) + 14  -- 14 = len('\docs_storage\')
        ),
        '\', '/'
    ),
    updated_at = NOW()
WHERE storage_path LIKE '%\docs_storage\%';
*/

-- ---------- 验证：迁移后统计 ----------
/*
SELECT
    COUNT(*) as total_with_path,
    COUNT(CASE WHEN storage_path LIKE '%\%' THEN 1 END) as still_windows_path,
    COUNT(CASE WHEN storage_path LIKE '%/%' AND storage_path NOT LIKE '%\%' THEN 1 END) as unix_relative_path
FROM biz.doc_asset
WHERE storage_path IS NOT NULL;

-- 验证样例
SELECT doc_id, asset_id, doc_type, storage_path
FROM biz.doc_asset
WHERE storage_path IS NOT NULL
ORDER BY doc_id
LIMIT 10;
*/
