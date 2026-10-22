# 落库核对表（P6 复验用）

**铁律：脚本返回 ok ≠ 已落库。** 必须用 SQL 逐项查表。以下表均已在本机实测存在。

## 通用前置

```python
from crypto_research.config import get_settings        # 必须用这个，不能 Settings()
from crypto_research.db.conn import get_connection     # 自动 commit / rollback
import crypto_research.persist as persist

s = get_settings()
with get_connection(s.database_url) as conn:
    ...
```
`get_connection` 池复用且**每次借出都把 `row_factory` 归位为 tuple_row**。若需 dict 行，在 `cursor(row_factory=dict_row)` 上指定，**不要**给 `conn.row_factory` 赋值（会毒化池）。

## 各 persist 函数契约

| 函数 | 目标表 | 幂等键 | 注意 |
|---|---|---|---|
| `persist_raises(conn, asset_id, protocol_name, raises)` | `asset_raises` | `(asset_id, round, raise_date)` | `raise_date` 为 NULL 时 PG 唯一约束**不生效**，函数内部改走 `WHERE NOT EXISTS`；返回 `{inserted, updated, skipped}` |
| `persist_related_assets(conn, asset_id, relations, as_of_date=None)` | `asset_related` | `(asset_id, related_symbol, relation_type)` | `relation_type` 只能是 10 个枚举之一（库内 CHECK）；返回 `{upserted, skipped, rejected, unresolved}`，**`rejected` 非空即写法有误** |
| `persist_protocol_tvl(conn, asset_id, tvl_usd, measured_at=None, source='DefiLlama', chains=None, protocol_slug=None, protocol_category=None, notes=None)` | `protocol_tvl` | `(asset_id, measured_at)` | `tvl_usd=None` 直接跳过，**不写 0 冒充** |
| `persist_catalysts(conn, asset_id, catalysts, symbol=None, source_code='research_manual')` | `asset_catalyst` | `(source_code, source_article_id)` | `article_id` 由 `(标题, 归一化发布时间)` 哈希；**不含 source_url**，否则同事实带/不带 URL 会建两行 |
| `persist_doc_summary(conn, asset_id, symbol, name, md_text, source_pages)` | `doc_whitepaper_summary` | 自动确保 `doc_asset` | 走 LLM，慢 |
| `persist_contract_security(conn, asset_id, chain, contract, ...)` | `asset_contract_security` | `(asset_id, chain, contract)` | — |
| `persist_holder_snapshot(conn, asset_id, chain, contract, snapshot_date, ...)` | `onchain_holder_snapshot/rank` | 快照日 | 先删后插 |
| `persist_research_report(conn, asset_id, symbol, report, report_md, ...)` | `research_report` + `thesis_forward_track` | `(asset_id, report_sha256)` | 建仓行 `thesis_forward_track` 的 `price_at` 取 `asset_market_daily` 最近收盘价，取不到留 NULL |

### `asset_related.relation_type` 合法值（库内 `ck_asset_related_type`）

```
sector_peer / ecosystem / narrative / upstream_downstream / funding_shared
etf_basket / cross_chain / fork_derivative / infra_dependency / other
```
`strength` 仅 `high|medium|low`。

### 催化剂幂等键的正确写法

```python
# ✅ 只含标题 + 归一化时间
seed = f"{title.strip()}|{norm_ts(published_at)}"
article_id = "rm_" + sha1(seed).hexdigest()[:24]

# ❌ 掺入 source_url —— 同一事实带 URL 与不带 URL 会算出两个 id，重复建行
```
归一化把 `2026-10-20T12:00:00Z` 与 `2026-10-20 12:00:00+00:00` 映射到同一字符串。

## 复验 SQL（逐项跑，不要只看返回值）

