FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制 requirements 并安装依赖
COPY 05_代码与脚本/workbench/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制 scripts 源码（数据处理模块 + SQL + 入口脚本）
COPY 05_代码与脚本/scripts/src /app/scripts/src
COPY 05_代码与脚本/scripts/sql /app/scripts/sql
COPY 05_代码与脚本/scripts/bin /app/scripts/bin

# 复制 workbench 应用
COPY 05_代码与脚本/workbench /app/workbench

WORKDIR /app/workbench

# Zeabur 会自动注入 PORT 环境变量
EXPOSE 5000

CMD ["python", "app.py"]
