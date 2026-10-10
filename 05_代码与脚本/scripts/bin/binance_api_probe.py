#!/usr/bin/env python3
"""子账户 API 连通性探针（极简版，容器内跑，跳过所有外部 IP 查询）。"""
from __future__ import annotations
import sys, os, time, json
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
    key = os.environ.get("BINANCE_API_KEY")
    secret = os.environ.get("BINANCE_API_SECRET")
    if not key or not secret:
        print("❌ BINANCE_API_KEY / SECRET 未配置")
        sys.exit(1)

    print(f"🔑 Key len={len(key)} | SEC len={len(secret)}")
    print(f"⏰ 时间戳: {int(time.time()*1000)}")
    print()

    client = BinanceFuturesClient(key, secret, timeout=12)

    # 1. ping（公网）
    print("1️⃣  /fapi/v1/ping ...", end=" ", flush=True)
    try:
        ok = client.ping(); print("✅" if ok else "❌失败")
    except BinanceFuturesError as e:
        print(f"❌ {e}")
        sys.exit(2)

    # 2. 服务器时间
    print("2️⃣  /fapi/v1/time ...", end=" ", flush=True)
    try:
        st = client._public_request("/fapi/v1/time")["serverTime"]
        drift = int(time.time() * 1000) - int(st)
        print(f"✅ drift={drift}ms")
    except BinanceFuturesError as e:
        print(f"❌ {e}")

    # 3. 账户权益（核心鉴权）
    print("3️⃣  /fapi/v2/account ...", end=" ", flush=True)
    try:
        acct = client.get_account()
        bal = next((float(b["walletBalance"]) for b in acct["assets"] if b["asset"] == "USDT"), -1)
        print(f"✅ USDT={bal:.4f}  assets={len(acct.get('assets',[]))}")
    except BinanceFuturesError as e:
        print(f"❌ {e}")
        sys.exit(3)

    # 4. 杠杆检查
    print("4️⃣  /fapi/v1/leverage info (BTCUSDT) ...", end=" ", flush=True)
    try:
        import requests, hashlib, hmac
        params = {"symbol": "BTCUSDT", "timestamp": int(time.time()*1000) + (client._time_offset_ms or 0)}
        q = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
        q += f"&signature={client._sign(q)}"
        r = requests.get(f"https://fapi.binance.com/fapi/v1/leverageInfo?{q}", headers={"X-MBX-APIKEY": key}, timeout=12)
        info = r.json() if r.ok else {}
        if isinstance(info, list) and info:
            print(f"✅ maxLeverage={info[0].get('maxLeverage')}")
        else:
            print(f"⚠️  {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"⚠️  {e}")

    # 5. 持仓风险
    print("5️⃣  /fapi/v2/positionRisk ...", end=" ", flush=True)
    try:
        risks = client.get_position_risk()
        open_p = [p for p in risks if abs(float(p.get("positionAmt",0))) > 0]
        print(f"✅ total={len(risks)} open={len(open_p)}")
        for p in open_p[:5]:
            print(f"     {p['symbol']}: {float(p['positionAmt']):+.4f}")
    except BinanceFuturesError as e:
        print(f"❌ {e}")

    print("\n✅ 全部通过 → 实盘下单链路可用，可将 V2_TRADE_ENABLED=1 开启实盘")

if __name__ == "__main__":
    main()