```sql
-- 报告
SELECT report_id, stance, score, confidence, length(report_md) md_len,
       report_sha256, email_status, generated_at
FROM biz.research_report WHERE asset_id = :aid ORDER BY created_at DESC LIMIT 1;

-- 前向跟踪
SELECT asset_id, as_of, price_at FROM biz.thesis_forward_track
WHERE asset_id = :aid ORDER BY as_of DESC LIMIT 1;

-- 融资
SELECT id, round, raise_date, amount, lead_investors, source
FROM biz.asset_raises WHERE asset_id = :aid ORDER BY raise_date DESC;

-- 关联资产
SELECT related_symbol, related_asset_id, relation_type, relation_direction,
       strength, confidence, as_of_date
FROM biz.asset_related WHERE asset_id = :aid ORDER BY strength, related_symbol;

-- 协议 TVL（每日一行）
SELECT tvl_id, tvl_usd, measured_at, source, chains, protocol_slug, notes
FROM biz.protocol_tvl WHERE asset_id = :aid ORDER BY measured_at DESC LIMIT 5;

-- 催化剂（研究型）
SELECT catalyst_id, source_code, source_article_id, title, published_at, event_category
FROM biz.asset_catalyst
WHERE asset_id = :aid AND source_code = 'research_manual'
ORDER BY catalyst_id DESC LIMIT 10;

-- 三因子
SELECT as_of, ar_72h_mean, ar_72h_median, hit_rate, n_events, elasticity_grade, elasticity_score
FROM biz.asset_price_elasticity WHERE asset_id = :aid ORDER BY as_of DESC LIMIT 1;

SELECT as_of, excess7_mean, n_events, reaction_grade, next_unlock,
       next_unlock_beneficiary, next_unlock_source
FROM biz.asset_unlock_reaction WHERE asset_id = :aid ORDER BY as_of DESC LIMIT 1;

SELECT as_of, flow_score, flow_grade, obi_7d, taker_imb_7d,
       netflow_7d_pct_mcap, sources
FROM biz.asset_flow_factor WHERE asset_id = :aid ORDER BY as_of DESC LIMIT 1;
```

## 字段名陷阱（实测踩过）

| 你以为 | 实际 |
|---|---|
| `research_report.report_date` | **`generated_at`** |
| `asset_klines.trade_date` | 无此列（日线在 `asset_market_daily.market_date`） |
| `onchain_netflow_hourly.ts` | **`bucket_hour`** |
| `github_repo_activity.activity_date` | 无此列 |
| `sop_switches.updated_at` | 无此列 |
| `sop_switches.api_kind` / `auto_flag` | **`data_kind`** / **`auto`** |

## 按 symbol 查询是陷阱

`biz.coin_basic` 有 **2,079 个重复 symbol**（CAT×19 / PEPE×18 / BTC×16 / META×15 / ETH×15 / DOGE×14 / MIM×12 / SOL×12）。

```sql
-- ❌ 会返回 16 行
SELECT * FROM biz.coin_basic WHERE coin_symbol = 'BTC';

-- ✅ 先解析、再用 asset_id
SELECT asset_id FROM biz.coin_basic WHERE coin_symbol = 'BTC' AND asset_id = :aid;
```
`core.asset` 同理：用 `DISTINCT ON (upper(canonical_symbol)) ... ORDER BY market_cap DESC` 取市值最大者。

## 三源断更自检

资金流三源任一对不上就会影响评级，跑之前先看：

```sql
SELECT 'OBI' k, max(ts) FROM biz.orderbook_depth_history
UNION ALL SELECT 'OFI', max(ts) FROM biz.cg_taker_volume_hist WHERE interval='4h'
UNION ALL SELECT 'netflow', max(bucket_hour) FROM biz.onchain_netflow_hourly;
```
阈值：OBI 24h / OFI 24h / 链上 48h。超了就 `python bin/ingest_cg_taker.py --stale` 补 OFI。
`asset_flow_factor.py` 会把断更源自动剔除出评分并在 markdown 里告警——**看到告警必须下调置信度**。
