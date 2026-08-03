# 加密货币白皮书批量抓取 → ima / NotebookLM 投研知识库

## 目标
从 Zeabur 上的 PostgreSQL 导出币种基础数据（official_website / docs_url / github_url），用**分层混合方案**批量抓取白皮书/投研文档，输出 **PDF + Markdown 双份**，整理成规范文件夹 + 清单，最后**尝试自动上传** ima 知识库与 NotebookLM（自动不成则手动批量导入兜底）。

## 已确认的决策
| 项 | 决策 |
|---|---|
| 数据源 | Zeabur PostgreSQL（主人贴连接串，只读导出后断开） |
| 抓取方案 | A：分层混合（HTTP 为主，Playwright 浏览器兜底） |
| 规模 | 先拿截图里 ~28 个币试点，跑通验证后再扩量 |
| 输出 | PDF + Markdown 双份 |
| 上传 | 尝试自动上传（ima 走 MCP 探测、NotebookLM 走浏览器自动化），手动导入兜底 |

## 执行步骤

### 阶段 0：环境准备 + 数据接入
- 用 managed Python 3.13 建独立 venv：`C:\Users\SuperTing\.workbuddy\binaries\python\envs\whitepaper`
- 装依赖：httpx、trafilatura、psycopg[binary]、playwright、python-dotenv；`playwright install chromium`（一次性 ~150MB）
- 主人提供连接串 → 写入 `E:\瞎搞乱搞\workbuddy\白皮书文件抓取\.env`（仅本地使用，不外传；建议跑完后主人改 DB 密码）
- 只读查询导出币种表 → `data/coins.csv`；试点批次按截图里的币筛选（约 28 个：aidcoin、feathercoin、bitcoin、primecoin、tera、zetac、etherlegends、idex、oceanprotocol、irisnet、ironfish、safehaven 等）
- 导出后即断开，后续流程不再依赖远程库

### 阶段 1：直连 PDF 批量下载（HTTP 层）
- docs_url 以 .pdf 结尾或 Content-Type 为 application/pdf 的（如 aidcoin、primecoin、TERA、oceanprotocol、firebase storage 链接）
- httpx 异步并发（并发 5–8、超时 30s、重试 2 次、浏览器 UA）
- 校验 %PDF 魔数 → `output/pdfs/{slug}.pdf`

### 阶段 2：HTML 文档页 → Markdown（提取层）
- trafilatura 提取正文转 Markdown → `output/markdown/{slug}.md`
- 多页文档站（docs.idex.io、ironfish.network/docs 等）：同域限深爬取（≤20 页/站）合并为单个 md
- 同时用 Playwright 打印 PDF（双份的 PDF 侧）→ `output/pdfs/{slug}.pdf`

### 阶段 3：Playwright 浏览器兜底
- 对象：slideshare、docsend、JS 重渲染页面、阶段 2 正文过短/失败的
- 渲染后 `page.pdf()` + 文本提取双份落盘
- 遇 Cloudflare 人机验证 → 标记 `manual`，进失败清单

### 阶段 4：GitHub 补充（docs_url 为空的币）
- GitHub API 搜 repo 内 whitepaper*.pdf / docs 目录 / README 白皮书链接（有 token 最好，无 token 60 次/小时够试点用）
- 命中后回流阶段 1/2 处理

### 阶段 5：死链 archive.org 回退
- 404/超时/域名失效的 docs_url → Wayback Machine CDX API 查最近快照 → 回流阶段 1/2

### 阶段 6：整理交付
```
E:\瞎搞乱搞\workbuddy\白皮书文件抓取\
├── .env                     # DB 凭据（本地专用）
├── data\coins.csv           # 导出的币种基础数据
├── output\
│   ├── pdfs\{slug}.pdf      # 全部 PDF
│   ├── markdown\{slug}.md   # 全部 Markdown
│   └── manifest.csv         # coin, slug, 来源URL, source_type, status, fail_reason, 文件路径
└── scraper\                 # 抓取脚本（可断点续跑）
```
- 试点报告：成功率、失败原因分布、manual 清单

### 阶段 7：上传（先探测后动手，实验性）
- **ima**：请主人连接 ima-mcp 连接器 → 探测其能力（大概率偏检索）；若不支持文件上传 → 主人在 ima 客户端批量导入 `output` 文件夹
- **NotebookLM**：无公开 API → Playwright + 主人 Google 登录态（storage_state）半自动上传，每 notebook ~50 源上限需分批；登录态/风控过不去 → 手动上传
- 试点阶段先验证 1–2 个文件的自动上传链路，不通就直接走手动

## 风险与注意
- 凭据安全：连接串只存本地 .env；建议试点完成后修改 DB 密码
- 反爬：并发克制、随机间隔、UA 伪装；slideshare/docsend 成功率不保证
- 老币（2017–2018 时代）站点大量失效，archive.org 回退可挽回一部分
- 自动上传是实验性环节，手动批量导入是可靠兜底

## 需要主人提供
1. Zeabur PostgreSQL 连接串（host / port / username / password / database），并确认「网络」页签已开公网访问
2. （可选）GitHub personal token，提高 API 额度
3. 试点跑完确认效果后，再决定是否扩量到全量数据
