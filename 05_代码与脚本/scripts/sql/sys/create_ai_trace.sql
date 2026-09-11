-- sys.ai_trace 表：AI 调用追溯日志持久化
-- 替代原来的 workbench/output/ai_trace/*.jsonl 文件方案
-- 特点：
--   - 存储完整的 LLM 调用上下文（system_prompt / user_prompt / response / thinking）
--   - 支持按 tag / asset_id / 日期 快速检索
--   - 大文本字段（prompt/response/thinking）存 TEXT，便于全文检索
--   - signal_types 用 TEXT[] 数组，便于按信号类型过滤

CREATE TABLE IF NOT EXISTS sys.ai_trace (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),   -- 调用时间
    tag             VARCHAR(50) NOT NULL,                 -- 标签：signal_v2 / ...
    asset_id        INT,                                  -- 关联资产 ID
    symbol          VARCHAR(50),                          -- 代币符号
    signal_types    TEXT[] NOT NULL DEFAULT '{}',         -- 触发的信号类型
    provider        VARCHAR(50),                          -- LLM 提供商
    model           VARCHAR(100),                         -- 模型名
    system_prompt   TEXT NOT NULL DEFAULT '',             -- 系统提示词
    user_prompt     TEXT NOT NULL DEFAULT '',             -- 用户提示词
    raw_response    TEXT NOT NULL DEFAULT '',             -- 原始响应
    thinking_content TEXT,                                 -- 思考过程（推理内容）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_trace_tag_ts ON sys.ai_trace(tag, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ai_trace_asset_id ON sys.ai_trace(asset_id);
CREATE INDEX IF NOT EXISTS idx_ai_trace_symbol ON sys.ai_trace(symbol);
CREATE INDEX IF NOT EXISTS idx_ai_trace_signal_types ON sys.ai_trace USING GIN(signal_types);
