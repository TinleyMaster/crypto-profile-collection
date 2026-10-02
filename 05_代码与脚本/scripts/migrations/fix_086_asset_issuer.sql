-- ============================================================
-- fix_086_asset_issuer.sql
-- 代币 → 发行方/公司映射表（代币关系图谱第四维「同一家公司」）
--   1. biz.asset_issuer  asset_id → issuer（人工梳理种子）
-- ============================================================
-- 背景：代币联动研究 → 关系分组（赛道/公链/产业链之外，补上「同一公司」）。
-- 发行方归属多为人工可核实的事实（Circle→USDC、Lido→LDO/wstETH/stETH…），
-- 不适合从公开 API 自动抓，故用人工种子脚本维护：
--   scripts/bin/seed_asset_issuer.py   （可重复执行 upsert）
-- token_relation_graph.prepare 读本表作为 'issuer' 维度参与组内相关度与回测；
-- 通过稳定性回测的 issuer 组会随 refresh_stable_groups 写入 biz.stable_linkage_group
-- （dim='issuer'），从而进入扫描归因的「同稳定组可能联动」候选。
-- 注：稳定币虽也映射（Circle/Tether 等），但分析宇宙会排除稳定币，其组在
-- 相关度/回测中自然为空，不影响结论。
-- ============================================================

CREATE TABLE IF NOT EXISTS biz.asset_issuer (
    asset_id    BIGINT      NOT NULL PRIMARY KEY REFERENCES core.asset(asset_id),
    issuer      TEXT        NOT NULL,   -- 发行方/公司（规范化名称，如 'Lido'）
    source      TEXT        NOT NULL DEFAULT 'manual',  -- 来源：manual（人工种子）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.asset_issuer IS
    '代币→发行方/公司映射（关系图谱第四维）：由 seed_asset_issuer.py 人工种子维护，参与 issuer 维度相关度与回测';
COMMENT ON COLUMN biz.asset_issuer.issuer IS '发行方/公司规范化名称，作为关系分组名';
COMMENT ON COLUMN biz.asset_issuer.source IS '来源标记：manual=人工种子脚本';
