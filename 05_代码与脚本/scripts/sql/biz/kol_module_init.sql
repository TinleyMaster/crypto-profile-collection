-- ============================================================
-- KOL 信号监控模块 — 数据库初始化
-- 新增表：biz.kol_profile / biz.kol_post / biz.kol_signal
-- 新增平台：sys.source_platform.binance_square
-- ============================================================

-- 确保 biz schema 存在
CREATE SCHEMA IF NOT EXISTS biz;

-- ------------------------------------------------------------
-- 1. 博主档案表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.kol_profile (
    profile_id          BIGSERIAL PRIMARY KEY,
    platform_code       VARCHAR(32)     NOT NULL,
    platform_user_id    VARCHAR(128)    NOT NULL,
    nickname            VARCHAR(256)    NOT NULL,
    avatar_url          TEXT,
    follower_count      BIGINT          DEFAULT 0,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    first_seen_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_post_id        VARCHAR(128),
    last_crawled_at     TIMESTAMPTZ,
    win_rate            NUMERIC(5,2),
    total_signals       INTEGER         NOT NULL DEFAULT 0,
    notes               TEXT,
    extra_json          JSONB,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_kol_profile_platform_user
        UNIQUE (platform_code, platform_user_id),
    CONSTRAINT chk_kol_profile_platform
        CHECK (platform_code IN ('binance_square', 'twitter', 'telegram'))
);

CREATE INDEX IF NOT EXISTS idx_kol_profile_active
    ON biz.kol_profile (is_active) WHERE is_active = TRUE;

COMMENT ON TABLE  biz.kol_profile IS 'KOL 博主档案表';
COMMENT ON COLUMN biz.kol_profile.profile_id       IS '自增主键';
COMMENT ON COLUMN biz.kol_profile.platform_code    IS '平台编码，关联 sys.source_platform';
COMMENT ON COLUMN biz.kol_profile.platform_user_id IS '平台内用户 ID';
COMMENT ON COLUMN biz.kol_profile.nickname         IS '博主昵称';
COMMENT ON COLUMN biz.kol_profile.avatar_url       IS '头像 URL';
COMMENT ON COLUMN biz.kol_profile.follower_count   IS '粉丝数';
COMMENT ON COLUMN biz.kol_profile.is_active        IS '是否启用监控';
COMMENT ON COLUMN biz.kol_profile.first_seen_at    IS '首次发现时间';
COMMENT ON COLUMN biz.kol_profile.last_post_id     IS '最后一条已处理帖子的平台内 ID（增量抓取游标）';
COMMENT ON COLUMN biz.kol_profile.last_crawled_at  IS '最后一次抓取时间';
COMMENT ON COLUMN biz.kol_profile.win_rate         IS '历史胜率（回测计算后回填，0~100）';
COMMENT ON COLUMN biz.kol_profile.total_signals    IS '累计 prediction 信号数';
COMMENT ON COLUMN biz.kol_profile.notes            IS '备注';
COMMENT ON COLUMN biz.kol_profile.extra_json       IS '扩展字段（JSONB）';

-- ------------------------------------------------------------
-- 2. 帖子原文表（全量存档）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.kol_post (
    post_id             BIGSERIAL PRIMARY KEY,
    profile_id          BIGINT          NOT NULL REFERENCES biz.kol_profile(profile_id) ON DELETE CASCADE,
    platform_code       VARCHAR(32)     NOT NULL,
    platform_post_id    VARCHAR(128)    NOT NULL,
    content_text        TEXT            NOT NULL DEFAULT '',
    image_urls          TEXT[]          NOT NULL DEFAULT '{}',
    post_url            TEXT,
    posted_at           TIMESTAMPTZ     NOT NULL,
    raw_json            JSONB,
    ai_failed           BOOLEAN         NOT NULL DEFAULT FALSE,
    ai_retry_count      INTEGER         NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_kol_post_platform_post
        UNIQUE (platform_code, platform_post_id)
);

