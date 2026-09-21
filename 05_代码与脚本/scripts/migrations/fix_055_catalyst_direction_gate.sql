-- =====================================================================
-- 催化剂信号：方向闸门（d6）存量收敛
-- 编号：fix_055
-- 日期：2026-09-21
--
-- 背景（审计 P0-2 / P1-1 / P2-2）：
--   本系统档位是「做多」口径，但 catalyst_impact.impact_direction / ai_sentiment
--   此前只参与共振打分（G2），不参与档位与动作判定，导致：
--     P0-2  利空新闻（bearish）被公式合成 A/B 级做多推送
--           实测 ZEC：正文实为「Zcash 涨6% / XRP 跌7% / Clarity Act 受阻」的
--           行情综述，被 rule_event_type 误判为 listing（事件权重 95）。
--     P1-1  中性方向（neutral）合成出 A 级档位
--           实测 ZEC：impact_direction=neutral 却 composite 89 / tier A。
--     P2-2  二阶传导信号 tier 上限 C 只在创建路径生效，重算路径漏加
--           实测近 14 天二阶信号滞留 B 级 2,558 条。
--
-- 新的方向闸门（落在 CatalystSignalBuilder.build()，所有调用方共用）：
--   bearish → status='invalid'（利空不产做多机会；tier/composite 保留供回测）
--   neutral → tier 封顶 C（中性方向不占 A/B 推送位；信号与档位保留）
--   二阶信号（catalyst_second_order 命中）→ tier 封顶 C（间接关联，不占 A/B）
--
-- 与 RR 闸门同属「只降级不改分」的显式例外：composite_score 仍为 tier 唯一真源，
-- 但 tier 允许被这两类闸门降级（见 signal.py 模块 docstring）。
--
-- 存量规模（近 14 天实测）：bearish tier A=9 / B=101 / C=226；
--   neutral tier A=41 / B=412 / C=895；二阶信号 tier=B=2,558。
--
-- 收敛口径：
--   1) bearish → status='invalid'，不改 tier/composite（保留供回测）
--   2) neutral → tier='C'，不改 composite/status
--   3) 二阶 → tier='C'，不改 composite/status
--   方向取值与代码一致：COALESCE(ci.impact_direction, ac.ai_sentiment)，LOWER+TRIM 归一。
--   终态 expired/done 冻结不改（与 upsert_to_db 的状态迁移规则一致）；
--   已 invalid 的行不再改（避免 updated_at 无谓抖动，updated_at 被「信号滞后」口径引用）。
--
-- 幂等性：三段 UPDATE 均为「条件命中才改」，重复执行不再命中；
--   备份表 IF NOT EXISTS。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. 备份受影响行（便于回滚核对，确认无误后可删除）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.catalyst_signal_direction_backup_20260921 AS
SELECT cs.signal_id,
       cs.catalyst_id,
       cs.asset_id,
       cs.status AS old_status,
       cs.tier   AS old_tier,
       LOWER(TRIM(COALESCE(ci.impact_direction, ac.ai_sentiment))) AS direction,
       (cso.catalyst_id IS NOT NULL) AS is_second_order
FROM biz.catalyst_signal cs
JOIN biz.asset_catalyst ac
  ON ac.catalyst_id = cs.catalyst_id
LEFT JOIN biz.catalyst_impact ci
  ON ci.catalyst_id = cs.catalyst_id AND ci.asset_id = cs.asset_id
LEFT JOIN biz.catalyst_second_order cso
  ON cso.catalyst_id = cs.catalyst_id AND cso.asset_id = cs.asset_id
WHERE cs.status NOT IN ('expired', 'done')
  AND (
        LOWER(TRIM(COALESCE(ci.impact_direction, ac.ai_sentiment))) IN ('bearish', 'neutral')
     OR (cso.catalyst_id IS NOT NULL AND cs.tier IN ('A', 'B'))
  );

COMMENT ON TABLE biz.catalyst_signal_direction_backup_20260921
    IS '方向闸门(d6)收敛前备份（fix_055）：便于回滚核对，确认无误后可删除';

-- ---------------------------------------------------------------------
-- 1. bearish → status='invalid'（利空不产做多机会）
-- ---------------------------------------------------------------------
WITH dir AS (
    SELECT cs.signal_id,
           LOWER(TRIM(COALESCE(ci.impact_direction, ac.ai_sentiment))) AS direction
    FROM biz.catalyst_signal cs
    JOIN biz.asset_catalyst ac
      ON ac.catalyst_id = cs.catalyst_id
    LEFT JOIN biz.catalyst_impact ci
      ON ci.catalyst_id = cs.catalyst_id AND ci.asset_id = cs.asset_id
)
UPDATE biz.catalyst_signal cs
SET status = 'invalid',
    updated_at = NOW()
FROM dir d
WHERE cs.signal_id = d.signal_id
  AND d.direction = 'bearish'
  AND cs.status NOT IN ('invalid', 'expired', 'done');

-- ---------------------------------------------------------------------
-- 2. neutral → tier='C'（中性方向不占 A/B 推送位）
-- ---------------------------------------------------------------------
WITH dir AS (
    SELECT cs.signal_id,
           LOWER(TRIM(COALESCE(ci.impact_direction, ac.ai_sentiment))) AS direction
    FROM biz.catalyst_signal cs
    JOIN biz.asset_catalyst ac
      ON ac.catalyst_id = cs.catalyst_id
    LEFT JOIN biz.catalyst_impact ci
      ON ci.catalyst_id = cs.catalyst_id AND ci.asset_id = cs.asset_id
)
UPDATE biz.catalyst_signal cs
SET tier = 'C',
    updated_at = NOW()
FROM dir d
WHERE cs.signal_id = d.signal_id
  AND d.direction = 'neutral'
  AND cs.tier IN ('A', 'B')
  AND cs.status NOT IN ('expired', 'done');

-- ---------------------------------------------------------------------
-- 3. 二阶传导信号 → tier='C'（P2-2 对齐创建路径 run_slow_second_order）
-- ---------------------------------------------------------------------
UPDATE biz.catalyst_signal cs
SET tier = 'C',
    updated_at = NOW()
WHERE cs.tier IN ('A', 'B')
  AND cs.status IN ('open', 'watch', 'invalid')
  AND EXISTS (
      SELECT 1 FROM biz.catalyst_second_order cso
      WHERE cso.catalyst_id = cs.catalyst_id
        AND cso.asset_id = cs.asset_id
  );