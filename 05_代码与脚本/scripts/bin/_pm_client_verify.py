#!/usr/bin/env python3
"""PM 客户端直接验证（绕过 V2_TRADE_ENABLED 开关，容器内跑）。"""
from __future__ import annotations
import sys, os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

env_path = SCRIPT_DIR.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

from crypto_research.clients.binance_futures import BinanceFuturesClient, BinanceFuturesError
from crypto_research.config import get_settings

def main():
    settings = get_settings(require_database=False)
    key = os.environ["BINANCE_API_KEY"]
    secret = os.environ["BINANCE_API_SECRET"]

    print(f"🔑 Key: {key[:12]}...{key[-4:]}")
    print(f"📡 settings.binance_fapi_base_url = {settings.binance_fapi_base_url}")

    client = BinanceFuturesClient(key, secret, base_url=settings.binance_fapi_base_url)
    print(f"📡 client.base_url = {client.base_url}")
    print(f"📡 client.market_base_url = {client.market_base_url}")
    print()

    # 1. ping（papi）
    print(f"1️⃣  ping = {client.ping()}")

    # 2. 行情（fapi）
    try:
        print(f"2️⃣  BTCUSDT 现价 = {client.get_price('BTCUSDT')}")
    except Exception as e:
        print(f"2️⃣  ❌ {e}")

    # 3. get_balance（papi）—— 关键验证
    print(f"\n3️⃣  ⭐ client.get_balance('USDT') ...", end=" ", flush=True)
    try:
        bal = client.get_balance("USDT")
        print(f"✅ {bal} USDT")
    except BinanceFuturesError as e:
        print(f"❌ {e}")
        return

    # 4. set_leverage（papi）
    print(f"4️⃣  ⭐ client.set_leverage('BTCUSDT', 5) ...", end=" ", flush=True)
    try:
        r = client.set_leverage("BTCUSDT", 5)
        print(f"✅ {r}")
    except BinanceFuturesError as e:
        print(f"❌ {e}")

    # 5. get_position_risk（papi）
    print(f"5️⃣  client.get_position_risk() ...", end=" ", flush=True)
    try:
        risks = client.get_position_risk()
        opens = [p for p in risks if abs(float(p.get("positionAmt",0))) > 0]
        print(f"✅ total={len(risks)} open={len(opens)}")
    except BinanceFuturesError as e:
        print(f"❌ {e}")

    # 6. open_position（真实下单测试——极小 qty，不会有什么影响）
    print(f"\n6️⃣  ⭐ client.open_position('BTCUSDT','long',0.2,...) ...", end=" ", flush=True)
    try:
        # 0.2 USDT 名义 → MARKET BUY BTCUSDT ≈ 0.000002 BTC
        r = client.open_position("BTCUSDT", "long", 0.2, leverage=1)
        print(f"✅ orderId={r.get('orderId')} qty={r.get('_qty')} price={r.get('_price')}")
    except BinanceFuturesError as e:
        print(f"❌ {e}")

    print("\n✅ PM 客户端完整链路验证完成")

if __name__ == "__main__":
    main()
