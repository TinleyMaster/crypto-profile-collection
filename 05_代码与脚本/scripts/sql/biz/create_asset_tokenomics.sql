-- 代币经济学结构化数据表
-- 多源聚合（官网/白皮书/Docs + CMC/CG API）→ LLM 提取合并 → 入库

CREATE TABLE IF NOT EXISTS biz.asset_tokenomics (
    id                BIGSERIAL PRIMARY KEY,
    asset_id          BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    source_urls       TEXT[],                        -- 数据来源 URL 列表
    total_supply      NUMERIC,                       -- 总量
    max_supply        NUMERIC,                       -- 最大供应（如有上限）
    circulating_supply NUMERIC,                      -- 流通量（来自 API）
    buy_tax_pct       NUMERIC(5,2),                  -- 买入税率 %
    sell_tax_pct      NUMERIC(5,2),                  -- 卖出税率 %
    tax_info          TEXT,                          -- 税率说明（自由文本）
    contract_renounced BOOLEAN,                      -- 合约是否已放弃
    lp_locked         BOOLEAN,                       -- 流动性是否锁定
    lp_lock_info      TEXT,                          -- 流动性锁定说明
    allocation_json   JSONB,                         -- 分配明细 [{"category":"Team","pct":5},...]
    burn_info         TEXT,                          -- 销毁说明
    emission_schedule TEXT,                          -- 释放/解锁/vesting计划
    inflation_info    TEXT,                          -- 通胀/通缩说明
    governance_info   TEXT,                          -- 治理代币说明
    utility_info      TEXT,                          -- 代币用途说明
    raw_text          TEXT,                          -- 原始提取文本（用于校验）
    extracted_by      VARCHAR(32) NOT NULL DEFAULT 'llm',  -- 提取方式: llm / manual
    confidence        NUMERIC(3,2),                  -- 置信度 0.00~1.00
    extraction_notes  TEXT,                          -- 提取备注（冲突、缺失等）
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (asset_id)                               -- 每个资产一条记录
);

COMMENT ON TABLE biz.asset_tokenomics IS
    '代币经济学结构化数据。多源聚合（官网/白皮书/Docs + API）经 LLM 提取合并后入库。';

COMMENT ON COLUMN biz.asset_tokenomics.source_urls IS
    '本次提取参考的所有文档 URL 列表';
COMMENT ON COLUMN biz.asset_tokenomics.confidence IS
    'LLM 提取的整体置信度 0.00~1.00，综合评估数据完整性和来源可靠性';
COMMENT ON COLUMN biz.asset_tokenomics.extraction_notes IS
    '提取过程中的备注：数据冲突、字段缺失、来源标注等';