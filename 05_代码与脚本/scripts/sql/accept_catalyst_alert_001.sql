-- =============================================================================
-- 验收 SQL · 催化剂邮件转型：Alert 与早报分流（OPT-CATALYST-ALERT-001）
-- 日期：2026-09-15
-- 说明：分步执行（每段独立 SELECT，可单独运行），GUI 友好，无临时表冲突、可重复跑。
-- 期望：检查 ① 结果为 0；其余为分布/预览信息，供人工判断。
-- 涉及改动：notifier.py（A-only + confirmed + 交易档位 + 取前2）、macro_market.py（CATALYST_HOTSPOTS）
-- =============================================================================


-- -----------------------------------------------------------------------------
-- ① 铁律：tier 一致性必须仍 = 0
--    composite→tier 是唯一真源（A≥80 / B≥60 / C≥40），本工单不得破坏单点真源。
--    若 > 0：存在 composite 与 tier 不一致行，需排查（P0-2 邮件层闸门不应影响此值）。
-- -----------------------------------------------------------------------------
SELECT
    count(*) FILTER (WHERE composite_score >= 80  AND tier != 'A') AS mismatch_a,
    count(*) FILTER (WHERE composite_score >= 60 AND composite_score < 80 AND tier != 'B') AS mismatch_b,
    count(*) FILTER (WHERE composite_score >= 40 AND composite_score < 60 AND tier != 'C') AS mismatch_c,
    count(*) AS total_mismatch
FROM biz.catalyst_signal
WHERE status IN ('open', 'expired')
  AND composite_score >= 40;


-- -----------------------------------------------------------------------------
-- ② A 级可达性分布：tier × resonance_state
--    目标：A 级应主要落在 resonance_state='confirmed'（高置信度）。
--    若 A 级里混入大量 weak/divergent → 说明分级口径需复查（邮件层已会排除，但前端仍会展示）。
-- -----------------------------------------------------------------------------
SELECT s.tier, s.resonance_state, count(*) AS cnt
FROM biz.catalyst_signal s
WHERE s.status = 'open'
  AND s.tier IN ('A', 'B', 'C')
GROUP BY 1, 2
ORDER BY 1, 3 DESC;


-- -----------------------------------------------------------------------------
-- ③ A 级可交易性：tier='A' 但缺交易档位（entry/stop/tp）
--    邮件 Alert 只发「有完整档位」的 A；缺失的会被邮件层排除（前端仍显示为 A）。
--    期望：通常应为 0；若 > 0 说明快通道先于 G5 技术面补全，属预期，观察即可。
-- -----------------------------------------------------------------------------
SELECT count(*) AS a_without_prices
FROM biz.catalyst_signal
WHERE status = 'open'
  AND tier = 'A'
  AND (entry_price IS NULL OR stop_loss IS NULL OR take_profit IS NULL);


-- -----------------------------------------------------------------------------
-- ④ 邮件 Alert 入选预览（与 notifier._recent_new_a_signals 同口径）
--    入选：tier='A' + resonance_state='confirmed' + entry/stop/tp 齐全，按分取前 2
--    期望：≤2 条；>2 说明当日不止 2 个 idea，符合「每天 1~2 idea」上限即取最高分 2 条。
-- -----------------------------------------------------------------------------
SELECT t.canonical_symbol AS symbol, t.canonical_name AS name,
       t.composite_score, t.resonance_state, t.rr_ratio,
       t.catalyst_title, t.created_at::date AS created_day
FROM (
    SELECT DISTINCT ON (a.asset_id)
           a.canonical_symbol, a.canonical_name,
           s.composite_score, s.resonance_state, s.rr_ratio,
           ac.title AS catalyst_title, s.created_at,
           LOWER(COALESCE(a.canonical_name, '')) AS name_lc
    FROM biz.catalyst_signal s
    JOIN core.asset a ON s.asset_id = a.asset_id
    JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
    WHERE s.status = 'open'
      AND s.created_at > NOW() - INTERVAL '24 hours'
      AND s.tier = 'A'
      AND s.resonance_state = 'confirmed'
      AND s.entry_price IS NOT NULL
      AND s.stop_loss IS NOT NULL
      AND s.take_profit IS NOT NULL
      AND name_lc !~ 'tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ|crude[[:space:]]+oil|brent'
    ORDER BY a.asset_id, s.composite_score DESC
) t
ORDER BY t.composite_score DESC
LIMIT 2;


-- -----------------------------------------------------------------------------
-- ⑤ 早报热点候选：最近 24h B/C 级 open 信号（crypto 类）
--    即早报「📡 催化剂热点」卡片的数据源。
--    期望：>0（热点下沉早报）；若 =0 且 ④ 也为 0 → 今日全空窗，属可接受（女王已拍板）。
-- -----------------------------------------------------------------------------
SELECT count(DISTINCT s.asset_id) AS hotspot_assets,
       count(*) AS hotspot_rows
FROM biz.catalyst_signal s
JOIN core.asset a ON s.asset_id = a.asset_id
WHERE s.status = 'open'
  AND s.created_at > NOW() - INTERVAL '24 hours'
  AND s.tier IN ('B', 'C')
  AND LOWER(COALESCE(a.canonical_name, ''))
      !~ 'tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ|crude[[:space:]]+oil|brent';
