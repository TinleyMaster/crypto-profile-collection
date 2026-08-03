# [OPEN] debug-zeabur-pg-connect

## 问题

- 目标：从本地 Python 脚本连接 Zeabur PostgreSQL
- 现象：`psycopg.connect()` 返回 `server closed the connection unexpectedly`
- 期望：能成功建立连接，并为后续 `ingest_cmc_map.py` 提供真实写库能力

## 当前假设

1. 主机 `43.166.198.83:32405` 的 TCP 端口本身不可达或不稳定。
2. Zeabur PostgreSQL 未允许当前公网来源访问，或服务端策略在握手阶段主动断开。
3. 连接串参数不完整，可能需要额外的 SSL / libpq 连接选项。
4. 数据库实例当前异常、重启中或连接数达到上限，导致服务端提前关闭连接。

## 已知信息

- `.env` 中存在 `DATABASE_URL`
- `ingest_cmc_map.py --dry-run` 已成功
- 当前阻塞点只剩真实写库

## 证据更新

- `43.166.198.83:32405` 的 TCP 连通性正常。
- 使用同一账号连接 PostgreSQL 实例时，数据库 `zeabur` 可连，但里面是 n8n / agents 相关表，不是投研库。
- 同一实例中存在数据库：`crypto`、`postgres`、`zeabur`。
- `crypto` 数据库中存在本项目新建的 `sys/raw/src_cmc/src_cg/src_llama/core/biz` schema 与目标表。
- `sslmode=require` 会失败，提示服务端不支持 SSL；原始连接或 `sslmode=disable/prefer` 可正常连接。

## 当前结论

根因不是公网连通性，也不是账号密码错误，而是 `scripts/.env` 指向了错误的数据库名：

- 错误：`/zeabur`
- 正确：`/crypto`

## 修复与验证

### 已执行修复

1. 将 `scripts/.env` 中的 `DATABASE_URL` 从 `/zeabur` 改为 `/crypto`
2. 修复 `ingest_cmc_map.py` 在写库阶段的两个代码问题：
   - `is_active` 从 `0/1` 归一化为 `boolean`
   - 失败时先 `rollback()` 再记录失败状态

### 验证结果

- `WF_CMC_MAP_INGEST` 最新运行记录：
  - `run_id = 2`
  - `status = success`
  - `total_items = 8091`
- `raw.api_response` 已写入 `cmc_map` 原始响应
- `src_cmc.cmc_asset_map` 当前记录数：`8091`
- 抽样数据：
  - `(1, 'BTC', 'Bitcoin', True)`
  - `(2, 'LTC', 'Litecoin', True)`
  - `(3, 'NMC', 'Namecoin', True)`

## 下一步

1. 以同样方式推进 `ingest_cmc_info.py`
2. 开始搭建 `WF_CMC_INFO_BATCH`
3. 再进入 `core` 映射层刷新
