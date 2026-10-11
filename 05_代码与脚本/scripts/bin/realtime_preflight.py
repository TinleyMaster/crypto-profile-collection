#!/usr/bin/env python3
"""实盘上线前预检：确认客户端走 fapi + 账户余额/持仓/挂单 + 交易开关状态。

容器内运行（本地 IP 被币安拦截签名请求）。
"""
from __future__ import annotations
import sys, os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

env_path = SCRIPT_DIR.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

from crypto_research.config import get_settings
from crypto_research.clients.binance_futures import BinanceFuturesClient

settings = get_settings()
base = settings.binance_fapi_base_url
print("=" * 64)
print("[1] 域名路由 / 开关")
print(f"    settings.binance_fapi_base_url = {base}")
print(f"    ENV BINANCE_FAPI_BASE_URL      = {os.environ.get('BINANCE_FAPI_BASE_URL', '(未设置)')}")
print(f"    signal_trade_enabled           = {getattr(settings, 'signal_trade_enabled', None)}")
print(f"    ENV SIGNAL_TRADE_ENABLED       = {os.environ.get('SIGNAL_TRADE_ENABLED', '(未设置)')}")
print(f"    kol_auto_trade_enabled         = {getattr(settings, 'kol_auto_trade_enabled', None)}")
print(f"    ENV KOL_AUTO_TRADE_ENABLED     = {os.environ.get('KOL_AUTO_TRADE_ENABLED', '(未设置)')}")
_routed = base.rstrip("/").endswith("fapi.binance.com")
print(f"    → {'✅ 走 fapi（正确）' if _routed else '❌ 不是 fapi！环境变量还在覆盖'}")
print("=" * 64)

client = BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, base_url=base)

print(f"\n[2] ping = {client.ping()}")
try:
    print(f"    BTC 现价 = {client.get_price('BTCUSDT')}")
except Exception as e:
    print(f"    ❌ get_price: {e}")

print("\n[3] 账户余额")
try:
    acct = client.get_account()
    print(f"    totalWalletBalance = {acct.get('totalWalletBalance')}")
    print(f"    availableBalance   = {acct.get('availableBalance')}")
    for a in acct.get("assets", []):
        if float(a.get("walletBalance", 0)) > 0:
            print(f"    {a['asset']}: wallet={a.get('walletBalance')} "
                  f"avail={a.get('availableBalance')} margin={a.get('marginBalance')}")
except Exception as e:
    print(f"    ❌ get_account: {e}")

print("\n[4] 持仓")
try:
    pos = [p for p in client.get_position_risk() if float(p.get("positionAmt", 0)) != 0]
    if not pos:
        print("    (无持仓)")
    for p in pos:
        print(f"    {p['symbol']} amt={p['positionAmt']} entry={p.get('entryPrice')} "
              f"uPnl={p.get('unRealizedProfit')} lev={p.get('leverage')}")
except Exception as e:
    print(f"    ❌ get_position_risk: {e}")

print("\n[5] 挂单")
try:
    oo = client.get_open_orders()
    print(f"    挂单数 = {len(oo)}")
    for o in oo[:10]:
        print(f"    {o.get('symbol')} {o.get('side')} {o.get('type')} "
              f"qty={o.get('origQty')} price={o.get('price')} stop={o.get('stopPrice')}")
except Exception as e:
    print(f"    ❌ get_open_orders: {e}")

print("\n[6] 持仓模式")
try:
    print(f"    position_mode = {client.get_position_mode()}")
except Exception as e:
    print(f"    ❌ get_position_mode: {e}")

print("\n" + "=" * 64)
print("预检完成。")