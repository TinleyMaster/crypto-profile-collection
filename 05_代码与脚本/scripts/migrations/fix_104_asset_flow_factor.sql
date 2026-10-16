-- fix_104: 单币资金流因子快照（OBI / OFI-taker / 链上净流）（幂等）
--
-- 动机：把「利好 + 资金进」做成双确认。事件弹性（P1.6）回答「消息值不值钱」，
--       本表回答「资金有没有真进来」。两者同向 = 高置信；背离 = 需警惕。
-- 数据源：
--   OBI  biz.orderbook_depth_history  (symbol, interval='4h', bids_usd/asks_usd)  → 盘口买卖压力
--   OFI  biz.cg_taker_volume_hist     (symbol, interval='4h', taker_buy/sell_usd) → 主动成交失衡
--   链上  biz.onchain_netflow_hourly  (asset_id, netflow_usd)                     → 交易所净流入/出
-- 口径：各源以自身 MAX(ts) 为锚，取近 7d / 30d 窗口（源滞后时自动对齐，避免空窗）。

BEGIN;

CREATE TABLE IF NOT EXISTS biz.asset_flow_factor (
    asset_id          BIGINT  NOT NULL,
    as_of             DATE    NOT NULL,
    obi_7d            NUMERIC,   -- 盘口失衡 (bids-asks)/(bids+asks)，>0 买盘占优
    obi_30d           NUMERIC,
    taker_imb_7d      NUMERIC,   -- 主动成交失衡 (taker_buy-sell)/(buy+sell)，>0 主动买占优
    taker_imb_30d     NUMERIC,
    netflow_7d_usd    NUMERIC,   -- 链上净流（交易所口径）
    netflow_30d_usd   NUMERIC,
    netflow_7d_pct_mcap NUMERIC, -- 净流 / 市值（%）
    n_obi_7d          INTEGER,
    n_taker_7d        INTEGER,
    n_netflow_7d      INTEGER,
    flow_score        NUMERIC,   -- 综合资金流分 0-100
    flow_grade        VARCHAR(16),  -- 资金流入 / 中性 / 资金流出 / 无数据
    sources           TEXT,
    method_version    VARCHAR(8) NOT NULL DEFAULT 'v1',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, as_of)
);
CREATE INDEX IF NOT EXISTS idx_flow_factor_score ON biz.asset_flow_factor (flow_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_flow_factor_grade ON biz.asset_flow_factor (flow_grade);

COMMIT;
