-- fix_022: KOL 链上信号分类扩展
-- 对应工单 KOL-ONCHAIN-001
-- 新增 12 列支持 onchain 信号维度，存量数据自动归 trading

BEGIN;

-- 新增链上信号维度列
ALTER TABLE biz.kol_signal
  ADD COLUMN IF NOT EXISTS signal_category varchar(20) NOT NULL DEFAULT 'trading',
  ADD COLUMN IF NOT EXISTS signal_subtype varchar(40),
  ADD COLUMN IF NOT EXISTS event_direction varchar(20),
  ADD COLUMN IF NOT EXISTS from_address text,
  ADD COLUMN IF NOT EXISTS to_address text,
  ADD COLUMN IF NOT EXISTS event_amount numeric,
  ADD COLUMN IF NOT EXISTS event_token varchar(20),
  ADD COLUMN IF NOT EXISTS event_usd_value numeric,
  ADD COLUMN IF NOT EXISTS tx_hash text,
  ADD COLUMN IF NOT EXISTS event_exchange varchar(40),
  ADD COLUMN IF NOT EXISTS address_label varchar(60),
  ADD COLUMN IF NOT EXISTS event_time timestamptz;

COMMENT ON COLUMN biz.kol_signal.signal_category IS 'trading=交易喊单类, onchain=链上情报类, news=新闻资讯类';
COMMENT ON COLUMN biz.kol_signal.signal_subtype IS 'onchain 细分: whale_move/exchange_flow/liquidation/accumulation/distribution/smart_money';
COMMENT ON COLUMN biz.kol_signal.event_direction IS 'inflow/outflow/liquidated_long/liquidated_short/accumulating/distributing';

COMMIT;
