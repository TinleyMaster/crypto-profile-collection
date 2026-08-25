-- ============================================================
-- KOL 信号表迁移：新增 noise 类型 + 支撑/压力位字段
-- 对应 classifier.py v2 升级
-- 执行时间：预计 < 1 秒（仅改约束和加列，无数据重写）
-- ============================================================

-- 1. 修改 post_type CHECK 约束，新增 noise
--    策略：先删旧约束，再加新约束（PostgreSQL 不支持 ALTER CHECK）
ALTER TABLE biz.kol_signal
    DROP CONSTRAINT IF EXISTS chk_kol_signal_post_type;

ALTER TABLE biz.kol_signal
    ADD CONSTRAINT chk_kol_signal_post_type
        CHECK (post_type IN ('prediction', 'after_action', 'analysis', 'noise'));

-- 2. 新增支撑位 / 压力位列
ALTER TABLE biz.kol_signal
    ADD COLUMN IF NOT EXISTS support_level     NUMERIC(20,8),
    ADD COLUMN IF NOT EXISTS resistance_level  NUMERIC(20,8);

-- 3. 更新注释
COMMENT ON COLUMN biz.kol_signal.post_type
    IS '帖子类型：prediction/after_action/analysis/noise';
COMMENT ON COLUMN biz.kol_signal.support_level
    IS '支撑位价格（帖子中提到的关键支撑）';
COMMENT ON COLUMN biz.kol_signal.resistance_level
    IS '压力位价格（帖子中提到的关键压力/阻力）';

-- 4. 为新增字段建索引（加速按支撑/压力位筛选的场景，可选）
CREATE INDEX IF NOT EXISTS idx_kol_signal_support_level
    ON biz.kol_signal (support_level)
    WHERE support_level IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kol_signal_resistance_level
    ON biz.kol_signal (resistance_level)
    WHERE resistance_level IS NOT NULL;

-- 5. 验证
--    执行后可运行以下查询确认：
--    SELECT column_name, data_type FROM information_schema.columns
--    WHERE table_schema = 'biz' AND table_name = 'kol_signal'
--    AND column_name IN ('support_level', 'resistance_level');
--
--    SELECT conname, consrc FROM pg_constraint
--    WHERE conname = 'chk_kol_signal_post_type';
