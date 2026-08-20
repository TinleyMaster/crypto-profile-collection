FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（psycopg 需要 gcc/libpq-dev，supervisor 用于多进程管理）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libpq-dev supervisor \
    && rm -rf /var/lib/apt/lists/*

# 复制 workbench 依赖并安装
COPY 05_代码与脚本/workbench/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制 scripts 源码（数据处理模块）
COPY 05_代码与脚本/scripts/src /app/scripts/src
COPY 05_代码与脚本/scripts/sql /app/scripts/sql
COPY 05_代码与脚本/scripts/bin /app/scripts/bin

# 复制 workbench 应用
COPY 05_代码与脚本/workbench/*.py /app/
COPY 05_代码与脚本/workbench/kol /app/kol
COPY 05_代码与脚本/workbench/templates /app/templates
COPY 05_代码与脚本/workbench/supervisord.conf /app/supervisord.conf

# 文档存储目录
RUN mkdir -p /app/docs_storage

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

EXPOSE 9999

# supervisord 同时拉起 gunicorn(web) + scheduler(定时任务) + kol_daemon(KOL轮询)
# PORT 环境变量由 Zeabur 自动注入（默认 9999），supervisord 通过 %(ENV_PORT)s 读取
CMD ["supervisord", "-c", "/app/supervisord.conf"]
