-- =====================================================================
-- G1 分级新增「发布前启动程度」惩罚项（prelaunch_ret_24h / prelaunch_penalty）
-- 实测（2026-09-18，741 个 L1 精确样本）：
--   发布前 24h 已涨 >10% 的催化剂，72h 超额收益显著为负
--     （10~20% 组中位 -16.4%、≥5% 占比 0%；>20% 组同样为负）
--   含义：信息已被市场定价，发布后无反应空间，追高风险大。
--   注意：整体预测力弱（Spearman 0.076），仅作为扣分惩罚项，不参与四维加权。
-- 编号：fix_050
-- 日期：2026-09-18
-- =====================================================================

ALTER TABLE biz.catalyst_grade
    ADD COLUMN IF NOT EXISTS prelaunch_ret_24h DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS prelaunch_penalty SMALLINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN biz.catalyst_grade.prelaunch_ret_24h
    IS '发布前 24h 价格变化 %（发布时刻 vs 发布前24h，取锚定资产）NULL=无K线数据';
COMMENT ON COLUMN biz.catalyst_grade.prelaunch_penalty
    IS '追高惩罚分（>10% 扣 8 分，<=10% 或数据不足 0 分）';
