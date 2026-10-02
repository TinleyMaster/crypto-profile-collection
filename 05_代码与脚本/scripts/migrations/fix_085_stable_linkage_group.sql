-- ============================================================
-- fix_085_stable_linkage_group.sql
-- 稳定联动组名单（扫描归因「同稳定组可能联动」的数据源）
--   1. biz.stable_linkage_group  回测验证的高相关组（周任务刷新）
-- ============================================================
-- 背景：代币联动研究 → 关系分组 → 回测（workbench/backtest_group_linkage.py）
-- 只把「组内相关首/后半段均超全市场基线」的组记为稳定联动组。
-- catalyst_attribution.linkage_peers 实时读本表给告警打「同稳定组可能联动」；
-- 表空/缺表时回退到代码内置的 VERIFIED_STABLE_GROUPS 默认名单。
-- ============================================================

CREATE TABLE IF NOT EXISTS biz.stable_linkage_group (
    dim         TEXT        NOT NULL,   -- narrative / chain / sector
    group_name  TEXT        NOT NULL,   -- 如 'Layer 2' / 'tron' / 'depin'
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- 最近一次回测验证时间
    detail      JSONB,                  -- 可选：组内相关首/后段指标快照
    PRIMARY KEY (dim, group_name)
);

COMMENT ON TABLE biz.stable_linkage_group IS
    '稳定联动组名单（回测验证的高相关组）：扫描归因「同稳定组可能联动」的数据源，由 refresh_stable_groups.py 每周刷新';
COMMENT ON COLUMN biz.stable_linkage_group.dim IS
    '关系维度：narrative(产业链叙事) / chain(公链) / sector(赛道)';
COMMENT ON COLUMN biz.stable_linkage_group.verified_at IS
    '最近一次回测验证时间（周任务刷新后更新）';
