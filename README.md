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

**五大数据源：**
- **CoinMarketCap** — 币种 map + info（URLs：官网/文档/GitHub/Twitter/Telegram/Reddit/Facebook）
- **CoinGecko** — 币种列表 + coin_info（links 提取文档入口，详情 API 国内不可达）
- **DeFiLlama** — 协议列表 + TVL（url/twitter 提取官网链接）
- **DexScreener** — 无文档入口资产的兜底补充（官网/社交链接）
- **Etherscan / BSCScan** — 链上数据（持仓集中度、大额转账监控）

---

## 文档处理流水线（Phase A → B）

```
Phase A: 资产核心构建
  CMC/CG/DL 数据源 → 跨源匹配 → core.asset（统一实体）
  去重（按合约地址）、coin_basic 基础数据
            │
            ▼
Phase B1: 文档入口发现          ← 已完成
  CMC/CG/DL/DexScreener 四大数据源
  biz.doc_source_entry ~18,000+ 条
            │
            ▼
Phase B2: 深度文档发现          ← 进行中
  从官网和文档页 HTML 中进一步抓取嵌入的 PDF/白皮书链接
  并发 8 worker，优先处理 official_website → docs
  聚合类域名阻断（30+ 域名，防止跨资产污染）
            │
            ▼
Phase B2.5: AI 噪声清理         ← 进行中
  三层防御：源头阻断 → 规则直删（22+ 域名）→ AI 按资产分组判断
  审计平台白名单保留，AI 误判纠正机制
            │
            ▼
Phase B3: 文档下载与落盘        ← 已完成 77 个 PDF
  目录结构: docs_storage/{symbol}_{asset_id}/whitepapers/{原始文件名}
            │
            ▼
Phase B4: 文档解析              ← 暂停（pypdf 太慢，待优化）
  PDF → Markdown
            │
            ▼
Phase B5: 链接健康检查 + AI 筛选 ← 脚本就绪
  健康检测 + AI 投研相关性评分 → biz.research_url
            │
            ▼
Phase B6: 生成投研资料文件      ← 脚本就绪
  {symbol}_投研网址链接.txt + {symbol}_基础数据.md
            │
            ▼
Phase B7: 防屏蔽链接 Fallback 下载 ← 脚本就绪
  Cloudflare/WAF 链接的兜底下载
```

---

## 链上数据监控（Phase C）

分层策略——告警常驻 + 快照每日自动 + 明细按需查询：

