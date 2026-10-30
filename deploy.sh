#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 加密货币投研系统 - 一键部署脚本
# 服务器：腾讯云轻量 2核8G 东京 43.165.185.221
# 用法：
#   1. 在本地 Windows：scp deploy.sh root@43.165.185.221:~/
#   2. SSH 登录：ssh root@43.165.185.221
#   3. 拉代码：git clone <你的仓库地址> /opt/crypto-research
#   4. 进入目录：cd /opt/crypto-research
#   5. 复制环境变量：cp .env.example .env && vi .env   # 填入真实值
#   6. 执行部署：bash deploy.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo "═══════════════════════════════════════════════════"
echo "  加密货币投研系统 - 部署脚本"
echo "  服务器: 腾讯云轻量 东京 2核8G"
echo "  IP:     43.165.185.221"
echo "  时间:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════"

# ── 0. 检查 .env ────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        warn ".env 不存在，已从 .env.example 复制"
        warn "⚠️  请编辑 .env 填入真实值后再运行：vi .env"
        warn "特别注意 POSTGRES_PASSWORD 和所有 API Key"
        exit 1
    else
        error ".env 和 .env.example 都不存在"
    fi
fi
info "✓ .env 文件存在"

# ── 1. 安装 Docker + Docker Compose（如果没有）───────────────
if ! command -v docker &>/dev/null; then
    info "安装 Docker ..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
fi
if ! command -v docker &>/dev/null; then
    error "Docker 安装失败"
fi
DOCKER_VERSION=$(docker --version | awk '{print $3}')
info "✓ Docker $DOCKER_VERSION"

if ! docker compose version &>/dev/null; then
    info "安装 Docker Compose 插件 ..."
    apt-get update && apt-get install -y docker-compose-plugin
fi
COMPOSE_VERSION=$(docker compose version | awk '{print $4}')
info "✓ Docker Compose $COMPOSE_VERSION"

# ── 2. 创建运行时目录 ────────────────────────────────────────
info "创建运行时目录 ..."
mkdir -p runtime/{task_state,docs_storage,logs}
touch runtime/{scan_daemon.log,scan_daemon.err.log,v2_executor.log,v2_executor.err.log,catalyst_fast_daemon.log,catalyst_fast_daemon.err.log}
info "✓ runtime/ 目录就绪"

# ── 3. Caddyfile 提示 ────────────────────────────────────────
if grep -q "YOUR_DOMAIN" Caddyfile 2>/dev/null; then
    warn "Caddyfile 里还有 YOUR_DOMAIN 占位符，需要改成真实域名"
    warn "如果还没配域名，可以先把 Caddyfile 里的域名块注释掉，只留 :80 块"
    warn "或者临时直接访问 http://43.165.185.221"
fi

# ── 4. 拉 PG 镜像 + 构建 app 镜像 ───────────────────────────
info "拉取 postgres:16-alpine 和 caddy:2-alpine ..."
docker pull postgres:16-alpine
docker pull caddy:2-alpine

info "构建 app 镜像（首次构建需要几分钟，后续用缓存）..."
docker compose build app

# ── 5. 启动容器 ──────────────────────────────────────────────
info "启动所有容器 ..."
docker compose up -d

info "等待 PG 就绪 ..."
for i in {1..30}; do
    if docker compose exec -T postgres pg_isready -U postgres &>/dev/null; then
        info "✓ PostgreSQL 就绪"
        break
    fi
    sleep 2
done

# ── 6. 绑定挂载完整性校验（关键！常规健康检查漏过）────────
echo ""
info "═══════════════════════════════════════════════════"
info "  绑定挂载完整性校验"
info "═══════════════════════════════════════════════════"
APP_CID=$(docker compose ps -q app)
if [ -z "$APP_CID" ]; then
    error "找不到 app 容器，检查 docker compose ps"
fi

# 检查 /app/logs 挂载（supervisord 日志轮转的根基）
LOG_MOUNT_COUNT=$(docker exec "$APP_CID" cat /proc/1/mountinfo 2>/dev/null | grep -c " /app/logs " || echo 0)
if [ "$LOG_MOUNT_COUNT" -gt 0 ]; then
    info "✓ /app/logs 挂载存在（mountinfo 命中 $LOG_MOUNT_COUNT 条）"
    # 实测写入（验证不是"僵尸挂载"）
    if docker exec "$APP_CID" sh -c 'echo probe > /app/logs/.mount_probe && rm /app/logs/.mount_probe' 2>/dev/null; then
        info "✓ /app/logs 写入测试 OK"
    else
        error "/app/logs 挂载存在但写入失败 — supervisord 轮转仍会卡死，请 force-recreate"
    fi
else
    error "✗ /app/logs 挂载丢失！常规健康检查发现不了此问题，必须 force-recreate："
    error "  docker compose up -d --force-recreate app"
fi

# 其他关键挂载
for mp in "/app/scripts" "/app/task_state" "/app/docs_storage"; do
    CNT=$(docker exec "$APP_CID" cat /proc/1/mountinfo 2>/dev/null | grep -c " $mp " || echo 0)
    if [ "$CNT" -gt 0 ]; then
        info "✓ $mp 挂载存在"
    else
        warn "⚠  $mp 挂载缺失（可能已在镜像内 COPY 过，临时可用但重启可能失效）"
    fi
done

# ── 7. 状态检查 ──────────────────────────────────────────────
echo ""
info "═══════════════════════════════════════════════════"
info "  部署完成！容器状态："
info "═══════════════════════════════════════════════════"
docker compose ps

echo ""
info "═══════════════════════════════════════════════════"
info "  下一步："
info "═══════════════════════════════════════════════════"
info "  1. 导出 Zeabur PG 数据："
info "     Zeabur 控制台 → PostgreSQL → 备份与恢复 → 立即创建备份"
info "     或者：pg_dump -h <zeabur_host> -p <port> -U <user> -d <db> -Fc -f zeabur.dump"
info ""
info "  2. 恢复数据到新服务器："
info "     # 把 zeabur.dump 传到服务器："
info "     scp zeabur.dump root@43.165.185.221:~/"
info "     # 恢复（在服务器上执行）："
info "     docker cp ~/zeabur.dump crypto-postgres:/tmp/"
info "     docker exec -it crypto-postgres pg_restore -U postgres -d crypto --no-owner --no-privileges /tmp/zeabur.dump"
info ""
info "  3. 日志查看："
info "     docker compose logs -f app        # 所有进程"
info "     tail -f runtime/logs/scan_daemon.log    # scan_daemon"
info ""
info "  4. 常用维护命令："
info "     docker compose down               # 停所有容器"
info "     docker compose up -d              # 启动"
info "     docker compose restart app        # 重启 app"
info "     docker compose build app && docker compose up -d app  # 重建镜像"
info ""
info "  5. 更新代码："
info "     git pull && docker compose restart app  # 代码目录是 volume 挂载，重启即生效"
echo ""
