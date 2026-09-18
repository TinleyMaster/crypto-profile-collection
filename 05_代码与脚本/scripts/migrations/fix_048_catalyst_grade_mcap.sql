-- =====================================================================
-- G1 分级新增市值因子（mcap_score）
-- 实测（2026-09-18 画像）：小市值(<10亿)催化剂强影响占比 30-50%，
--   大市值(>50亿)仅 3%——市值是催化剂影响的首要因子。
-- base_strength 升级为四维：authority×0.30 + event×0.35 + scope×0.15 + mcap×0.20
-- 编号：fix_048
-- 日期：2026-09-18
-- =====================================================================

ALTER TABLE biz.catalyst_grade
    ADD COLUMN IF NOT EXISTS mcap_score SMALLINT NOT NULL DEFAULT 50;

COMMENT ON COLUMN biz.catalyst_grade.mcap_score
    IS '市值因子分 0-100（关联资产最小市值档：<1亿=90 / 1-10亿=80 / 10-50亿=60 / >50亿=40 / 未知=50）';
