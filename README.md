# 加密货币投研资料采集系统 (Crypto Profile Collection)

一套**加密货币投研资料采集与沉淀系统**，采用 **n8n 调度中枢 + Python 数据处理引擎 + Flask Web 工作台** 的混合架构。

核心思路：把"从多数据源抓取币种/协议资料，找到官网/白皮书/文档，再将文档变成可沉淀资产"这件事，拆成**层层可维护的流水线**，并在末端提供**投研分析工具箱**（代币经济学、解锁时间表、链上数据等）。

---

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│  biz    业务消费层  doc_source_entry / doc_asset             │
│                    research_url / coin_basic                 │
│                    asset_tokenomics / asset_token_unlocks    │
│                    onchain_holder_snapshot / transfer_log    │
│                    doc_source_notebooklm / exchange_wallet   │
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

**六大主要数据源：**

| 数据源 | 用途 | 接口 |
|--------|------|------|
| CoinMarketCap | 币种信息 + URLs（官网/文档/GitHub/Twitter/Telegram/Reddit/Facebook） | CMC API |
| CoinGecko | 币种列表 + coin_info（links 提取文档入口） | CoinGecko API |
| DeFiLlama | 协议列表 + TVL（url/twitter 提取官网链接） | DL API |
| DexScreener | 无文档入口资产的兜底补充（官网/社交链接） | DexScreener API |
| Binance Web3 | 无文档入口资产的兜底补充 + 每日投研推荐 | Binance Web3 API |
| Ethplorer | 链上持仓快照（Top 持有者、持仓集中度） | Ethplorer API |
| Tokenomist | 代币解锁时间表（无头浏览器爬取） | 网页爬取 |

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
  代币经济学提取（多源聚合 + LLM 结构化）
  代币解锁测算（Tokenomist 无头浏览器爬取）
  链上数据分析（持仓集中度 + 大额转账告警）
  社交热度（待开发）
```

---

## Phase C：投研分析工具箱

针对单个资产的深度投研分析，从 Web 工作台的"投研分析"面板触发。

### 代币经济学提取

从多源文档中提取结构化代币经济学数据，写入 `biz.asset_tokenomics`。

**数据来源：**
1. **文档层**：tokenomics / whitepaper / docs 类型的文档（优先）
2. **网页层**：官网 deep_crawl 子页面（兜底补充）
3. **API 层**：CMC 市场数据 + CoinGecko supply 数据（参考）

**提取流程：**
```
收集文档 → Playwright 抓取纯文本 → 拼接 CMC/CG supply 数据
    → LLM 提取结构化字段 → 写入 biz.asset_tokenomics
