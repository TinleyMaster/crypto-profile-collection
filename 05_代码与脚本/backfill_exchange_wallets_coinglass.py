"""CoinGlass 交易所钱包地址库回填脚本（补 onchain_alert 页的覆盖率缺口）。

设计原则（对齐项目铁律 + 解析器语义）：
  1. 纯 additive：只 INSERT/UPSERT，绝不 DELETE/UPDATE 现有数据语义。
  2. 默认 --dry-run：只打印将写入的行 + 样本 + 与现有库去重后的新增数，不写库。
     必须显式 --write 才真写 prod。
  3. 链推断：单链原生币按 symbol→chain 映射；跨链稳定币按地址格式反推
     (T 开头→tron / base58~44→solana / 0x→eth 默认，靠 evm_propagate 后续传播到 bsc/arb/base)。
  4. 大小写：tron/solana 地址绝不 lower()（解析器 CASE_SENSITIVE_CHAINS）。
  5. CoinGlass 为权威源 → confidence='high', source='coinglass'。

用法：
  # 1) 先看 API 真实返回结构（不解析，只打印原始 JSON 样本）
  CG_API_KEY=xxx python backfill_exchange_wallets_coinglass.py --probe Binance

  # 2) dry-run：拉全部内置交易所，打印将写入行 + 去重新增数（需 DATABASE_URL 才能算去重）
  DATABASE_URL=xxx CG_API_KEY=yyy python backfill_exchange_wallets_coinglass.py --all --dry-run

  # 3) 真写（女王单独口头授权后）
  DATABASE_URL=xxx CG_API_KEY=yyy python backfill_exchange_wallets_coinglass.py --all --write

依赖：requests（venv 已装）。CoinGlass 为美国服务，本机需走出口代理（requests 自动读 HTTPS_PROXY）。
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少 requests：venv 里 pip install requests")

try:
    import psycopg2
    import psycopg.rows
except ImportError:  # pragma: no cover
    psycopg2 = None

CG_BASE = "https://open-api-v4.coinglass.com"

# 与解析器 address_label_resolver.CASE_SENSITIVE_CHAINS 对齐
CASE_SENSITIVE_CHAINS = {"solana", "tron", "ton", "sui", "aptos"}

# 单链原生币 symbol -> chain（小写，对齐 transfer_log / onchain_exchange_wallet 取值）
SINGLE_CHAIN = {
    "BTC": "bitcoin", "ETH": "eth", "SOL": "solana", "LTC": "litecoin",
    "BCH": "bitcoincash", "XRP": "ripple", "DOGE": "dogecoin", "TRX": "tron",
    "ADA": "cardano", "DOT": "polkadot", "AVAX": "avax", "TON": "ton",
    "ATOM": "cosmos", "NEAR": "near", "APT": "aptos", "SUI": "sui",
    "ALGO": "algorand", "XTZ": "tezos", "EOS": "eos", "XLM": "stellar",
    "FIL": "filecoin", "HBAR": "hedera", "MKR": "ethereum",  # MKR 是 ERC-20
}

# 跨链稳定币 / 主流 ERC-20（靠地址格式推断链）
STABLECOINS = {"USDT", "USDC", "DAI", "BUSD", "USDE", "FDUSD", "TUSD", "USDD"}

# 内置交易所 code（CoinGlass /api/exchange/assets?exchange= 用，首字母大写）
BUILTIN_EXCHANGES = [
    "Binance", "Coinbase", "OKX", "Bybit", "Kraken", "KuCoin", "Bitfinex",
    "Gate.io", "HTX", "Upbit", "Bitstamp", "Gemini", "Crypto.com", "MEXC",
    "BinanceUS", "CoinbaseExchange", "Bitget", "Pionex", "WhiteBIT", "Digifinex",
]


def _is_base58(s: str) -> bool:
    if not (25 <= len(s) <= 64):
        return False
    try:
        int(s, 58)
        return True
    except ValueError:
        return False


def infer_chain(symbol: str, address: str) -> str | None:
    """链推断：返回 chain 字符串或 None（无法推断则跳过）。"""
    sym = (symbol or "").upper().strip()
    addr = address or ""
    if sym in SINGLE_CHAIN:
        return SINGLE_CHAIN[sym]
    if sym in STABLECOINS or sym in {"WETH", "WBTC", "STETH", "WSTETH"}:
        if addr.startswith("T"):
            return "tron"
        if _is_base58(addr) and not addr.startswith("0x"):
            return "solana"
        if addr.startswith("0x"):
            return "eth"  # EVM 默认 eth；后续 evm_propagate 会传播到 bsc/arb/base
        # 其他格式（如纯数字 TRON 旧格式）保守跳过
        return None
    # 其他未知 symbol：不硬猜，跳过（避免误标）
    return None


def _norm_addr(addr: str, chain: str) -> str:
    if chain in CASE_SENSITIVE_CHAINS:
        return addr.strip()
    return addr.strip().lower()


def fetch_exchange_assets(api_key: str, exchange: str, proxy: str | None = None):
    """拉单个交易所的资产钱包地址。返回 (exchange, [(symbol, wallet_address, balance_usd)])."""
    url = f"{CG_BASE}/api/exchange/assets"
    headers = {"CG-API-KEY": api_key, "Accept": "application/json"}
    params = {"exchange": exchange}
    proxies = {"https": proxy, "http": proxy} if proxy else None
    resp = requests.get(url, headers=headers, params=params, proxies=proxies, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # CoinGlass V4 返回通常在 data 字段
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        # 兜底：可能是 {data: {list: [...]}} 之类
        rows = data.get("data", {}).get("list", []) if isinstance(data.get("data"), dict) else []
    out = []
    for r in rows:
        out.append((
            r.get("symbol"),
            r.get("wallet_address") or r.get("address"),
            r.get("balance_usd"),
        ))
    return exchange, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="EXCHANGE",
                    help="只拉一个交易所，打印原始 JSON 样本（不解析/不写库）")
    ap.add_argument("--exchange", action="append", default=[],
                    help="指定单个交易所（可多次），与 --all 互斥")
    ap.add_argument("--all", action="store_true", help="遍历内置交易所列表")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印 + 算去重新增数，不写库（默认行为，显式写出更清晰）")
    ap.add_argument("--write", action="store_true",
                    help="真正 UPSERT 进 prod（需女王口头授权）")
    args = ap.parse_args()

    api_key = os.environ.get("CG_API_KEY")
    if not api_key:
        sys.exit("CG_API_KEY 未设置（环境变量或 --probe 调试时也需要）")
    db_url = os.environ.get("DATABASE_URL")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    # ---- probe 模式：只看真实结构 ----
    if args.probe:
        ex, rows = fetch_exchange_assets(api_key, args.probe, proxy)
        print(f"[PROBE] exchange={ex} 返回 {len(rows)} 条；前 3 条原始样本：")
        for sym, addr, usd in rows[:3]:
            print(f"  symbol={sym!r} addr={addr!r} usd={usd}")
        print("[PROBE] DONE（未解析/未写库）")
        return

    # ---- 选定交易所列表 ----
    if args.all:
        exchanges = BUILTIN_EXCHANGES
    elif args.exchange:
        exchanges = args.exchange
    else:
        sys.exit("需指定 --all 或 --exchange X（或 --probe X 看结构）")

    # ---- 拉取 + 解析 ----
    parsed: list[dict] = []  # {address, exchange_name, chain, symbol, balance_usd}
    skipped_no_chain = 0
    skipped_no_addr = 0
    for ex in exchanges:
        try:
            _, rows = fetch_exchange_assets(api_key, ex, proxy)
        except Exception as e:  # 单所失败不阻断其他所
            print(f"[WARN] 拉 {ex} 失败: {e}", file=sys.stderr)
            continue
        for sym, addr, usd in rows:
            if not addr:
                skipped_no_addr += 1
                continue
            chain = infer_chain(sym, addr)
            if not chain:
                skipped_no_chain += 1
                continue
            parsed.append({
                "address": _norm_addr(addr, chain),
                "exchange_name": ex,
                "chain": chain,
                "symbol": sym,
                "balance_usd": usd,
            })

    print(f"\n=== 解析结果 ===")
    print(f"  成功解析地址-链: {len(parsed)} 条")
    print(f"  跳过(无法推断链): {skipped_no_chain} 条")
    print(f"  跳过(无地址): {skipped_no_addr} 条")

    # 样本
    print("\n  样本（前 8 条）:")
    for p in parsed[:8]:
        print(f"    {p['exchange_name']:<12} {p['symbol']:<8} {p['chain']:<10} {p['address']}")

    # 按链分布
    from collections import Counter
    chain_dist = Counter(p["chain"] for p in parsed)
    print("\n  按链分布:", dict(chain_dist))

    # ---- 与现有库去重（需 DATABASE_URL）----
    new_rows = parsed
    if db_url and psycopg2 is not None:
        try:
            with psycopg2.connect(db_url, connect_timeout=30) as conn:
                with conn.cursor() as cur:
                    # 现有 (address, chain) 集合（两表 union）
                    cur.execute("""
                        SELECT address, chain FROM biz.onchain_exchange_wallet
                        UNION
                        SELECT address, chain FROM biz.onchain_address_label
                        WHERE label_type='exchange'
                    """)
                    existing = set((r[0].lower() if r[1] not in CASE_SENSITIVE_CHAINS else r[0], r[1])
                                   for r in cur.fetchall())
                    new_rows = [p for p in parsed
                                if (p["address"].lower() if p["chain"] not in CASE_SENSITIVE_CHAINS
                                    else p["address"], p["chain"]) not in existing]
                    print(f"\n  与现有库去重后，将新增: {len(new_rows)} 条（已存在 {len(parsed)-len(new_rows)} 条）")
        except Exception as e:
            print(f"[WARN] 去重查询失败（不影响 dry-run 打印）: {e}", file=sys.stderr)
    elif not args.write:
        print("\n  [提示] 未提供 DATABASE_URL，无法算去重新增数；以上为 CoinGlass 原始解析规模。")

    # ---- 写库 ----
    if not args.write:
        print("\n[DRY-RUN] 未写库。确认无误后加 --write 执行（需女王授权）。")
        return

    # --write 模式
    if not db_url:
        sys.exit("--write 需要 DATABASE_URL")
    print(f"\n[WRITE] 准备 UPSERT {len(new_rows)} 条到 biz.onchain_exchange_wallet ...")
    with psycopg2.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            inserted = 0
            for p in new_rows:
                display = f"{p['exchange_name']} {p['symbol']} ({p['chain']}) via CoinGlass"
                cur.execute("""
                    INSERT INTO biz.onchain_exchange_wallet
                        (address, exchange_name, chain, display_name, label, confidence, source, added_at)
                    VALUES (%s, %s, %s, %s, 'exchange', 'high', 'coinglass', NOW())
                    ON CONFLICT (address, chain) DO UPDATE SET
                        exchange_name = EXCLUDED.exchange_name,
                        confidence   = 'high',
                        source       = 'coinglass',
                        display_name = EXCLUDED.display_name,
                        added_at     = NOW()
                """, (p["address"], p["exchange_name"], p["chain"], display))
                inserted += 1
        conn.commit()
    print(f"[WRITE] 完成，UPSERT {inserted} 条。")


if __name__ == "__main__":
    main()
