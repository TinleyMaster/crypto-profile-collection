-- TGE / 融资轮次结构化数据表
-- 来源：DefiLlama /protocol/{slug} 的 raises 字段（融资轮次，无稳定 URL）
-- 映射：defillamaId -> src_dl.protocol_list.protocol_id -> core.asset_source_map -> asset_id

CREATE TABLE IF NOT EXISTS biz.asset_raises (
    id              BIGSERIAL PRIMARY KEY,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    defillama_id    TEXT,                          -- DefiLlama 协议 id（protocol_list.protocol_id）
    protocol_name   TEXT,                          -- 协议名称
    round           TEXT,                          -- 融资轮次（如 Private token sale）
    raise_date      DATE,                          -- 融资日期
    amount          NUMERIC,                       -- 融资金额（DefiLlama 原始数值，单位见其返回）
    chains          TEXT[],                        -- 涉及链
    sector          TEXT,                          -- 赛道
    category        TEXT,                          -- 分类
    lead_investors  TEXT[],                        -- 领投方
    other_investors TEXT[],                        -- 其他投资方
    valuation       NUMERIC,                       -- 估值
    source          TEXT,                          -- 原始来源 URL（DefiLlama raises.source 常为空）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (asset_id, round, raise_date)
);

COMMENT ON TABLE biz.asset_raises IS
    'TGE / 融资轮次结构化数据。来自 DefiLlama /protocol/{slug} 的 raises 字段，按协议映射到资产。';

COMMENT ON COLUMN biz.asset_raises.amount IS
    'DefiLlama raises.amount 原始数值，未做单位换算（DefiLlama 返回口径以百万美元计）。';