| 层级 | 功能 | 触发方式 | 数据表 |
|------|------|---------|--------|
| 告警层 | 大额转入交易所 | 后台自动循环 | `biz.onchain_transfer_log` |
| 快照层 | 持仓集中度 / Holder 数 | 每日单次全量 | `biz.onchain_holder_snapshot` |
| 查询层 | 巨鲸明细 / 转账全貌 | 投研时按需查询 | 实时 API 拉取 |

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
│   │   │   ├── supplement_*.py # DexScreener 兜底补充
│   │   │   ├── phase_b2_*.py   # 深度文档发现 + AI 噪声清理
│   │   │   ├── phase_b3_*.py   # 文档下载
│   │   │   ├── phase_b5_*.py   # 链接健康 + AI 筛选
│   │   │   ├── phase_b6_*.py   # 生成投研资料
│   │   │   ├── phase_b7_*.py   # Fallback 下载
│   │   │   ├── phase_chain_*.py # 链上数据监控
│   │   │   ├── diag_*.py       # 诊断脚本
│   │   │   ├── curate_*.py     # NotebookLM 精选
│   │   │   └── collect_*.py    # GitHub 活跃度采集
│   │   ├── src/crypto_research/ # 可复用模块
│   │   │   ├── clients/        # API 客户端
│   │   │   │   ├── cmc_client.py        # CoinMarketCap
│   │   │   │   ├── coingecko_client.py  # CoinGecko
│   │   │   │   ├── defillama_client.py  # DeFiLlama
│   │   │   │   ├── etherscan_client.py  # Etherscan / BSCScan
│   │   │   │   ├── http_client.py       # 通用 HTTP
│   │   │   │   └── llm_client.py        # LLM（DeepSeek）
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
│       ├── task_manager.py     # 后台任务管理器（文件状态持久化）
│       ├── db_stats.py         # 数据库统计查询 + 进度计算
│       └── templates/          # 前端页面
│           └── index.html      # 仪表盘 + 币种查询 + 任务面板
│
├── 07_测试与验收/              # 诊断脚本与报告
├── Dockerfile                  # 工作台 Docker 镜像
└── README.md                   # 本文件
```

---

## Web 工作台

基于 Flask 的轻量级操作面板，部署在 Zeabur 云端，提供：

### 仪表盘
- 资产总数、活跃资产、有文档链接的资产数
- 文档链接来源分布（CMC / CG / DL / DexScreener / deep_crawl）
- 任务进度（CG 币种详情采集、CG/CMC/DL 文档入口补充、B2 深度文档发现、B2 AI 噪声清理、DexScreener 补充、链上持仓快照、大额转入交易所告警）

### 币种查询
- 按资产 ID 查询，展示完整资料面板
- 文档链接列表（按来源分类，标注入库来源）
- 一键复制全部链接
- **NotebookLM 精选**：配额粗筛 + AI 排序，智能选出 Top 50 投研链接
- **链上数据**：按需查询持仓集中度 + 大额转账告警
- 手动添加官网链接、创建新资产

### 任务面板
- 按分类展示全部任务（数据源采集 / 文档采集 / AI 筛选 / 投研筛选 / 链上数据 / 诊断 / 维护）
- 一键启动任务，实时查看状态、进度、日志
- 支持终止运行中的任务

### 任务分类

| 分类 | 任务 | 说明 |
|------|------|------|
| 数据源采集 | CG 拉取币种详情（自动循环） | CoinGecko coin_info 拉取 |
| | CG 新增币种入库 | CG 独有币种补充到 core.asset |
| | CG 补充文档入口（自动循环） | 从 coin_info links 提取文档链接 |
| | CMC 补充文档入口（自动循环） | 从 cmc_asset_info urls 提取文档链接 |
| | DL 补充文档入口（自动循环） | 从 DefiLlama protocol_list 提取官网链接 |
| | DexScreener 补充文档入口（自动循环） | 无文档入口资产的兜底补充 |
| 文档采集 | B2 深度文档发现（自动循环） | 从官网 HTML 抓取嵌入的 PDF/白皮书链接 |
| AI 筛选 | B2 AI 噪声清理（按资产·自动循环） | AI 按域名粒度批量判断噪声 |
| 链上数据 | 链上持仓快照采集（每日单次） | 拉取 Top 持有者，计算持仓集中度 |
| | 大额转账监控（告警模式·自动循环） | 标记转入交易所的大额转账 |
| 诊断 | 噪声诊断报告 | 今日新增文档链接的噪声情况 |
| | 数据链路诊断 | 全链路健康度检查 |
| | 高条目资产污染溯源 | 分析文档链接 >500 的代币污染链路 |
| 维护 | 重置高条目资产（>500条） | 清除高条目资产的 deep_crawl 数据 |

---

## 噪声清理体系

三层防御策略：

```
第一层 ── B2 源头阻断
  phase_b2_deep_doc_discovery.py 中 AGGREGATION_DOMAINS
  30+ 聚合域名（code4rena, sherlock, backed.fi 等），爬取时直接跳过
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
```

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

### 本地运行

```bash
cd 05_代码与脚本/workbench
pip install -r requirements.txt
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
- `(asset_id, entry_url)` 唯一约束（DexScreener 文档入口）
- `(chain, tx_hash, contract_address, from_address, to_address)` 唯一约束（转账日志）
- Python 层 `seen.set()` 辅助去重
- URL 归一化处理

---

## 关键工程约束

- CoinGecko API 调用限 60 次/分钟
- DexScreener API 请求间隔 1.2 秒（~50 次/分钟）
- B2 深度爬取并发数 8 worker
- B2 自动循环单轮超时 30 分钟
- 候选 SQL 必须包含 `dse.entry_id IS NULL` 避免重复处理
- 自动循环脚本 `entry_count == 0` 时终止
- 子进程需 `sys.stdout.reconfigure(line_buffering=True)` 保证实时日志
- 线程池 `shutdown(wait=False)` 避免死锁

---

## 部署平台

- **数据库**：Zeabur PostgreSQL
- **Web 工作台**：Zeabur Docker 部署
- **调度**：n8n（Zeabur 同项目） + Web 工作台手动触发

---

## License

MIT