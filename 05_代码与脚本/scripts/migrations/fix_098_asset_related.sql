-- FEAT-RELATED-001: 关联资产图谱表
-- 用途：投研时由 AI 标注「目标代币 ↔ 其他代币」的关联关系（同赛道/同生态/同叙事/上下游/共同投资方/同ETF篮子等），
--       为后期相关性因子回测与「叙事传导 → 联动代币推测」提供结构化底座。
-- 分工：AI 负责判断「谁相关、什么关系、强度、传导逻辑」；本表只存结构化事实。
-- 幂等：按 (asset_id, related_symbol, relation_type) upsert，每次投研更新最新版本。

CREATE TABLE IF NOT EXISTS biz.asset_related (
    id                BIGSERIAL PRIMARY KEY,
    asset_id          BIGINT NOT NULL,          -- 主资产（本次投研标的）
    related_asset_id  BIGINT,                   -- 关联资产 core.asset.asset_id（未收录则 NULL）
    related_symbol    TEXT NOT NULL,            -- 关联资产符号（必填，如 GRASS）
    related_name      TEXT,                     -- 关联资产全名
    relation_type     TEXT NOT NULL,            -- 关系类型（枚举见下）
    relation_direction TEXT DEFAULT 'bidirectional',  -- 传导方向：bidirectional双向 / spillover溢出(主→关) / hedge反向 / source_only(关→主)
    strength          TEXT NOT NULL DEFAULT 'medium',  -- high / medium / low
    confidence        NUMERIC(3,2),             -- AI 判断置信度 0-1
    rationale         TEXT,                     -- 传导逻辑（为什么相关、叙事如何传导、可观测联动指标）
    source            TEXT DEFAULT 'ai_analysis',
    as_of_date        DATE NOT NULL DEFAULT CURRENT_DATE,  -- 投研标注日期
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (asset_id, related_symbol, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_asset_related_asset    ON biz.asset_related(asset_id);
CREATE INDEX IF NOT EXISTS idx_asset_related_sym      ON biz.asset_related(related_symbol);
CREATE INDEX IF NOT EXISTS idx_asset_related_rel_asset ON biz.asset_related(related_asset_id);
CREATE INDEX IF NOT EXISTS idx_asset_related_type     ON biz.asset_related(relation_type);

-- 关系类型约束（可扩展）
ALTER TABLE biz.asset_related DROP CONSTRAINT IF EXISTS ck_asset_related_type;
ALTER TABLE biz.asset_related ADD CONSTRAINT ck_asset_related_type CHECK (
    relation_type IN (
        'sector_peer',          -- 同赛道竞品（如 HYPE ↔ dYdX/GMX，perp DEX）
        'ecosystem',            -- 同生态/同链（如 HYPE ↔ HyperEVM 上代币）
        'narrative',            -- 同叙事/同板块（如 ZEC ↔ XMR 隐私）
        'upstream_downstream',  -- 上下游依赖（如质押层 ↔ 底层链）
        'funding_shared',       -- 共同投资方/资本关联
        'etf_basket',           -- 同 ETF 篮子/同批上市
        'cross_chain',          -- 跨链/封装/桥接映射
        'fork_derivative',      -- 分叉/衍生
        'infra_dependency',     -- 基础设施依赖（如 L2 ↔ L1 gas）
        'other'
    )
);

COMMENT ON TABLE biz.asset_related IS '关联资产图谱（FEAT-RELATED-001）。投研时 AI 标注目标代币与其他代币的关联关系，支撑相关性因子回测与叙事传导推测。';
COMMENT ON COLUMN biz.asset_related.relation_type IS '关系类型：sector_peer同赛道竞品 / ecosystem同生态 / narrative同叙事 / upstream_downstream上下游 / funding_shared共同投资方 / etf_basket同ETF篮子 / cross_chain跨链 / fork_derivative分叉衍生 / infra_dependency基础设施依赖 / other';
COMMENT ON COLUMN biz.asset_related.relation_direction IS '传导方向：bidirectional双向联动 / spillover主资产叙事溢出到关联方 / hedge反向对冲 / source_only关联方反哺主资产';
COMMENT ON COLUMN biz.asset_related.rationale IS 'AI 判断的传导逻辑：为什么相关、叙事如何传导、建议可观测的联动指标（含数据来源）';
