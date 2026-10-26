-- fix_114: 高确定性预筛增补「未来解锁闸门」（工单 SKILL-HC-009 结论 2，幂等）
--
-- 背景
--   hc_prefilter 的解锁维度存在一个结构性漏洞：它只校验「历史解锁的**反应质量**」
--   （u_n_events ≥ 5 且评级非崩），却不校验「还有没有**下一次**解锁」。
--   于是「解锁已全部走完、无任何未来事件」的标的照样能进 hc 池 —— 但它没有任何
--   即将到来的解锁事件，在「解锁前布局」这个用法上不产生任何交易窗口，属于纯噪音占位。
--
-- 实测证据（2026-10-26，biz.asset_unlock_event vs biz.mv_asset_screen）
--   hc_n = 7，逐个体检未来解锁数：
--     APT=6  ARKM=4  ATH=4  GRT=4  ZRO=6   ← 5 个有真未来事件（最近 ARKM 2026-10-26）
--     AXS=0  FET=0                        ← 2 个全库零未来事件，且 CMC 下次解锁亦为空
--   ⇒ 加 `fut_n >= 1` 后 hc_n 7 → 5，剔掉的正好是这 2 个假信号。
--
-- 为什么用 biz.asset_unlock_event 而不是 biz.asset_unlock_next_cmc
--   CMC 侧缺口实测存在：ATH 在 CMC 的 next_unlock_* 全为 NULL，但事件表有 4 个未来
--   事件 ⇒ 以 CMC 为准会误杀。事件表由自家抓取产出（tokenomics.com 双表），覆盖更全。
--   ⚠️ 因此 hc_prefilter 里既有的 `COALESCE(c.next_unlock_pct_supply, 0) < 5.0`
--      **保持不动**：把它收紧成 IS NOT NULL 同样会误杀 ATH 这类 CMC 缺数据型标的。
--      「有没有下次解锁」交给本闸门（事件表），「下次解锁大不大」交给 CMC（缺数据按不拦）。
--
-- 口径（与 bin/asset_screen.py 的 _hc 必须两边同步；沿用 fix_105/fix_110 词表约束）
--   弹性 高 = {高弹性, 中高弹性} / 低 = {低弹性, 负弹性}
--   解锁 抗 = {抗解禁型, 偏抗解禁} / 崩 = {偏解禁即崩, 解禁即崩}
--   资金流 in = 资金流入
--   未来解锁 fut_n = count(asset_unlock_event where unlock_date >= CURRENT_DATE)
--   ⚠️ hc_prefilter 仅"预筛"：报告层仍需叠加 ④无高危负向标签 ⑤因子冲突矩阵无强冲突
--      终判（见 SKILL.md 铁律 #14），本列不得被当作买入信号。
--   ⚠️ fut_n 以 CURRENT_DATE 为界的**快照语义**：MV 不自动刷新，跨过某天解锁日后
--      fut_n 会偏大 1。这与 u_n_events / latest_as_of 的既有语义一致，刷新即修正。
--
-- 新增列
--   unlock_fut_n    未来（含今日）解锁事件数 —— 闸门依据，也可直接给看板做「有无事件」标记
--   unlock_next_date 最近一次未来解锁日 —— 替代 CMC 缺数据时的空窗
--
-- 刷新：REFRESH MATERIALIZED VIEW CONCURRENTLY biz.mv_asset_screen（见 bin/refresh_asset_screen_mv.py）
-- 整链刷新：bin/refresh_unlock_chain.py（抓取 → event 同步 → reaction 重算 → 本视图刷新）

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
    e.by_impact,

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

    -- ── fix_114：未来解锁（事件表口径，hc 闸门依据）─────────────────
    COALESCE(fu.fut_n, 0)                    AS unlock_fut_n,
    fu.next_dt                               AS unlock_next_date,

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

    -- ── fix_110 + fix_114：高确定性预筛（仅预筛；报告层再做风险标签 + 冲突终判）──
    COALESCE(
        (e.n_events >= 10)
        AND e.elasticity_grade IN ('高弹性', '中高弹性')
        AND f.flow_grade = '资金流入'
        AND (r.n_events >= 5)
        AND (r.reaction_grade IS NULL OR r.reaction_grade NOT IN ('偏解禁即崩', '解禁即崩'))
        AND COALESCE(c.next_unlock_pct_supply, 0) < 5.0
        -- fix_114：必须有未来（含今日）解锁事件，否则无「解锁前布局」窗口
        AND COALESCE(fu.fut_n, 0) >= 1
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
-- fix_114：未来解锁聚合（含今日；事件表口径，比 CMC 覆盖更全）
LEFT JOIN LATERAL (
    SELECT COUNT(*) FILTER (WHERE x.unlock_date >= CURRENT_DATE) AS fut_n,
           MIN(x.unlock_date) FILTER (WHERE x.unlock_date >= CURRENT_DATE) AS next_dt
    FROM biz.asset_unlock_event x
    WHERE x.asset_id = a.asset_id
) fu ON TRUE
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
-- fix_114：按「有未来解锁」快速筛（看板/告警常用）
CREATE INDEX IF NOT EXISTS idx_mv_asset_screen_fut
    ON biz.mv_asset_screen (unlock_next_date) WHERE unlock_fut_n > 0;

COMMENT ON MATERIALIZED VIEW biz.mv_asset_screen IS
    '投研多维筛选宽表（P1.9）：弹性×解锁四象限 + 弹性×资金流双确认 + fix_110 高确定性预筛（hc_prefilter）+ fix_114 未来解锁闸门（unlock_fut_n）；刷新见 bin/refresh_asset_screen_mv.py';

COMMIT;
