-- fix_105: 资产多维筛选物化视图 biz.mv_asset_screen（幂等）
--
-- 动机（投研 SOP P1.9）：四象限（弹性 × 解锁）+ 双确认（弹性 × 资金流）此前只在
--   bin/asset_screen.py 里做实时 LATERAL 三表联接，前端/看板每次都要重跑一遍联接。
--   本视图把「三条独立指标 → 一个选币宽表」物化下来，前端可直接 SELECT，无需理解
--   LATERAL/评级词汇表，也避免看板查询打爆 core.asset 全表。
--
-- 口径（与 bin/asset_screen.py 严格一致，改动必须两边同步）：
--   弹性 P1.6 biz.asset_price_elasticity  高 = {高弹性, 中高弹性} / 低 = {低弹性, 负弹性}
--   解锁 P1.7 biz.asset_unlock_reaction   抗 = {抗解禁型, 偏抗解禁} / 崩 = {偏解禁即崩, 解禁即崩}
--   资金流 P1.8 biz.asset_flow_factor     in = 资金流入 / out = 资金流出
--   四象限  ideal(高+抗) / elastic_dump(高+崩) / defensive(低+抗) / avoid(低+崩)
--   双确认  is_double_confirm = 高弹性 AND 资金流入
--   背离    is_divergence     = 高弹性 AND 资金流出
--   下次解锁受益人 next_unlock_beneficiary（事件级）+ next_unlock_insider（团队/投资人主导旗标）
--   + next_unlock_source（来源标注：tokenomics_hero | cmc_next）
--   CMC 下次解锁压力（fix_108/109）：cmc_next_unlock_date / _usd / _pct_supply /
--     cmc_next_unlock_beneficiary / cmc_next_unlock_insider（独立快照源，覆盖更广、含美元金额）
--
-- 取数：每张指标表按 asset_id 取 as_of 最新一行（LATERAL ... ORDER BY as_of DESC LIMIT 1）。
-- 行域：三表中任一有数据即入视图（不要求弹性存在——高资金流但无事件的资产也要能被看板看到）。
-- 刷新：REFRESH MATERIALIZED VIEW CONCURRENTLY biz.mv_asset_screen
--       （需下面的唯一索引；见 bin/refresh_asset_screen_mv.py）

BEGIN;

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
    --   ① reaction 侧：源 r.next_unlock_beneficiary（事件级；tokenomics hero 优先、CMC 兜底）
    --   ② CMC 侧  ：源 c.dominant_bucket（独立源快照，可交叉印证）
    (r.next_unlock_beneficiary IN ('team', 'investor')) AS next_unlock_insider,
    (c.dominant_bucket IN ('team', 'investor'))         AS cmc_next_unlock_insider,
    -- CMC 口径的大额解锁预警（下次解锁 ≥5% 流通供应）
    (c.next_unlock_pct_supply >= 5)                     AS cmc_big_unlock,

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

COMMENT ON MATERIALIZED VIEW biz.mv_asset_screen IS
    '投研多维筛选宽表（P1.9）：弹性×解锁四象限 + 弹性×资金流双确认；刷新见 bin/refresh_asset_screen_mv.py';

COMMIT;
