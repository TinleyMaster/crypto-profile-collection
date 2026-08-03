# n8n 加密投研自动化｜最终完整落地方案（基于Zeabur n8n + Zeabur PostgreSQL）
> 适配你：链上KOL/加密投研、定时采集基本面&行情、后续对接 NotebookLM + ima知识库，彻底规避飞书多维表格行数上限、迁移困难问题
## 一、整体架构总览
### 架构分层
1. **调度执行层**：Zeabur 美区 n8n（外部API统一请求、数据清洗、定时调度）
2. **持久存储层**：Zeabur PostgreSQL（唯一权威主数据库，存储全部结构化时序/基础数据）
3. **可视化浏览层（可选）**：飞书多维表格（定时从PG同步数据，仅用作日常查看、筛选看板，**不再承担写入存储**）
4. **文档资产层**：Google Drive + ima知识库（统一存放Markdown投研文档、白皮书、审计PDF，Drive承担原始归档，ima承担知识化沉淀）
5. **知识消费层**：NotebookLM + ima知识库（NotebookLM负责深度阅读/问答/总结，ima负责中文知识检索与沉淀）
6. **通知告警层**：飞书机器人（任务日报、异常告警、大额解锁提醒）

### 核心优势对比旧飞书方案
✅ 无2000行记录上限，时序数据可长期沉淀
✅ 标准PostgreSQL，任意服务器一键迁移，不绑定平台
✅ 原生SQL支持幂等写入、主键约束、数据统计，稳定性远高于飞书Bitable API
✅ 冷热数据分离、归档、指标查询极其灵活
✅ 后续可扩展Grafana可视化、自定义数据分析脚本

### 外部数据源清单
1. DefiLlama API：TVL、协议收入、代币解锁计划
2. CoinMarketCap(CMC) API：币种基础信息、现货行情
3. CoinGlass API：衍生品OI、多空比、资金费率

## 二、前置环境清单（当前已具备）
1. Zeabur n8n 服务（美区节点）
2. Zeabur PostgreSQL 17（独立服务）
3. 飞书自建应用 + 飞书机器人Webhook
4. API密钥：CMC_API_KEY、COINGLASS_API_KEY（存入n8n External Secrets，禁止硬编码）
5. Google服务账号（后续素材生成、PDF归档上传Drive使用）
6. ima知识库账号/空间（用于文档上传、检索与知识沉淀）

