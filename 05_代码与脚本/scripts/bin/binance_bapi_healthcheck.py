#!/usr/bin/env python3
"""Binance bapi 健康探测（scheduler 入口，每 30 分钟）。

逻辑在 workbench/binance_bapi_healthcheck.py，本脚本仅注入 workbench 到 sys.path 并调用 run_healthcheck()。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_code_root = _here.parent.parent  # /app (prod) 或 05_代码与脚本 (local)
for cand in (_code_root / "workbench", Path("/app"), _code_root):
    if cand.exists() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from binance_bapi_healthcheck import run_healthcheck  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(run_healthcheck(), ensure_ascii=False, indent=2))