```

**提取字段：** total_supply、max_supply、circulating_supply、buy/sell tax、contract_renounced、lp_locked、allocation（分配比例）、burn_info、emission_schedule、governance_info、utility_info 等。

**数据表：** `biz.asset_tokenomics`（按 asset_id 唯一，ON CONFLICT 更新）

### 代币解锁测算

从 [Tokenomist](https://tokenomist.ai/)（原 TokenUnlocks）用 Playwright 无头浏览器爬取解锁时间表。

**为什么不用 API：** Tokenomist API 按次收费，单币投研场景用爬虫更经济。

**爬取内容：**
- **Overview 页面**：释放进度、市值、FDV、流通率、分配表、下一次解锁
- **Unlock Events 页面**：Notable Cliff Release Events 列表（日期、解锁价值、释放比例、分配类别数、状态）

**数据表：** `biz.asset_token_unlocks`（按 asset_id 唯一）

**注意：**
- 自动关闭 CLI 广告弹窗（Dismiss / Escape）
- slug 推断优先使用 CoinGecko ID，兜底 symbol/name
- 免费版只能看到 Cliff 大额解锁事件，逐日解锁数据需要 Pro

### 链上数据分析

分层策略——**告警常驻 + 快照每日 + 明细按需**：

| 层级 | 功能 | 触发方式 | 数据表 | 状态 |
|------|------|---------|--------|------|
| 快照层 | 持仓集中度 / Holder 数 | 每日单次全量 | `biz.onchain_holder_snapshot` | 运行中 |
| 告警层 | 大额转入交易所 | 后台自动循环 | `biz.onchain_transfer_log` | 已隐藏（大部分链无 API Key） |
| 明细层 | 持仓 + 大额转账明细 | 投研按需查询 | — | 可用 |

**按需查询**：在投研分析面板点击"拉取链上数据"，先查当日缓存，缓存未命中则实时从 Etherscan 拉取。

---

## 每日投研推荐

基于市场数据驱动的投研价值评分，从 Binance Web3 API + CMC API 双源交叉验证：

1. **评分维度**：24h 交易量、价格涨跌幅、交易笔数、买入占比、短期动量
2. **交叉验证**：Binance 实时数据 + CMC 市场数据（双源共识标记 2/3）
3. **前端展示**：默认显示 5 个代币，点击"加载更多"展示全部
4. **信息展示**：项目名称、合约地址、交易量、涨跌幅、评分
5. **一键投研**：点击代币直接打开资料面板 + 投研分析

---

## Web 工作台

基于 Flask 的轻量级操作面板，部署在 Zeabur 云端，提供：

### 仪表盘
- 资产总数、活跃资产、有文档链接的资产数
- 文档链接来源分布（CMC / CG / DL / DexScreener / Binance / deep_crawl）
- 任务进度（CG 币种详情、CG/CMC/DL 文档入口补充、双源补充、B2 深度文档发现、SPA 无头浏览器爬取、B2 AI 噪声清理、链上持仓快照）

### 币种查询与投研分析
- **搜索**：按 symbol 或 name 搜索（pg_trgm GIN 索引加速模糊搜索）
- **搜索回退**：core.asset 搜不到时，自动从 src_cmc 查找并写入 core.asset
- **资料面板**：文档链接列表（按来源分类，标注入库来源）
- **一键复制全部链接** / **NotebookLM 精选**
- **投研分析面板**：
  - 💰 代币经济学提取（多源 + LLM 结构化）
  - 🔓 代币解锁测算（Tokenomist 爬取）
  - 📊 链上数据分析（持仓 + 大额转账）
  - 📱 社交热度（待开发）
- **单资产重新爬取**：B2→B3→B2 循环最多 6 轮，深度覆盖子页面
- **手动添加官网链接** / **创建新资产**

### 任务面板

按分类展示全部任务，支持一键启动、实时日志查看、终止运行中任务。

| 分类 | 任务 | 说明 | 状态 |
|------|------|------|------|
| 数据源采集 | CG 新增币种入库 | CG 独有币种补充到 core.asset（先于拉取详情） | 可见 |
| | CG 拉取币种详情（自动循环） | CoinGecko coin_info 拉取 | 可见 |
| | CMC 拉取币种详情 | CoinMarketCap asset_info 拉取 | 可见 |
| | CMC 资产全量入库（自动循环） | 从 src_cmc 全量写入 core.asset，每批 500 | 可见 |
| | DL 拉取协议列表 | DefiLlama 全量协议列表拉取 | 可见 |
| | CG 补充文档入口（自动循环） | 从 coin_info links 提取文档链接 | 可见 |
| | CMC 补充文档入口（自动循环） | 从 cmc_asset_info urls 提取文档链接 | 可见 |
| | DL 补充文档入口（自动循环） | 从 DefiLlama protocol_list 提取官网链接 | 可见 |
| | 双源补充文档入口（自动循环） | DexScreener+Binance 双源兜底补充 | 可见 |
| | B3 SPA 无头浏览器爬取（自动循环） | Playwright 渲染 JS 页面，提取 SPA 网站链接 | 可见 |
| 文档采集 | B2 深度文档发现（自动循环） | 从官网 HTML 抓取嵌入的 PDF/白皮书链接 | 可见 |
| AI 筛选 | B4 AI 噪声清理（按资产·自动循环） | AI 按域名粒度批量判断噪声 | 可见 |
| 链上数据 | 链上持仓快照采集（每日单次） | 拉取 Top 持有者，计算持仓集中度 | 可见 |
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
- `canonical_symbol` 和 `canonical_name` 双字段索引
- 查询响应从 ~12ms（全表扫描）优化至 ~0.1ms（Bitmap Index Scan）
- 搜索排序：精确匹配 > 前缀匹配 > 包含匹配

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

## 项目结构

```
├── 02_数据库设计/              # 数据库 Schema 设计文档 + SQL
├── 04_架构与代码方案/          # 项目完整逻辑文档
├── 05_代码与脚本/
│   ├── scripts/                # 核心 Python 脚本
│   │   ├── bin/                # 入口脚本（n8n / 工作台调用）
│   │   │   ├── ingest_*.py     # 数据源采集（CG/CMC/DL）
│   │   │   ├── bootstrap_*.py  # source → core 批量映射
│   │   │   ├── backfill_*.py   # 历史数据回填
│   │   │   ├── refresh_*.py    # 核心资产/文档入口刷新
│   │   │   ├── supplement_*.py # 双源(DexScreener+Binance)兜底补充
│   │   │   ├── phase_b2_*.py   # 深度文档发现 + SPA 爬取 + AI 噪声清理
│   │   │   ├── phase_c_*.py    # 代币经济学提取
│   │   │   ├── phase_chain_*.py # 链上数据 + 解锁数据
│   │   │   ├── diag_*.py       # 诊断脚本
│   │   │   ├── curate_*.py     # NotebookLM 精选
│   │   │   └── collect_*.py    # GitHub 活跃度采集
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
│   │   │   ├── mapping/        # 映射逻辑
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
│       ├── db_stats.py         # 数据库统计查询 + 进度计算 + 搜索
│       ├── binance_market.py   # 每日投研推荐（市场数据+评分）
│       ├── cross_market.py     # 多源交叉验证
│       └── templates/          # 前端页面
│           └── index.html      # 仪表盘 + 币种查询 + 任务面板 + 投研分析
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
| `OPENAI_API_KEY` / `ARK_API_KEY` | LLM API Key（代币经济学提取 + AI 噪声清理） | ❌ |

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

---

## 部署平台

- **数据库**：Zeabur PostgreSQL
- **Web 工作台**：Zeabur Docker 部署
- **调度**：n8n（Zeabur 同项目） + Web 工作台手动触发

---

## License

MIT
