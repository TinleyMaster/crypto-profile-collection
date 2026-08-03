# scripts

这个目录用于承接从 `n8n Code` 节点逐步抽离出来的复杂逻辑。

当前第一阶段目标：

- 先落 `CMC MAP` 采集入口脚本
- 让代码结构和数据库新架构对齐

目录约定：

- `bin/`：给 n8n 或命令行直接调用的入口脚本
- `src/crypto_research/clients/`：外部 API 客户端
- `src/crypto_research/parsers/`：来源响应解析器
- `src/crypto_research/db/`：数据库连接与写入封装
- `src/crypto_research/utils/`：公共工具函数
- `sql/`：SQL 模板文件

建议运行方式：

```powershell
python ".\bin\ingest_cmc_map.py" --dry-run
```

配置方式：

1. 复制 `.env.example` 为 `.env`
2. 至少填写：
   - `CMC_API_KEY`
   - `DATABASE_URL`（仅真实写库时需要）

说明：

- `--dry-run` 只要求 `CMC_API_KEY`
- 正式写库模式同时要求 `CMC_API_KEY` 和 `DATABASE_URL`
- 脚本会优先读取当前 shell 环境变量，其次读取 `scripts/.env`
