#!/usr/bin/env python3
"""容器里查 API Key 权限 + 所有账户余额（现货/资金/杠杆/U本位/币本位/PM）。

本地跑不通（币安拦截大陆出口 IP 的签名请求），必须在 Zeabur 容器里跑。
"""
from __future__ import annotations
import sys, os, time, hashlib, hmac, requests, json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

env_path = SCRIPT_DIR.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

key = os.environ["BINANCE_API_KEY"]
secret = os.environ["BINANCE_API_SECRET"]

SPOT = "https://api.binance.com"
PAPI = "https://papi.binance.com"
FAPI = "https://fapi.binance.com"
DAPI = "https://dapi.binance.com"


def signed_get(base, path, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qs}&signature={sig}"
    return requests.get(url, headers={"X-MBX-APIKEY": key}, timeout=15)


def show(label, base, path, params=None, extractor=None):
    print(f"\n[{label}] GET {path} ...", flush=True)
    try:
        r = signed_get(base, path, params)
    except Exception as e:
        print(f"    ❌ 网络错误: {e}")
        return None
    print(f"    HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"    {r.text[:300]}")
        return None
    d = r.json()
    if extractor:
        extractor(d)
    else:
        print(f"    {json.dumps(d, ensure_ascii=False)[:500]}")
    return d


print("=" * 64)
print(f"API Key 前缀: {key[:8]}...  出口 IP / 各账户余额检查")
print("=" * 64)


def _perm(d):
    for k2 in ("canTrade", "canWithdraw", "canDeposit", "enablePortfolioMarginTrading",
               "enableSpotAndMarginTrading", "enableFutures", "enableReading",
               "enableMargin", "ipRestrict", "enableInternalTransfer"):
        if k2 in d:
            print(f"    {k2}: {d[k2]}")


show("1 权限", SPOT, "/sapi/v1/account/apiRestrictions", extractor=_perm)


def _spot(d):
    nz = [b for b in d.get("balances", []) if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0]
    if not nz:
        print("    (无任何非零资产)")
    for b in nz:
        print(f"    {b['asset']}: free={b['free']} locked={b['locked']}")


show("2 现货 Spot", SPOT, "/api/v3/account", extractor=_spot)


def _funding(d):
    rows = d if isinstance(d, list) else d.get("balances", d)
    usdt = None
    for w in (rows or []):
        if w.get("asset") == "USDT" or w.get("walletName"):
            print(f"    {json.dumps(w, ensure_ascii=False)}")
        if w.get("asset") == "USDT":
            usdt = w
    if usdt is None and isinstance(rows, list):
        for w in rows:
            b = float(w.get("balance", w.get("free", 0)) or 0)
            if b > 0:
                print(f"    {json.dumps(w, ensure_ascii=False)}")
    print(f"    → USDT: {usdt.get('balance', usdt.get('free')) if usdt else '0'}")


show("3 资金账户 Funding", SPOT, "/sapi/v1/asset/wallet/balance", params={"asset": "USDT"},
     extractor=_funding)


def _margin(d):
    print(f"    totalAssetOfBtc={d.get('totalAssetOfBtc')}  totalNetAssetOfBtc={d.get('totalNetAssetOfBtc')}")
    for a in d.get("userAssets", []):
        if float(a.get("free", 0)) + float(a.get("locked", 0)) + float(a.get("borrowed", 0)) > 0:
            print(f"    {a['asset']}: free={a['free']} locked={a['locked']} borrowed={a['borrowed']}")


show("4 全仓杠杆 Cross Margin", SPOT, "/sapi/v1/margin/account", extractor=_margin)


def _usdm(d):
    rows = d if isinstance(d, list) else d.get("assets", [])
    for b in rows:
        if float(b.get("balance", 0)) > 0:
            print(f"    {b['asset']}: balance={b.get('balance')} available={b.get('availableBalance')}")


show("5 U本位合约 USDⓈ-M", FAPI, "/fapi/v2/balance", extractor=_usdm)


def _coinm(d):
    rows = d if isinstance(d, list) else d.get("assets", [])
    for b in rows:
        if float(b.get("balance", 0)) > 0:
            print(f"    {b['asset']}: balance={b.get('balance')} available={b.get('availableBalance')}")


show("6 币本位合约 COIN-M", DAPI, "/dapi/v1/balance", extractor=_coinm)


def _pm(d):
    if not d:
        print("    (PM 无资产)")
    for a in d:
        print(f"    {a.get('asset')}: totalWalletBalance={a.get('totalWalletBalance')} "
              f"crossUnPnl={a.get('crossUnPnl')} available={a.get('availableBalance')}")


show("7 统一账户 PM", PAPI, "/papi/v1/balance", extractor=_pm)


def _pmacct(d):
    for k2 in ("accountEquity", "totalWalletBalance", "totalAvailableBalance",
               "totalMarginOpenLoss", "accountStatus", "uniMMR"):
        if k2 in d:
            print(f"    {k2}: {d[k2]}")


show("8 PM 账户概览", PAPI, "/papi/v1/account", extractor=_pmacct)

print("\n" + "=" * 64)
print("✅ 完成。哪个账户有 USDT，就往那个账户方向做划转。")