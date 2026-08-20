FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（psycopg 需要 gcc，Playwright 需要浏览器运行时库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc supervisor \
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
COPY 05_代码与脚本/workbench/*.conf /app/
COPY 05_代码与脚本/workbench/kol /app/kol
COPY 05_代码与脚本/workbench/templates /app/templates

# 文档存储目录
RUN mkdir -p /app/docs_storage
# tokenomics 图片存储目录（需挂载持久化卷）
RUN mkdir -p /app/data/tokenomics_images

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

EXPOSE 5000

# 用 supervisord 同时启动 gunicorn（Web）和 scheduler（定时调度）
# 两者共享 /app/task_state 目录，调度任务提交到 TaskManager 队列统一执行
CMD ["supervisord", "-c", "/app/supervisord.conf"]
