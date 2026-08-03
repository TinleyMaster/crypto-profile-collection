# 加密货币投研资料采集系统 (Crypto Profile Collection)

一套**加密货币投研资料采集与沉淀系统**，采用 **n8n 调度中枢 + Python 数据处理引擎 + Web 工作台** 的混合架构。

核心思路：把"从多数据源抓取币种/协议资料，找到官网/白皮书/文档，再将文档变成可沉淀资产"这件事，拆成**层层可维护的流水线**。

---

## 技术架构

```
┌─────────────────────────────────────────────────────┐
│  biz    业务消费层  doc_source_entry / doc_asset      │
│                    / research_url / coin_basic       │
├─────────────────────────────────────────────────────┤
│  core   统一实体层  asset / asset_source_map          │
├─────────────────────────────────────────────────────┤
│  src_*  来源解析层  src_cmc / src_cg / src_dl        │
├─────────────────────────────────────────────────────┤
│  raw    原始响应层  api_response                     │
├─────────────────────────────────────────────────────┤
│  sys    系统元数据  ingest_run / source_platform      │
└─────────────────────────────────────────────────────┘
```

**三大数据源：**
- **CoinMarketCap** — 币种 map + info（8,091 条）
- **CoinGecko** — 币种列表（18,082 条，详情 API 国内不可达）
- **DeFiLlama** — 协议列表 + TVL（7,969 条）

---

## 文档处理流水线（Phase B）

```
Phase B1: 入口发现            ← 已完成（CMC/CG/DL 三大数据源）
  biz.doc_source_entry ~18,000 条
            │
            ▼
Phase B2: 深度文档发现          ← 进行中
  从官网和文档页 HTML 中进一步抓取嵌入的 PDF/白皮书链接
  并发 15 worker，优先级: official_website → docs
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

## 项目结构

```
├── 02_数据库设计/              # 数据库 Schema 设计文档 + SQL
├── 04_架构与代码方案/          # 项目完整逻辑文档
├── 05_代码与脚本/
│   ├── scripts/                # 核心 Python 脚本
│   │   ├── bin/                # 入口脚本（n8n / 工作台调用）
│   │   │   ├── ingest_*.py     # 数据源采集
│   │   │   ├── bootstrap_*.py  # source → core 批量映射
│   │   │   ├── phase_b2_*.py   # 深度文档发现
│   │   │   ├── phase_b3_*.py   # 文档下载
│   │   │   ├── phase_b5_*.py   # 链接健康 + AI 筛选
│   │   │   ├── phase_b6_*.py   # 生成投研资料
│   │   │   └── phase_b7_*.py   # Fallback 下载
│   │   ├── src/crypto_research/ # 可复用模块
│   │   │   ├── clients/        # API 客户端
│   │   │   ├── parsers/        # 响应解析器
│   │   │   ├── mapping/        # 映射逻辑
│   │   │   ├── db/             # 数据库工具
│   │   │   └── utils/          # 通用工具
│   │   └── sql/                # SQL 模板
│   │
│   └── workbench/              # Flask Web 工作台
│       ├── app.py              # 主应用
│       ├── task_manager.py     # 后台任务管理器
│       ├── db_stats.py         # 数据库统计查询
│       └── templates/          # 前端页面
│
├── Dockerfile                  # 工作台 Docker 镜像
└── .env.example                # 环境变量示例
```

---

## Web 工作台

基于 Flask 的轻量级操作面板，部署在 Zeabur 云端，提供：

- 📊 **仪表盘** — 资产总数、文档入口、爬取进度、文档下载统计
- 🚀 **任务启动** — 一键启动 B2/B3/B5/B6/B7 各阶段采集任务
- 📋 **任务列表** — 实时查看运行状态、进度、日志
- ⏹ **任务控制** — 随时停止运行中的任务

### 本地运行

```bash
cd 05_代码与脚本/workbench
pip install -r requirements.txt
python app.py
# 打开 http://localhost:5000
```

环境变量：`DATABASE_URL=postgresql://user:pass@host:5432/dbname`

### Docker 部署

```bash
docker build -t crypto-workbench .
docker run -p 5000:5000 -e DATABASE_URL=... crypto-workbench
```

---

## 配置

需要以下环境变量（`.env` 文件或系统环境变量）：

| 变量 | 说明 | 必填 |
|------|------|------|
| `DATABASE_URL` | PostgreSQL 连接串 | ✅ |
| `CMC_API_KEY` | CoinMarketCap API Key | ✅ |
| `COINGECKO_API_KEY` | CoinGecko API Key（可选） | ❌ |

---

## 数据去重保证

- `ON CONFLICT DO UPDATE` 主键级去重
- `content_hash` UNIQUE 约束（文档内容级去重）
- Python 层 `seen.set()` 辅助去重
- URL 归一化处理

---

## 部署平台

- **数据库**：Zeabur PostgreSQL
- **Web 工作台**：Zeabur Docker 部署
- **调度**：n8n（Zeabur 同项目） + Web 工作台手动触发

---

## License

MIT
