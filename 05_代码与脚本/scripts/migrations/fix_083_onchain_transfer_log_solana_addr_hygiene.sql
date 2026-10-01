-- fix_083: biz.onchain_transfer_log 地址脏值治理（solana 空地址 + 大小写变体）
--
-- 背景（2026-10-01 实测，只读诊断）：
--   1) 196 行「单端残缺」：from_address / to_address 有一端为空串，**仅 solana 链存在**
--      （eth 199,529 / bsc 78,388 / base 27,957 / optimism 3,798 / polygon 621 /
--        avalanche 493 / arbitrum 139 全为 0）。成因是旧解析器按 pre/postTokenBalances
--      逐账户记账，每条只能写出单端 owner（对端未知）⇒ 同一笔转账落成 from-only 与
--      to-only 两行。现写入方（phase_chain_transfer_monitor.py）已有空地址过滤，
--      实测近 7 天零新增，最后一笔为 2026-09-03 ⇒ 根因已闭环，本步只治存量。
--      处置：**不伪造对端**（无链上依据合并即编造），置 is_suspect=TRUE 留痕隐藏——
--      下游所有聚合统一按 `is_suspect IS NOT TRUE` 过滤（见 onchain_alert._window_conds、
--      backfill_netflow_factor、db_stats 等 30+ 处），标记后即退出统计口径。
--   2) 105 行「全小写」脏地址（按 lower() 归并出 67 组，**每组都存在含大写的规范形式**）：
--      base58 大小写敏感，全小写串解出的字节 ≠ 真实地址 ⇒ 归因 SQL
--      （_exch_cte / onchain_alert 的 `f.address = tl.from_address AND f.chain = tl.chain`）
--      直接 join 失配。实证：Bitget 热钱包 A77HErqtfN1hLLpvZ9pCtu66FEtM8BveoaKbbMoZ4RiR
--      的小写变体 a77herqt...（5 行）全部丢归因。
--      秩据：44 位 base58 全部落在「小写字母或数字」的概率约 0.586^44 ≈ 5e-11 ⇒
--      全小写形态几乎必然是真实地址被 lower() 后的产物，规范形式取同组的含大写变体。
--      处置：按规范形式归一；归一后与已有规范行完全重复的直接删重（保留规范行，其余 22 行原地改）。
--   3) 追加 DB 层护栏：from/to 两列非空 CHECK（NOT VALID，不校验存量单端行）。
--      注：**不把「不得全小写」写进 CHECK**——真实地址理论上可全小写，属误伤风险；
--      大小写正确性由写入方（CASE_SENSITIVE_CHAINS 不 lower）与链上取数保证。
--
-- 幂等：归一化按 `grp.lo = t.xxx` 精确匹配全小写形态，跑第二遍无可匹配行；
--       CHECK 用 pg_constraint 判存在；标脏带 `is_suspect IS NOT TRUE` 守卫。
-- 应用：python 05_代码与脚本/scripts/apply_migration.py fix_083_onchain_transfer_log_solana_addr_hygiene.sql
-- 锁：DDL 置于文件末尾（此前只有 DML），缩短 AccessExclusiveLock 的持有窗口。

-- ============================================================
-- 0) 预估：执行前基线（**首次运行**应读到 196 / 92 / 67 / 0；复跑读到 182 / 1 / 0 / 0）
-- ============================================================
SELECT 'solana 单端残缺行' AS cls, count(*) AS n
FROM biz.onchain_transfer_log
WHERE chain = 'solana'
  AND (coalesce(from_address, '') = '' OR coalesce(to_address, '') = '')
UNION ALL
SELECT 'solana 全小写地址行(任一端)', count(*)
FROM biz.onchain_transfer_log
WHERE chain = 'solana' AND from_address <> '' AND to_address <> ''
  AND (from_address = lower(from_address) OR to_address = lower(to_address))
