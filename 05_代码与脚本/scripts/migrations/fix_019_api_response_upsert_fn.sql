-- ============================================================
-- raw.api_response UPSERT 存储过程
-- 解决 ON CONFLICT ON INDEX 不可用问题（PostgreSQL 不支持该语法）
-- 唯一索引 uq_raw_api_response_dedup 含 COALESCE 表达式，
-- 无法用 ON CONFLICT (columns) 语法，故用存储过程实现 upsert。
-- ============================================================

CREATE OR REPLACE FUNCTION raw.upsert_api_response(
    p_run_id       BIGINT,
    p_platform     TEXT,
    p_endpoint     TEXT,
    p_request_key  TEXT,
    p_entity_key   TEXT,
    p_page_key     TEXT,
    p_payload      JSONB,
    p_payload_hash TEXT,
    p_fetched_at   TIMESTAMPTZ
) RETURNS BIGINT AS $$
DECLARE
    v_response_id BIGINT;
BEGIN
    -- 尝试更新已有记录
    UPDATE raw.api_response
    SET run_id    = p_run_id,
        payload   = p_payload,
        fetched_at = p_fetched_at
    WHERE platform_code = p_platform
      AND endpoint_code = p_endpoint
      AND COALESCE(request_key, '') = COALESCE(p_request_key, '')
      AND COALESCE(page_key, '') = COALESCE(p_page_key, '')
      AND payload_hash = p_payload_hash
    RETURNING response_id INTO v_response_id;

    IF v_response_id IS NOT NULL THEN
        RETURN v_response_id;
    END IF;

    -- 无冲突：插入新记录
    INSERT INTO raw.api_response (
        run_id, platform_code, endpoint_code,
        request_key, entity_key, page_key,
        payload, payload_hash, fetched_at
    ) VALUES (
        p_run_id, p_platform, p_endpoint,
        p_request_key, p_entity_key, p_page_key,
        p_payload, p_payload_hash, p_fetched_at
    )
    RETURNING response_id INTO v_response_id;

    RETURN v_response_id;
END;
$$ LANGUAGE plpgsql;
