-- =====================================================================
-- 催化剂决策管道 P0 迁移
-- 功能：快通道 MVP（规则分类 + G0 市场环境 + G1 分级 + G2 共振 + G6 信号骨架）
-- 编号：fix_038
-- 日期：2026-09-11
-- =====================================================================

-- 确保 schema 存在
CREATE SCHEMA IF NOT EXISTS biz;

-- =====================================================================
-- 0. 给 biz.asset_catalyst 补列（规则分类 + 多源数组）
-- =====================================================================

-- rule_event_type: 规则兜底分类（快通道零 LLM 用）
ALTER TABLE biz.asset_catalyst
    ADD COLUMN IF NOT EXISTS rule_event_type VARCHAR(64);
COMMENT ON COLUMN biz.asset_catalyst.rule_event_type
    IS '规则兜底分类事件类型（快通道零LLM用），由 classify.py 正则匹配产出';

-- rule_event_type 索引（快通道查询用）
CREATE INDEX IF NOT EXISTS idx_asset_catalyst_rule_event_type
    ON biz.asset_catalyst (rule_event_type)
    WHERE rule_event_type IS NOT NULL;

-- content_hash: 跨源去重哈希（如果之前的 migration 没加的话）
-- 注意：pipeline.py 已经在用 content_hash 了，但表结构 SQL 里没列，可能是后续 migration 加的
-- 这里用 IF NOT EXISTS 安全补列
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'biz' AND table_name = 'asset_catalyst'
          AND column_name = 'content_hash'
    ) THEN
        ALTER TABLE biz.asset_catalyst ADD COLUMN content_hash VARCHAR(64);
        CREATE INDEX idx_asset_catalyst_content_hash
            ON biz.asset_catalyst (content_hash);
    END IF;
END $$;

-- source_codes: 多来源数组（JSONB，存所有来源编码）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'biz' AND table_name = 'asset_catalyst'
          AND column_name = 'source_codes'
    ) THEN
        ALTER TABLE biz.asset_catalyst ADD COLUMN source_codes JSONB DEFAULT '[]'::jsonb;
    END IF;
END $$;

-- =====================================================================
-- 1. 市场环境快照表（G0 闸门输入）
-- =====================================================================

