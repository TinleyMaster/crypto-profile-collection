-- 白皮书结构化摘要表
-- 从白皮书 PDF 经 LLM 提取关键信息，供投研快速查阅
-- 每个 doc_asset（白皮书文档）对应一条摘要

CREATE TABLE IF NOT EXISTS biz.doc_whitepaper_summary (
    id                BIGSERIAL PRIMARY KEY,
    doc_id            BIGINT NOT NULL REFERENCES biz.doc_asset(doc_id) ON DELETE CASCADE,
    asset_id          BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,

    -- ── 核心信息 ──
    one_liner         TEXT,                           -- 一句话简介（15字以内）
    summary_short     TEXT,                           -- 简短摘要（100字以内）
    summary_long      TEXT,                           -- 详细摘要（500字以内）

    -- ── 问题与方案 ──
    problem_statement TEXT,                           -- 项目要解决的核心问题
    solution          TEXT,                           -- 解决方案概述

    -- ── 核心机制 ──
    core_mechanism    TEXT,                           -- 核心技术/经济机制
    key_innovations   TEXT[],                         -- 关键创新点列表
    tech_stack        TEXT[],                         -- 技术栈/协议标准

    -- ── 代币经济 ──
    token_utility     TEXT,                           -- 代币用途/价值捕获
    tokenomics_notes  TEXT,                           -- 代币经济补充说明

    -- ── 团队与融资 ──
    team_info         TEXT,                           -- 核心团队信息
    investors         TEXT[],                         -- 投资方/融资方列表
    funding_info      TEXT,                           -- 融资历史/金额

    -- ── 路线图与里程碑 ──
    roadmap           TEXT,                           -- 路线图概述
    key_milestones    TEXT[],                         -- 关键里程碑列表

    -- ── 风险与挑战 ──
    risks             TEXT[],                         -- 项目风险列表
    challenges        TEXT,                           -- 面临的挑战

    -- ── 元数据 ──
    source_pages      TEXT[],                         -- 信息来源页码（如 ["p3", "p7-9"]）
    raw_text          TEXT,                           -- 原始提取文本（用于校验）
    extracted_by      VARCHAR(32) NOT NULL DEFAULT 'llm',  -- 提取方式: llm / manual
    confidence        NUMERIC(3,2),                   -- 置信度 0.00~1.00
    extraction_notes  TEXT,                           -- 提取备注（缺失、不确定等）

    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (doc_id)                                   -- 每个文档一条摘要
);

CREATE INDEX IF NOT EXISTS idx_whitepaper_summary_asset ON biz.doc_whitepaper_summary(asset_id);

COMMENT ON TABLE biz.doc_whitepaper_summary IS
    '白皮书结构化摘要。从白皮书 PDF 经 LLM 提取关键信息，供投研快速查阅。';
COMMENT ON COLUMN biz.doc_whitepaper_summary.one_liner IS
    '一句话简介，15字以内';
COMMENT ON COLUMN biz.doc_whitepaper_summary.confidence IS
    'LLM 提取的整体置信度 0.00~1.00';
