-- fix_059: 存量 biz.oi_cvd_snapshot.source 回标（复验 P1-N4）
--
-- 背景：fix_057 用 `ADD COLUMN ... NOT NULL DEFAULT 'realtime'` 引入 source 列，
--   PG 11+ 不重写表，但会把**存量行全部填成默认值** → 历史 1h 回填行被标成
--   'realtime'，使「新鲜度判断 / 主池 OI 变化」的 realtime 过滤形同虚设。
--   实测（2026-09-21，只读）：全表 172,531 行 source 全为 realtime，
--   source='backfill' 现存 0 行，其中 111,593 行（66.7%）是 1h 回填签名。
--
-- 判据（比「整分钟点」更可靠）：同一 (exchange, symbol, 小时) 桶内**仅 1 行**
--   且该行落在整点 → 该桶来自 1h 历史回填。真实的 5m 实时采样每小时约 12 行，
--   不会只有 1 行。
--
-- 窗口：仅处理 ts < 2026-09-21 01:00 UTC（63h 停摆结束前的历史区间）。
--   停摆结束后重启的实时采样本身是正确的 realtime，不动。
--
-- 幂等：第 2 次执行时 UPDATE 命中 0 行（已成为 backfill）；备份表 IF NOT EXISTS。
-- 可回滚：备份表 biz.oi_cvd_source_backup_20260921 存了被改行的 (exchange, symbol, ts)。

-- 1. 物化「1h 回填签名桶」（一次扫描，供备份与 UPDATE 共用）
CREATE TABLE IF NOT EXISTS biz.oi_cvd_hourly_backfill_20260921 AS
SELECT exchange, symbol, date_trunc('hour', ts) AS h
FROM biz.oi_cvd_snapshot
WHERE ts < TIMESTAMPTZ '2026-09-21 01:00:00+00'
GROUP BY exchange, symbol, date_trunc('hour', ts)
HAVING COUNT(*) = 1
   AND COUNT(*) FILTER (WHERE date_trunc('hour', ts) = ts) = 1;

CREATE INDEX IF NOT EXISTS idx_oi_cvd_hbf_20260921
    ON biz.oi_cvd_hourly_backfill_20260921 (exchange, symbol, h);

COMMENT ON TABLE biz.oi_cvd_hourly_backfill_20260921 IS
    'fix_059 临时分类表：1h 回填签名桶（同符号同小时仅 1 行且落在整点）。'
    '回标校验通过后可 DROP。';

-- 2. 备份被改行的身份（用于回滚）
CREATE TABLE IF NOT EXISTS biz.oi_cvd_source_backup_20260921 AS
SELECT s.exchange, s.symbol, s.ts, s.source
FROM biz.oi_cvd_snapshot s
JOIN biz.oi_cvd_hourly_backfill_20260921 g
  ON g.exchange = s.exchange AND g.symbol = s.symbol
 AND g.h = date_trunc('hour', s.ts);

COMMENT ON TABLE biz.oi_cvd_source_backup_20260921 IS
    'fix_059 回滚备份：被回标为 backfill 的 (exchange, symbol, ts) 及其原 source。';

-- 3. 回标
UPDATE biz.oi_cvd_snapshot s
SET source = 'backfill'
FROM biz.oi_cvd_hourly_backfill_20260921 g
WHERE s.exchange = g.exchange
  AND s.symbol = g.symbol
  AND date_trunc('hour', s.ts) = g.h
  AND s.source = 'realtime';

-- 4. 校验：停摆窗口内不应再有「1h 回填签名但标 realtime」的行
DO $$
DECLARE
    bad BIGINT;
BEGIN
    SELECT COUNT(*) INTO bad
    FROM biz.oi_cvd_snapshot s
    JOIN biz.oi_cvd_hourly_backfill_20260921 g
      ON g.exchange = s.exchange AND g.symbol = s.symbol
     AND g.h = date_trunc('hour', s.ts)
    WHERE s.source = 'realtime';
    IF bad <> 0 THEN
        RAISE EXCEPTION 'fix_059 回标不完整：仍有 % 行回填签名被标 realtime', bad;
    END IF;
    RAISE NOTICE 'fix_059 校验通过：回填签名桶已全部标为 backfill';
END $$;