CREATE INDEX IF NOT EXISTS idx_kol_post_profile_id
    ON biz.kol_post (profile_id);
CREATE INDEX IF NOT EXISTS idx_kol_post_posted_at
    ON biz.kol_post (posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_kol_post_ai_failed
    ON biz.kol_post (ai_failed) WHERE ai_failed = TRUE;

COMMENT ON TABLE  biz.kol_post IS 'KOL 帖子原文表（全量存档）';
COMMENT ON COLUMN biz.kol_post.post_id          IS '自增主键';
COMMENT ON COLUMN biz.kol_post.profile_id       IS '关联博主 ID';
COMMENT ON COLUMN biz.kol_post.platform_code    IS '平台编码';
COMMENT ON COLUMN biz.kol_post.platform_post_id IS '平台内帖子 ID（去重用）';
COMMENT ON COLUMN biz.kol_post.content_text     IS '帖子正文文本';
COMMENT ON COLUMN biz.kol_post.image_urls       IS '图片 URL 列表';
COMMENT ON COLUMN biz.kol_post.post_url         IS '帖子原始链接';
COMMENT ON COLUMN biz.kol_post.posted_at        IS '发帖时间（平台时间）';
COMMENT ON COLUMN biz.kol_post.raw_json         IS '原始 API 响应 JSON（留底）';
COMMENT ON COLUMN biz.kol_post.ai_failed        IS 'AI 分析是否失败（失败待重试）';
COMMENT ON COLUMN biz.kol_post.ai_retry_count   IS 'AI 分析重试次数';

-- ------------------------------------------------------------
-- 3. AI 分析后的信号表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.kol_signal (
    signal_id           BIGSERIAL PRIMARY KEY,
    post_id             BIGINT          NOT NULL REFERENCES biz.kol_post(post_id) ON DELETE CASCADE,
    profile_id          BIGINT          NOT NULL REFERENCES biz.kol_profile(profile_id) ON DELETE CASCADE,
    asset_id            BIGINT          REFERENCES core.asset(asset_id) ON DELETE SET NULL,
    post_type           VARCHAR(20)     NOT NULL,
    direction           VARCHAR(10),
    symbol              VARCHAR(32),
    entry_condition     TEXT,
    entry_price         NUMERIC(20,8),
    stop_loss           NUMERIC(20,8),
    take_profit         NUMERIC(20,8),
    leverage            NUMERIC(8,2),
    already_entered     BOOLEAN         NOT NULL DEFAULT FALSE,
    has_pnl_number      BOOLEAN         NOT NULL DEFAULT FALSE,
    confidence          NUMERIC(4,3)    NOT NULL DEFAULT 0,
    is_alerted          BOOLEAN         NOT NULL DEFAULT FALSE,
    alerted_at          TIMESTAMPTZ,
    alert_failed        BOOLEAN         NOT NULL DEFAULT FALSE,
    alert_error         TEXT,
    -- 回测预留字段
    backtest_pnl            NUMERIC(20,8),
    backtest_hit_stop_loss  BOOLEAN,
    backtest_hit_take_profit BOOLEAN,
    backtest_hitted_at      TIMESTAMPTZ,
    backtest_done           BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_kol_signal_post_type
        CHECK (post_type IN ('prediction', 'after_action', 'analysis')),
    CONSTRAINT chk_kol_signal_direction
        CHECK (direction IN ('long', 'short', 'neutral', NULL)),
    CONSTRAINT chk_kol_signal_confidence
        CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX IF NOT EXISTS idx_kol_signal_post_id
    ON biz.kol_signal (post_id);
CREATE INDEX IF NOT EXISTS idx_kol_signal_profile_id
    ON biz.kol_signal (profile_id);
CREATE INDEX IF NOT EXISTS idx_kol_signal_asset_id
    ON biz.kol_signal (asset_id);
CREATE INDEX IF NOT EXISTS idx_kol_signal_post_type
    ON biz.kol_signal (post_type);
CREATE INDEX IF NOT EXISTS idx_kol_signal_created_at
    ON biz.kol_signal (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kol_signal_is_alerted
    ON biz.kol_signal (is_alerted);
CREATE INDEX IF NOT EXISTS idx_kol_signal_prediction_alert
    ON biz.kol_signal (post_type, already_entered, confidence, is_alerted)
    WHERE post_type = 'prediction' AND is_alerted = FALSE;

COMMENT ON TABLE  biz.kol_signal IS 'KOL 信号表（AI 结构化分析结果）';
COMMENT ON COLUMN biz.kol_signal.signal_id        IS '自增主键';
COMMENT ON COLUMN biz.kol_signal.post_id          IS '关联帖子 ID';
COMMENT ON COLUMN biz.kol_signal.profile_id       IS '关联博主 ID';
COMMENT ON COLUMN biz.kol_signal.asset_id         IS '关联币种 ID（匹配 core.asset）';
COMMENT ON COLUMN biz.kol_signal.post_type        IS '帖子类型：prediction/after_action/analysis';
COMMENT ON COLUMN biz.kol_signal.direction        IS '方向：long/short/neutral';
COMMENT ON COLUMN biz.kol_signal.symbol           IS '标的币种符号（原始提取）';
COMMENT ON COLUMN biz.kol_signal.entry_condition  IS '入场条件文本';
COMMENT ON COLUMN biz.kol_signal.entry_price      IS '明确入场价格';
COMMENT ON COLUMN biz.kol_signal.stop_loss        IS '止损价';
COMMENT ON COLUMN biz.kol_signal.take_profit      IS '止盈价';
COMMENT ON COLUMN biz.kol_signal.leverage         IS '杠杆倍数';
COMMENT ON COLUMN biz.kol_signal.already_entered  IS '博主是否已进场持仓';
COMMENT ON COLUMN biz.kol_signal.has_pnl_number   IS '是否出现具体盈亏数字';
COMMENT ON COLUMN biz.kol_signal.confidence       IS 'AI 分类置信度 0~1';
COMMENT ON COLUMN biz.kol_signal.is_alerted       IS '是否已发邮件提醒';
COMMENT ON COLUMN biz.kol_signal.alerted_at       IS '邮件发送时间';
COMMENT ON COLUMN biz.kol_signal.alert_failed     IS '邮件发送是否失败';
COMMENT ON COLUMN biz.kol_signal.alert_error      IS '邮件发送错误信息';
COMMENT ON COLUMN biz.kol_signal.backtest_pnl     IS '回测盈亏（预留）';
COMMENT ON COLUMN biz.kol_signal.backtest_hit_stop_loss IS '回测是否触发止损（预留）';
COMMENT ON COLUMN biz.kol_signal.backtest_hit_take_profit IS '回测是否触发止盈（预留）';
COMMENT ON COLUMN biz.kol_signal.backtest_hitted_at   IS '回测命中时间（预留）';
COMMENT ON COLUMN biz.kol_signal.backtest_done    IS '回测是否已完成（预留）';

-- ------------------------------------------------------------
-- 4. 新增平台记录
-- ------------------------------------------------------------
INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active)
VALUES ('binance_square', 'Binance Square', 'https://www.binance.com/zh-CN/square', '币安广场 — KOL 发帖平台', TRUE)
ON CONFLICT (platform_code) DO NOTHING;

INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active)
VALUES ('twitter', 'Twitter / X', 'https://x.com', 'Twitter/X 社交平台（预留）', FALSE)
ON CONFLICT (platform_code) DO NOTHING;

INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active)
VALUES ('telegram', 'Telegram', 'https://t.me', 'Telegram 频道（预留）', FALSE)
ON CONFLICT (platform_code) DO NOTHING;
