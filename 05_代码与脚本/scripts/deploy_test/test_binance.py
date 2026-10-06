#!/usr/bin/env python3
"""测试币安子账户 API Key 连通性（只读查询，不下单）。

检查项：
1. API Key/Secret 是否有效
2. 能否获取账户余额
3. 能否获取持仓信息
4. 能否获取合约行情

环境变量：
- BINANCE_API_KEY: 子账户 API Key
- BINANCE_API_SECRET: 子账户 API Secret
- DATABASE_URL: PostgreSQL 连接串（可选，用于验证 DB 连通）

用法：python test_binance.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time

import requests

FAPI_BASE = "https://fapi.binance.com"


def sign_request(params: dict, secret: str) -> str:
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


def get_server_time() -> int:
    r = requests.get(f"{FAPI_BASE}/fapi/v1/time", timeout=10)
    return r.json()["serverTime"]


def test_ping() -> bool:
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v1/ping", timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def test_price(symbol: str = "BTCUSDT") -> float | None:
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
        return float(r.json()["price"])
    except Exception as e:
        print(f"  FAIL: {e}")
        return None


def test_account(api_key: str, api_secret: str) -> dict | None:
    params = {
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000,
    }
    params["signature"] = sign_request(params, api_secret)
    headers = {"X-MBX-APIKEY": api_key}
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v2/account", params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"  FAIL: HTTP {r.status_code} - {r.text[:200]}")
            return None
    except Exception as e:
        print(f"  FAIL: {e}")
        return None


def test_position_risk(api_key: str, api_secret: str) -> list | None:
    params = {
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000,
    }
    params["signature"] = sign_request(params, api_secret)
    headers = {"X-MBX-APIKEY": api_key}
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v2/positionRisk", params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"  FAIL: HTTP {r.status_code} - {r.text[:200]}")
            return None
    except Exception as e:
        print(f"  FAIL: {e}")
        return None


def test_db_connection(db_url: str) -> bool:
    try:
        import psycopg
        with psycopg.connect(db_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def main() -> int:
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    db_url = os.getenv("DATABASE_URL", "").strip()

    print("=" * 60)
    print("币安子账户 API Key 连通性测试")
    print("=" * 60)
    print()

    if not api_key or not api_secret:
        print("[FAIL] BINANCE_API_KEY 或 BINANCE_API_SECRET 未配置")
        print("请设置环境变量后重试")
        return 1

    print(f"[INFO] API Key: {api_key[:8]}...{api_key[-4:]}")
    print(f"[INFO] Base URL: {FAPI_BASE}")
    print()

    # 测试 1: Ping
    print("[TEST 1] Ping 服务器...")
    if test_ping():
        print("  PASS: 服务器连接正常")
    else:
        print("  FAIL: 服务器连接失败")
        return 1

    # 测试 2: 获取最新价
    print("[TEST 2] 获取 BTCUSDT 最新价...")
    price = test_price("BTCUSDT")
    if price:
        print(f"  PASS: BTCUSDT = ${price:,.2f}")
    else:
        print("  FAIL: 获取价格失败")

    # 测试 3: 获取账户余额
    print("[TEST 3] 获取账户余额...")
    account = test_account(api_key, api_secret)
    if account:
        usdt_balance = 0
        for asset in account.get("assets", []):
            if asset["asset"] == "USDT":
                usdt_balance = float(asset.get("walletBalance", 0))
                break
        print(f"  PASS: USDT 余额 = {usdt_balance:,.2f}")
        print(f"  PASS: 账户类型 = {account.get('accountType', 'N/A')}")
        print(f"  PASS: 可用余额 = {float(account.get('availableBalance', 0)):,.2f}")
    else:
        print("  FAIL: 获取账户信息失败")
        print("  >>> 可能原因：API Key 无效 / IP 未白名单 / 权限不足")
        return 1

    # 测试 4: 获取持仓
    print("[TEST 4] 获取当前持仓...")
    positions = test_position_risk(api_key, api_secret)
    if positions:
        active = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
        print(f"  PASS: 总合约 {len(positions)}，活跃持仓 {len(active)}")
        if active:
            for p in active[:5]:
                print(f"    {p['symbol']}: {p['positionAmt']} (杠杆 {p.get('leverage', 'N/A')}x)")
    else:
        print("  FAIL: 获取持仓信息失败")

    # 测试 5: DB 连接（可选）
    if db_url:
        print("[TEST 5] 测试数据库连接...")
        if test_db_connection(db_url):
            print("  PASS: 数据库连接正常")
        else:
            print("  FAIL: 数据库连接失败")
    else:
        print("[TEST 5] 跳过数据库测试（DATABASE_URL 未配置）")

    print()
    print("=" * 60)
    print("[DONE] 所有测试完成")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())