## 三、数据库设计（PostgreSQL 7张核心表）
> 复制下方SQL在Zeabur PG【命令】终端一次性执行，自动创建全部数据表、主键约束
```sql
-- 1.币种基础档案（维度表，1币种1行，更新为主）
CREATE TABLE coin_basic (
    coin_symbol VARCHAR(128) NOT NULL,
    coin_name VARCHAR(128),
    defillama_slug VARCHAR(128),
    coingecko_id VARCHAR(128),
    cmc_id BIGINT PRIMARY KEY,
    category VARCHAR(64),
    main_chain VARCHAR(64),
    contract_addresses JSONB,
    total_supply NUMERIC,
    team_allocation NUMERIC,
    investor_allocation NUMERIC,
    community_allocation NUMERIC,
    audit_status VARCHAR(32),
    audit_firm VARCHAR(128),
    audit_report_url TEXT,
    official_website TEXT,
    docs_url TEXT,
    github_url TEXT,
    financing_amount NUMERIC,
    investors TEXT,
    launch_date DATE,
    track_status VARCHAR(32) DEFAULT '跟踪中',
    last_updated TIMESTAMP,
    remark TEXT
);

-- 2.TVL&协议现金流时序表（日追加时序数据）
CREATE TABLE coin_tvl_timeseries (
    defillama_slug VARCHAR(128) NOT NULL,
    coin_symbol VARCHAR(32),
    record_date DATE NOT NULL,
    total_tvl NUMERIC,
    tvl_btc_chain NUMERIC,
    tvl_eth NUMERIC,
    tvl_bsc NUMERIC,
    tvl_other NUMERIC,
    daily_fees NUMERIC,
    daily_revenue NUMERIC,
    apr_7d NUMERIC,
    tvl_change_7d NUMERIC,
    revenue_change_7d NUMERIC,
    data_source VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY(defillama_slug, record_date)
);

-- 3.现货行情时序表
CREATE TABLE coin_market (
    coin_symbol VARCHAR(32) NOT NULL,
    record_date DATE NOT NULL,
    price_usd NUMERIC,
    market_cap NUMERIC,
    fdv NUMERIC,
    circulating_supply NUMERIC,
    volume_24h NUMERIC,
    change_24h NUMERIC,
    change_7d NUMERIC,
    holder_count BIGINT,
    data_source VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY(coin_symbol, record_date)
);

-- 4.衍生品合约数据表（8小时高频采集）
CREATE TABLE coin_derivatives (
    coin_symbol VARCHAR(32) NOT NULL,
    record_time TIMESTAMP NOT NULL,
    total_oi NUMERIC,
    oi_change_24h NUMERIC,
    long_short_ratio NUMERIC,
    funding_rate NUMERIC,
    liquidation_long_24h NUMERIC,
    liquidation_short_24h NUMERIC,
    data_source VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY(coin_symbol, record_time)
);

-- 5.代币解锁事件表（周度更新）
CREATE TABLE coin_unlock_events (
    coin_symbol VARCHAR(32) NOT NULL,
    unlock_date DATE NOT NULL,
    unlock_type VARCHAR(32) NOT NULL,
    unlock_amount NUMERIC,
    unlock_ratio_total NUMERIC,
    unlock_ratio_circulating NUMERIC,
    unlock_value_usd NUMERIC,
    beneficiary_type VARCHAR(64),
    remaining_locked NUMERIC,
    risk_level VARCHAR(32),
    data_source VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY(coin_symbol, unlock_date, unlock_type)
);

-- 6.工作流运行日志表（运维日志）
CREATE TABLE sys_run_log (
    id SERIAL PRIMARY KEY,
    workflow_name VARCHAR(128),
    run_time TIMESTAMP DEFAULT NOW(),
    status VARCHAR(32),
    total_count INT,
    success_count INT,
    fail_count INT,
    error_detail TEXT,
    duration_second INT
);

-- 7.文档资产表（白皮书/审计报告/docs/PDF归档与知识库同步状态）
CREATE TABLE doc_asset (
    id SERIAL PRIMARY KEY,
    cmc_id BIGINT NOT NULL,
    coin_symbol VARCHAR(128) NOT NULL,
    coin_name VARCHAR(128),
    defillama_slug VARCHAR(128),
    source_type VARCHAR(32) NOT NULL,
    doc_type VARCHAR(32) NOT NULL,
    source_url TEXT NOT NULL,
    resolved_url TEXT,
    file_name VARCHAR(256),
    mime_type VARCHAR(128),
    content_hash VARCHAR(128) NOT NULL,
    file_size_bytes BIGINT,
    language VARCHAR(32),
    drive_folder_path TEXT,
    drive_file_url TEXT,
    ima_folder_path TEXT,
    ima_doc_url TEXT,
    sync_notebooklm_status VARCHAR(32) DEFAULT '待同步',
    sync_ima_status VARCHAR(32) DEFAULT '待同步',
    parse_status VARCHAR(32) DEFAULT '待解析',
    version_tag VARCHAR(64),
    last_seen_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (content_hash),
    UNIQUE (cmc_id, source_url)
);
```
### 写入核心规则
所有时序表使用 `ON CONFLICT ... DO UPDATE` 实现**幂等写入**：重复执行任务不会产生重复脏数据。

### 文档资产表管理规则
1. `doc_asset` 以 `content_hash` 作为主去重键，避免同一PDF从官网、docs、GitHub重复入库。
2. 同一币种全部文档必须落到同一个币种目录下，不按来源拆散存放，来源仅作为目录子层和元数据字段保留；目录唯一键以 `cmc_id` 为准，不以 `coin_symbol` 为准。
3. `source_type` 记录入口来源（official_website / docs_url / github_url），`doc_type` 记录文档类型（whitepaper / audit / docs / deck / tokenomics / research）。
4. `drive_folder_path` 与 `ima_folder_path` 必须同步写入，保证后续重传、回溯、NotebookLM 与 ima 检索路径一致。
5. 已存在相同 `content_hash` 时，仅更新 `last_seen_at`、最新 `source_url`、同步状态与时间戳，不重复上传文件。

