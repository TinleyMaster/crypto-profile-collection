-- fix_110: 高确定性机会 P0 增强（工单 SKILL-HC-001，幂等）
--
-- 两块改动：
--   ① biz.asset_price_elasticity 增列 by_impact —— 按催化剂影响强度（strong/medium/weak/
--      unknown，源 biz.catalyst_impact.impact_strength）分层的 AR72 统计，回答"哪一档消息
--      对它有效"，避免弱噪音事件（AMA/转发/次要合作）拉高事件数造成误判。
--   ② 重建物化视图 biz.mv_asset_screen —— 追加统计显著性与高确定性预筛列：
--        e_stat_valid  = 弹性样本 n ≥ 10（= asset_price_elasticity.MIN_N）
--        u_stat_valid  = 解锁样本 n ≥ 5
--        hc_prefilter  = 高确定性预筛（统计有效 + 高弹性 + 资金流入 + 解锁非崩 + 无大额解锁）
--      ⇒ 让看板/SQL 能直接筛掉小样本噪音，不必在应用层重算判定。
--
-- 口径（与 bin/asset_screen.py 必须两边同步；沿用 fix_105 词表约束）：
--   弹性 P1.6 高 = {高弹性, 中高弹性} / 低 = {低弹性, 负弹性}
--   解锁 P1.7 抗 = {抗解禁型, 偏抗解禁} / 崩 = {偏解禁即崩, 解禁即崩}
--   资金流 P1.8 in = 资金流入
--   ⚠️ hc_prefilter 仅"预筛"：报告层仍需叠加 ④无高危负向标签 ⑤因子冲突矩阵无强冲突
--      终判（见 SKILL.md 铁律 #14），本列不得被当作买入信号。
--
-- 刷新：REFRESH MATERIALIZED VIEW CONCURRENTLY biz.mv_asset_screen（见 bin/refresh_asset_screen_mv.py）

BEGIN;

-- ── ① 弹性表加分层列（不动既有列 / 不改幂等键 asset_id+as_of）──────────
ALTER TABLE biz.asset_price_elasticity
    ADD COLUMN IF NOT EXISTS by_impact JSONB;

COMMENT ON COLUMN biz.asset_price_elasticity.by_impact IS
    'SKILL-HC-001：按催化剂 impact_strength 分层的 AR72（strong/medium/weak/unknown）；'
    '每档 {n, ar72_mean, hit_rate, insufficient}，insufficient=n<10 不外推';

-- ── ② 重建物化视图（复刻 fix_105 全列 + 追加 by_impact / e_stat_valid / u_stat_valid / hc_prefilter）
DROP MATERIALIZED VIEW IF EXISTS biz.mv_asset_screen;

