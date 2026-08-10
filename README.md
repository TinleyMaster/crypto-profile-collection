# 加密货币投研资料采集系统 (Crypto Profile Collection)

一套**加密货币投研资料采集与沉淀系统**，采用 **n8n 调度中枢 + Python 数据处理引擎 + Flask Web 工作台** 的混合架构。

核心思路：把"从多数据源抓取币种/协议资料，找到官网/白皮书/文档，再将文档变成可沉淀资产"这件事，拆成**层层可维护的流水线**。

---

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│  biz    业务消费层  doc_source_entry / doc_asset             │
│                    research_url / coin_basic                 │
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
| Binance Web3 | 无文档入口资产的兜底补充（官网/社交链接） | Binance Web3 API |
| Ethplorer | 链上持仓快照（Top 持有者、持仓集中度） | Ethplorer API |

---

## 文档处理流水线

```
Phase A: 资产核心构建
  CMC/CG/DL 数据源 → 跨源匹配 → core.asset（统一实体）
  去重（按合约地址）、coin_basic 基础数据
            │
            ▼
Phase B1: 文档入口发现              ← 已完成
  CMC/CG/DL 三大数据源提取官网/文档链接
  双源补充（DexScreener + Binance Web3）兜底无文档入口资产
  biz.doc_source_entry ~18,000+ 条
            │
            ▼
Phase B2: 深度文档发现              ← 进行中
  从官网和文档页 HTML 中进一步抓取嵌入的 PDF/白皮书链接
  并发 8 worker，优先处理 official_website → docs
  聚合类域名阻断（30+ 域名，防止跨资产污染）
  SPA 检测：识别 JS 渲染页面，标记 needs_browser=TRUE
            │
            ▼
Phase B2-SPA: 无头浏览器爬取        ← 进行中
  用 Playwright 渲染 JS 页面，提取 B2 静态爬取无法处理的 SPA 网站链接
  4 并发浏览器窗口，8 秒超时，HEAD 预检跳过非 HTML 内容
  SPA 回溯扫描：重检历史已爬取页面，识别遗漏的 SPA 页面
  写入的链接回流到 B2 继续深度爬取，形成闭环
            │
            ▼
Phase B2.5: AI 噪声清理             ← 进行中
  多层防御：源头阻断 → 规则直删 → AI 按资产分组判断
  密度触发 + 项目标识匹配：同一域名下 >5 条链接时触发拦截
  审计报告白名单保留，AAI 误判纠正机制
            │
            ▼
Phase B3: 文档下载与落盘            ← 已完成 77 个 PDF
  目录结构: docs_storage/{symbol}_{asset_id}/whitepapers/{原始文件名}
            │
            ▼
Phase B4: 文档解析                  ← 暂停（pypdf 太慢，待优化）
  PDF → Markdown
            │
            ▼
Phase B5: 链接健康检查 + AI 筛选     ← 脚本就绪
  健康检测 + AI 投研相关性评分 → biz.research_url
            │
            ▼
Phase B6: 生成投研资料文件          ← 脚本就绪
  {symbol}_投研网址链接.txt + {symbol}_基础数据.md
            │
            ▼
Phase B7: 防屏蔽链接 Fallback 下载   ← 脚本就绪
  Cloudflare/WAF 链接的兜底下载
```

---

## 链上数据监控（Phase C）

分层策略——告警常驻 + 快照每日自动 + 明细按需查询：

| 层级 | 功能 | 触发方式 | 数据表 | 状态 |
|------|------|---------|--------|------|
| 快照层 | 持仓集中度 / Holder 数 | 每日单次全量 | `biz.onchain_holder_snapshot` | 运行中 |
| 告警层 | 大额转入交易所 | 后台自动循环 | `biz.onchain_transfer_log` | 已隐藏（大部分链无 API Key） |

---

## 每日投研推荐

基于市场数据驱动的投研价值评分，从 Binance Web3 API 获取市场热点数据，按多维度评分模型排序：