### doc_asset 幂等写入SQL模板
```sql
INSERT INTO doc_asset (
    cmc_id, coin_symbol, coin_name, defillama_slug,
    source_type, doc_type, source_url, resolved_url,
    file_name, mime_type, content_hash, file_size_bytes,
    language, drive_folder_path, drive_file_url,
    ima_folder_path, ima_doc_url,
    sync_notebooklm_status, sync_ima_status, parse_status,
    version_tag, last_seen_at, updated_at
) VALUES (
    $1, $2, $3, $4,
    $5, $6, $7, $8,
    $9, $10, $11, $12,
    $13, $14, $15,
    $16, $17,
    $18, $19, $20,
    $21, NOW(), NOW()
)
ON CONFLICT (content_hash) DO UPDATE SET
    source_url = EXCLUDED.source_url,
    resolved_url = EXCLUDED.resolved_url,
    drive_folder_path = EXCLUDED.drive_folder_path,
    drive_file_url = EXCLUDED.drive_file_url,
    ima_folder_path = EXCLUDED.ima_folder_path,
    ima_doc_url = EXCLUDED.ima_doc_url,
    sync_notebooklm_status = EXCLUDED.sync_notebooklm_status,
    sync_ima_status = EXCLUDED.sync_ima_status,
    parse_status = EXCLUDED.parse_status,
    version_tag = EXCLUDED.version_tag,
    last_seen_at = NOW(),
    updated_at = NOW()
RETURNING id, cmc_id, coin_symbol, content_hash;
```

## 四、n8n 工作流架构规范
> 统一规范：**所有采集子工作流触发器 = When executed by another workflow**
> 定时Cron只放在顶层【主调度工作流】，统一管控时序、限流等待，便于维护

### 调度总控工作流（3条）
#### A｜日级主调度（Cron `0 6 * * *` 每日06:00执行）
执行顺序：
1. 调用【子工作流2：TVL&现金流采集】
2. Wait 30秒限流缓冲
3. 调用【子工作流3：现货行情采集】
4. Wait 30秒
5. 调用【子工作流8：每日简报汇总&飞书推送】

#### B｜周级主调度（Cron `0 8 * * 1` 每周一08:00执行）
执行顺序：
1. 调用【子工作流1：币种基础档案批量更新】
2. Wait 60秒
3. 调用【子工作流5：代币解锁事件同步】
4. Wait 30秒
5. 调用【子工作流9：周度数据质量巡检】

#### C｜衍生品高频调度（Cron `0 */8 * * *` 每8小时执行）
1. 调用【子工作流4：衍生品合约数据采集】
2. IF判断：OI 24h波动＞20% → 飞书推送行情异常预警

### 业务子工作流清单
1. 子工作流1：币种基础档案批量同步（周更｜CMC API → coin_basic）
2. 子工作流2：TVL&协议现金流时序采集（日更｜DefiLlama API → coin_tvl_timeseries）【优先搭建测试】
3. 子工作流3：现货二级市场行情采集（日更｜CMC API → coin_market）
4. 子工作流4：衍生品合约数据采集（8小时｜CoinGlass API → coin_derivatives）
5. 子工作流5：代币解锁事件同步（周更｜DefiLlama Unlocks API → coin_unlock_events）
6. 子工作流6：单币种投研素材生成（手动触发｜聚合数据库数据→Markdown→上传Google Drive + ima知识库，对接NotebookLM）
7. 子工作流7：项目文档抓取归档（周更/手动｜official_website/docs_url/github_url → 发现PDF/白皮书/审计报告 → 下载 → 去重 → 上传Google Drive + ima知识库 → 写入doc_asset）
8. 子工作流8：每日数据简报生成推送
9. 子工作流9：周度数据质量巡检
10. 【可选辅助】子工作流10：PG → 飞书多维表格定时同步（每日同步精选数据，仅用于查看）

### 子工作流7细化设计｜项目文档抓取归档
链路顺序：
1. 从 `coin_basic` 读取 `cmc_id`、`coin_symbol`、`coin_name`、`defillama_slug`、`official_website`、`docs_url`、`github_url`
2. 对三个入口地址分别抓取页面内 PDF 链接、白皮书链接、审计报告链接和 docs 下载链接
3. 标准化 URL 后下载文件，计算 `content_hash`、识别 `doc_type`、`mime_type`、语言和版本标签
4. 查询 `doc_asset` 去重：若已存在相同哈希，则仅更新状态；否则继续上传
5. 上传到 Google Drive 固定币种目录，再同步上传到 ima知识库对应币种目录
6. 将 `drive_folder_path`、`drive_file_url`、`ima_folder_path`、`ima_doc_url`、同步状态写回 `doc_asset`
7. 若抓取失败或上传失败，写入 `sys_run_log` 并标记待重试