CREATE TABLE IF NOT EXISTS biz.market_regime_daily (
    regime_date     DATE PRIMARY KEY,
    regime          VARCHAR(16) NOT NULL CHECK (regime IN ('risk_on','neutral','risk_off')),
    btc_trend_7d    NUMERIC(8,4),       -- BTC 7日涨跌幅 %
    alt_index_7d    NUMERIC(8,4),       -- 山寨指数（前30非BTC市值加权）7日涨跌幅 %
    total_mcap_7d   NUMERIC(8,4),       -- 全市场市值 7日涨跌幅 %
    fear_greed      SMALLINT,           -- 恐惧贪婪指数 0-100（可选）
    derived_from    TEXT DEFAULT 'rule',-- 推导方式
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.market_regime_daily IS '日度市场环境快照（risk_on / neutral / risk_off）';
COMMENT ON COLUMN biz.market_regime_daily.regime IS '市场环境：risk_on(进攻) / neutral(中性) / risk_off(避险)';

CREATE INDEX IF NOT EXISTS idx_market_regime_daily_date
    ON biz.market_regime_daily (regime_date DESC);

-- =====================================================================
-- 2. 催化剂分级表（G1）
-- =====================================================================

CREATE TABLE IF NOT EXISTS biz.catalyst_grade (
    catalyst_id     BIGINT PRIMARY KEY REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    authority_score SMALLINT NOT NULL DEFAULT 0,   -- 0-100 来源权威度
    event_weight    SMALLINT NOT NULL DEFAULT 0,   -- 0-100 事件类型权重
    scope_score     SMALLINT NOT NULL DEFAULT 0,   -- 0-100 影响聚焦度（单币 90 > 板块 60 > 全市场 30）
    tradable        BOOLEAN NOT NULL DEFAULT FALSE,-- 是否关联到 core.asset 可交易标的
    catalyst_kind   TEXT NOT NULL CHECK (catalyst_kind IN ('structural','event','sentiment','noise')),
    base_strength   SMALLINT NOT NULL DEFAULT 0,   -- 加权综合 0-100
    event_type_src  TEXT NOT NULL DEFAULT 'rule',  -- rule(快通道规则) | ai(慢通道LLM) | hybrid
    graded_by       TEXT NOT NULL DEFAULT 'rule',  -- rule | llm
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.catalyst_grade IS '催化剂 G1 分级结果（权威度×事件权重×影响范围 → kind + base_strength）';
COMMENT ON COLUMN biz.catalyst_grade.catalyst_kind IS '催化剂类型：structural(结构性) / event(事件型) / sentiment(情绪型) / noise(噪声)';
COMMENT ON COLUMN biz.catalyst_grade.base_strength IS '基础强度加权分 0-100（0.4*authority + 0.4*event_weight + 0.2*scope）';
COMMENT ON COLUMN biz.catalyst_grade.event_type_src IS '事件类型来源：rule(规则兜底) / ai(LLM) / hybrid';

CREATE INDEX IF NOT EXISTS idx_catalyst_grade_kind
    ON biz.catalyst_grade (catalyst_kind, base_strength DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_grade_tradable
    ON biz.catalyst_grade (tradable, base_strength DESC) WHERE tradable = true;

-- =====================================================================
-- 3. 催化剂共振打分表（G2）
-- =====================================================================

CREATE TABLE IF NOT EXISTS biz.catalyst_resonance (
    resonance_id    BIGSERIAL PRIMARY KEY,
    catalyst_id     BIGINT NOT NULL REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    -- 超额收益（扣 BTC beta）
    excess_ret_1h   NUMERIC(10,4),
    excess_ret_4h   NUMERIC(10,4),
    excess_ret_24h  NUMERIC(10,4),
    excess_ret_72h  NUMERIC(10,4),
    -- 量能 z-score
    vol_zscore_24h  NUMERIC(10,4),
    -- 板块 peer 中位数收益
    peer_median_ret_24h NUMERIC(10,4),
    -- 方向一致性
    direction_match BOOLEAN,
    -- 综合共振分 0-100
    resonance_score SMALLINT NOT NULL DEFAULT 0,
    -- 共振状态
    resonance_state TEXT NOT NULL CHECK (resonance_state IN ('confirmed','weak','divergent','pending')),
    -- 数据来源标记
    ret_source      TEXT DEFAULT 'cmc_snapshot', -- cmc_snapshot | market_daily
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (catalyst_id, asset_id)
);

COMMENT ON TABLE biz.catalyst_resonance IS '催化剂 G2 价格共振打分（超额收益 + 量能 + 方向一致 → resonance_state）';
COMMENT ON COLUMN biz.catalyst_resonance.excess_ret_1h IS 't+1h 超额收益（资产收益 - BTC 同期收益），单位 %';
COMMENT ON COLUMN biz.catalyst_resonance.resonance_score IS '共振综合分 0-100';
COMMENT ON COLUMN biz.catalyst_resonance.resonance_state IS '共振状态：confirmed(确认) / weak(弱) / divergent(背离) / pending(待观察)';

CREATE INDEX IF NOT EXISTS idx_catalyst_resonance_catalyst
    ON biz.catalyst_resonance (catalyst_id);
CREATE INDEX IF NOT EXISTS idx_catalyst_resonance_asset
    ON biz.catalyst_resonance (asset_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_resonance_state
    ON biz.catalyst_resonance (resonance_state, computed_at DESC);

-- =====================================================================
-- 4. 二阶受益映射表（G3，P0 建表占位，P1 填充数据）
-- =====================================================================

CREATE TABLE IF NOT EXISTS biz.catalyst_second_order (
    second_order_id BIGSERIAL PRIMARY KEY,
    catalyst_id     BIGINT NOT NULL REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    order_level     SMALLINT NOT NULL DEFAULT 2,   -- 2: 同板块直接受益 / 3: 间接受益
    confidence      NUMERIC(4,3) NOT NULL DEFAULT 0.4, -- 置信度 0-1
    sector_name     VARCHAR(64),                    -- 关联赛道名
    derived_from    TEXT DEFAULT 'sector_mapping',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (catalyst_id, asset_id, order_level)
);

COMMENT ON TABLE biz.catalyst_second_order IS '催化剂二阶受益资产映射（同板块/同生态间接受益标的）';

CREATE INDEX IF NOT EXISTS idx_catalyst_second_order_catalyst
    ON biz.catalyst_second_order (catalyst_id);
CREATE INDEX IF NOT EXISTS idx_catalyst_second_order_asset
    ON biz.catalyst_second_order (asset_id);

-- =====================================================================
-- 5. 催化剂决策信号表（G6，快通道 MVP 骨架）
-- =====================================================================

CREATE TABLE IF NOT EXISTS biz.catalyst_signal (
    signal_id        BIGSERIAL PRIMARY KEY,
    catalyst_id      BIGINT NOT NULL REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    asset_id         BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    -- G1
    kind             TEXT CHECK (kind IN ('structural','event','sentiment','noise')),
    base_strength    SMALLINT,                      -- 0-100
    -- G2
    resonance_score  SMALLINT,                      -- 0-100
    resonance_state  TEXT CHECK (resonance_state IN ('confirmed','weak','divergent','pending')),
    -- G3（P0 仅预判值，P1 补验证）
    persistence      TEXT CHECK (persistence IN ('structural','one_off','decaying')),
    persistence_verified BOOLEAN DEFAULT FALSE,
    -- G4（P0 占位，P1 填充）
    fundamental_pass BOOLEAN,
    fundamental_detail JSONB,
    -- G5（P0 占位，P1 填充，日线级）
    technical_state  TEXT CHECK (technical_state IN ('up','range','down')),
    entry_trigger    TEXT,                          -- 触发条件描述
    entry_trigger_price NUMERIC(24,10),             -- 触发价
    -- G6 风险层
    entry_price      NUMERIC(24,10),                -- = entry_trigger_price
    stop_loss        NUMERIC(24,10),
    take_profit      NUMERIC(24,10),
    rr_ratio         NUMERIC(6,2),                  -- 风险收益比
    -- ★ 综合分 + tier（单点真源，下游只读）
    composite_score  SMALLINT,                      -- 0-100 加权综合分
    tier             TEXT CHECK (tier IN ('A','B','C')),  -- A≥80 / B≥60 / C≥40
    confidence       NUMERIC(4,3),                  -- 综合 0-1
    regime           TEXT,                          -- 生成时市场环境
    invalidation     TEXT,                          -- 失效条件描述
    -- 生命周期
    expires_at       TIMESTAMPTZ,                   -- 自动过期时间（structural=7d / event=3d / sentiment=1d）
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','invalid','expired','done')),
    -- 通知去重
    pre_alert_sent_at TIMESTAMPTZ,                  -- 快提醒发送时间
    notified_at      TIMESTAMPTZ,                   -- 正式信号邮件发送时间
    -- 事后回测（P2 填充）
    backtest_pnl          NUMERIC(12,4),
    backtest_hit_tp       BOOLEAN,
    backtest_hit_sl       BOOLEAN,
    backtest_done         BOOLEAN DEFAULT FALSE,
    backtest_hitted_at    TIMESTAMPTZ,
    -- 审计
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (catalyst_id, asset_id)
);

COMMENT ON TABLE biz.catalyst_signal IS '催化剂决策信号（G0-G6 全链路输出，下游唯一入口）';
COMMENT ON COLUMN biz.catalyst_signal.composite_score IS '★ 加权综合分 0-100，tier 的唯一来源（口径单点真源）';
COMMENT ON COLUMN biz.catalyst_signal.tier IS '信号等级：A≥80 / B≥60 / C≥40，由 composite_score 唯一决定';
COMMENT ON COLUMN biz.catalyst_signal.expires_at IS '信号自动过期时间，慢通道巡检置为 expired';
COMMENT ON COLUMN biz.catalyst_signal.status IS '信号状态：open(有效) / invalid(失效) / expired(过期) / done(已了结)';
COMMENT ON COLUMN biz.catalyst_signal.notified_at IS '【预留】正式信号邮件发送时间；当前去重走 biz.catalyst_notification_log 表';
COMMENT ON COLUMN biz.catalyst_signal.pre_alert_sent_at IS '【预留】快提醒发送时间；当前去重走 biz.catalyst_notification_log 表';

CREATE INDEX IF NOT EXISTS idx_catalyst_signal_open
    ON biz.catalyst_signal (status, tier, created_at DESC) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_catalyst_signal_expires
    ON biz.catalyst_signal (expires_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_catalyst_signal_asset
    ON biz.catalyst_signal (asset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_signal_tier
    ON biz.catalyst_signal (tier, created_at DESC) WHERE tier IN ('A','B');

-- =====================================================================
-- P1-5 通知日志表（快提醒 + 慢汇总去重）
-- =====================================================================
CREATE TABLE IF NOT EXISTS biz.catalyst_notification_log (
    log_id              BIGSERIAL PRIMARY KEY,
    signal_id           BIGINT NOT NULL,         -- 快提醒=真实signal_id，慢汇总=-1（哨兵值，保证UNIQUE生效）
    notification_type   VARCHAR(32) NOT NULL,    -- fast_alert / slow_digest
    tier                VARCHAR(4),
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    subject             VARCHAR(256),
    status              VARCHAR(16) NOT NULL DEFAULT 'sent',  -- sent / failed / skipped
    error_msg           TEXT,
    UNIQUE (signal_id, notification_type)
);

COMMENT ON TABLE biz.catalyst_notification_log IS '催化剂邮件通知日志 + 24h 去重（signal_id + notification_type 唯一）';
COMMENT ON COLUMN biz.catalyst_notification_log.signal_id IS '信号ID；慢汇总用 -1 哨兵值（NULL 不触发 UNIQUE 约束）';

CREATE INDEX IF NOT EXISTS idx_cat_notif_type_time
    ON biz.catalyst_notification_log (notification_type, sent_at DESC);

-- =====================================================================
-- 迁移说明
-- =====================================================================
-- 执行方式：psql -d <db> -f fix_038_catalyst_decision_pipeline_P0.sql
-- 或在 scripts/migrations/ 下按编号顺序执行
-- 幂等性：全部使用 IF NOT EXISTS / DO $$ 安全包裹，可重复执行