1. **评分维度**：24h 交易量、价格涨跌幅、交易笔数、买入占比、短期动量
2. **交叉验证**：Binance 实时数据 + CMC 市场数据 + DexScreener 链上数据
3. **前端展示**：默认显示 5 个代币，点击"加载更多"展示全部
4. **信息展示**：项目名称、合约地址、交易量、涨跌幅、评分

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
│   │   │   ├── phase_b3_*.py   # 文档下载
│   │   │   ├── phase_b4_*.py   # PDF 解析
│   │   │   ├── phase_b5_*.py   # 链接健康 + AI 筛选
│   │   │   ├── phase_b6_*.py   # 生成投研资料
│   │   │   ├── phase_b7_*.py   # Fallback 下载
│   │   │   ├── phase_chain_*.py # 链上数据监控
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
│   │       ├── biz/            # 业务表（doc_source_entry, onchain_*, notebooklm）
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
│       └── templates/          # 前端页面
│           └── index.html      # 仪表盘 + 币种查询 + 任务面板
│
├── 07_测试与验收/              # 诊断脚本与报告
├── Dockerfile                  # 工作台 Docker 镜像（含 Playwright）
└── README.md                   # 本文件
```

---

## Web 工作台

基于 Flask 的轻量级操作面板，部署在 Zeabur 云端，提供：

### 仪表盘
- 资产总数、活跃资产、有文档链接的资产数
- 文档链接来源分布（CMC / CG / DL / DexScreener / Binance / deep_crawl）
- 任务进度（CG 币种详情、CG/CMC/DL 文档入口补充、双源补充、B2 深度文档发现、SPA 无头浏览器爬取、B2 AI 噪声清理、链上持仓快照）

### 币种查询
- 按资产 ID 或关键词搜索（pg_trgm GIN 索引加速模糊搜索，~0.1ms 响应）
- 展示完整资料面板：项目名称、代币符号、合约地址、数据来源
- 文档链接列表（按来源分类，标注入库来源）
- 一键复制全部链接
- **NotebookLM 精选**：配额粗筛 + AI 排序，智能选出 Top 50 投研链接
- **链上数据**：按需查询持仓集中度
- 手动添加官网链接、创建新资产

### 任务面板

按分类展示全部任务，支持一键启动、实时日志查看、终止运行中任务。

| 分类 | 任务 | 说明 | 状态 |
|------|------|------|------|
| 数据源采集 | CG 拉取币种详情（自动循环） | CoinGecko coin_info 拉取 | 可见 |
| | CG 新增币种入库 | CG 独有币种补充到 core.asset | 可见 |
| | CG 补充文档入口（自动循环） | 从 coin_info links 提取文档链接 | 可见 |
| | CMC 补充文档入口（自动循环） | 从 cmc_asset_info urls 提取文档链接 | 可见 |
| | DL 补充文档入口（自动循环） | 从 DefiLlama protocol_list 提取官网链接 | 可见 |
| | 双源补充文档入口（自动循环） | DexScreener+Binance 双源兜底补充 | 可见 |
| | SPA 无头浏览器爬取（自动循环） | Playwright 渲染 JS 页面，提取 SPA 网站链接 | 可见 |
| 文档采集 | B2 深度文档发现（自动循环） | 从官网 HTML 抓取嵌入的 PDF/白皮书链接 | 可见 |
| AI 筛选 | B2 AI 噪声清理（按资产·自动循环） | AI 按域名粒度批量判断噪声 | 可见 |
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
  标记 needs_browser=TRUE，交由无头浏览器处理
            │
            ▼
第二层 ── 回溯扫描（一次性任务，已完成）
  扫描历史已爬取页面，用轻量 HTTP 请求检测 SPA 标记
  识别遗漏的 SPA 页面，标记 needs_browser=TRUE
  已处理页面打 retro_scan_checked_at 时间戳，不重复扫描
            │
            ▼
第三层 ── 无头浏览器爬取
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

**SPA 与 B2 闭环**：
```
SPA 爬取 → 发现新链接 → 写入 doc_source_entry → B2 深度爬取
    → 遇到 SPA 页面 → 标记 needs_browser=TRUE → SPA 爬取 → ...
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
  关联 >50 资产的非审计域名 → AI 误判纠正机制
  单资产域名 >100 链接且占比 >90% → 重置 re-evaluate
```

---

## 搜索优化

- `pg_trgm` 扩展 + GIN 索引加速 `ILIKE '%query%'` 模糊搜索
- `canonical_symbol` 和 `canonical_name` 双字段索引
- 查询响应从 ~12ms（全表扫描）优化至 ~0.1ms（Bitmap Index Scan）
- 搜索排序：精确匹配 > 前缀匹配 > 包含匹配

---

## NotebookLM 投研精选

智能选出最有价值的 50 个投研链接：

1. **配额粗筛**：按类型优先分配配额（白皮书 3、文档 5、官网 3、GitHub 5、博客 5、审计 3、其他 3）
2. **AI 排序**：DeepSeek 按投研价值排序
3. **缓存**：结果写入 `biz.doc_source_notebooklm`，下次秒出
4. **排除**：Twitter/Reddit/Telegram 等社交链接不参与精选

---

## 配置

需要以下环境变量（`05_代码与脚本/scripts/.env` 文件）：

| 变量 | 说明 | 必填 |
|------|------|------|
| `DATABASE_URL` | PostgreSQL 连接串 | ✅ |
| `CMC_API_KEY` | CoinMarketCap API Key | ✅ |
| `COINGECKO_API_KEY` | CoinGecko API Key（可选，提升限流） | ❌ |
| `GITHUB_TOKEN` | GitHub Token（可选，提升 API 限流） | ❌ |
| `ETHERSCAN_API_KEY` | Etherscan API Key（可选，链上数据） | ❌ |
| `BSCSCAN_API_KEY` | BSCScan API Key（可选，链上数据） | ❌ |
| `ETHPLORER_API_KEY` | Ethplorer API Key（可选，持仓快照） | ❌ |

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