推荐节点清单：
1. `Manual Trigger / Execute Workflow Trigger`：支持手动调试与主工作流调用。
2. `Postgres - Read coin_basic`：读取 `track_status='跟踪中'` 且至少存在一个文档入口URL的项目。
3. `Code - Build Source Queue`：把 `official_website`、`docs_url`、`github_url` 展平成待抓取队列，并附带 `cmc_id`。
4. `Split In Batches`：按项目或URL分批抓取，控制外部站点压力。
5. `HTTP Request - Fetch HTML/PDF`：先拉页面HTML，再对候选PDF链接发起下载请求。
6. `Code - Extract & Normalize Links`：抽取 `.pdf`、whitepaper、audit、docs 下载链接，补全相对路径。
7. `IF - Is PDF Candidate`：过滤非PDF或明显无效链接。
8. `HTTP Request - Download Binary`：下载文件二进制内容。
9. `Code - Hash & Classify`：计算 `content_hash`，识别 `doc_type`、`source_domain`、`version_tag`、建议文件名。
10. `Postgres - Check doc_asset`：按 `content_hash` 查询是否已存在。
11. `IF - Exists?`：若已存在则走状态更新分支；不存在则继续上传分支。
12. `Google Drive - Upload`：写入 `CryptoResearch/{coin_symbol}__cmc_{cmc_id}/...` 对应子目录。
13. `ima知识库 - Upload`：写入与 Drive 一致的币种目录结构。
14. `Postgres - Upsert doc_asset`：执行幂等写入SQL，回写双端路径与状态。
15. `Postgres - Insert sys_run_log`：记录成功数、失败数、错误明细与耗时。

## 五、分阶段落地执行路线（严格顺序，最小试错成本）
### 阶段1｜环境初始化（预估2h）
1. Zeabur PostgreSQL执行建表SQL，创建7张数据表
2. n8n新建PostgreSQL凭证，填入Zeabur PG连接信息，测试连通
3. n8n录入全部API密钥至External Secrets
4. 手动在 `coin_basic` 插入第一条测试币种：Lorenzo $BANK
    - cmc_id: 29481
    - coin_symbol: BANK
    - defillama_slug: lorenzo-protocol

✅ 验收标准：n8n可正常连接PG，可手动查询、写入测试数据

### 阶段2｜最小闭环验证：TVL采集子工作流（预估1.5h，最高优先级）
工作流链路：
手动触发 → PG读取coin_basic【跟踪中】币种 → Split分批循环 → HTTP动态请求DefiLlama API `https://api.llama.fi/protocol/{{$json.defillama_slug}}` → Set清洗字段 → PostgreSQL幂等写入coin_tvl_timeseries
✅ 验收：重复执行不会产生重复记录；飞书可选择同步预览数据

### 阶段3｜逐个搭建剩余采集子工作流（预估1.5h）
搭建一条、单独测试稳定后，再开发下一条：
1. CMC基础档案同步
2. 现货行情采集
3. 代币解锁采集
4. 衍生品合约采集

### 阶段4｜MVP小批量测试（20个重点币种）（预估2h）
1. coin_basic批量录入20个跟踪币种，完善cmc_id、defillama_slug映射
2. 搭建3条顶层主调度工作流，配置分批限流、等待间隔
3. 持续运行3天测试定时任务，确认无时序断档、无429限流报错

✅ 验收：自动定时采集，日志正常写入sys_run_log

### 阶段5｜告警与运维体系搭建（预估2h）
1. 完善sys_run_log自动写入逻辑
2. 配置飞书机器人：每日简报、任务失败告警、大额解锁预警
3. 开启Zeabur PostgreSQL【自动定时备份】

### 阶段6｜NotebookLM + ima素材自动化链路（预估2.5h）
1. 搭建子工作流6：手动输入 `cmc_id` 或 `coin_symbol` → 查询PG内全部该币种数据 → 生成标准化Markdown投研文档 → 上传Google Drive + ima知识库
2. 搭建子工作流7：读取 `official_website`、`docs_url`、`github_url` → 发现PDF/白皮书/审计报告 → 下载 → 计算哈希去重 → 上传Google Drive + ima知识库 → 写入 `doc_asset`
✅ 验收：NotebookLM可自动同步Drive内md/pdf文件；ima知识库内可检索对应项目资料

### 阶段7｜高阶优化迭代（预估2h）
1. 配置时序数据冷热归档策略（可选定期归档90天前历史数据）
2. 微调API重试、分批限流参数
3. 为 `doc_asset` 增加版本标签、失败重试和同步状态巡检
4. 【可选】搭建PG→飞书多维表格每日同步任务

## 六、数据库迁移方案（未来更换服务器备用）
### 方式：标准pg_dump 离线备份迁移（通用、跨任何服务商）
#### 1）当前Zeabur PG执行备份
打开Zeabur PG【命令】终端执行：
```bash
pg_dump -h 数据库host -p 端口 -U 用户名 -d 数据库名 -F c -f crypto_backup.dump
```
参数全部从Zeabur页面复制，`-F c` 压缩二进制格式，标准通用。
执行完成后，页面【文件】下载 `crypto_backup.dump` 本地云端双重存档。

