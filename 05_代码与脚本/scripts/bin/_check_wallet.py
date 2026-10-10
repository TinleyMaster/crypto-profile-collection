#!/usr/bin/env python3
"""容器里查 API Key 权限 + 资金账户 USDT 余额。"""
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

def signed_get(base, path, params=None):
    params = dict(params or {})
    params['timestamp'] = int(time.time()*1000); params['recvWindow'] = 5000
    qs = '&'.join(f'{k}={v}' for k,v in sorted(params.items()))
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f'{base}{path}?{qs}&signature={sig}'
    return requests.get(url, headers={'X-MBX-APIKEY': key}, timeout=15)

API = 'https://api.binance.com'

print('='*60)
print('API Key 权限 + 账户余额检查')
print('='*60)

# 1. 权限
print(f'\n[1] GET /sapi/v1/account/apiRestrictions ...', flush=True)
r = signed_get(API, '/sapi/v1/account/apiRestrictions')
print(f'    HTTP {r.status_code}')
if r.status_code == 200:
    d = r.json()
    for k2,v in d.items():
        if k2 in ('canTrade','canWithdraw','canDeposit','enablePortfolioMarginTrading','enableSpotAndMarginTradingEnabled','marginStatus'):
            print(f'    {k2}: {v}')
else:
    print(f'    {r.text[:300]}')

# 2. 资金账户余额（USDT）
print(f'\n[2] GET /sapi/v1/asset/wallet/balance (asset=USDT) ...', flush=True)
r = signed_get(API, '/sapi/v1/asset/wallet/balance', {'asset': 'USDT'})
print(f'    HTTP {r.status_code}')
print(f'    {r.text[:500]}')

# 3. PM 账户余额（再确认）
print(f'\n[3] GET /papi/v1/balance (PM) ...', flush=True)
PAPI = 'https://papi.binance.com'
r = signed_get(PAPI, '/papi/v1/balance')
print(f'    HTTP {r.status_code}')
print(f'    {r.text[:500]}')
