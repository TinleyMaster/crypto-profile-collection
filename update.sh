#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 加密货币投研系统 - 日常代码更新脚本
# 用法：在服务器 /opt/crypto-research 下执行 bash update.sh
# 
# 核心流程：
#   git pull → force-recreate 容器 → 挂载校验 → 健康检查
#
# 为什么必须 force-recreate 而不是 restart？
#   Docker restart 不重建挂载，丢失的 bind-mount 不会恢复。
#   实测现象：容器运行数天后 /proc/1/mountinfo 中 /app/logs 消失，
#   supervisord 日志轮转需要 rename + 新建文件 → 永久卡死。
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo "═══════════════════════════════════════════════════"
echo "  加密货币投研系统 - 日常更新"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════"

# ── 1. 拉代码 ─────────────────────────────────────────────────
info "[1/4] git pull ..."
git pull --ff-only
LATEST_COMMIT=$(git rev-parse --short HEAD)
info "✓ 最新提交: $LATEST_COMMIT $(git log -1 --pretty=%s)"

# ── 2. force-recreate（关键：restart 不重建挂载）──────────────
info "[2/4] force-recreate app 容器（恢复可能丢失的 bind-mount）..."
docker compose up -d --force-recreate app
info "✓ 容器已重建"

# ── 3. 绑定挂载校验 ──────────────────────────────────────────
info "[3/4] 绑定挂载完整性校验 ..."
APP_CID=$(docker compose ps -q app)
if [ -z "$APP_CID" ]; then
    error "找不到 app 容器"
fi

ALL_MOUNTS_OK=true
for mp in "/app/logs" "/app/scripts" "/app/task_state" "/app/docs_storage"; do
    CNT=$(docker exec "$APP_CID" cat /proc/1/mountinfo 2>/dev/null | grep -c " $mp " || true)
    if [ "$CNT" -gt 0 ]; then
        info "✓ $mp 挂载存在"
    else
        warn "✗ $mp 挂载缺失"
        ALL_MOUNTS_OK=false
    fi
done

# /app/logs 写入测试（supervisord 轮转的根基）
if docker exec "$APP_CID" sh -c 'echo probe > /app/logs/.mount_probe && rm /app/logs/.mount_probe' 2>/dev/null; then
    info "✓ /app/logs 写入测试 OK"
else
    error "/app/logs 写入失败 — supervisord 日志轮转将卡死"
fi

if [ "$ALL_MOUNTS_OK" = false ]; then
    warn "部分挂载缺失，重建 app 容器 ..."
    # ⚠️ 不能用 docker compose down：它会连 caddy/postgres 一起停，而
    #    `up -d app` 不会把 caddy 拉回来 → 公网 80/443 直接挂掉。
    docker rm -f "$APP_CID" 2>/dev/null || true
    docker compose up -d --force-recreate app
    APP_CID=$(docker compose ps -q app)
    sleep 10
fi

# ── 4. 健康检查 ──────────────────────────────────────────────
info "[4/4] 健康检查 ..."

# 等 supervisord 拉起（scan_daemon/catalyst/v2_executor 的 startsecs=10，5s 会误报）
sleep 15

# 检查 8 个进程
RUNNING=$(docker exec "$APP_CID" supervisorctl status 2>/dev/null | grep -c "RUNNING" || true)
if [ "$RUNNING" -ge 7 ]; then
    info "✓ supervisord $RUNNING/8 进程 RUNNING"
else
    warn "⚠ supervisord 只有 $RUNNING 个进程 RUNNING，检查日志"
fi

# HTTP 检查
HTTP_CODE=$(docker exec "$APP_CID" curl -s -o /dev/null -w '%{http_code}' -m 8 \
    http://127.0.0.1:9999/healthz 2>/dev/null || true)
if [ "$HTTP_CODE" = "200" ]; then
    info "✓ 应用 HTTP 200 (/healthz)"
else
    warn "⚠ 应用 HTTP 响应: ${HTTP_CODE:-无响应}"
fi

# traceback 检查
# 只数应用层栈帧（File "/app"）：supervisord 自身打不开日志通道时会刷自己的
# traceback，按 "Traceback" 计数会误判为部署失败。
TB_COUNT=$(docker compose logs app --since 5m 2>/dev/null | grep -c 'File "/app' || true)
if [ "$TB_COUNT" -eq 0 ]; then
    info "✓ 最近日志无 traceback"
else
    warn "⚠ 最近日志有 $TB_COUNT 条 traceback"
fi

echo ""
info "═══════════════════════════════════════════════════"
info "  ✅ 更新完成"
info "═══════════════════════════════════════════════════"
docker compose ps
