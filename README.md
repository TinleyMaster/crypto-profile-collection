# 加密货币投研资料采集系统 (Crypto Profile Collection)

一套**加密货币投研资料采集与沉淀系统**，采用 **Python 数据处理引擎 + Flask Web 工作台 + 定时调度器（scheduler.py）** 的架构。

核心思路：把"从多数据源抓取币种/协议资料，找到官网/白皮书/文档，再将文档变成可沉淀资产"这件事，拆成**层层可维护的流水线**，并在末端提供**投研分析工具箱**（代币经济学、解锁时间表、链上数据等）。

---

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│  biz    业务消费层  doc_source_entry / doc_asset             │
│                    research_url / coin_basic                 │
│                    asset_tokenomics / asset_token_unlocks    │
│                    onchain_holder_snapshot / transfer_log    │
│                    onchain_exchange_wallet                   │
│                    asset_social_heat / onchain_holder_snapshot   │
│                    asset_raises / asset_hacks                │
│                    asset_derivatives / daily_recommendation  │
│                    asset_market_daily                         │
│                    doc_source_notebooklm / research_notebook │
│                    research_thesis / research_message        │
│                    unlock_watchlist / dl_protocol_checked    │
│                    kol_profile / kol_post / kol_signal       │
├─────────────────────────────────────────────────────────────┤
│  core   统一实体层  asset / asset_source_map                  │
│                    asset_contract_map                        │
├─────────────────────────────────────────────────────────────┤
│  src_*  来源解析层  src_cmc / src_cg / src_dl / dexscreener  │
├─────────────────────────────────────────────────────────────┤
│  raw    原始响应层  api_response                             │
├─────────────────────────────────────────────────────────────┤
│  sys    系统元数据  ingest_run / source_platform              │
└─────────────────────────────────────────────────────────────┘
```

**主要数据源：**

| 数据源 | 用途 | 接口 |
|--------|------|------|
| CoinMarketCap | 币种信息 + URLs（官网/文档/GitHub/Twitter/Telegram/Reddit/Facebook） | CMC API |
| CoinGecko | 币种列表 + coin_info（links 提取文档入口） | CoinGecko API |
| DeFiLlama | 协议列表 + TVL（url/twitter 提取官网链接）+ 协议详情（audit_links 审计 / raises 融资轮次 / 评级页）+ /hacks 异常事件 | DL API |
| DexScreener | 无文档入口资产的兜底补充（官网/社交链接） | DexScreener API |
| Binance Web3 | 无文档入口资产的兜底补充 + 每日投研推荐 | Binance Web3 API |
| Ethplorer | 链上持仓快照（Top 持有者、持仓集中度） | Ethplorer API |
| Tokenomist / tokenomics.com | 代币解锁时间表 + 代币经济学四板块（overview / unlocks / revenue / valuation） | 网页爬取（Playwright 无头浏览器） |
| Binance / OKX / Bybit / Bitget / Gate 永续合约公开接口 | 衍生品资金面（资金费率 / OI / 成交，本地多交易所求和） | 匿名 REST API |
| Binance Square（币安广场） | KOL 交易信号监控（实时喊单识别 + 邮件提醒） | Playwright 无头浏览器 + bapi 接口 |

---

## 文档处理流水线

```
Phase A: 资产核心构建
  CMC/CG/DL 数据源 → 跨源匹配 → core.asset（统一实体）
  去重（按合约地址）、coin_basic 基础数据
  搜索回退自动入库：core.asset 搜不到时从 src_cmc 自动补录
            │
            ▼
Phase B1: 文档入口发现              ← 已完成
  CMC/CG/DL 三大数据源提取官网/文档链接
  双源补充（DexScreener + Binance Web3）兜底无文档入口资产
  biz.doc_source_entry ~220,000+ 条
            │
            ▼
Phase B2: 深度文档发现              ← 进行中
  从官网和文档页 HTML 中进一步抓取嵌入的 PDF/白皮书链接
  并发 8 worker，优先处理 official_website → docs
  聚合类域名阻断（30+ 域名，防止跨资产污染）
  SPA 检测：识别 JS 渲染页面，标记 needs_browser=TRUE
  单资产重新爬取：B2→B3→B2 循环最多 6 轮，覆盖 about/team/roadmap 等子页面
            │
            ▼
Phase B3: 无头浏览器爬取            ← 进行中
  用 Playwright 渲染 JS 页面，提取 B2 静态爬取无法处理的 SPA 网站链接
  4 并发浏览器窗口，8 秒超时，HEAD 预检跳过非 HTML 内容
  SPA 回溯扫描：重检历史已爬取页面，识别遗漏的 SPA 页面
  写入的链接回流到 B2 继续深度爬取，形成闭环
            │
            ▼
Phase B4: AI 噪声清理               ← 进行中
  多层防御：源头阻断 → 规则直删 → AI 按资产分组判断
  密度触发 + 项目标识匹配：同一域名下 >5 条链接时触发拦截
  审计报告白名单保留：AI 提示词携带资产上下文，避免误删本项目审计
  关联 >50 资产的非审计域名 → AI 误判纠正机制
  单资产域名 >100 链接且占比 >90% → 重置 re-evaluate
            │
            ▼
Phase C: 投研分析提取              ← 进行中
  代币经济学提取（tokenomics.com 四板块优先，未命中弹网址框 → URL/AI 测算）
  代币解锁测算（tokenomics.com 四板块 + 未命中弹网址框 → URL/AI 测算）
  链上数据分析（区块浏览器 HTML 解析持仓集中度 + 大额转账告警）
  社交热度（单币按需：社区规模 + 实时舆情 + 趋势新闻 + 市场热度）
            │
            ▼
Phase D: 一键投研（NotebookLM 风格） ← 进行中
  单代币对应一个笔记本，自动收集全部已采资料快照 + 21 类完整性清单
  缺失项一键补齐：按缺失类型映射动作串行执行（deep/spa/第三方/AI 分类等）
  AI 问答（RAG）：严格依据资料库回答，强制 [编号] 标注来源
  对话与资料快照持久化（biz.research_notebook / biz.research_message）
