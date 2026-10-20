# 🔄 Zeabur → 腾讯云轻量 迁移手册

> **目标服务器**：腾讯云轻量 东京，2核8G，IP `43.165.185.221`，实例名 `richwoman`  
> **源服务器**：Zeabur Ashburn 美东，服务 `crypto-profile-collection`，2核8G

---

## 📋 迁移 Checklist（按顺序执行）

| # | 动作 | 负责人 | 截止 | 状态 |
|---|------|--------|------|------|
| 0 | Zeabur → **导出 PG 全量备份** | 你 | **10/21 前** | 🔴 紧急 |
| 1 | Zeabur → 抄下所有环境变量 | 你 | 10/21 | ⚪ |
| 2 | Zeabur → 轮换所有 API Key（安全事件）| 你 | 部署前 | ⚪ |
| 3 | 本地 → 把仓库 clone 到腾讯云服务器 | 你 | 10/22 | ⚪ |
| 4 | 服务器上 → 复制 `.env.example` 填真实值 | 你 | 10/22 | ⚪ |
| 5 | 服务器上 → 跑 `bash deploy.sh` | 你 | 10/22 | ⚪ |
| 6 | 服务器上 → pg_restore 导入 Zeabur 备份 | 你 | 10/23 | ⚪ |
| 7 | DNS 切域名到新服务器 IP | 你 | 部署稳定后 | ⚪ |
| 8 | 验证：工作台 60 条调度任务正常跑 | 你 | 切换后 1 天 | ⚪ |
| 9 | Zeabur 停服后才删 | 你 | 12月31日后 | ⚪ |

---

## 🔴 第 0 步：Zeabur 数据库导出（最紧急）

### 方式 A：Zeabur 控制台导出（推荐）

1. 登录 https://zeabur.com → 你的项目 → 左侧菜单 **PostgreSQL**
2. 找到你的数据库实例，点击进入
3. 详情页找「**备份与恢复**」或「**立即创建备份**」按钮
4. 等几分钟，下载生成的 `.sql` 或 `.dump` 文件

### 方式 B：pg_dump 远程导出

Zeabur 控制台 → PostgreSQL → **连接信息**（Connection Info）→ 复制连接串。然后：

```bash
# 在你本地 Windows（装了 pg_dump 的机器上）
pg_dump -h <host> -p <port> -U <user> -d <dbname> -Fc --no-owner --no-privileges -f zeabur_backup.dump

# 交互式输入密码（Zeabur 连接信息里找）
```

导出后文件约 **几百 MB**，传到服务器：

```powershell
scp zeabur_backup.dump root@43.165.185.221:~/
```

---

## 📦 第 3 步：仓库拉到腾讯云服务器

```bash
# SSH 登录（用自动生成密码）
ssh root@43.165.185.221

# 安装 git
apt-get update && apt-get install -y git

# clone 你的仓库
cd /opt
git clone <你的仓库地址> crypto-research
cd crypto-research

# 确认目录结构
ls -la
# 应该能看到：Dockerfile, docker-compose.yml, Caddyfile, .env.example, deploy.sh, 05_代码与脚本/
```

---

## ⚙️ 第 4 步：填环境变量

```bash
cd /opt/crypto-research
cp .env.example .env
vi .env   # 或者 nano .env
```

必须改的值：

| 变量 | 改成 |
|------|------|
| `POSTGRES_PASSWORD` | 随机强密码（至少 20 字符，别用 `CHANGE_ME_IN_ENV`）|
| `SMTP_*` | 从 Zeabur 抄过来（轮换后的值）|
| `BINANCE_API_*` | 新生成的 |
| `*_API_KEY` | 全部轮换后填入 |

---

## 🚀 第 5 步：跑部署脚本

```bash
cd /opt/crypto-research
bash deploy.sh
```

脚本会自动：
1. ✅ 安装 Docker + Docker Compose
2. ✅ 创建 runtime/ 目录
3. ✅ 拉 postgres:16-alpine + caddy:2-alpine
4. ✅ 构建 app 镜像（用项目根目录的 Dockerfile）
5. ✅ 启动 3 个容器（app + postgres + caddy）
6. ✅ 等 PG 就绪

**预期输出**：最后打印 `docker compose ps`，3 个容器都 `Up (healthy)`。

---

## 📥 第 6 步：导入 Zeabur 备份

```bash
# 先确认 Zeabur 备份文件在服务器上
ls -lh ~/zeabur_backup.dump   # 或你的文件名

# 把备份文件拷进 postgres 容器
docker cp ~/zeabur_backup.dump crypto-postgres:/tmp/zeabur_backup.dump

# 在容器内执行 pg_restore
# 注意：密码用你 .env 里 POSTGRES_PASSWORD 的值
docker exec -it crypto-postgres \
  pg_restore -U postgres -d crypto \
  --no-owner --no-privileges \
  --clean --if-exists \
  /tmp/zeabur_backup.dump
```

参数解释：
- `--clean --if-exists`：删旧表再导入（幂等，重跑不报错）
- `--no-owner --no-privileges`：跳过权限设置（Docker 里不需要）

**导入时间**：几十秒到几分钟取决于数据库大小。导入完可以进容器确认：

```bash
docker exec -it crypto-postgres psql -U postgres -d crypto -c "\dt"
# 应该看到一堆 biz.* / core.* 表
docker exec -it crypto-postgres psql -U postgres -d crypto -c "SELECT count(*) FROM biz.asset_master;"
# 应该有数据
```

---

## 🌐 第 7 步：切域名