UNION ALL
SELECT 'solana 大小写变体组数', count(*) FROM (
    SELECT lower(addr) AS lo
    FROM (
        SELECT DISTINCT from_address AS addr FROM biz.onchain_transfer_log
        WHERE chain = 'solana' AND from_address <> ''
        UNION
        SELECT DISTINCT to_address FROM biz.onchain_transfer_log
        WHERE chain = 'solana' AND to_address <> ''
    ) s
    GROUP BY lower(addr) HAVING count(*) > 1
) g
UNION ALL
SELECT '其它链空地址行(应为 0)', count(*)
FROM biz.onchain_transfer_log
WHERE chain <> 'solana'
  AND (coalesce(from_address, '') = '' OR coalesce(to_address, '') = '');

-- ============================================================
-- 1) 大小写归一 · 删重：归一化后与「保留行」完全同键的脏行直接删除
--    保留行选取：同键下优先「本身已是规范形式」的行，否则取最小 log_id。
-- ============================================================
WITH s AS (
    SELECT DISTINCT from_address AS addr FROM biz.onchain_transfer_log
    WHERE chain = 'solana' AND from_address <> ''
    UNION
    SELECT DISTINCT to_address FROM biz.onchain_transfer_log
    WHERE chain = 'solana' AND to_address <> ''
),
grp AS (
    -- 每组全小写形态 -> 规范形式（含大写变体中最小的一个）
    SELECT lower(addr) AS lo, min(addr) FILTER (WHERE addr <> lower(addr)) AS canon
    FROM s
    GROUP BY lower(addr)
    HAVING count(*) > 1
),
norm AS (
    -- LEFT JOIN 到 lo（全小写串）才能命中脏行：canon 非空 = 该端是脏值
    SELECT t.log_id, t.tx_hash, t.contract_address,
           coalesce(gf.canon, t.from_address) AS nfrom,
           coalesce(gt.canon, t.to_address)   AS nto,
           (gf.canon IS NOT NULL OR gt.canon IS NOT NULL) AS is_dirty
    FROM biz.onchain_transfer_log t
    LEFT JOIN grp gf ON gf.lo = t.from_address
    LEFT JOIN grp gt ON gt.lo = t.to_address
    WHERE t.chain = 'solana'
),
keep AS (
    SELECT DISTINCT ON (tx_hash, contract_address, nfrom, nto) log_id
    FROM norm
    ORDER BY tx_hash, contract_address, nfrom, nto, is_dirty ASC, log_id ASC
)
DELETE FROM biz.onchain_transfer_log t
WHERE t.chain = 'solana'
  AND t.log_id IN (SELECT log_id FROM norm WHERE is_dirty)
  AND t.log_id NOT IN (SELECT log_id FROM keep);

-- ============================================================
-- 2) 大小写归一 · 改写：剩余脏行的地址端替换为规范形式
--    （第 1 步已保证归一后键唯一，不会与 uq_onchain_tx 冲突）
-- ============================================================
WITH s AS (
    SELECT DISTINCT from_address AS addr FROM biz.onchain_transfer_log
    WHERE chain = 'solana' AND from_address <> ''
    UNION
    SELECT DISTINCT to_address FROM biz.onchain_transfer_log
    WHERE chain = 'solana' AND to_address <> ''
),
grp AS (
    SELECT lower(addr) AS lo, min(addr) FILTER (WHERE addr <> lower(addr)) AS canon
    FROM s
    GROUP BY lower(addr)
    HAVING count(*) > 1
)
UPDATE biz.onchain_transfer_log t
SET from_address = coalesce((SELECT canon FROM grp WHERE lo = t.from_address), t.from_address),
    to_address   = coalesce((SELECT canon FROM grp WHERE lo = t.to_address),   t.to_address)
WHERE t.chain = 'solana'
  AND ((SELECT canon FROM grp WHERE lo = t.from_address) IS NOT NULL
       OR (SELECT canon FROM grp WHERE lo = t.to_address) IS NOT NULL);

-- ============================================================
-- 3) 单端残缺行标脏（保留留痕，退出所有下游聚合口径）
-- ============================================================
UPDATE biz.onchain_transfer_log
SET is_suspect = TRUE
WHERE chain = 'solana'
  AND (coalesce(from_address, '') = '' OR coalesce(to_address, '') = '')
  AND is_suspect IS NOT TRUE;