CREATE MATERIALIZED VIEW biz.mv_asset_screen AS
SELECT
    a.asset_id,
    a.canonical_symbol                       AS symbol,
    a.canonical_name                         AS name,
    a.primary_sector,
    a.market_cap,
    a.market_cap_rank,

    -- ── 价格弹性（P1.6）──────────────────────────────────────────────
    e.as_of                                  AS elasticity_as_of,
    e.n_events                               AS e_n_events,
    e.n_l1                                   AS e_n_l1,
    e.ar_72h_mean,
    e.ar_72h_median,
    e.hit_rate,
    e.hit_z,
    e.rs_90d,
    e.beta_btc,
    e.elasticity_grade,
    e.elasticity_score,
    e.by_impact,                              -- fix_110 新增

    -- ── 解锁表现（P1.7）──────────────────────────────────────────────
    r.as_of                                  AS unlock_as_of,
    r.n_events                               AS u_n_events,
    r.n_seen                                 AS u_n_seen,
    r.excess7_mean,
    r.excess7_median,
    r.up_rate_7,
    r.pump_rate,
    r.dump_rate,
    r.pressure_score,
    r.pressure_level,
    r.reaction_grade,
    r.reaction_score,
    r.dominant_beneficiary,
    r.next_unlock_beneficiary,
    r.next_unlock_source,
    r.insider_pct,
    r.next_unlock,
    r.unlock_pct_7d,
    r.unlock_pct_30d,

    -- ── CMC 下次解锁压力（fix_108，独立快照源）──────────────────────
    c.as_of                                  AS cmc_as_of,
    c.next_unlock_date                       AS cmc_next_unlock_date,
    c.next_unlock_usd                        AS cmc_next_unlock_usd,
    c.next_unlock_pct_supply                 AS cmc_next_unlock_pct_supply,
    c.total_unlocked_pct                     AS cmc_total_unlocked_pct,
    c.dominant_bucket                        AS cmc_next_unlock_beneficiary,
    c.insider_pct                            AS cmc_next_unlock_insider_pct,

    -- ── 资金流因子（P1.8）────────────────────────────────────────────
    f.as_of                                  AS flow_as_of,
    f.flow_score,
    f.flow_grade,
    f.obi_7d,
    f.taker_imb_7d,
    f.netflow_7d_pct_mcap,

    -- ── 派生分桶（与 asset_screen.py 的 E_HI/E_LO/U_UP/U_DN 同步）────
    CASE WHEN e.elasticity_grade IN ('高弹性', '中高弹性') THEN 'high'
         WHEN e.elasticity_grade IN ('低弹性', '负弹性')   THEN 'low'
    END                                      AS elasticity_bucket,
    CASE WHEN r.reaction_grade IN ('抗解禁型', '偏抗解禁') THEN 'up'
         WHEN r.reaction_grade IN ('偏解禁即崩', '解禁即崩') THEN 'dn'
    END                                      AS unlock_bucket,
    CASE WHEN f.flow_grade = '资金流入' THEN 'in'
         WHEN f.flow_grade = '资金流出' THEN 'out'
    END                                      AS flow_bucket,

    -- 四象限 key（任一侧缺桶则为 NULL，不硬塞）
    CASE
        WHEN e.elasticity_grade IN ('高弹性', '中高弹性')
             AND r.reaction_grade IN ('抗解禁型', '偏抗解禁') THEN 'ideal'
        WHEN e.elasticity_grade IN ('高弹性', '中高弹性')
             AND r.reaction_grade IN ('偏解禁即崩', '解禁即崩') THEN 'elastic_dump'
        WHEN e.elasticity_grade IN ('低弹性', '负弹性')
             AND r.reaction_grade IN ('抗解禁型', '偏抗解禁') THEN 'defensive'
        WHEN e.elasticity_grade IN ('低弹性', '负弹性')
             AND r.reaction_grade IN ('偏解禁即崩', '解禁即崩') THEN 'avoid'
    END                                      AS quad_key,

    -- 双确认 / 背离（P1.9 铁律 13）
    (e.elasticity_grade IN ('高弹性', '中高弹性')
        AND f.flow_grade = '资金流入')        AS is_double_confirm,
    (e.elasticity_grade IN ('高弹性', '中高弹性')
        AND f.flow_grade = '资金流出')        AS is_divergence,

    -- 下一次解锁是否由内部人（团队/投资人）主导 → 短期抛压预警
    (r.next_unlock_beneficiary IN ('team', 'investor')) AS next_unlock_insider,
    (c.dominant_bucket IN ('team', 'investor'))         AS cmc_next_unlock_insider,
    -- CMC 口径的大额解锁预警（下次解锁 ≥5% 流通供应）
    (c.next_unlock_pct_supply >= 5)                     AS cmc_big_unlock,

    -- ── fix_110：统计显著性 ───────────────────────────────────────────
    (COALESCE(e.n_events, 0) >= 10)                     AS e_stat_valid,
    (COALESCE(r.n_events, 0) >= 5)                      AS u_stat_valid,

    -- ── fix_110：高确定性预筛（仅预筛；报告层再做风险标签 + 冲突终判）────
    COALESCE(
        (e.n_events >= 10)
        AND e.elasticity_grade IN ('高弹性', '中高弹性')
        AND f.flow_grade = '资金流入'
        AND (r.n_events >= 5)
        AND (r.reaction_grade IS NULL OR r.reaction_grade NOT IN ('偏解禁即崩', '解禁即崩'))
        AND COALESCE(c.next_unlock_pct_supply, 0) < 5.0
    , FALSE)                                            AS hc_prefilter,

    -- 综合分：三指标等权（缺失项按 50 = 中性收缩），仅作排序参考，不作为买入信号
    ROUND((
        COALESCE(e.elasticity_score, 50) * 0.40
      + COALESCE(r.reaction_score,   50) * 0.30
      + COALESCE(f.flow_score,       50) * 0.30
    )::numeric, 2)                           AS screen_score,

    GREATEST(
        COALESCE(e.as_of, DATE '1970-01-01'),
        COALESCE(r.as_of, DATE '1970-01-01'),
        COALESCE(f.as_of, DATE '1970-01-01'),
        COALESCE(c.as_of, DATE '1970-01-01')
    )                                        AS latest_as_of,
    NOW()                                    AS built_at
FROM core.asset a
LEFT JOIN LATERAL (
    SELECT * FROM biz.asset_price_elasticity x
    WHERE x.asset_id = a.asset_id ORDER BY x.as_of DESC LIMIT 1
) e ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM biz.asset_unlock_reaction x
    WHERE x.asset_id = a.asset_id ORDER BY x.as_of DESC LIMIT 1
) r ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM biz.asset_flow_factor x
    WHERE x.asset_id = a.asset_id ORDER BY x.as_of DESC LIMIT 1
) f ON TRUE
LEFT JOIN biz.asset_unlock_next_cmc c ON c.asset_id = a.asset_id
WHERE r.asset_id IS NOT NULL OR f.asset_id IS NOT NULL
   OR e.asset_id IS NOT NULL;

-- CONCURRENTLY 刷新必需：唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_asset_screen_asset_id
    ON biz.mv_asset_screen (asset_id);
CREATE INDEX IF NOT EXISTS idx_mv_asset_screen_rank
    ON biz.mv_asset_screen (market_cap_rank);
CREATE INDEX IF NOT EXISTS idx_mv_asset_screen_quad
    ON biz.mv_asset_screen (quad_key);
CREATE INDEX IF NOT EXISTS idx_mv_asset_screen_double
    ON biz.mv_asset_screen (is_double_confirm, flow_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_mv_asset_screen_hc
    ON biz.mv_asset_screen (hc_prefilter, screen_score DESC NULLS LAST);

COMMENT ON MATERIALIZED VIEW biz.mv_asset_screen IS
    '投研多维筛选宽表（P1.9）：弹性×解锁四象限 + 弹性×资金流双确认 + fix_110 高确定性预筛（hc_prefilter）；刷新见 bin/refresh_asset_screen_mv.py';

COMMIT;
