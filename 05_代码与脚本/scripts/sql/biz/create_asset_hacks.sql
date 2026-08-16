-- 链上异常事件结构化数据表
-- 来源：DefiLlama /hacks 接口（攻击/漏洞事件列表，source 字段全为空，无稳定 URL）
-- 映射：defillamaId -> src_dl.protocol_list.protocol_id -> core.asset_source_map -> asset_id

CREATE TABLE IF NOT EXISTS biz.asset_hacks (
    id             BIGSERIAL PRIMARY KEY,
    asset_id       BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    defillama_id   TEXT,                          -- DefiLlama 协议 id（protocol_list.protocol_id）
    name           TEXT,                          -- 事件/项目名
    technique      TEXT,                          -- 攻击手法
    amount         NUMERIC,                       -- 损失金额（USD）
    returned_funds NUMERIC,                       -- 追回金额（USD）
    chain          TEXT[],                        -- 涉及链
    target_type    TEXT,                          -- 目标类型
    classification TEXT,                          -- 分类
    bridge_hack    BOOLEAN,                       -- 是否跨链桥攻击
    hack_date      DATE,                          -- 事件日期
    source         TEXT,                          -- 原始来源 URL（DefiLlama hacks.source 全为空）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (asset_id, name, hack_date)
);

COMMENT ON TABLE biz.asset_hacks IS
    '链上异常事件结构化数据。来自 DefiLlama /hacks 接口，按 defillamaId 映射到资产。';

COMMENT ON COLUMN biz.asset_hacks.amount IS
    '损失金额，单位 USD（DefiLlama /hacks amount 字段）。';