-- ============================================================
-- 3b) 归一化后仍残留、且**两端同时**全小写的行：全表无同组规范形式可对齐
--     （跑完第 1/2 步后仅 1 行，log_id=5278 / asset 2315 / 2026-08-01）。
--     判据取「两端全小写」而非「任一端全小写」：单个 base58 地址全小写概率约 0.586^44≈5e-11，
--     两端同时全小写 ≈ 3e-21 ⇒ 无 误伤 可能。不伪造正确大小写，标脏留痕。
-- ============================================================
UPDATE biz.onchain_transfer_log
SET is_suspect = TRUE
WHERE chain = 'solana'
  AND from_address <> '' AND to_address <> ''
  AND from_address = lower(from_address) AND to_address = lower(to_address)
  AND is_suspect IS NOT TRUE;

-- ============================================================
-- 4) DDL 护栏：from/to 两列非空 CHECK（NOT VALID：存量单端行已在第 3 步标脏隔离，
--    此处只约束后续 INSERT/UPDATE，不触发全表校验扫描）
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'biz.onchain_transfer_log'::regclass
          AND conname = 'ck_onchain_transfer_log_addr_not_empty'
    ) THEN
        ALTER TABLE biz.onchain_transfer_log
            ADD CONSTRAINT ck_onchain_transfer_log_addr_not_empty
            CHECK (coalesce(from_address, '') <> '' AND coalesce(to_address, '') <> '')
            NOT VALID;
    END IF;
END $$;

COMMENT ON CONSTRAINT ck_onchain_transfer_log_addr_not_empty ON biz.onchain_transfer_log IS
    'ADDRHYG-001（2026-10-01）：from/to 两列不得为空/NULL。存量 196 行 solana 单端残缺行以 '
    'is_suspect=TRUE 隔离故用 NOT VALID 只约束新增写入；不含「不得全小写」判据（真实地址理论上可全小写，'
    '写进 CHECK 有误伤风险，大小写正确性由 CASE_SENSITIVE_CHAINS 写入侧保证）。';

-- ============================================================
-- 5) 复验：未标脏的脏值残留应均为 0；已标脏计数用于核对闭环
-- ============================================================
SELECT '残留单端残缺(未标脏)' AS cls, count(*) AS n
FROM biz.onchain_transfer_log
WHERE chain = 'solana'
  AND (coalesce(from_address, '') = '' OR coalesce(to_address, '') = '')
  AND is_suspect IS NOT TRUE
UNION ALL
SELECT '残留全小写脏地址(未标脏)', count(*)
FROM biz.onchain_transfer_log
WHERE chain = 'solana' AND from_address <> '' AND to_address <> ''
  AND (from_address = lower(from_address) OR to_address = lower(to_address))
  AND is_suspect IS NOT TRUE
UNION ALL
SELECT '已标脏 · 单端残缺行', count(*)
FROM biz.onchain_transfer_log
WHERE chain = 'solana'
  AND (coalesce(from_address, '') = '' OR coalesce(to_address, '') = '')
  AND is_suspect IS TRUE
UNION ALL
SELECT '已标脏 · 全小写残行', count(*)
FROM biz.onchain_transfer_log
WHERE chain = 'solana' AND from_address <> '' AND to_address <> ''
  AND (from_address = lower(from_address) OR to_address = lower(to_address))
  AND is_suspect IS TRUE
UNION ALL
SELECT '残留其它链空地址', count(*)
FROM biz.onchain_transfer_log
WHERE chain <> 'solana'
  AND (coalesce(from_address, '') = '' OR coalesce(to_address, '') = '')
UNION ALL
SELECT 'CHECK 护栏是否就位', count(*)
FROM pg_constraint
WHERE conrelid = 'biz.onchain_transfer_log'::regclass
  AND conname = 'ck_onchain_transfer_log_addr_not_empty';