```

---

## 第三方专项数据源（Phase B2 third_party 扩展）

在深度文档发现之外，针对「审计 / 第三方评级 / 融资 / 链上异常」四类投研资料，从 DefiLlama 协议详情与 `/hacks` 接口结构化补入：

| 脚本 | 数据 | 落库 |
|------|------|------|
| `phase_b2_third_party.py` | DefiLlama `audit_links`（审计报告链接）+ 协议页（第三方评级） | `biz.doc_source_entry`（content_topics=audit / third_party_rating） |
| `phase_b2_third_party_raises.py` | DefiLlama `raises` 字段（TGE / 融资轮次） | `biz.asset_raises`（结构化表） |
| `phase_b2_third_party_hacks.py` | DefiLlama `/hacks` 全量异常事件 | `biz.asset_hacks`（结构化表） |

- 审计/评级是「URL 维度」，落 `doc_source_entry`；融资/异常是「结构化维度」（无稳定可映射 URL），落独立表。
- raises/hacks 均按 `defillamaId` → `src_dl.protocol_list` → `core.asset_source_map` 映射到资产；无法映射的直接跳过。
- raises 用 `biz.dl_protocol_checked` 标记断点续跑；hacks 全量一次性拉取。

---

## 文档链接统一分类体系

对 `biz.doc_source_entry` / `biz.doc_asset` / `biz.research_url` 的全部链接，统一做「**来源类型 + 内容主题**」两个正交维度的分类，取代早期分散在 `infer_entry_type` / `infer_doc_type` / `_classify_url` 三处不一致的规则。

**统一 taxonomy（单一数据源，`mapping/taxonomy.py`）**
- `SOURCE_TYPES` 来源类型：`official_website` / `docs` / `docs_portal` / `whitepaper_page` / `github` / `medium` / `announcement` / `twitter` / `telegram` / `reddit` / `facebook` / `other`
- `CONTENT_TOPICS` 内容主题（20 类多标签）：`whitepaper` / `docs` / `audit` / `deck` / `tokenomics` / `research` / `announcement` / `roadmap` / `tge_ido` / `lp_liquidity` / `treasury_multisig` / `team_vc` / `dao_governance` / `bug_bounty` / `exchange_listing` / `competitor` / `major_event` / `third_party_rating` / `onchain_abnormal` / `other`
- `CONTENT_TOPIC_KEYWORDS`：每个主题的关键词规则（多词 `-`/`_` 归一化为空格后匹配）
- `DOMAIN_SOURCE_TYPES`：域名 → 来源类型（github.com→github、x.com→twitter 等）

**两层分类**
1. **L1 规则（免费、确定性）**：`mapping/classify_link.py` 的 `classify_link()`，按优先级「域名规则 → CMC `url_key` 元数据 → 标签/URL 关键词 → 兜底」判定，输出 `method` + `confidence`：
   - `domain` 0.98 / `url_key` 0.9 / `keyword` 0.6 / `default` 0.3（主题 `["other"]`）
2. **L2 AI 内容分类**：`llm_client.batch_classify_content_topics()` + `backfill_ai_classify_links.py`，对 L1 低置信度项（`default`/0.3）抓页面正文（HTML 去标签 / PDF 用 PyPDF2），喂 LLM 输出对齐 20 类 taxonomy 的多标签 + `confidence` + `reason`，回写 `classify_method='ai_content'`（正文明确 0.8~0.95，仅凭 URL 0.5~0.7）。

**落库字段**：每条链接最终带 `content_topics TEXT[]` + `classify_method TEXT` + `classify_confidence REAL`，供一键投研的 21 类缺失清单精确判定。

**新链接分类的两阶段流程**

B2 深度爬取发现的新链接不是一步到位精确分类，而是「规则初分 → AI 精分」递进：

1. **阶段 0：B2 即时分类（规则，秒级）**
   - B2 每抓到一个新链接，立刻调用 `classify_link()` 按域名 / URL 关键词规则初分，写入 `content_topics` + `classify_method` + `classify_confidence`。
   - 域名精确匹配（0.98）/ CMC url_key 元数据（0.9）/ URL 关键词命中（0.6）/ 兜底 `other`（0.3）。
   - **局限**：很多文档页 URL 路径是语义化的（如 `/getting-started`、`/core-concepts`），不含特征关键词，会被暂标为 `other`（置信度 0.3）。

2. **阶段 1：规则回填（`backfill_classify_links.py`）**
   - 对存量 `content_topics IS NULL` 的记录，用统一分类器补充分类（利用 CMC url_key 元数据等 B2 时拿不到的信息）。
   - 纯规则、免费、速度快。

3. **阶段 2：AI 内容分类（`backfill_ai_classify_links.py`，调度器每日 10:00 跑）**
   - 对阶段 0/1 低置信度项（`classify_method='default'`，即 `content_topics=['other']`），抓页面正文（HTML 去标签 / PDF 抽文本），喂 LLM 做多标签分类。
   - 回写 `classify_method='ai_content'` + 置信度 + 分类理由。
   - 抓不到正文的 SPA 页面 → 标 `ai_failed` + `needs_browser=TRUE`，交 B3 无头浏览器重抓后再分类。

**结论**：新爬取的链接刚入库时分类可能不准（尤其是 docs 类很多暂标 `other`），过一天 AI 分类跑完后质量会被修正到可用水平。

**回填脚本**
- `backfill_classify_links.py`：阶段1，规则 + 元数据（已全量跑通）
- `backfill_ai_classify_links.py`：阶段2，AI 内容分类（主键分页 + 并发抓正文 + LLM 批量分类 + 断点续跑）

---

## 分赛道体系（sector.py 单一数据源）

所有赛道相关的分类、评分、采集优先级、解锁预警规则统一收敛在 `crypto_research/mapping/sector.py`，各脚本禁止重复硬编码赛道规则。

**赛道枚举（12 类）**：`l1`（L1 公链）/ `l2`（L2 二层）/ `defi` / `meme` / `gamefi` / `rwa` / `ai` / `cex_token`（平台币）/ `derivatives` / `depin` / `infra` / `other`。

**主赛道判定**：`core.asset.primary_sector` 是统一主赛道字段，由 `refresh_asset_sectors.py` 从 CMC tags + category_hint 归一化写入（`CMC_TAG_SECTOR_MAP` / `CMC_CATEGORY_SECTOR_MAP` 只映射「明确赛道」标签，ecosystem 类不参与判定，避免误判）；取置信度最高者为 `primary_sector`，无明确赛道回退 `other`。

**分赛道评分权重**（`SECTOR_SCORE_WEIGHTS`）：市场五维（volume / change_24h / txns / buy_ratio / momentum）按赛道微调，`get_sector_weights()` 未知赛道回退 `DEFAULT_SCORE_WEIGHTS`。例如 Meme 重涨幅与买入占比、DeFi 重交易量与流动性。

**分赛道采集优先级**：`SECTOR_COLLECT_PRIORITY`（资产级，L2 95 / AI 90 最高、meme 45 最低）+ `SECTOR_TOPIC_PRIORITY`（主题级，对齐 `taxonomy.CONTENT_TOPICS`）。`select_target_assets.sql` 的 `sector_priority` CASE 映射必须与 `SECTOR_COLLECT_PRIORITY` 保持一致。

**分赛道资料展示**（`get_sector_visible_material_keys` / `topic_priority_rank`）：投研页「资料完整性」清单按赛道只展示该赛道关心的资料类型——基础资料（官网/白皮书/GitHub/合约/链上/社交）所有赛道展示；代币解锁数据除 Meme（无 vesting）外均展示；主题类资料按 `SECTOR_TOPIC_PRIORITY` 命中展示。

**分赛道解锁预警**（`SECTOR_UNLOCK_ALERT_DAYS`）：L2 / AI 提前 21 天预警（解锁密集/叙事敏感），多数赛道默认 14 天，Meme 无 vesting 不解锁预警、改监控大户转账（`phase_watchlist_monitor.py` 复用 `unlock_alert_sent_at` 字段去重）。

---

## Phase C：投研分析工具箱

针对单个资产的深度投研分析，从 Web 工作台的"投研分析"面板触发。

### 代币经济学提取

从 tokenomics.com 结构化数据或文档中提取代币经济学数据，写入 `biz.asset_tokenomics`。

**优先数据源：tokenomics.com 结构化平台**

命中后直接使用平台结构化数据入库，**跳过文档解析与 LLM**，并将四个子板块分别保存：

- **Overview**：TGE 日期、总供应量、分配表、投资者轮次、FAQ
- **Unlocks**：释放进度、下一次解锁、Cliff 解锁事件列表
- **Revenue**：协议收入 FAQ、收入报表（原文文本 + 表格）
- **Valuation**：FDV、P/E 等估值数据（原文文本 + 表格）

**提取流程（三层降级）：**
```
① tokenomics.com 优先：slug 推断 + 无头浏览器爬取四板块
   ├─ 命中 → 四板块分别入库（overview/unlocks/revenue/valuation），跳过 LLM
   └─ 未命中 ↓
② 前端弹网址输入框：用户提供 tokenomics / 白皮书网址
   ├─ 有网址 → Playwright 抓取该网址 → LLM 提取结构化字段
   └─ 无网址 ↓
③ AI 测算：收集全部文档链接 → LLM 筛选相关链接 → 抓取 → LLM 提取
```

**数据来源（AI 测算时）：**
1. **文档层**：tokenomics / whitepaper / docs 类型的文档
2. **网页层**：官网 deep_crawl 子页面
3. **API 层**：CMC 市场数据 + CoinGecko supply 数据（补充总/最大/流通供应）

**提取字段：** total_supply、max_supply、circulating_supply、buy/sell tax、contract_renounced、lp_locked、allocation（分配比例）、burn_info、emission_schedule、governance_info、utility_info 等。

**数据表：** `biz.asset_tokenomics`（按 asset_id 唯一，ON CONFLICT 更新）

### 代币解锁测算

从 [tokenomics.com](https://app.tokenomics.com/)（原 Tokenomist / TokenUnlocks）用 Playwright 无头浏览器爬取解锁时间表。

**为什么不用 API：** Tokenomist API 按次收费，单币投研场景用爬虫更经济。

**爬取内容（四板块）：**
- **Overview 页面**：TGE 日期、总供应、释放进度、市值、FDV、流通率、分配表、投资者轮次、FAQ
- **Unlock Events 页面**：Cliff 解锁事件列表（日期、解锁价值、释放比例、分配类别数、状态）
- **Revenue 页面**：协议收入 FAQ + 收入报表
- **Valuation 页面**：FDV、P/E 等估值数据

**查询流程（缓存优先 + 三层降级）：**
```
① 缓存优先：命中 biz.asset_token_unlocks 缓存则秒级返回
② tokenomics.com 爬取：slug 推断（CG ID → symbol/name → 搜索兜底）
   ├─ 命中 → 四板块分别入库
   └─ 未命中 ↓
