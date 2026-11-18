-- =====================================================================
-- 催化剂 × β × BTC Regime × 催化剂密度 → 三维决策信号 View
-- 编号：fix_113
-- 日期：2026-10-20
-- =====================================================================
--
-- 用途：给定任意时间点，查询该时点前 7 天内发生的催化剂事件，
--       结合资产 β 和 BTC 近 7d regime，输出操作建议。
--
-- 核心逻辑：
--   ① 催化剂事件 → biz.asset_catalyst JOIN biz.catalyst_asset_link
--   ② 资产 β     → core.asset_beta（已落库）
--   ③ BTC regime → 近 7d BTC 滚动涨跌幅，>1% bull / <-1% bear / 其余 flat
--   ④ 催化剂密度 → 同资产过去 7d 内催化剂数量（窗口函数）
--   ⑤ 操作建议  → beta_full ≥ 1.5 AND regime=bull → STRONG_BUY
--                 beta_full ≥ 1.5 AND regime=flat → LIGHT_BUY
--                 beta_full ≥ 1.5 AND regime=bear → WATCH
--                 beta_full < 1.5                 → SKIP
-- =====================================================================

DROP VIEW IF EXISTS core.v_catalyst_signal CASCADE;

CREATE VIEW core.v_catalyst_signal AS
WITH btc_regime AS (
    -- 每个时点的 BTC 近 7d 涨跌幅 + regime 标签
    SELECT k.open_time AS ts,
           (k.close_px / LAG(k.close_px, 168) OVER (ORDER BY k.open_time) - 1)
             AS btc_ret_7d,
           CASE
               WHEN (k.close_px / LAG(k.close_px, 168) OVER (ORDER BY k.open_time) - 1) > 0.01  THEN 'bull'
               WHEN (k.close_px / LAG(k.close_px, 168) OVER (ORDER BY k.open_time) - 1) < -0.01 THEN 'bear'
               ELSE 'flat'
           END AS btc_regime
    FROM biz.asset_klines k
    WHERE k.symbol = 'BTCUSDT' AND k.interval = '1h'
),
event_base AS (
    -- 催化剂事件 → 关联资产 β
    SELECT
        c.catalyst_id,
        c.published_at,
        c.ai_event_type,
        c.event_category,
        a.asset_id,
        a.canonical_symbol,
        b.symbol AS trading_pair,
        b.beta_full,
        b.beta_med_full,
        b.beta_bull,
        b.beta_regression,
        b.beta_bucket,
        b.bull_excess,
        b.bear_excess,
        b.n_samples,
        -- BTC regime（催化剂发布时最近 1h K 线对应的 regime）
        r.btc_ret_7d,
        r.btc_regime,
        -- 催化剂密度：同资产过去 7d 内催化剂数量
        count(*) OVER (
            PARTITION BY a.asset_id
            ORDER BY c.published_at
            RANGE BETWEEN interval '7 days' PRECEDING AND CURRENT ROW
        ) AS catalyst_density_7d
    FROM biz.asset_catalyst c
    JOIN biz.catalyst_asset_link cal ON cal.catalyst_id = c.catalyst_id
    JOIN core.asset a ON a.asset_id = cal.asset_id AND a.status = 'active'
    JOIN core.asset_beta b ON b.asset_id = a.asset_id
    LEFT JOIN btc_regime r
      ON r.ts <= c.published_at
     AND r.ts >= c.published_at - interval '1 hour'
)
SELECT
    catalyst_id,
    published_at,
    ai_event_type,
    asset_id,
    canonical_symbol,
    trading_pair,
    beta_full,
    beta_med_full,
    beta_bull,
    beta_bucket,
    bull_excess,
    bear_excess,
    n_samples,
    btc_ret_7d,
    btc_regime,
    catalyst_density_7d,

    -- 事件类型仓位权重（基于之前回测结论）
    CASE ai_event_type
        WHEN 'burn'        THEN 1.0
        WHEN 'security'    THEN 1.0
        WHEN 'partnership' THEN 1.0
        WHEN 'listing'     THEN 0.5
        WHEN 'tech_upgrade'THEN 0.5
        WHEN 'funding'     THEN 0.5
        WHEN 'regulation'  THEN 0.2
        WHEN 'market_update'THEN 0.2
        ELSE 0.2
    END AS position_weight,

    -- 核心：操作建议
    CASE
        WHEN beta_full < 1.5 THEN 'SKIP'
        WHEN btc_regime = 'bull'    AND beta_full >= 2.5 THEN 'STRONG_BUY'
        WHEN btc_regime = 'bull'    AND beta_full >= 1.5 THEN 'BUY'
        WHEN btc_regime = 'flat'    AND beta_full >= 1.5 THEN 'LIGHT_BUY'
        WHEN btc_regime = 'bear'    AND beta_full >= 1.5 THEN 'WATCH'
        ELSE 'SKIP'
    END AS action_signal,

    -- 置信度综合评分（0-100）
    ROUND(
        (CASE WHEN beta_full >= 2.5 THEN 40 WHEN beta_full >= 1.5 THEN 30 ELSE 0 END)
        + (CASE WHEN btc_regime = 'bull' THEN 30 WHEN btc_regime = 'flat' THEN 15 ELSE 0 END)
        + (CASE WHEN catalyst_density_7d >= 3 THEN 20 WHEN catalyst_density_7d >= 2 THEN 10 ELSE 0 END)
        + (CASE ai_event_type WHEN 'burn' THEN 10 WHEN 'security' THEN 10 WHEN 'partnership' THEN 10 ELSE 5 END)
    , 0) AS confidence_score

FROM event_base
WHERE btc_regime IS NOT NULL  -- 过滤 BTC regime 缺失（历史早期）
ORDER BY published_at DESC;

COMMENT ON VIEW core.v_catalyst_signal
    IS '催化剂 × β × BTC regime × 催化剂密度 → 三维决策信号';
COMMENT ON COLUMN core.v_catalyst_signal.action_signal
    IS '操作建议: STRONG_BUY/BUY/LIGHT_BUY/WATCH/SKIP';
COMMENT ON COLUMN core.v_catalyst_signal.confidence_score
    IS '综合置信度 0-100: β分 + regime分 + 密度分 + 事件类型分';
