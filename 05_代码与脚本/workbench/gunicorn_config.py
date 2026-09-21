#!/usr/bin/env python
"""
启动入口：读取 PORT 环境变量，用 gunicorn 或 flask 启动。
兼容 Zeabur 自动注入 PORT 的机制。
"""

import os

port = int(os.environ.get("PORT", "5000"))
bind = f"0.0.0.0:{port}"
# P1-1（2026-09-21 审计）：默认 2 worker 起步。
# 单 worker 时一次搜索（并发 4 请求）能把全站 API 拖住、末次响应排队到 7.65s。
# Zeabur 可用 GUNICORN_WORKERS 环境变量覆盖。
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
timeout = 120
accesslog = "-"
errorlog = "-"
loglevel = "info"

# 预加载 app（减少内存占用，也能提前暴露 import 错误）
preload_app = True

wsgi_app = "app:app"
