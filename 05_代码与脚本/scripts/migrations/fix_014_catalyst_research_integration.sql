-- FIX-014: 催化剂 → 投研框架打通
-- 日期：2026-08-26
-- 背景：asset_catalyst 与 research_thesis 是平行孤岛，无 ID 级关联
-- 修复：1) 加 ai_processed_at 字段
--       2) 建按资产聚合的催化剂视图（供投研直接 JOIN）
--       3) 建 get_asset_catalysts(asset_id, days) 函数
--       4) research_thesis.catalysts_json 增加 catalyst_id 引用字段（向后兼容）

-- ============================================================
-- 第一步：补 ai_processed_at 字段（process_catalyst_ai.py 需要）
-- ============================================================

ALTER TABLE biz.asset_catalyst
    ADD COLUMN IF NOT EXISTS ai_processed_at TIMESTAMPTZ;

COMMENT ON COLUMN biz.asset_catalyst.ai_processed_at
    IS 'AI 预处理完成时间';

-- ============================================================
-- 第二步：按资产聚合的催化剂视图（最近 90 天，供投研直接用）
-- ============================================================

CREATE OR REPLACE VIEW biz.v_asset_catalyst_recent AS
SELECT
    cal.asset_id,
    ac.catalyst_id,
    ac.source_code,
    ac.title,
    ac.body_text,
    ac.published_at,
    ac.event_category,
    ac.ai_event_type,
    ac.ai_sentiment,
    ac.ai_summary,
    ac.ai_keywords,
    ac.related_pairs,
    ac.source_url,
    cal.link_source,
    cal.confidence
FROM biz.catalyst_asset_link cal
JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
WHERE ac.published_at >= NOW() - INTERVAL '90 days'
  AND ac.is_active = TRUE;

COMMENT ON VIEW biz.v_asset_catalyst_recent
    IS '按资产聚合的近期催化剂（90天内，已激活），供投研/分析直接 JOIN';

-- ============================================================
-- 第三步：get_asset_catalysts(asset_id, days) 表函数
-- ============================================================

CREATE OR REPLACE FUNCTION biz.get_asset_catalysts(
    p_asset_id INT,
    p_days INT DEFAULT 90
)
RETURNS TABLE (
    catalyst_id INT,
    source_code VARCHAR(50),
    title VARCHAR(500),
    body_text TEXT,
    published_at TIMESTAMPTZ,
    event_category VARCHAR(100),
    ai_event_type VARCHAR(50),
    ai_sentiment VARCHAR(20),
    ai_summary VARCHAR(500),
    ai_keywords JSONB,
    related_pairs JSONB,
    source_url VARCHAR(1000),
    link_source VARCHAR(50),
    confidence NUMERIC(3,2)
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ac.catalyst_id,
        ac.source_code,
        ac.title,
        ac.body_text,
        ac.published_at,
        ac.event_category,
        ac.ai_event_type,
        ac.ai_sentiment,
        ac.ai_summary,
        ac.ai_keywords,
        ac.related_pairs,
        ac.source_url,
        cal.link_source,
        cal.confidence
    FROM biz.catalyst_asset_link cal
    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
    WHERE cal.asset_id = p_asset_id
      AND ac.published_at >= NOW() - (p_days || ' days')::INTERVAL
      AND ac.is_active = TRUE
    ORDER BY ac.published_at DESC;
END;
$$;

COMMENT ON FUNCTION biz.get_asset_catalysts(INT, INT)
    IS '获取指定资产的近期催化剂列表（按时间倒序）';

-- ============================================================
-- 第四步：催化剂统计视图（每个资产的催化剂概况）
-- ============================================================

CREATE OR REPLACE VIEW biz.v_asset_catalyst_stats AS
SELECT
    cal.asset_id,
    COUNT(*) AS total_catalysts,
    COUNT(*) FILTER (WHERE ac.ai_sentiment = 'bullish') AS bullish_count,
    COUNT(*) FILTER (WHERE ac.ai_sentiment = 'bearish') AS bearish_count,
    COUNT(*) FILTER (WHERE ac.ai_sentiment = 'neutral') AS neutral_count,
    COUNT(*) FILTER (WHERE ac.ai_event_type = 'listing') AS listing_count,
    COUNT(*) FILTER (WHERE ac.ai_event_type = 'delisting') AS delisting_count,
    COUNT(*) FILTER (WHERE ac.ai_event_type = 'partnership') AS partnership_count,
    COUNT(*) FILTER (WHERE ac.ai_event_type = 'funding') AS funding_count,
    COUNT(DISTINCT ac.source_code) AS source_count,
    MAX(ac.published_at) AS latest_catalyst_at
FROM biz.catalyst_asset_link cal
JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
WHERE ac.published_at >= NOW() - INTERVAL '90 days'
  AND ac.is_active = TRUE
GROUP BY cal.asset_id;

COMMENT ON VIEW biz.v_asset_catalyst_stats
    IS '每个资产的催化剂统计概况（90天内）：总数/多空分布/事件类型分布';

-- ============================================================
-- 第五步：research_thesis.catalysts_json 结构升级说明
-- ============================================================
-- 旧格式（自由文本，向后兼容）：
--   [{"catalyst": "描述", "timing": "时间"}]
--
-- 新格式（带 ID 引用，可回溯）：
--   [{"catalyst_id": 123, "catalyst": "描述", "timing": "时间",
--     "source_code": "binance_news", "event_type": "listing",
--     "sentiment": "bullish"}]
--
-- 应用层（generate_research_thesis）会自动填充新格式。
-- 旧数据无需迁移，读取时做兼容处理即可。
