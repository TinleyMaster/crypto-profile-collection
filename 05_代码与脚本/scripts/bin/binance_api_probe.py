#!/usr/bin/env python3
"""子账户 API 连通性探针 - Portfolio Margin 专属（papi.binance.com）。

⚠️  你的账户是币安「统一账户/Portfolio Margin」，必须用 papi.binance.com，
   不能再用 fapi.binance.com（PM 账户在 fapi 上永远返回 -2015）。

用法（Zeabur 容器内）：
    python bin/binance_api_probe.py
"""
from __future__ import annotations
import sys, os, time, hashlib, hmac
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

import requests

def main():
    key = os.environ.get("BINANCE_API_KEY")
    secret = os.environ.get("BINANCE_API_SECRET")
    if not key or not secret:
        print("❌ BINANCE_API_KEY / SECRET 未配置"); sys.exit(1)

    print(f"🔑 Key len={len(key)}  ⚠️ Portfolio Margin 账户 → papi.binance.com")
    print()

    # ── 基础设施 ──
    def _sign(params: dict) -> str:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
        return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def _get(path: str, params: dict | None = None, desc: str = ""):
        base = "https://papi.binance.com"
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        sig = _sign(params)
        full_qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        url = f"{base}{path}?{full_qs}&signature={sig}"
        print(f"\n  → GET  papi{path}")
        t0 = time.time()
        try:
            r = requests.get(url, headers={"X-MBX-APIKEY": key}, timeout=12)
            ms = (time.time() - t0) * 1000
            print(f"  ← HTTP {r.status_code}  ⏱ {ms:.0f}ms")
            body = r.text.strip()
            # 截断长 body
            print(f"  ← {body[:400]}")
            return r
        except requests.RequestException as e:
            print(f"  ❌ 网络层错误: {e}")

    def _post(path: str, params: dict, desc: str = ""):
        base = "https://papi.binance.com"
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        sig = _sign(params)
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        full_body = f"{qs}&signature={sig}"
        print(f"\n  → POST papi{path}  body={qs[:120]}...")
        t0 = time.time()
        try:
            r = requests.post(
                f"{base}{path}", data=full_body,
                headers={
                    "X-MBX-APIKEY": key,
                    "Content-Type": "application/x-www-form-urlencoded",
                }, timeout=12,
            )
            ms = (time.time() - t0) * 1000
            print(f"  ← HTTP {r.status_code}  ⏱ {ms:.0f}ms")
            print(f"  ← {r.text.strip()[:400]}")
            return r
        except requests.RequestException as e:
            print(f"  ❌ 网络层错误: {e}")

    # ── 公网端点 ──
    print("1️⃣  papi /papi/v1/ping（公网） ...", end=" ", flush=True)
    try:
        r = requests.get("https://papi.binance.com/papi/v1/ping", timeout=12)
        print(f"HTTP {r.status_code}  body={r.text.strip()}")
    except Exception as e:
        print(f"❌ {e}"); sys.exit(2)

    print("2️⃣  papi /papi/v1/time ...", end=" ", flush=True)
    try:
        st = requests.get("https://papi.binance.com/papi/v1/time", timeout=12).json()["serverTime"]
        print(f"✅ serverTime={st} local={int(time.time()*1000)} drift={int(time.time()*1000)-int(st)}ms")
    except Exception as e:
        print(f"❌ {e}")

    # ── 关键鉴权端点（PM 专属） ──
    print(f"\n{'─'*60}\n3️⃣  ⭐ /papi/v1/balance（PM 核心：钱包余额）")
    r = _get("/papi/v1/balance")
    if r and r.status_code == 200:
        data = r.json()
        for item in data[:3]:
            print(f"     {item}")
    elif r and "-2015" in r.text:
        # 提取 request ip（papi 才会显式返回被拒 IP）
        import re
        m = re.search(r"request ip: ([\d\.]+)", r.text)
        req_ip = m.group(1) if m else "?"
        print(f"\n⚠️  -2015 被拒，币安报告请求 IP={req_ip}")
        print("   如果这个 IP 不是你白名单里的，去币安后台加一下。")

    print(f"\n{'─'*60}\n4️⃣  ⭐ /papi/v1/account（PM 账户概览）")
    r = _get("/papi/v1/account")
    if r and r.status_code == 200:
        data = r.json()
        print(f"     totalWalletBalance={data.get('totalWalletBalance')}")
        print(f"     umWalletBalance={data.get('umWalletBalance')}  cmWalletBalance={data.get('cmWalletBalance')}")

    print(f"\n{'─'*60}\n5️⃣  /papi/v1/um/positionRisk（U本位持仓）")
    r = _get("/papi/v1/um/positionRisk", {"symbol": "BTCUSDT"})

    print(f"\n{'─'*60}\n6️⃣  /papi/v1/um/leverage  SET（POST）")
    r = _post("/papi/v1/um/leverage", {"symbol": "BTCUSDT", "leverage": 5})

    # ── 最小下单测试（不会成交，POST_ONLY + 天价） ──
    print(f"\n{'─'*60}\n7️⃣  ⚠️ 最小下单测试（POST_ONLY LIMIT @ $1 天价，不会成交）")
    r = _post("/papi/v1/um/order", {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "POST_ONLY",
        "quantity": "0.001",
        "price": "1",
        "positionSide": "LONG",
    })
    if r and r.status_code == 200 and r.json().get("orderId"):
        print("     ✅ 订单被接受但不会成交（价格无效）。说明下单链路通！")

    print(f"\n{'─'*60}")
    print("✅ 探针完成。上面 3/4/7 全部 HTTP 200 = papi 账户可用。")
    print("   如果任一返回 -2015 且 request ip 不在白名单 → 去币安后台加 IP。")

if __name__ == "__main__":
    main()
