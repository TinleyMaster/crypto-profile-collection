#!/usr/bin/env python3
"""诊断 catalyst 模块卡住问题"""
import sys
import os
from pathlib import Path
import time

def test(name, fn):
    print(f"Testing {name}...", flush=True)
    try:
        result = fn()
        print(f"  OK: {result}", flush=True)
        return result
    except Exception as e:
        print(f"  FAIL: {e}", flush=True)
        return None

SCRIPT_DIR = Path("/app/scripts/bin")
WORKBENCH_DIR = Path("/app/workbench")
if not WORKBENCH_DIR.exists():
    WORKBENCH_DIR = Path("/app")

sys.path.insert(0, str(WORKBENCH_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

print("=" * 60, flush=True)
print("Catalyst 诊断", flush=True)
print("=" * 60, flush=True)

# 1. 测试数据库连接
def test_db():
    import psycopg
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "DATABASE_URL not set"
    conn = psycopg.connect(url, connect_timeout=10)
    conn.close()
    return f"Connected (url={url[:30]}...)"

test("DATABASE", test_db)

# 2. 测试 catalyst.db 导入
def test_import_db():
    from catalyst.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT 1 as test").fetchone()
        return row

test("catalyst.db", test_import_db)

# 3. 测试 catalyst.sources 导入
def test_import_sources():
    from catalyst.sources import SOURCE_REGISTRY
    return f"Registered: {list(SOURCE_REGISTRY.keys())}"

test("catalyst.sources", test_import_sources)

# 4. 测试单个源
def test_binance_cms():
    from catalyst.sources.binance_cms import BinanceNewsSource
    with BinanceNewsSource() as src:
        items = src.fetch(since_ts=None)
        return f"Fetched {len(items)} items"

test("binance_cms", test_binance_cms)

# 5. 测试 square 源
def test_square():
    from catalyst.sources.binance_square_news import BinanceSquareNewsSource
    with BinanceSquareNewsSource() as src:
        items = src.fetch(since_ts=None)
        return f"Fetched {len(items)} items"

test("binance_square_news", test_square)

print("=" * 60, flush=True)
print("诊断完成", flush=True)
