FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（psycopg 需要 gcc/libpq-dev，Playwright 需要浏览器运行时库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libpq-dev \
    libnss3 libnspr4 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 复制 workbench 依赖并安装
COPY 05_代码与脚本/workbench/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 安装 Playwright Chromium 浏览器
RUN playwright install chromium

# 复制 scripts 源码（数据处理模块）
COPY 05_代码与脚本/scripts/src /app/scripts/src
COPY 05_代码与脚本/scripts/sql /app/scripts/sql
COPY 05_代码与脚本/scripts/bin /app/scripts/bin

# 复制 workbench 应用
COPY 05_代码与脚本/workbench/*.py /app/
COPY 05_代码与脚本/workbench/templates /app/templates

# 文档存储目录
RUN mkdir -p /app/docs_storage

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

EXPOSE 9999

# 同一镜像支持两个服务角色，由环境变量 SERVICE_ROLE 决定启动命令：
#   web       （默认）gunicorn 监听 $PORT（Zeabur 自动注入，默认 9999）
#   scheduler         独立调度进程（替代 n8n），按 cron 触发 scripts/bin 脚本
CMD ["sh", "-c", "if [ \"$SERVICE_ROLE\" = \"scheduler\" ]; then exec python scheduler.py; else exec gunicorn app:app --bind 0.0.0.0:${PORT:-9999} --workers 2 --timeout 120 --access-logfile - --error-logfile -; fi"]
