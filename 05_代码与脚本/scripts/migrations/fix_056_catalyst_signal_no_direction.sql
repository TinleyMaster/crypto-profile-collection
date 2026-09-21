-- =====================================================================
-- d6 方向闸门扩展：方向缺失（NULL/未知）同样不占 A/B 推送位（2026-09-21）
--
-- 背景：部署验收发现非终态 45 条「无方向」行中 8 条进了 tier B —— 快通道产信号
--   时 catalyst_impact.impact_direction 与 asset_catalyst.ai_sentiment 都还是 NULL，
--   而 d6 闸门只认显式的 neutral/bearish，NULL 被直接放行；这批行方向永缺
--   （不会被后续 AI 结果修正），且样本正是 regulation / other /「24h 涨幅播报」
--   等实测中位超额为负的类别（-0.78% / -2.12%）。
--
-- 口径：只有显式 bullish 保留 A/B；neutral / 方向缺失 / 未知值 一律 tier 封顶 C。
--   bearish → status='invalid' 已由 fix_055 收敛（当前非终态残留 0 行），本迁移不重复。
--
-- 约定：幂等；expired/done 终态冻结不改；已为 C 的行不动（避免 updated_at 抖动，
--   该列被「信号滞后」口径引用）。
-- 编号：fix_056
-- 日期：2026-09-21
-- =====================================================================

-- 1. 备份受影响行（便于回滚核对，确认无误后可删）
CREATE TABLE IF NOT EXISTS biz.catalyst_signal_nodir_backup_20260921 AS
SELECT cs.signal_id, cs.catalyst_id, cs.asset_id, cs.tier, cs.status,
       cs.composite_score, cs.created_at
FROM biz.catalyst_signal cs
JOIN biz.asset_catalyst ac ON ac.catalyst_id = cs.catalyst_id
LEFT JOIN biz.catalyst_impact ci
       ON cs.catalyst_id = ci.catalyst_id AND cs.asset_id = ci.asset_id
WHERE cs.tier IN ('A', 'B')
  AND cs.status NOT IN ('expired', 'done')
  AND LOWER(TRIM(COALESCE(ci.impact_direction, ac.ai_sentiment, '')))
      NOT IN ('bullish', 'bearish', 'neutral');

COMMENT ON TABLE biz.catalyst_signal_nodir_backup_20260921
    IS '方向缺失封顶 C 前备份（fix_056）：便于回滚核对，确认无误后可删除';

-- 2. 收敛：方向非 bullish（含 NULL/未知值）→ tier 封顶 C
WITH targets AS (
    SELECT cs.signal_id
    FROM biz.catalyst_signal cs
    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cs.catalyst_id
    LEFT JOIN biz.catalyst_impact ci
           ON cs.catalyst_id = ci.catalyst_id AND cs.asset_id = ci.asset_id
    WHERE cs.tier IN ('A', 'B')
      AND cs.status NOT IN ('expired', 'done')
      AND LOWER(TRIM(COALESCE(ci.impact_direction, ac.ai_sentiment, '')))
          NOT IN ('bullish', 'bearish', 'neutral')
)
UPDATE biz.catalyst_signal cs
SET tier = 'C',
    updated_at = NOW()
FROM targets t
WHERE cs.signal_id = t.signal_id;