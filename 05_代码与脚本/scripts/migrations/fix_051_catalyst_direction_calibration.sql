-- =====================================================================
-- event_type 方向映射实测校正回填（方向校验，2026-09-18）
-- 依据 verify_event_direction.py：72h 扣 BTC 超额收益实测
--   tech_upgrade: bullish → bearish（n=47, 中位 -0.55%, 涨占比 0.40）
--   funding:      bullish → neutral（n=28, 中位 -0.78%, 涨占比 0.32，样本不足）
--   burn:         bullish → neutral（n=14, 中位 -0.77%, 涨占比 0.43，样本不足）
-- 仅回填 direction_src='event_type' 的行（即 catalyst_impact 无方向、走默认映射的）；
-- impact 源（AI 给出的方向）不受影响。
-- hit 语义与 collect_catalyst_outcome.hit_of() 一致：
--   方向 neutral/null → hit=NULL（不参与命中率）；bullish → excess>0；bearish → excess<0
-- 编号：fix_051
-- 日期：2026-09-18
-- =====================================================================

-- 1. 先备份受影响行的当前判定（便于回滚核对）
CREATE TABLE IF NOT EXISTS biz.catalyst_outcome_dir_backup_20260918 AS
SELECT co.outcome_id, co.catalyst_id, co.asset_id, co.impact_direction, co.direction_src,
       co.hit_4h, co.hit_24h, co.hit_72h, co.hit_7d
FROM biz.catalyst_outcome co
JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
WHERE co.direction_src = 'event_type'
  AND COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') IN ('tech_upgrade', 'funding', 'burn');

COMMENT ON TABLE biz.catalyst_outcome_dir_backup_20260918
    IS '方向映射校正前备份（fix_051）：便于回滚核对，确认无误后可删除';

-- 2. 重算 impact_direction + hit_*
WITH newdir AS (
    SELECT co.outcome_id AS oid,
           CASE COALESCE(ac.ai_event_type, ac.rule_event_type, 'other')
                WHEN 'tech_upgrade' THEN 'bearish'
                ELSE 'neutral'          -- funding / burn
           END AS new_dir
    FROM biz.catalyst_outcome co
    JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
    WHERE co.direction_src = 'event_type'
      AND COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') IN ('tech_upgrade', 'funding', 'burn')
)
UPDATE biz.catalyst_outcome co
SET impact_direction = n.new_dir,
    hit_4h  = CASE WHEN n.new_dir = 'neutral' OR co.excess_4h  IS NULL THEN NULL
                   WHEN n.new_dir = 'bullish' THEN co.excess_4h  > 0 ELSE co.excess_4h  < 0 END,
    hit_24h = CASE WHEN n.new_dir = 'neutral' OR co.excess_24h IS NULL THEN NULL
                   WHEN n.new_dir = 'bullish' THEN co.excess_24h > 0 ELSE co.excess_24h < 0 END,
    hit_72h = CASE WHEN n.new_dir = 'neutral' OR co.excess_72h IS NULL THEN NULL
                   WHEN n.new_dir = 'bullish' THEN co.excess_72h > 0 ELSE co.excess_72h < 0 END,
    hit_7d  = CASE WHEN n.new_dir = 'neutral' OR co.excess_7d  IS NULL THEN NULL
                   WHEN n.new_dir = 'bullish' THEN co.excess_7d  > 0 ELSE co.excess_7d  < 0 END,
    updated_at = NOW()
FROM newdir n
WHERE co.outcome_id = n.oid;