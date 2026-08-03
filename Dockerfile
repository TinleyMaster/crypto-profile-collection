FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（psycopg 需要 gcc/libpq-dev，slim 镜像没有）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libpq-dev \
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
COPY 05_代码与脚本/workbench/templates /app/templates

# 文档存储目录
RUN mkdir -p /app/docs_storage

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

EXPOSE 9999

# 用 gunicorn 启动，监听 $PORT（Zeabur 自动注入，默认 9999）
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-9999} --workers 2 --timeout 120 --access-logfile - --error-logfile -"]