③ 前端弹网址输入框：用户提供 tokenomics 网址
   ├─ 有网址 → 提取 slug 重爬 tokenomics.com，仍失败则回退 AI
   └─ 无网址 ↓
④ AI 测算：基于代币经济学数据 + 当前价格/市值/FDV 估算解锁时间表
   （价格/市值/FDV 缺失时直接报错，不调用 AI）
```

**数据表：** `biz.asset_token_unlocks`（按 asset_id 唯一）

**注意：**
- 自动关闭 CLI 广告弹窗（Dismiss / Escape）
- slug 推断优先使用 CoinGecko ID，兜底 symbol/name；搜索 API 被 Cloudflare 拦截时改用无头浏览器首页搜索
- 免费版只能看到 Cliff 大额解锁事件，逐日解锁数据需要 Pro
- AI 测算结果带 methodology（数据来源/关键假设/计算步骤/置信度）和 input_snapshot 供核验

### 解锁追踪列表（Watchlist）

手动将代币加入解锁追踪列表，后台定期监控价格跌幅与解锁到期，触发邮件提醒。

**功能：**
- **加入追踪**：记录加入时价格（entry_price），可设置目标解锁日期、解锁占比、做空计划备注
- **列表展示**：跌幅（相对 entry_price）、到期天数、临近（≤14 天）/逾期标记
- **后台监控**：`phase_watchlist_monitor.py` 定期检查，触发两类提醒：
  1. 解锁到期提醒：target_unlock_date 距今 ≤ `UNLOCK_ALERT_DAYS`（默认 14 天）且未提醒过
  2. 空头趋势提醒：最新价相对 entry_price 跌幅 ≤ `-TREND_DROP_PCT%`（默认 -15%）且未提醒过
- **邮件通知**：SMTP 配置（SMTP_HOST/PORT/USER/PASS/TO/FROM）

**数据表：** `biz.unlock_watchlist`（按 asset_id 唯一）

### 链上数据分析

分层策略——**告警常驻 + 快照每日 + 明细按需**：

| 层级 | 功能 | 触发方式 | 数据表 | 状态 |
|------|------|---------|--------|------|
| 快照层 | 持仓集中度 / Holder 数 | 每日单次全量 | `biz.onchain_holder_snapshot` | 运行中 |
| 告警层 | 大额转入交易所 | 后台自动循环 | `biz.onchain_transfer_log` | 已隐藏（大部分链无 API Key） |
| 明细层 | 持仓 + 大额转账明细 | 投研按需查询 | `biz.onchain_holder_snapshot` / `onchain_transfer_log` | 可用 |

**持仓分布数据来源：** 优先使用区块浏览器 HTML 解析（BSCScan/Etherscan 等 BeautifulSoup 抓取），Base 链优先 Blockscout 免费 REST API；不依赖 Etherscan API（BSCScan 已无免费 API）。

### 社交热度

单币按需拉取，四维度加权综合评分（0-100），结果写入 `biz.asset_social_heat` 缓存。

| 维度 | 数据来源（免费公开 API） | 指标 |
|------|------------------------|------|
| 社区规模 | CoinGecko `/coins/{id}`（community_data） | Twitter 粉丝、Reddit 订阅、Telegram 成员、GitHub Stars |
| 实时舆情 | Reddit 搜索 JSON + Google News RSS + LLM 情绪分析 | 情绪正负、情绪分、看涨/看跌占比、热点主题 |
| 趋势新闻 | CoinGecko `/search/trending` + `/coins/{id}/status_updates` + Google News | 热搜位次、项目动态、相关新闻 |
| 市场热度 | CoinGecko `/coins/{id}`（market_data） | 24h 成交、涨跌幅、市值排名 |

**综合评分：** 社区规模 25% + 舆情情绪 30% + 趋势新闻 25% + 市场热度 20%，缺失维度自动剔除并重新归一化；输出 `confidence`（high/medium/low，按可用维度数）。

**容错：** 各维度独立 try/except，单源失败不阻断整体；无 CoinGecko 映射且各源均无数据时返回 `not_found`；CoinGecko demo key 限流(429)时自动重试并回退公共 API；Reddit 无鉴权接口常被 403 拦截，自动降级到 Google News；搜索词附带 `crypto` 关键词以消除通用符号歧义（如 APR=年利率）。LLM 情绪仅在拿到 Reddit/项目动态/新闻文本时调用。本期不含 X/Twitter 抓取（免费 API 已停用，爬取脆弱）。

**数据表：** `biz.asset_social_heat`（按 asset_id 唯一），含 `community_json` / `sentiment_json` / `trend_json` / `market_json` / `score_detail_json` / `methodology_json` / `input_snapshot_json`。

### 竞品结构化对比

`get_sector_competitors()` + `GET /api/research/<asset_id>/competitors`：按 `primary_sector` 找同赛道币，聚合关键指标横向对比（目标币置顶）。

**对比维度：** 基础（市值 / FDV / 价格）、代币经济学（总供应 / 流通量 / 未流通占比 / 买卖税）、链上（Top10 集中度 / 持有者数）、社交（热度分 / X 粉丝）、解锁（未来 30 天解锁 %）、融资（轮数 / 总额）。市值/FDV/价格从 `biz.asset_token_unlocks.input_snapshot_json` 降级取，无数据则为空。

### 情绪 × 价格 × 链上 背离检测

`get_divergence_signals()` + `GET /api/research/<asset_id>/divergence`：基于社交热度分 + 价格涨跌幅 + 链上持仓/转账变化率，识别两类经典背离（各条件满足 ≥3 条才触发）：

- **顶部背离（看空）**：情绪高涨（>65）+ 价格滞涨（24h<5% 或 7d<10%）+ 链上资金流出（鲸鱼 7d 减持 >3% 或 24h 大额转入交易所 >市值 0.1%）→ 散户 FOMO 而聪明钱出货。
- **底部背离（看多）**：情绪低迷（<35）+ 价格抗跌（24h>-5% 或 7d>-10%）+ 链上资金流入（鲸鱼 7d 增持 >3% 或持有者 7d 增长 >5%）→ 散户绝望而聪明钱吸筹。

输出信号列表 + 各维度原始指标 + 数据可用性；`severity` 按命中条件数分 high/medium。

### 衍生品 / 资金面数据（多交易所聚合）

`get_asset_derivatives()` + `derivatives_client.py` + `GET /api/research/<asset_id>/derivatives`：匿名调用 Binance / OKX / Bybit / Bitget / Gate 五家公开 REST 接口，本地并行探测 + 聚合求和：

- **资金费率**：实时 + 7d/30d 历史平均（按 OI 加权）。
- **未平仓合约 OI**：总价值 + 24h 变化。
- **CVD 成交净流入**：拉近期成交本地计算主动买/卖净额。

仅主流币有永续合约，Meme 小币无合约时返回「未找到衍生品数据」；结果 15 分钟 DB 缓存（`biz.asset_derivatives`，含 `exchanges_json` / `available_exchanges`）。

### 链上 CEX 净流入（方案 B）

`get_cex_netflow()` + `GET /api/research/<asset_id>/cex-netflow`：不依赖 CryptoQuant，用已有 `biz.onchain_transfer_log` + `biz.onchain_exchange_wallet`（交易所钱包地址库，16 个高可信度 CEX 钱包种子）自己计算。

CEX Netflow = 从交易所转出金额 − 转入交易所金额（正值=提币到链上，潜在看涨；负值=充值到交易所，潜在抛压），输出 24h/7d 净流入 + 按交易所分组明细。主流币链上监控采集中，Meme 小币暂不纳入。

### 鲸鱼 / 聪明钱行为流

`get_whale_flow()` + `GET /api/research/<asset_id>/whale-flow`：不依赖预定义鲸鱼地址标签，从两个维度综合判断增持/减持方向：

1. **持仓集中度变化**（`biz.onchain_holder_snapshot` 最新快照的 `whale_balance_change_7d/30d_pct` + Top10/Top100 集中度）。
2. **大额转账流向**（`biz.onchain_transfer_log` 按金额分级 >1M / 100K-1M / 10K-100K + 交易所进出方向分类）。

`_compute_whale_signal()` 加权评分（鲸鱼 7d 持仓 40% + 鲸鱼 30d 持仓 20% + 24h 净流向 25% + 7d 净流向 15%），归一化到 -100~+100：>20 增持、<-20 减持、否则中性，输出 `stance` + `confidence` + 因子明细 + Top 大额转账明细。

### 链上快照趋势化

`get_onchain_holder_trend()` + `GET /api/onchain/holder/<asset_id>/trend?days=30`：持仓集中度（Top10/Top50/Top100）、持有者数、鲸鱼持仓变化等指标按快照日期展开为时间序列，前端 SVG sparkline 迷你趋势图展示 7/30 天变化率，支持按链切换。

### 市值分层（排序层）

`get_market_tier()` + `search_assets(tier=...)` + `GET /api/search?q=...&tier=top100`：基于 CMC 排名将资产分为四层，投研结论按分层圈定范围，避免被小币稀释失真。

| 分层 | CMC 排名 | 说明 |
|------|---------|------|
| `top100` | 1-100 | 主流币，数据最齐全 |
| `top500` | 1-500 | 中盘币，投研主力范围 |
| `top1000` | 1-1000 | 小盘币，需谨慎 |
| `other` | >1000 或无排名 | 土狗/新币，高风险 |

搜索接口、社交热度排行榜、覆盖率统计、相关性矩阵等均支持 `tier` 参数过滤。

### 行情历史落库

`etl_asset_market_daily_from_cmc.py`：从 CMC 快照（`src_cmc.cmc_quote_snapshot`）按日聚合，UPSERT 到 `biz.asset_market_daily`，零额外 API 成本。

- **粒度**：日级（每日最后一条快照）
- **字段**：price_usd / market_cap / fdv / volume_24h / change_24h / change_7d / circulating_supply / total_supply
- **API**：`GET /api/research/<asset_id>/market-history?days=30`
- **用途**：解锁事件研究、相关性矩阵、信号层异动检测、趋势分析等所有时间序列功能的地基

### 信号层（异动检测 diff）

`detect_asset_signals()` + `GET /api/research/<asset_id>/signals`：机器擅长发现 diff，不擅长判断重要性——只做异动检测 + 严重程度分级，不做投资建议。

| 信号类型 | 触发条件（warning / critical） |
|---------|-------------------------------|
| 价格异动 | 24h 涨跌 ≥15% / ≥30% |
| 成交量放大 | 24h 量 vs 7 日均量 ≥2x / ≥5x |
| OI 异动 | 24h OI 变化 ≥±20% / ≥±50% |
| 资金费率极端 | 费率 > 0.05% 或 < -0.05%（warning） |
| 解锁临近 | 30 天解锁 ≥5% / ≥20% |

按 severity（critical > warning）排序，输出信号列表 + 各维度原始值。

### 研究结论生成（四维模板化）

`generate_research_thesis()` + `POST /api/research/<asset_id>/thesis`：基于资料库 LLM 生成结构化研究结论（`stance` 看多/看空/中性 + `conviction` 置信度 + 论点/风险/催化剂/关键指标），严格依据资料库事实并 `[编号]` 标注来源，附加抛压评分作量化辅助；每次生成追加一条新记录（`biz.research_thesis`），保留历史版本用于「当时判断 vs 后续走势」回溯。

**四维框架模板化**：结论强制按「**估值 / 筹码 / 情绪 / 催化**」四维组织，每维包含 summary + 要点列表 + 引用，而非 LLM 自由发挥。LLM system prompt 注入四维框架说明 + 衍生品/链上定量指标说明，确保每维都挂数据、挂引用。

**定量指标注入**：thesis 生成时自动采集 8 项衍生品指标（资金费率 / OI / OI 变化 / CVD / 多空比等）+ 链上持仓数据 + 解锁压力数据，作为定量部分喂给 LLM，避免结论只谈定性。

**解锁事件研究**：`analyze_unlock_event_impact()` + `GET /api/unlocks/event-impact/<asset_id>`——基于历史解锁事件做事件研究法分析，计算每个事件前后 N 天的 pre_return / post_return / max_drawdown / max_rally / volume_surge，汇总平均收益、正收益占比、大额解锁单独统计，用于 thesis 中解锁催化维度的量化支撑。

**相关性矩阵**：`compute_correlation_matrix()` + `GET /api/research/correlation-matrix`——基于日收益率的 Pearson 相关系数，支持按市值分层取 top N 或指定资产列表，输出 N×N 矩阵 + Top 正相关/负相关配对，用于判断赛道联动性与 Beta 暴露。

**社媒趋势排行榜**：`get_social_heat_leaderboard()` + `GET /api/social/leaderboard`——按市值分层展示社交热度最高的资产（综合评分 / 情绪分 / 热搜排名）+ 情绪分布统计（positive/neutral/negative），用于发现市场情绪热点。

**覆盖率统计按分层**：`get_coverage_by_tier()` + `GET /api/coverage-by-tier`——5 个分层（top100/top500/top1000/other/all）× 6 个维度（白皮书/代币经济/社交/解锁/衍生品/链上），直观展示各分层的数据完整度。

---

## KOL 信号监控模块

监控币安广场等平台的交易类 KOL 发帖，通过 AI 自动区分「实时喊单」与「事后晒单」，只对有跟单价值的实时信号发邮件提醒，同时全量存档用于后续回测博主胜率。

访问路径：`/kol`（Web 工作台 → KOL 信号监控面板）。

### 核心流程

```
博主配置（DB INSERT，不改代码）
        │
        ▼