### 方案 A：沿用 Zeabur 的二级域名

Zeabur 说 12月1日起共享服务免费但随时可能停。`crypto-profile-collection.zeabur.app` 这个域名可能失效。**推荐买一个自己的域名**（阿里云 / 腾讯云 / Cloudflare 都可以，¥30-60/年）。

### 方案 B：买新域名

1. 在腾讯云 / 阿里云 / Cloudflare 买一个域名，比如 `crypto-research.xyz`
2. DNS 加一条 **A 记录**：
   - 主机记录：`@`（根域名）或 `crypto`（子域名）
   - 记录值：`43.165.185.221`
3. 等待 DNS 生效（几分钟到几小时）
4. 编辑 Caddyfile，把域名改成你的新域名
5. 重启 caddy：`docker compose restart caddy`
6. Caddy 会自动向 Let's Encrypt 申请 HTTPS 证书

### 临时方案：先不用域名

Caddyfile 里已经配了 `:80` 块，直接访问 http://43.165.185.221 就能看。**只是不安全（没有 HTTPS），而且如果服务器重启后 IP 变了会失联**（Lighthouse 是弹性公网 IP，重启不变）。

---

## ✅ 第 8 步：验证切稳

部署后等 1 天确认：

| 验证点 | 命令 | 预期 |
|--------|------|------|
| 60+ cron 任务在跑 | `tail -f runtime/logs/scheduler.log` | 每到点有 `[调度] 已提交 xxx` 日志 |
| scan_daemon K 线刷新 | `tail -f runtime/scan_daemon.log` | 每 5 分钟有 `scan_klines` 日志 |
| v2_executor 对账 | `tail -f runtime/v2_executor.log` | 每 60 秒轮询 |
| PG 表数据有更新 | `docker exec -it crypto-postgres psql -U postgres -d crypto -c "SELECT max(snapshot_time) FROM biz.asset_quote_snapshot;"` | 时间戳在 0-10 分钟内 |
| 邮件能发出去 | 手动触发一次 `send_daily_brief.py` | SMTP 日志无报错 |
| DNS 指向正确 | `dig crypto-profile-collection.zeabur.app`（或你的新域名）| Answer 段有 `43.165.185.221` |

---

## 🔧 常用维护命令速查

```bash
# 进入服务器
ssh root@43.165.185.221
cd /opt/crypto-research

# 容器状态
docker compose ps

# 看某个容器日志（实时跟踪）
docker compose logs -f --tail 100 app
docker compose logs -f --tail 100 postgres
docker compose logs -f --tail 100 caddy

# 重启
docker compose restart app
docker compose restart postgres
docker compose restart       # 全部重启

# 停/起
docker compose down
docker compose up -d

# 更新代码（代码目录是 volume 挂载，git pull 后重启就行）
git pull
docker compose restart app

# 重建镜像（Dockerfile 改了才需要）
docker compose build app
docker compose up -d app

# 进 PG 容器调试
docker exec -it crypto-postgres psql -U postgres -d crypto

# 备份导出
docker exec -t crypto-postgres pg_dump -U postgres -d crypto -Fc -f /tmp/backup.dump
docker cp crypto-postgres:/tmp/backup.dump ~/backup_$(date +%Y%m%d).dump

# PostgreSQL 连接串（在服务器内）
docker exec -it crypto-postgres psql -U postgres -d crypto

# 远程也能连（不推荐生产用，防火墙没开 5432）
# 需要腾讯云控制台防火墙先开 5432，然后：
# psql -h 43.165.185.221 -p 5432 -U postgres -d crypto
```

---

## ⚠️ 已知边界 & 注意事项

| 项 | 说明 |
|----|------|
| **Zeabur 还没删** | 保持跑着，等新服务器稳定运行一周再停 Zeabur |
| **两个 DB 双写期？** | 不用。Zeabur 旧容器还在写旧 DB，新容器写新 DB。等切换后只新容器跑 |
| **流量切换瞬间** | DNS 改完后，旧域名（Zeabur）和新域名会并存一段时间。Caddyfile 里两个域名都反代到 :9999 就行 |
| **实盘交易** | `SIGNAL_TRADE_ENABLED` 先设 `0`，验证稳定后再改 `1` |
| **Hermes/OpenClaw** | 本次迁移只装研究系统。Agent 稍后再评估：单独装在同一台服务器（再开一个 volume 挂载）或者后续再加 |
| **PG 安全** | 腾讯云防火墙**不要**开 5432 端口。PG 只在 docker-compose 内部通信，外部永远不暴露 |
| **Docker 数据卷** | `pgdata` / `caddy-data` / `caddy-config` 都是 Docker managed volume，路径在 `/var/lib/docker/volumes/`，备份用 `docker cp` 或 `pg_dump` |
| **重启服务器** | `docker compose` 的容器默认 `restart: unless-stopped`，服务器重启后会自动拉起 |

---

## 🗓️ 时间线建议

```
10/20 周一：Zeabur 导出 PG 备份 + 抄环境变量 + 轮换 API Key
10/21 周二：仓库 clone 到服务器 + 填 .env + 跑 deploy.sh
10/22 周三：pg_restore 导入 + 验证数据正常 + 调 DNS
10/23 周四：观察 60+ 调度任务跑一天，日志正常
10/24 周五：SIGNAL_TRADE_ENABLED=1，正式接管实盘
10/25 周六：观察两天稳定性
11/xx 周日：决定要不要停 Zeabur（12月才停，不急）
12/31 最终：Zeabur 停服，确认数据安全后删除
```
