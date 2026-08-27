-- DefiLlama 协议处理标记表
-- 用于记录「已拉取过 /protocol/{slug} 并提取 raises」的协议，
-- 解决 raises 为空的协议不产生 biz.asset_raises 记录、无法断点续跑的问题。

CREATE TABLE IF NOT EXISTS biz.dl_protocol_checked (
    protocol_id TEXT PRIMARY KEY,               -- src_dl.protocol_list.protocol_id
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.dl_protocol_checked IS
    'DefiLlama 协议处理标记。记录已提取过 raises 的协议，供断点续跑。';
