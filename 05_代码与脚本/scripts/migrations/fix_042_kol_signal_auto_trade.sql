-- KOL 信号自动开单：DB 驱动改造
-- 1) biz.kol_signal 新增 is_auto_traded（自动开单是否已处理，与 KOL 模块的 is_alerted 相互独立）
-- 2) biz.kol_auto_trade_log 新增 kol_signal_id（关联源信号，替代邮件 Message-ID 去重）
-- 幂等，可重复执行。

ALTER TABLE biz.kol_signal
    ADD COLUMN IF NOT EXISTS is_auto_traded BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_kol_signal_auto_trade_pending
    ON biz.kol_signal (signal_id)
    WHERE post_type = 'prediction' AND is_auto_traded = FALSE;

ALTER TABLE biz.kol_auto_trade_log
    ADD COLUMN IF NOT EXISTS kol_signal_id BIGINT;
