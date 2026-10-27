-- fix_117_l5_revenue_layer.sql
-- 工单：SOP-L5-001「L5 估值层无收入分母，P/S 与价值捕获无法判断」
--
-- 背景（2026-10-27 实测取证）：
--   L5「贵不贵」此前被判定为「空」——`biz.protocol_revenue` 全库仅 18 行、
--   且只覆盖 2 个资产（AVA / USD.AI），metric_name 是 loan_apr / lighthouse_ytd
--   这类人工录入的业务指标，**不是流水线产出**。等同没有。
--
--   实测可补性（DeFiLlama 免费接口，无需 key、无付费墙）：
--     /overview/fees?dataType=dailyFees           → 3084 个协议，2139 个 30d > 0
--     /overview/fees?dataType=dailyRevenue        → 2720 个协议，1711 个 30d > 0
--     /overview/fees?dataType=dailyHoldersRevenue → 1250 个协议，  388 个 30d > 0
--     /summary/fees/{slug}?dataType=dailyRevenue  → 单协议 2153 天日线
--   ⇒ 一次请求可拿全市场收入快照；单协议可拿 5.9 年日线。
--
--   落到本地资产池的覆盖率（市值 ≥10M 且有排名，684 只）：
--     有 revenue 数据 122 只（17.8%）、有 fees 数据 148 只、并集（含 tokenomics
--     页面 P/E）201 只（29.4%）。
--   ⇒ **天花板 29%，补不齐**。原因结构性：BTC/DOGE/XRP 这类没有「协议收入」
--     这个口径；ARKM 经核查映射正确（dl id 3269 = Arkham），是 DeFiLlama
--     本身不收录，无法自动化。
--   ⇒ 故本层必须允许 NULL，且读取方必须把 NULL 解释为「无法判断贵不贵」，
--     不得解释为「便宜」。这是 HC-006 同一类错误（环境缺失伪装成数据缺失）。
--
-- 本迁移建两张表（全部 IF NOT EXISTS，可重复执行）：
--   ① src_dl.protocol_fees         —— 快照层，一协议×一口径一行
--   ② biz.asset_revenue_snapshot   —— 业务层，一资产×一天一行（宽表，便于算 P/S）
--
-- 为什么分成两层：源层保留 DeFiLlama 的原始口径与 id，业务层按本地 asset_id
-- 聚合（一个资产可能对应多个 DL 子协议，如 aave-v2 / aave-v3）。
-- 语义分层与 src_dl.protocol_list → biz.protocol_metric_daily（TVL）保持一致。
--
-- 执行者：新建表，用业务连接即可（无需 postgres）。

BEGIN;

-- ── ① 源层：DeFiLlama 费用/收入快照 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS src_dl.protocol_fees (
    defillama_id  text         NOT NULL,
    data_type     text         NOT NULL,
    slug          text,
    name          text,
    category      text,
    total_24h     numeric(24,2),
    total_7d      numeric(24,2),
    total_30d     numeric(24,2),
    total_1y      numeric(24,2),
    fetched_at    timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT protocol_fees_pk PRIMARY KEY (defillama_id, data_type),
    CONSTRAINT protocol_fees_type_chk
        CHECK (data_type IN ('fees', 'revenue', 'holders_revenue'))
);

COMMENT ON TABLE  src_dl.protocol_fees IS
    'DeFiLlama 费用/收入快照（源层）。一协议×一口径一行，可由 ingest_dl_fees.py 幂等刷新。';
COMMENT ON COLUMN src_dl.protocol_fees.defillama_id IS
    'DeFiLlama 内部协议 id（对应 src_dl.protocol_list.protocol_id，或 core.asset_source_map.source_asset_key 当 source_code=dl）';
COMMENT ON COLUMN src_dl.protocol_fees.data_type IS
    'fees=用户支付的总手续费；revenue=协议留存份额；holders_revenue=分配给代币持有者的份额。三者不可互换，混用会把手续费规模当成协议收入，差一个量级。';

-- ── ② 业务层：按资产聚合的日快照 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS biz.asset_revenue_snapshot (
    asset_id         integer      NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    as_of            date         NOT NULL,
    fees_24h         numeric(24,2),
    fees_30d         numeric(24,2),
    fees_1y          numeric(24,2),
    revenue_24h      numeric(24,2),
    revenue_30d      numeric(24,2),
    revenue_1y       numeric(24,2),
    holders_rev_30d  numeric(24,2),
    holders_rev_1y   numeric(24,2),
    protocol_count   smallint,
    source_code      text         NOT NULL DEFAULT 'dl',
    fetched_at       timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT asset_revenue_snapshot_pk PRIMARY KEY (asset_id, as_of, source_code)
);

COMMENT ON TABLE  biz.asset_revenue_snapshot IS
    '资产级费用/收入日快照（L5-B 收入估值层）。NULL = 该资产无协议收入口径（如 BTC/DOGE）或 DeFiLlama 未收录（如 ARKM），不代表收入为 0。';
COMMENT ON COLUMN biz.asset_revenue_snapshot.revenue_30d IS
    '协议留存收入 30 天合计（USD）。P/S = 流通市值 / (revenue_30d × 12.1667)。';
COMMENT ON COLUMN biz.asset_revenue_snapshot.holders_rev_30d IS
    '分配给持币者的收入 30 天合计。比 revenue_30d 更贴近「持币者真拿到多少」，做价值捕获判断优先用它。';
COMMENT ON COLUMN biz.asset_revenue_snapshot.protocol_count IS
    '该资产聚合了几个 DeFiLlama 子协议（如 Aave 的 v2/v3 分别计数后求和）。>1 表示多部署求和，跨链同协议已去重到同一 id。';

CREATE INDEX IF NOT EXISTS idx_asset_revenue_snapshot_asof
    ON biz.asset_revenue_snapshot (as_of DESC);

COMMIT;
