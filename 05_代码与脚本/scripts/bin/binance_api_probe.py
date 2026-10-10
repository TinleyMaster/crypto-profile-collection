#!/usr/bin/env python3
"""子账户 API 连通性探针（容器内跑，验证 Key + 网络 + 权限）。

本地跑 fapi.binance.com 会被 GFW 封（HTTP 403），所以本脚本的价值在 Zeabur 容器里。
"""
from __future__ import annotations
import sys, os, requests, time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

# 加载 .env（容器里不用，但本地 dry-run 需要）
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
    s = get_settings(require_database=False)
    key = os.environ.get("BINANCE_API_KEY") or getattr(s, "binance_api_key", None)
    secret = os.environ.get("BINANCE_API_SECRET") or getattr(s, "binance_api_secret", None)
    if not key or not secret:
        print("❌ BINANCE_API_KEY / SECRET 未配置"); sys.exit(1)

    # 不打出来密钥
    print(f"🔑 Key 已加载 len={len(key)}")
    print(f"🌐 出口 IP: {requests.get('https://api.ipify.org', timeout=8).text}")
    print()

    client = BinanceFuturesClient(key, secret)

    # 1. ping（公网端点，无签名）
    print("1️⃣  fapi /fapi/v1/ping ...", end=" ", flush=True)
    try:
        ok = client.ping(); print("✅" if ok else "❌")
    except BinanceFuturesError as e:
        print(f"❌ {e}")
        # 这里失败就不用往下试了——网络不通
        print("\n⚠️ 合约域名无法到达。若本地跑可能是 GFW 封锁；容器里则检查 DNS/网络策略。")
        sys.exit(2)

    # 2. 取服务器时间（验证时间同步）
    print("2️⃣  服务器时间 ...", end=" ", flush=True)
    try:
        st = client._public_request("/fapi/v1/time")["serverTime"]
        drift = int(time.time() * 1000) - int(st)
        print(f"✅ drift={drift}ms")
    except BinanceFuturesError as e:
        print(f"❌ {e}")

    # 3. 鉴权端点：账户权益（核心——验证 Key + 权限）
    print("3️⃣  /fapi/v2/account (鉴权 + 合约权限) ...", end=" ", flush=True)
    try:
        acct = client.get_account()
        bal_usdt = next((float(b["walletBalance"]) for b in acct["assets"] if b["asset"] == "USDT"), -1)
        print(f"✅ 账户类型 cross? assets_count={len(acct.get('assets',[]))} USDT={bal_usdt:.2f}")
    except BinanceFuturesError as e:
        print(f"❌ {e}")
        print("   ↳ 常见根因：①子账户未开合约权限 ②IP 白名单未加 ③Key 已禁用/删除")
        sys.exit(3)

    # 4. 子账户/主账户类型检测（subAccount 字段 / 主账户 futuresEnabled）
    print("4️⃣  账户类型检测 ...", end=" ", flush=True)
    try:
        # 主账户端点（如果是子账户会被拒）
        r = requests.get(
            "https://api.binance.com/sapi/v1/account/sub-account/list",
            headers={"X-MBX-APIKEY": key},
            params={"timestamp": int(time.time()*1000)},
            timeout=10,
        )
        if r.ok:
            print("✅ 这是主账户 Key（有子账户管理权限）")
        elif "sub-account" in r.text.lower() or "subaccount" in r.text.lower():
            print("✅ 这是子账户 Key（主账户端点被拒是正常的）")
        else:
            print(f"⚠️  主账户端点返回 {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"⚠️  跳过：{e}")

    # 5. 持仓风险（看当前有没有仓位）
    print("5️⃣  /fapi/v2/positionRisk ...", end=" ", flush=True)
    try:
        risks = client.get_position_risk()
        open_positions = [p for p in risks if abs(float(p.get("positionAmt", 0))) > 0]
        print(f"✅ 总仓位 {len(risks)}，开仓 {len(open_positions)}")
        for p in open_positions[:5]:
            amt = float(p["positionAmt"])
            print(f"      {p['symbol']}: {amt:+.4f} entry={p.get('entryPrice','?')}")
    except BinanceFuturesError as e:
        print(f"❌ {e}")

    print("\n✅ 探针完成。以上输出全部通过 → 实盘 API 可用，可把 V2_TRADE_ENABLED=1 开启实盘。")

if __name__ == "__main__":
    main()
