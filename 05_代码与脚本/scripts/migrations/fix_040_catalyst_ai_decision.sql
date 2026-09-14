-- =====================================================================
-- 催化剂 AI 决策增强（G7）
-- 功能：为 open 信号补全「AI 推荐原因 + 投资周期」
--       目标价(已有 take_profit)/止损(已有 stop_loss) 用作 AI 评审基线
-- 编号：fix_040
-- 日期：2026-09-14
-- =====================================================================

ALTER TABLE biz.catalyst_signal
    ADD COLUMN IF NOT EXISTS ai_reason TEXT;

COMMENT ON COLUMN biz.catalyst_signal.ai_reason
    IS 'AI 推荐原因（G7 慢通道 LLM 生成）：催化剂驱动力 + 上行逻辑 + 主要风险';

ALTER TABLE biz.catalyst_signal
    ADD COLUMN IF NOT EXISTS investment_cycle TEXT
    CHECK (investment_cycle IN ('短期', '中期', '长期') OR investment_cycle IS NULL);

COMMENT ON COLUMN biz.catalyst_signal.investment_cycle
    IS 'AI 推荐投资周期（G7 慢通道 LLM 生成）：短期/中期/长期';