#### 2）新服务器部署PostgreSQL（推荐16/17版本）
新建空数据库，执行恢复命令：
```bash
pg_restore -h 新host -p 新端口 -U 新用户名 -d 新数据库名 crypto_backup.dump
```

#### 3）切换n8n连接
仅修改n8n内PostgreSQL凭证的连接地址、账号密码；**所有工作流、SQL语句无需任何改动**。

#### 迁移前后数据校验SQL（复制直接执行，核对表行数）
```sql
SELECT 'coin_basic' table_name,COUNT(*) FROM coin_basic
UNION ALL
SELECT 'coin_tvl_timeseries',COUNT(*) FROM coin_tvl_timeseries
UNION ALL
SELECT 'coin_market',COUNT(*) FROM coin_market
UNION ALL
SELECT 'coin_derivatives',COUNT(*) FROM coin_derivatives
UNION ALL
SELECT 'coin_unlock_events',COUNT(*) FROM coin_unlock_events
UNION ALL
SELECT 'doc_asset',COUNT(*) FROM doc_asset
UNION ALL
SELECT 'sys_run_log',COUNT(*) FROM sys_run_log;
```

### 长期备份规范
1. 开启Zeabur PG平台自动快照备份（每日）
2. **每月手动导出一份dump文件，保存至Google Drive兜底**

## 七、API限流&容错规范
| 数据源 | 单批次并发 | 批次间隔 | 重试策略 |
|--------|------------|----------|----------|
| DefiLlama | 10 | 10s | 3次指数退避重试(2s/5s/10s) |
| CoinMarketCap | 优先批量接口 | 20s | 3次重试 |
| CoinGlass | 10 | 30s | 3次重试 |

硬性红线：
1. **禁止使用大模型获取slug、合约地址、供给量等事实参数**，规避幻觉；
2. defillama_slug、cmc_id映射关系人工维护在coin_basic，不使用模糊自动匹配；
3. 飞书自动化仅用作消息通知，**绝对不承担外部API采集工作**。

## 八、存储分层规范
1. 核心结构化时序数据：PostgreSQL（唯一数据源）
2. 原始文档归档层：Google Drive（存放Markdown、白皮书、审计PDF，作为长期原始文件仓库，并供给NotebookLM同步）
3. 知识沉淀层：ima知识库（存放精选项目资料、白皮书、审计报告与研究文档，用于中文检索、问答与知识复用）
4. 知识消费层：NotebookLM + ima知识库（NotebookLM偏深度阅读与总结，ima偏中文检索与沉淀）
5. 飞书多维表格：仅做可视化浏览看板，不再作为数据写入目标
6. 飞书15GB云盘：仅存放临时截图、短期文档，不存储海量归档数据

### Google Drive + ima知识库目录设计
核心原则：**同一个币的全部文件必须放在同一个币种主目录下**，禁止按来源站点把同币种文件拆到不同目录。

推荐目录结构：
- 主目录：`CryptoResearch/{coin_symbol}__cmc_{cmc_id}/`
- 子目录1：`01_overview/`（项目摘要、人工研究笔记、Markdown投研稿）
- 子目录2：`02_official_docs/`（官网 docs 导出的 PDF、产品文档）
- 子目录3：`03_whitepaper/`（whitepaper、litepaper、tokenomics）
- 子目录4：`04_audit/`（审计报告、安全评估）
- 子目录5：`05_github_exports/`（GitHub Release、仓库内 PDF、技术文档导出）
- 子目录6：`99_archive/`（旧版本、重复版本、失效替换文件）

目录落盘规则：
1. Google Drive 与 ima知识库使用相同的币种主目录命名规则，确保 NotebookLM 与 ima 的资料视图一致。
2. 币种主目录唯一键统一使用 `cmc_id`，推荐命名：`{coin_symbol}__cmc_{cmc_id}`；即使 `coin_symbol` 重复，也不会发生目录冲突。
3. 一个币种无论来源是 `official_website`、`docs_url` 还是 `github_url`，都只能写入该币种主目录下对应子目录。
4. 文件名统一建议：`{coin_symbol}_{doc_type}_{source_domain}_{version_tag}_{yyyymmdd}.pdf`
5. 若缺少 `version_tag`，则用 `unknown` 占位，不允许裸文件名上传。
6. `99_archive/` 仅存历史版本，不参与 NotebookLM 主分析目录；NotebookLM 优先读取前 5 个子目录。