Playwright 无头浏览器抓取（30 秒轮询，按博主增量拉取）
  币安广场：拦截 /bapi/composite/* 接口 + __NEXT_DATA__ 兜底
        │
        ▼
帖子存档（biz.kol_post，平台+帖子ID 唯一约束去重）
        │
        ▼
LLM AI 结构化分类（11 个字段，规则强制校验）
  post_type / direction / symbol / entry_condition / entry_price
  stop_loss / take_profit / leverage / already_entered / has_pnl_number / confidence
        │
        ▼
币种匹配（core.asset，精确匹配 + 50+ 别名 + 交易对后缀剥离）
        │
        ▼
信号过滤 → 邮件提醒（仅 prediction + 未进场 + 置信度≥0.8）
  邮件内容：信号详情 + 原文摘要 + 交叉验证数据（解锁/转账/资金费率/OI）
```

### AI 分类规则（优先级从高到低）

1. **`already_entered = true` → 强制 `after_action`**：博主明确说已进场/持仓/开仓
2. **`has_pnl_number = true` → 强制 `after_action`**：出现具体盈亏数字（+2341 USDT 等）
3. **晒持仓截图 / 回顾性语言** → 倾向 `after_action`（"我说过""果然""已经突破"）
4. **明确入场条件 + 未进场 + 无盈亏数字 + 无持仓截图** → `prediction`（实时喊单）
5. **只讲方向无明确操作** → `analysis`（纯行情分析）

### 数据库表（biz schema）

| 表 | 用途 | 关键字段 |
|----|------|---------|
| `biz.kol_profile` | 博主档案 | platform_code, platform_user_id, nickname, follower_count, is_active, last_post_id, win_rate, total_signals |
| `biz.kol_post` | 帖子原文（全量存档） | platform_post_id（唯一）, content_text, image_urls, posted_at, raw_json, ai_failed, ai_retry_count |
| `biz.kol_signal` | AI 分析信号 | post_type, direction, symbol, entry_price, stop_loss, take_profit, leverage, already_entered, has_pnl_number, confidence, is_alerted, backtest_*（回测预留） |

### Web 面板功能（3 个 Tab）

- **信号列表**：按类型/方向/博主筛选，置信度进度条，告警状态，详情弹窗（原文 + AI 结构化 + 图片）
- **博主管理**：新增/启用/停用博主，粉丝数、信号数、胜率展示，**手动立即抓取**按钮
- **帖子存档**：全量帖子浏览，AI 分析状态，原文链接

### 调度方式

- **主监控**：`kol_daemon.py` 常驻守护进程（30 秒间隔，由 supervisord 管理）
- **兜底调度**：`kol_monitor_run.py` 注册到 scheduler.py（每 5 分钟，防止常驻进程挂掉）

### 可扩展性

- 新增平台只需继承 `BaseScraper` 并注册到 `_SCRAPER_REGISTRY`
- AI 分类、过滤、邮件、存档逻辑全部复用
- 首期支持币安广场，预留 Twitter/X、Telegram 扩展位

### 本期不做

- 自动跟单交易（只做信号提醒，不接交易所 API）
- 历史信号回测逻辑（只预留数据库字段）
- 图片 OCR 识别持仓截图（首期通过文本上下文判断）

---

## 每日投研推荐

基于市场数据驱动的投研价值评分，从 Binance Web3 API + CMC API 双源交叉验证：

1. **评分维度**：24h 交易量、价格涨跌幅、交易笔数、买入占比、短期动量；评分权重按代币赛道（`SECTOR_SCORE_WEIGHTS`）微调，未知赛道回退默认权重
2. **跨源匹配（按合约地址）**：CMC 提取 `platform.token_address`，与 Binance 数据以**合约地址**作为唯一键交叉匹配（EVM 合约地址统一小写归一化，非 EVM 如 Solana base58 保持原样），无合约地址时回退 symbol。避免同名 symbol 的不同代币（如多个「牛来」meme）互相污染 name/contract
3. **交叉验证共识**：双源共识标记（2/3 表示双源命中，1/3 表示单源）
4. **前端展示**：默认显示 5 个代币，点击"加载更多"展示全部
5. **信息展示**：项目名称、合约地址、交易量、涨跌幅、评分（symbol/name/chain/contract 均来自匹配到的同一 token）
6. **一键投研**：点击代币直接打开资料面板 + 投研分析

### 推荐质量回测

`_archive_daily_recommendations()`（每日推荐自动存档到 `biz.daily_recommendation`，按 `(rec_date, symbol, chain)` 去重）+ `get_recommendation_backtest()` + `GET /api/market/backtest`。

- **收益近似**：用「推荐日存档价格 vs 当前价格」近似计算持有收益（`biz.asset_price_daily` 积累后可升级为精确 1d/3d/7d 周期回测）。
- **指标**：平均收益率、胜率、中位数收益、盈亏比（profit_factor）、最佳/最差推荐。
- **分层**：按评分分层（高 ≥70 / 中 50-70 / 低 <50）与按赛道分层，用于校准评分权重。

### 赛道轮动热力图

`get_sector_heatmap()` + `GET /api/market/sector-heatmap`：按 `primary_sector` 聚合多源交叉验证结果，输出各赛道上榜代币数、平均 24h 涨幅、平均综合评分、赛道龙头代币、综合热度分（heat_score 归一化 0-100）。

---

## Web 工作台

基于 Flask 的轻量级操作面板，部署在 Zeabur 云端，提供：

### 仪表盘
- 资产总数、活跃资产、有文档链接的资产数
- 文档链接来源分布（CMC / CG / DL / DexScreener / Binance / deep_crawl）
- 任务进度（CG 币种详情、CG/CMC/DL 文档入口补充、双源补充、B2 深度文档发现、SPA 无头浏览器爬取、B2 AI 噪声清理、链上持仓快照）
- 覆盖率统计按市值分层（白皮书 / 代币经济 / 社交 / 解锁 / 衍生品 / 链上，5 层 × 6 维）

### 币种查询与投研分析
- **搜索**：按 symbol / name / **合约地址**搜索（pg_trgm GIN 索引加速模糊搜索）。合约地址匹配：EVM（`0x` 开头）大小写不敏感、支持部分前缀匹配；非 EVM（如 Solana base58）精确匹配（大小写敏感）。搜索结果展示**代币赛道分类徽章**（`primary_sector` → 中文标签）
- **市值分层筛选**：搜索结果支持按 CMC 排名分层（top100 / top500 / top1000 / other），圈定投研范围，避免被小币稀释
- **搜索回退**：core.asset 搜不到时，自动从 src_cmc 查找（按 symbol/name）并写入 core.asset
- **资料面板**：文档链接列表（按来源分类，标注入库来源）
- **一键复制全部链接** / **NotebookLM 精选**
- **🤖 一键投研**：全屏 NotebookLM 风格投研页（自动收集资料 + 21 类完整性清单 + AI 问答带引用 + 对话持久化）
- **投研分析面板**：
  - 💰 代币经济学提取（tokenomics.com 四板块优先，未命中弹网址框 → URL/AI 测算）
  - 🔓 代币解锁测算（tokenomics.com 四板块，未命中弹网址框 → URL/AI 测算）
  - 📊 链上数据分析（持仓集中度 + 大额转账 + 快照趋势化 sparkline）
  - 📱 社交热度（社区规模 + 舆情 + 趋势 + 市场热度，综合评分）
  - 📡 情绪 × 价格 × 链上 背离检测
  - 🏆 同赛道竞品结构化对比
  - 📈 衍生品资金面（多交易所聚合：资金费率 / OI / CVD）
  - 🔗 链上 CEX 净流入
  - 🐋 鲸鱼 / 聪明钱行为流（持仓变化 + 大额转账方向）
  - 📉 行情历史（日级 K 线，解锁事件研究 / 相关性矩阵地基）
  - 🚨 异动信号（价格 / 成交量 / OI / 资金费率 / 解锁 5 类 diff 检测）
  - 🔬 解锁事件研究（历史解锁前后价格走势，事件研究法）
  - 🎯 研究结论生成（四维模板化：估值 / 筹码 / 情绪 / 催化 + 定量指标注入）
- **单资产重新爬取**：B2→B3→B2 循环最多 6 轮，深度覆盖子页面
- **手动添加官网链接** / **创建新资产**

### 任务面板

按分类展示全部任务，支持一键启动、实时日志查看、终止运行中任务。

| 分类 | 任务 | 说明 | 状态 |
|------|------|------|------|
| 数据源采集 | CG 新增币种入库 | CG 独有币种补充到 core.asset（先于拉取详情） | 可见 |
| | CG 拉取币种详情（自动循环） | CoinGecko coin_info 拉取 | 可见 |
| | CMC 拉取全量币种列表 | CMC listing/map 全量币种列表，写入 src_cmc.cmc_asset_map（CMC 后续步骤前置） | 可见 |
| | CMC 拉取币种详情 | CoinMarketCap asset_info 拉取（urls/描述/标签） | 可见 |
| | CMC 资产全量入库（自动循环） | 从 src_cmc 全量写入 core.asset，每批 500 | 可见 |
| | DL 拉取协议列表 | DefiLlama 全量协议列表拉取 | 可见 |
| | CG 补充文档入口（自动循环） | 从 coin_info links 提取文档链接 | 可见 |
| | CMC 补充文档入口（自动循环） | 从 cmc_asset_info urls 提取文档链接 | 可见 |
| | DL 补充文档入口（自动循环） | 从 DefiLlama protocol_list 提取官网链接 | 可见 |
| | 双源补充文档入口（自动循环） | DexScreener+Binance 双源兜底补充 | 可见 |
| | 第三方评级/审计回填（自动循环） | DefiLlama 协议详情，提取审计链接 + 评级页写入 doc_source_entry | 可见 |
| | TGE/融资轮次采集（自动循环） | DefiLlama 协议详情 raises 字段，写入 biz.asset_raises | 可见 |
| | 链上异常事件采集（hacks） | DefiLlama /hacks 全量异常事件，写入 biz.asset_hacks | 可见 |
| | B3 SPA 无头浏览器爬取（自动循环） | Playwright 渲染 JS 页面，提取 SPA 网站链接 | 可见 |
| 文档采集 | B2 深度文档发现（自动循环） | 从官网 HTML 抓取嵌入的 PDF/白皮书链接 | 可见 |
| AI 筛选 | B4 AI 噪声清理（按资产·自动循环） | AI 按域名粒度批量判断噪声 | 可见 |
| 投研分析 | C 代币经济学批量提取（自动循环） | 批量提取所有资产 tokenomics 数据（每批10个，无候选自动停止） | 可见 |
| 链上数据 | 链上持仓快照采集（每日单次） | 拉取 Top 持有者，计算持仓集中度 | 可见 |
| | 持仓分布爬取（区块浏览器 HTML） | 从 BSCScan/Etherscan 网页解析持仓分布（集中度/Top50/CEX标签），无需 API Key | 可见 |
| 诊断 | 噪声诊断报告 | 今日新增文档链接的噪声情况 | 可见 |
| | 数据链路诊断 | 全链路健康度检查 | 可见 |
| | 高条目资产污染溯源 | 分析文档链接 >500 的代币污染链路 | 可见 |
| 维护 | 重置高条目资产（>500条） | 清除高条目资产的 deep_crawl 数据 | 可见 |

---

## SPA 处理体系

现代 Web 应用越来越多使用 JS 框架（React/Vue/Next/Nuxt），静态 `requests.get` 无法获取页面内容。系统通过三层机制处理 SPA：

```
第一层 ── B2 实时检测
  深度爬取时检测 SPA 特征：0 链接 + (HTML < 5000 字节 或 框架标记)
  标记 needs_browser=TRUE，交由 B3 无头浏览器处理
            │
            ▼
第二层 ── 回溯扫描（一次性任务，已完成）
  扫描历史已爬取页面，用轻量 HTTP 请求检测 SPA 标记
  识别遗漏的 SPA 页面，标记 needs_browser=TRUE
  已处理页面打 retro_scan_checked_at 时间戳，不重复扫描
            │
            ▼
第三层 ── B3 无头浏览器爬取
  Playwright Chromium 渲染 JS 页面
  4 并发窗口，8 秒超时，domcontentloaded 等待策略
  HEAD 预检：跳过 PDF/图片/死链等非 HTML 内容
  失败链接保留 needs_browser=TRUE 下轮重试
  发现的链接写入 doc_source_entry，回流到 B2 继续深度爬取
```

**SPA 检测规则**：
- 0 个链接 + HTML < 5000 字节
- 或包含 `id="app"` / `id="root"` / `id="__next"` / `id="__nuxt"`
- 或包含 `react-dom` / `vue` 引用
- 或包含 `window.__NUXT__` / `__NEXT_DATA__`

**B3 与 B2 闭环**：
```
B3 爬取 → 发现新链接 → 写入 doc_source_entry → B2 深度爬取
    → 遇到 SPA 页面 → 标记 needs_browser=TRUE → B3 爬取 → ...
```

**单资产重爬流程**：
```
B2 深度爬取 → B3 SPA 爬取 → B2 再爬 → B3 再爬 → ...（最多 6 轮）
  单资产模式跳过聚合域名过滤，保留审计/白皮书等跨域链接
```

---

## 噪声清理体系

多层防御策略：

```
第一层 ── B2 源头阻断
  phase_b2_deep_doc_discovery.py 中 AGGREGATION_DOMAINS + GLOBAL_LINK_BLACKLIST
  30+ 聚合域名（code4rena, sherlock, backed.fi 等），爬取时直接跳过
  密度触发 + 项目标识匹配：同域名下 >5 条链接时触发拦截
  审计报告含项目标识（代币符号/名称分词/域名）则保留，否则拦截
            │
            ▼
第二层 ── 规则直删
  phase_b2_ai_noise_clean_by_asset.py 中 RULE_NOISE_DOMAINS
  22+ 噪声域名（pump.fun, docs.rs, coinmarketcap 等），直接删除
            │
            ▼
第三层 ── AI 按资产分组判断
  按资产聚合域名，AI 一次判断该资产所有域名是否噪声
  审计平台白名单（25+ 域名）保留
  AI 提示词携带资产上下文（[SYMBOL NAME] 前缀），避免误删本项目审计
  关联 >50 资产的非审计域名 → AI 误判纠正机制
  单资产域名 >100 链接且占比 >90% → 重置 re-evaluate
```

---

## 搜索优化与资产入库

### 搜索性能
- `pg_trgm` 扩展 + GIN 索引加速 `ILIKE '%query%'` 模糊搜索
- `canonical_symbol` 和 `canonical_name` 双字段索引，`core.asset_contract` 合约地址关联匹配
- 查询响应从 ~12ms（全表扫描）优化至 ~0.1ms（Bitmap Index Scan）
- 搜索排序：合约地址精确匹配 > symbol 精确匹配 > 前缀匹配 > 包含匹配
- 合约地址匹配：EVM（`0x` 开头）统一小写、支持部分前缀匹配；非 EVM（Solana base58）精确匹配、大小写敏感

### 资产入库流程
1. **批量入库**：CMC/CG/DL 数据源 → 跨源匹配 → core.asset
2. **CG 独有币种补充**：CG 有但 CMC 没有的币种，单独入库
3. **搜索回退自动入库**：搜索时 core.asset 搜不到，自动从 src_cmc.cmc_asset_map 查找并写入 core.asset + asset_source_map
4. **CMC 全量补录**：`cmc_backfill_assets_auto` 任务，每批 500 资产，自动循环直到全部入库

---

## NotebookLM 投研精选

智能选出最有价值的 50 个投研链接：

1. **配额粗筛**：按类型优先分配配额（白皮书 3、文档 5、官网 3、GitHub 5、博客 5、审计 3、其他 3）
2. **AI 排序**：DeepSeek 按投研价值排序
3. **缓存**：结果写入 `biz.doc_source_notebooklm`，下次秒出
4. **排除**：Twitter/Reddit/Telegram 等社交链接不参与精选

---

## 一键投研笔记本（NotebookLM 风格）

在单个代币搜索面板点击「🤖 一键投研」，跳转到全屏投研页：一个代币对应一个笔记本，自动收集该代币全部已收集资料并保存对话与资料快照，下次重新打开可继续。

**自动收集的资料快照**（`_collect_asset_snapshot`）：
- 文档入口 `doc_source_entry`（按类型排序）
- 文档文件 `doc_asset`（whitepaper / tokenomics / audit 优先）
- 投研精选 `research_url` + NotebookLM 精选 `doc_source_notebooklm`
- 结构化数据：代币经济学 `asset_tokenomics`、链上持仓 `onchain_holder_snapshot`、社交热度 `asset_social_heat`、解锁数据 `asset_token_unlocks`、合约地址 `asset_contract`

**21 类投研资料完整性清单**（`RESEARCH_MATERIAL_TYPES` + `_compute_missing_materials`）：
- 前 9 类结构化精确判定：官网 / 白皮书文档 / GitHub 仓库 / 审计报告 / 代币经济学 / 链上持仓 / 社交热度 / 代币解锁 / 合约地址
- 后 12 类 `content_topics` 精确判定：TGE&IDO / LP 流动性 / 国库&多签 / 团队&VC / 路线图 / 治理 DAO / 漏洞赏金 / 交易所上线 / 竞品对比 / 重大公告 / 第三方评级 / 链上异常（由 `_MATERIAL_TOPIC_MAP` 映射到内容主题，不再依赖 URL/标题关键词猜测）
- **分赛道过滤**（`get_sector_visible_material_keys`）：按代币赛道只展示该赛道关心的资料类型，无关类型隐藏。基础资料（官网/白皮书/GitHub/合约/链上/社交）所有赛道展示；代币解锁数据除 Meme（无 vesting）外均展示；主题类资料按 `SECTOR_TOPIC_PRIORITY` 命中展示
- **分赛道排序**（`topic_priority_rank`）：缺失项优先；缺失项内部按赛道主题优先级排序（如 DeFi 的审计、Meme 的交易所上线排最前），官网缺失最优先

投研页侧栏显示**代币分类**（`sector_label`），snapshot 携带 `sector` 字段供上述过滤/排序使用。

**AI 问答（RAG 式，`ask_research_notebook`）**：
- 严格只依据资料库回答，强制 `[编号]` 标注来源，返回 `{answer, citations}`；资料库无相关信息时明说、不编造
- **资料时效性**：文档来源带 `published_at`（发布/更新日期），正文按「类型优先 + 发布时间倒序」排序（新的在前），上下文显式标注发布时间，避免引用过时资料
- 正文抽取：HTML 去标签 + PDF（PyPDF2，最多 30 页、2500 字 snippet）
- 历史对话持久化到 `biz.research_message`（外键级联删除），下次打开保留

**API**
- `GET /api/research/<asset_id>/notebook`：打开（不存在则创建）笔记本
- `POST /api/research/notebook/<notebook_id>/ask`：提问（后台任务，前端轮询）

**数据表**：`biz.research_notebook`（asset_id 唯一）+ `biz.research_message`

---

## 缺失资料自动补齐

针对 21 类投研资料完整性清单中的缺失项，提供「单币一键补齐」和「批量补齐」两条流水线。

### 单币缺失一键补齐（`fill_missing_materials`）

投研页「补齐缺失」按钮触发：采集当前快照 → 计算缺失清单 → 按缺失项映射动作 → 按依赖顺序串行执行 → 重新采集对比补齐前后变化。

缺失类型 → 动作映射（`_MISSING_FILL_ACTIONS`），动作按 `_FILL_ACTION_ORDER` 顺序执行：

| 动作 | 脚本 / 逻辑 | 作用 |
|------|-------------|------|
| `deep` | `phase_b2_deep_doc_discovery.py --asset-id` | 文档深爬（单资产放宽模式） |
| `spa` | `phase_b2_spa_browser_crawl.py --asset-id` | SPA 浏览器兜底 |
| `third_party` | `phase_b2_third_party.py --asset-id` | 审计 / 评级链接 |
| `raises` | `phase_b2_third_party_raises.py --asset-id` | TGE / 融资轮次 |
| `hacks` | `phase_b2_third_party_hacks.py --asset-id` | 链上异常事件 |
| `ai_classify` | `ai_classify_asset` | AI 正文多标签分类 |
| `tokenomics` / `holders` / `social` / `unlocks` | 对应结构化补齐 | 代币经济学 / 持仓 / 社交 / 解锁 |

单个动作失败不中断整体，最终返回补齐前后缺失变化。

### 批量补齐（新币 + 热门赛道）

- `select_missing_material_distribution.sql`：诊断各资料类型缺失分布，决定先补哪类。
- `select_target_assets.sql`：生成目标资产清单（新币 `date_launched >= 2025` ∪ 热门赛道 7 类）。
- `collect_assets_batch.py`：节流批量编排，逐资产调用 `collect_asset_materials.py`（deep → spa → third_party → ai_classify 五阶段），jsonl 断点续跑。

### 自有站点主题抢救（第一步）

针对「有官网入口、但缺失自有站点主题」的资产，`phase_b2_rescue_ownsite_topics.py` 按「staging + 多轮深爬 + 按需 SPA 提升」抢救：

1. **staging**：`select_ownsite_rescue_targets.sql` 选出缺失「国库/多签、团队/VC、审计、漏洞赏金、交易所上线、公告」且含官网入口的资产（缺失多者优先）。
2. **多轮深爬**：重置官网 `deep_crawled_at` 后，用单资产放宽模式（含 sitemap 全站索引）反复深爬，直到无未爬官网/文档入口。
3. **按需提升**：仅当存在 `needs_browser=TRUE` 且重试未超限的 SPA 页面时，才提升到 Playwright 浏览器爬取。

只针对自有站点主题，不涉及第三方数据源；官网没有对应页面就跳过（不硬造数据）。

---

## 项目结构

```
├── 02_数据库设计/              # 数据库 Schema 设计文档 + SQL
├── 04_架构与代码方案/          # 项目完整逻辑文档
├── 05_代码与脚本/
│   ├── scripts/                # 核心 Python 脚本
│   │   ├── bin/                # 入口脚本（scheduler / 工作台调用）
│   │   │   ├── ingest_*.py     # 数据源采集（CG/CMC/DL）
│   │   │   ├── bootstrap_*.py  # source → core 批量映射
│   │   │   ├── backfill_*.py   # 历史数据回填（含链接分类阶段1/阶段2）
│   │   │   ├── refresh_*.py    # 核心资产/文档入口刷新
│   │   │   ├── supplement_*.py # 双源(DexScreener+Binance)兜底补充
│   │   │   ├── phase_b2_*.py   # 深度文档发现 + SPA 爬取 + AI 噪声清理 + 第三方专项(审计/评级/融资/异常) + 自有站点主题抢救
│   │   │   ├── phase_c_*.py    # 代币经济学提取 + 社交热度
│   │   │   ├── phase_chain_*.py # 链上数据 + 解锁数据
│   │   │   ├── etl_*.py        # ETL 脚本（行情历史日级聚合等）
│   │   │   ├── diag_*.py       # 诊断脚本
│   │   │   ├── curate_*.py     # NotebookLM 精选
│   │   │   └── collect_*.py    # GitHub 活跃度采集 + 批量补齐编排
│   │   ├── src/crypto_research/ # 可复用模块
│   │   │   ├── clients/        # API 客户端
│   │   │   │   ├── cmc_client.py         # CoinMarketCap
│   │   │   │   ├── coingecko_client.py   # CoinGecko
│   │   │   │   ├── defillama_client.py   # DeFiLlama
│   │   │   │   ├── ethplorer_client.py   # Ethplorer（持仓快照）
│   │   │   │   ├── etherscan_client.py   # Etherscan / BSCScan
│   │   │   │   ├── http_client.py        # 通用 HTTP
│   │   │   │   └── llm_client.py         # LLM（DeepSeek）
│   │   │   ├── parsers/        # 响应解析器
│   │   │   ├── mapping/        # 映射逻辑 + 链接分类（taxonomy / classify_link）+ 代币赛道（sector）
│   │   │   ├── db/             # 数据库工具
│   │   │   └── utils/          # 通用工具
│   │   └── sql/                # SQL 模板
│   │       ├── core/           # 核心表（insert_asset, upsert_asset_source_map）
│   │       ├── biz/            # 业务表（doc_source_entry, onchain_*, tokenomics, unlocks）
│   │       ├── src_cmc/        # CMC 数据源
│   │       ├── src_cg/         # CoinGecko 数据源
│   │       ├── src_dl/         # DeFiLlama 数据源
│   │       └── sys/            # 系统表（ingest_run）
│   │
│   └── workbench/              # Flask Web 工作台
│       ├── app.py              # 主应用 + API 路由
│       ├── task_manager.py     # 后台任务管理器（Popen 实时流式输出）
│       ├── db_stats.py         # 数据库统计查询 + 进度计算 + 搜索（含合约地址搜索、赛道过滤）
│       ├── binance_market.py   # Binance Web3 市场数据 + 评分（套用分赛道权重）
│       ├── cmc_market.py       # CMC 市场数据（提取 platform.token_address 供跨源匹配）
│       ├── cross_market.py     # 多源交叉验证（按合约地址匹配）+ 每日推荐存档 + 赛道热力图
│       ├── derivatives_client.py # 5 家交易所衍生品客户端（资金费率/OI/成交）
│       ├── scheduler.py        # 定时调度器（APScheduler，替代 n8n 调度）
│       ├── kol_daemon.py       # KOL 信号监控守护进程（30s 轮询）
│       ├── kol/                # KOL 信号监控模块
│       │   ├── db.py           # 数据库操作（博主/帖子/信号 CRUD）
│       │   ├── scraper.py      # 多平台抓取器（币安广场 Playwright）
│       │   ├── classifier.py   # AI 信号分类与结构化提取
│       │   ├── notifier.py     # 信号邮件提醒（含交叉验证数据）
│       │   ├── asset_match.py  # 币种匹配（关联 core.asset）
│       │   ├── routes.py       # Flask 蓝图（API 路由）
│       │   └── runner.py       # 主流程编排（抓取→AI→匹配→告警）
│       └── templates/          # 前端页面
│           ├── index.html      # 仪表盘 + 币种查询 + 任务面板 + 投研分析 + 回测/赛道热力图
│           ├── research.html   # 一键投研笔记本页
│           └── kol.html        # KOL 信号监控面板
│
├── 07_测试与验收/              # 诊断脚本与报告
├── Dockerfile                  # 工作台 Docker 镜像（含 Playwright）
└── README.md                   # 本文件
```

---

## 配置

需要以下环境变量（`05_代码与脚本/scripts/.env` 文件）：

| 变量 | 说明 | 必填 |
|------|------|------|
| `DATABASE_URL` | PostgreSQL 连接串 | ✅ |
| `CMC_API_KEY` | CoinMarketCap API Key | ✅ |
| `COINGECKO_API_KEY` | CoinGecko API Key（支持逗号分隔多 key 轮替） | ❌ |
| `GITHUB_TOKEN` | GitHub Token（可选，提升 API 限流） | ❌ |
| `ETHERSCAN_API_KEY` | Etherscan API Key（可选，链上数据） | ❌ |
| `BSCSCAN_API_KEY` | BSCScan API Key（可选，链上数据） | ❌ |
| `ETHPLORER_API_KEY` | Ethplorer API Key（可选，持仓快照） | ❌ |
| `OPENAI_API_KEY` / `ARK_API_KEY` | LLM API Key（代币经济学提取 + AI 噪声清理 + AI 链接分类） | ❌ |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_TO` / `SMTP_FROM` | 邮件通知（解锁提醒 + 空头告警 + 调度失败告警），端口 465=SSL / 587=STARTTLS | ❌ |
| `SERVICE_ROLE` | 服务角色：`web`（默认，gunicorn Web 工作台）或 `scheduler`（调度器常驻进程） | ❌ |
| `SCHEDULER_TIMEZONE` | 调度器时区，默认 `Asia/Shanghai` | ❌ |
| `SCHEDULER_ENABLED` | 调度器任务白名单（逗号分隔 key），空=全部启用 | ❌ |
| `SCHEDULER_LOG_DIR` | 调度器日志目录，默认 `/app/task_state/scheduler` | ❌ |
| `TREND_DROP_PCT` | 空头趋势跌幅阈值（%），默认 15 | ❌ |
| `UNLOCK_ALERT_DAYS` | 解锁提前提醒天数，默认 14（分赛道会覆盖） | ❌ |
| `WHALE_TRANSFER_MIN_USD` | Meme 大户转账告警阈值（美元），默认 100000 | ❌ |
| `MIN_NEW_ASSET_ID` | B2 新币水位线，只处理 asset_id >= 该值的新资产，老语料跳过 | ❌ |
| `KOL_POLL_INTERVAL` | KOL 监控轮询间隔（秒），默认 30 | ❌ |
| `KOL_ALERT_CONFIDENCE` | KOL 信号邮件告警置信度阈值（0~1），默认 0.8 | ❌ |

### 本地运行

```bash
cd 05_代码与脚本/workbench
pip install -r requirements.txt
playwright install chromium
python app.py
# 打开 http://localhost:5000
```

### Docker 部署

```bash
docker build -t crypto-workbench .
docker run -p 5000:5000 -e DATABASE_URL=... crypto-workbench
```

---

## 数据去重保证

- `ON CONFLICT DO UPDATE` 主键级去重
- `content_hash` UNIQUE 约束（文档内容级去重）
- `(asset_id, entry_url)` 唯一约束（文档入口）
- `(chain, tx_hash, contract_address, from_address, to_address)` 唯一约束（转账日志）
- Python 层 `seen.set()` 辅助去重
- URL 归一化处理

---

## 关键工程约束

- CoinGecko API 调用限 60 次/分钟
- Ethplorer API 调用限 0.5 次/秒（~30 次/分钟）
- DexScreener API 请求间隔 1.2 秒（~50 次/分钟）
- B2 深度爬取并发数 8 worker
- B2 自动循环单轮超时 30 分钟
- SPA 无头浏览器爬取 4 并发窗口，8 秒超时，HEAD 预检 8 秒
- SPA 回溯扫描 10 线程并发，每批 500 页
- 候选 SQL 必须包含 `dse.entry_id IS NULL` 或 `retro_scan_checked_at IS NULL` 避免重复处理
- 自动循环脚本 `entry_count == 0` 时终止
- 子进程使用 `Popen` + 双线程实时流式输出，避免日志缓冲卡住
- 线程池 `shutdown(wait=False)` 避免死锁
- 写入失败 `conn.rollback()` 重置事务，避免后续 SQL 被拒绝
- `discovered_from` 字段限制 VARCHAR(64)，URL 前缀需截断适配
- 单币按需提取（代币经济学/解锁）始终加 `--force` 覆盖已有数据，避免「已有数据」提前 return 导致无结果
- 代币经济学/解锁未命中 tokenomics.com 时，前端先弹网址输入框，用户未提供网址才走 AI 测算
- AI 测算解锁数据要求价格/市值/FDV 齐全，任一缺失直接报错、不调用 AI
- 衍生品资金面多交易所并行探测，结果 15 分钟 DB 缓存（`biz.asset_derivatives`）
- 鲸鱼行为流 / CEX 净流入不依赖预定义鲸鱼/交易所地址标签，改用「持仓快照变化 + 大额转账分级 + 交易所钱包种子」数据驱动方案
- 赛道规则（分类/评分/采集优先级/解锁预警）统一收敛在 `mapping/sector.py`，各脚本禁止重复硬编码

---

## 定时调度器（scheduler.py）

用 **APScheduler** 替代 n8n 的调度职责。n8n 原本的编排/调度/重试逻辑已被代码吸收（一键流水线 `run_*_pipeline.py`、自循环 `*_auto.py`、Web 工作台 `TaskManager`、`phase_watchlist_monitor.py`），n8n 只剩「到点把脚本踢起来」，由本调度器承担。

**职责**：
1. 按调度表定时启动 `scripts/bin` 下的脚本（调用方式与工作台 `TaskManager` 一致）。
2. 同一任务不重叠：`max_instances=1 + coalesce`，前一轮未结束则跳过新触发（脚本本身幂等，重叠也只是浪费资源）。
3. 子进程日志实时流式写入日志文件（也打到 stdout 便于 `docker logs` 查看）。
4. 子进程非零退出 → SMTP 邮件告警（复用 `SMTP_HOST/PORT/USER/PASS/TO/FROM`）。

**用法**：

```bash
python scheduler.py                 # 前台常驻
python scheduler.py --list          # 打印调度表
python scheduler.py --run-once cmc_pipeline   # 立即执行某个任务一次（测试/补跑）
```

**默认调度表**（编辑 `scheduler.py` 的 `SCHEDULE` 可调整）：

| 时间（Asia/Shanghai） | 任务 | 说明 |
|------|------|------|
| 每日 03:00 | CMC 一键流水线 | 数据源 |
| 每日 04:00 | DefiLlama 一键流水线 | 数据源 |
| 每周一 05:00 | CoinGecko 一键流水线 | 月配额 10k，低频 |
| 每日 05:30 | 链上持仓快照 | 每日单次 |
| 每日 06:00 | CMC/CG/DL 补充文档入口 | 自循环 |
| 每日 07:00 | 双源补充文档入口 | 自循环 |
| 每日 08:00 | 第三方评级/审计 + TGE/融资 | 自循环 |
| 每周一 08:30 | 链上异常事件（hacks） | — |
| 每 6 小时 | B2 深度文档发现 | 自循环 |
| 每日 09:00 | B3 SPA 无头浏览器爬取 | 自循环 |
| 每日 10:00 | B4 AI 噪声清理 | 自循环 |
| 每 30 分钟 | 大额转账监控 + 解锁/空头/大户监控 | 跑到完/单次 |

**环境变量**（可选）：`SCHEDULER_TIMEZONE`（默认 `Asia/Shanghai`）、`SCHEDULER_ENABLED`（逗号分隔 key 白名单，空=全部）、`SCHEDULER_LOG_DIR`（默认 `/app/task_state/scheduler`）。

### B2 自动循环的停止机制（不会无限爬取）

`phase_b2_auto_loop.py` 是「定时启动 → 跑到没东西可爬 → 退出 → 等下一次定时」的模式，有 4 层停止保护：

1. **Pending 数量阈值（主要停止条件）**：每跑完一轮查询待爬数量，全部可爬类型的 pending 低于 **500 条**就自动停。
2. **最大轮数上限（安全兜底）**：最多跑 **200 轮**强制停，每轮 1000 条上限，总量最多 20 万条。
3. **单轮超时保护**：单轮最长 **30 分钟**，卡住了强制跳过继续下一轮，不会吊死。
4. **新币水位线**：只处理 `asset_id >= MIN_NEW_ASSET_ID` 的新资产，老的已成熟语料不会反复爬。

调度器每 6 小时踢一次 B2 自动循环，每次启动后自己跑到 pending < 500 或跑满 200 轮就退出，不会 24 小时不停爬。

---

## 部署平台

- **数据库**：Zeabur PostgreSQL
- **Web 工作台**：Zeabur Docker 部署（默认 `SERVICE_ROLE=web`）
- **调度器**：同一镜像另起一个服务，环境变量设 `SERVICE_ROLE=scheduler` 跑 `python scheduler.py`；手动触发走 Web 工作台任务面板

### 新建调度器服务步骤（Zeabur）

调度器和 Web 工作台共用同一个 Docker 镜像，只是启动角色不同，需要在 Zeabur 上**再建一个服务**：

1. 项目页点「+ 添加服务」→ 选 **GitHub** → 选同一个仓库（`crypto-profile-collection`）。
2. 分支选 `main`，构建方式选 **Dockerfile**（根目录的 `Dockerfile`）。
3. 服务名建议叫 `crypto-scheduler`。
4. 部署完成后，进入新服务的「设置」→「环境变量」：
   - 加一条：`SERVICE_ROLE` = `scheduler`
   - 把 Web 服务里已有的环境变量（`DATABASE_URL`、`CMC_API_KEY`、`COINGECKO_API_KEY`、`SMTP_*` 等）**全部复制过来**，否则脚本跑起来会缺配置。
5. 「重新部署」让环境变量生效。
6. 验证：进「日志」看有没有 `[调度] 启动完成` 的输出；或进「命令」执行 `python scheduler.py --list` 看调度表。

> 调度服务**不需要域名/端口**，是纯后台进程。Zeabur 可能提示「没有健康检查/端口」，属正常现象，只要容器在跑、日志正常即可。

---

## License

MIT
