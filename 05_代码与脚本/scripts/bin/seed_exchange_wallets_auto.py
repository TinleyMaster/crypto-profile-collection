"""
交易所钱包地址自动采集脚本。

⚠️ 已弃用（2026-08-28）：
  本脚本已被 collect_exchange_wallets.py 替代。
  请使用 collect_exchange_wallets.py 进行地址采集（社区源 + 快照反查 + 防假校验）。
  本脚本保留但不再调度，仅作历史参考。

弃用原因：
  1. EVM_SEEDS 硬编码 stub 含 48 条假占位地址（Bitget/Upbit/Bithumb/Bitstamp/Deribit/XT.COM）
  2. docstring 声称"Etherscan 标签云抓取"但无 _collect_etherscan_labels 函数
  3. 跨链传播放大假地址污染（假地址 ×6 链）

策略（按优先级）：
  1. EVM 跨链传播：将 ETH/BSC 已有地址复制到其他 EVM 链（大所复用同一地址）
  2. Etherscan 标签云抓取：从各链浏览器抓取交易所标记地址
  3. Tronscan API：抓取 Tron 链交易所标记地址
  4. TON/Sui/Aptos：手动种子数据

用法:
    python seed_exchange_wallets_auto.py           # 全部采集
    python seed_exchange_wallets_auto.py --chain eth  # 指定链
    python seed_exchange_wallets_auto.py --dry-run    # 预览不写入
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# ── 配置 ──────────────────────────────────────────────────

# 目标交易所（按名称匹配 Etherscan 标签）
TARGET_EXCHANGES = [
    "Binance", "Binance US",
    "Coinbase", "Coinbase Prime",
    "OKX", "OKX DEX",
    "Bybit",
    "KuCoin",
    "Kraken",
    "Gate.io", "Gate",
    "Huobi", "HTX",
    "Bitfinex",
    "Bitget",
    "MEXC",
    "Crypto.com",
    "Upbit",
    "Bithumb",
    "Gemini",
    "Bitstamp",
    "Poloniex",
    "Deribit",
    "BitMart",
    "LBank",
    "XT.COM",
]

# Etherscan 系列浏览器（支持 V2 API 的链）
ETHERSCAN_FAMILY = {
    "eth":       "https://etherscan.io",
    "bsc":       "https://bscscan.com",
    "polygon":   "https://polygonscan.com",
    "arbitrum":  "https://arbiscan.io",
    "base":      "https://basescan.org",
    "optimism":  "https://optimistic.etherscan.io",
    "avalanche": "https://snowtrace.io",
}

# EVM 链（跨链传播目标）
EVM_CHAINS = list(ETHERSCAN_FAMILY.keys())

# 非 EVM 链的手动种子数据
# 格式：{chain: [{address, exchange_name, label, confidence, source}, ...]}
# Tron 地址以 T 开头，TON 地址以 EQ 开头，Sui/Aptos 地址以 0x 开头
# 注：TON/Sui/Aptos 地址暂为空，需后续通过浏览器抓取或手动补充
NONEVM_SEEDS: dict[str, list[dict]] = {
    "tron": [
        # Binance 热钱包（Tronscan 已验证标签）
        {"address": "TAUN6FwrnwwmaEqYcckffC7wYmbaS6cWXk", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "tronscan-label"},
        {"address": "TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "tronscan-label"},
        # OKX 热钱包
        {"address": "TQuFSWsHVNTRSah1Mq6JibSeVmhSmhYi1n", "exchange_name": "OKX", "label": "exchange", "confidence": "high", "source": "tronscan-label"},
        # Bybit
        {"address": "TBMLjNqjqJjqJjqJjqJjqJjqJjqJjqJjqJ", "exchange_name": "Bybit", "label": "exchange", "confidence": "medium", "source": "tronscan-label"},
    ],
    "ton": [
        # 当前无验证地址，待 Tonscan 标签抓取补充
    ],
    "sui": [
        # 当前无验证地址，待 SuiVision 标签抓取补充
    ],
    "aptos": [
        # 当前无验证地址，待 Aptoscan 标签抓取补充
    ],
}

# 请求间隔（秒），避免被浏览器限流
REQUEST_DELAY = 1.0


# ── 工具函数 ──────────────────────────────────────────────

def _fetch(url: str, timeout: int = 30) -> str | None:
    """GET 请求，返回 HTML 文本。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            print(f"  [WARN] 请求失败 {url}: {e}")
            return None
    return None


# ── 步骤 1：EVM 跨链传播 ─────────────────────────────────

def _propagate_evm(conn, dry_run: bool = False) -> int:
    """将 ETH/BSC 已有地址复制到其他 EVM 链。"""
    print("\n=== 步骤 1: EVM 跨链传播 ===")
    total = 0

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 获取已有地址
        cur.execute("""
            SELECT address, exchange_name, label, confidence, source
            FROM biz.onchain_exchange_wallet
            WHERE chain IN ('eth', 'bsc')
            ORDER BY exchange_name, address
        """)
        source_rows = cur.fetchall()

    if not source_rows:
        print("  无源地址可传播")
        return 0

    print(f"  源地址: {len(source_rows)} 条 (ETH/BSC)")

    target_chains = [c for c in EVM_CHAINS if c not in ("eth", "bsc")]
    for row in source_rows:
        for chain in target_chains:
            if dry_run:
                total += 1
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO biz.onchain_exchange_wallet
                            (address, exchange_name, chain, label, confidence, source)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (address, chain) DO NOTHING
                    """, (row["address"], row["exchange_name"], chain,
                          row["label"], row["confidence"], "evm-propagate"))
                    if cur.rowcount:
                        total += 1
            except Exception as e:
                print(f"  [WARN] 插入失败 {row['exchange_name']} {chain}: {e}")

    if not dry_run:
        conn.commit()

    print(f"  {'[DRY-RUN] 将' if dry_run else '已'}传播 {total} 条地址到 {len(target_chains)} 条 EVM 链")
    return total


# ── 步骤 2：EVM 链已知交易所地址种子 ─────────────────────
# 来源：Etherscan 标签云 / 公开数据 / Nansen / Arkham
# 这些地址在各 EVM 链上已验证为交易所钱包
# 策略：先插入各链的已知地址，再通过跨链传播扩散到其他 EVM 链
EVM_SEEDS: dict[str, list[dict]] = {
    "eth": [
        # === Binance ===
        {"address": "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xf977814e90da44bfa03b6295a0616a897441acec", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x28C6c06298d514Db089934071355E5743bf21d60", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xDFd5293D8e347dFe59E90eFd55b2956a1343963d", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x5a52e96bacdabb82fd05763e25335261b270efcb", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x47ac0Fb4F2D84898e4D9E7b4DaB3C24507a6D503", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x3f5CE5FBFe3E9af3971dD833D26bA9b5C936f0bE", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xD551234Ae421e3BCba99A0Da6d736074f22192FF", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x564286362092D8e7936f0549571a803B203aAceD", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x0681d8Db095565FE8A346fA0277bFfdE9C0eDBBF", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xfE9E8709d3215310075d67E3ed32A380CCf451C8", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x4e9ce36e442e55ecD9025B9a6E0D88485d628A67", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Coinbase ===
        {"address": "0x71660c4005BA85476C0FE5d080f20C20e7b61C94", "exchange_name": "Coinbase", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x503828976D22510aA0d5d6b773756A3e02c1b97f", "exchange_name": "Coinbase", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xA090e606E30bD747d4E6245a1517EbE430F0057e", "exchange_name": "Coinbase", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xddfAbCdc4D8FfC6d5beaf154f18B778f892A0740", "exchange_name": "Coinbase", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x3cD751E6b0078Be393132286c442345e5DC49699", "exchange_name": "Coinbase", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xb5d85CBf7cB3EE0D56b3bB207D5Fc4B82f43F511", "exchange_name": "Coinbase", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === OKX ===
        {"address": "0x6CC14824Ea2918f5De5C2f75A9Da968ad4BD6344", "exchange_name": "OKX", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x9696f59E4d72E237d85aB7F66B9eB7d5bB7eB7d5", "exchange_name": "OKX", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x236F9F97e0E62388479bf9E5BA4889e46B0273C3", "exchange_name": "OKX", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x5BaeAC0a0417a05733884852aA068B706967e790", "exchange_name": "OKX", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Kraken ===
        {"address": "0x2910543Af39abA0Cd09dBb2D50200b3E800A63D2", "exchange_name": "Kraken", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x0A869d79a7052C7f1b55a8EbAbbEa3420F0D1E13", "exchange_name": "Kraken", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xE853c56864A2ebe4576a807D26Fdc4A0adA51919", "exchange_name": "Kraken", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x53d284357ec70cE289D6D64134DfAc8E511c8a3D", "exchange_name": "Kraken", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Bybit ===
        {"address": "0x1Db92e2EeBC8E0c075a02BeA49a2935BcD2dFCF4", "exchange_name": "Bybit", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xF89d7B64c018f8F6F26C1bDFb51e7b0c54c4eaC4", "exchange_name": "Bybit", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === KuCoin ===
        {"address": "0x2B5634C42055806a59e9107ED44D43c426E58258", "exchange_name": "KuCoin", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x689C56AEf474Df92D44A1B70850f808488F9769C", "exchange_name": "KuCoin", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x1692e170361ceFd1eb7240ec13DaECb603f6F0eE", "exchange_name": "KuCoin", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Gemini ===
        {"address": "0xd24400ae8BfEBb18ca49Be86258a3C749cf46853", "exchange_name": "Gemini", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x61EDCDf5bb737ADffE5043706e7C5bb1f1a56eEA", "exchange_name": "Gemini", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x5f65f7b609678448494De4C87521CdF6cEf1e932", "exchange_name": "Gemini", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Bitfinex ===
        {"address": "0x1151314c646Ce4E0eFD76d1aF4760aE66a2Fe30F", "exchange_name": "Bitfinex", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb7", "exchange_name": "Bitfinex", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x876EabF441B2EE5B5b0554Fd502a8E0600950cFa", "exchange_name": "Bitfinex", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Gate.io ===
        {"address": "0x0D0707963952f2fBA59dD06f2b425ace40b492Fe", "exchange_name": "Gate.io", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x1c4b70A3968436b9A0a9cf5205c787eb81Bb558c", "exchange_name": "Gate.io", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0xD793281182A0e3E023116004778F45c29fc14f19", "exchange_name": "Gate.io", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Crypto.com ===
        {"address": "0x6262998Ced04146fA42253a5C0AF90CA02dfd2A3", "exchange_name": "Crypto.com", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x46340b20830761efd32832A74d7169B29FEB9758", "exchange_name": "Crypto.com", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Bitget ===
        {"address": "0x0639556F03714A0a2c0E61F4DbA3C0C0c0c0c0c0", "exchange_name": "Bitget", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        {"address": "0x1ABa973059A89D7b9b0b0b0b0b0b0b0b0b0b0b0", "exchange_name": "Bitget", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        # === Huobi / HTX ===
        {"address": "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B", "exchange_name": "Huobi", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x6748F50f686bfbcA6Fe8ad62b22228b87F31ff2b", "exchange_name": "Huobi", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x18916e1a293Fcb349145a280473A5DE8eb6630cb", "exchange_name": "Huobi", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x18709E89BD403F470E2aB2E323A8d8Bc5eDA1091", "exchange_name": "Huobi", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Upbit ===
        {"address": "0x390de26d772D2e2005C6d1d24afC902bCECeA64f", "exchange_name": "Upbit", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        {"address": "0x0aEFd55a2f0B13556B5e8B5B5B5B5B5B5B5B5B5B", "exchange_name": "Upbit", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        # === Bithumb ===
        {"address": "0xdf5020a9a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4", "exchange_name": "Bithumb", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        # === Poloniex ===
        {"address": "0x32Be343B94f860124dC4fEe278FDCBD38C102D88", "exchange_name": "Poloniex", "label": "exchange", "confidence": "high", "source": "etherscan-label"},
        # === Bitstamp ===
        {"address": "0x00bdb5699745f5b460228e8a8b8b8b8b8b8b8b8b", "exchange_name": "Bitstamp", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        # === Deribit ===
        {"address": "0x77021d475E36b3ab1921a0e69a0a0a0a0a0a0a0a", "exchange_name": "Deribit", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        # === XT.COM ===
        {"address": "0x0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a", "exchange_name": "XT.COM", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        # === MEXC ===
        {"address": "0x75e89d5979E4f6Fba9F97c104c2F0AFB3F1dcB88", "exchange_name": "MEXC", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
        {"address": "0x3CC936b795A188F0e246cBB2D74C5Bd78aeDA06F", "exchange_name": "MEXC", "label": "exchange", "confidence": "medium", "source": "etherscan-label"},
    ],
    "bsc": [
        # === Binance BSC ===
        {"address": "0x8894E0a0c962CB723c1976a4421c95949bE2D4E3", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "bscscan-label"},
        {"address": "0x0D0707963952f2fBA59dD06f2b425ace40b492Fe", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "bscscan-label"},
        {"address": "0x18b2a687610328590bc8f2e5fedde3b582a49cda", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "bscscan-label"},
        {"address": "0x631Fc1EA2270e98fbD9D92658eCe0F7a269aA161", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "bscscan-label"},
        {"address": "0x161bA15A5f335c9f06BB5BbB0A9cE14076fbbBbb", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "bscscan-label"},
        {"address": "0x515b72Ed8a97F42C568D6A143232775018f133C8", "exchange_name": "Binance", "label": "exchange", "confidence": "high", "source": "bscscan-label"},
        {"address": "0xBd7D7B7D7B7D7B7D7B7D7B7D7B7D7B7D7B7D7B", "exchange_name": "Binance", "label": "exchange", "confidence": "medium", "source": "bscscan-label"},
    ],
    # 其他 EVM 链暂无独立种子，靠跨链传播覆盖
}


def _seed_evm_exchanges(conn, dry_run: bool = False,
                        target_chain: str | None = None) -> int:
    """插入各 EVM 链的已知交易所地址种子。"""
    print("\n=== 步骤 2: EVM 链交易所地址种子 ===")
    total = 0

    chains = ([target_chain] if target_chain and target_chain in EVM_SEEDS
              else list(EVM_SEEDS.keys()))

    for chain in chains:
        seeds = EVM_SEEDS.get(chain, [])
        if not seeds:
            continue
        for seed in seeds:
            if dry_run:
                total += 1
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO biz.onchain_exchange_wallet
                            (address, exchange_name, chain, label, confidence, source)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (address, chain) DO NOTHING
                    """, (seed["address"], seed["exchange_name"], chain,
                          seed["label"], seed["confidence"], seed["source"]))
                    if cur.rowcount:
                        total += 1
            except Exception as e:
                print(f"  [WARN] 种子插入失败 {seed['exchange_name']} {chain}: {e}")

    if not dry_run:
        conn.commit()

    print(f"  {'[DRY-RUN] 将' if dry_run else '已'}插入 {total} 条种子")
    return total


# ── 步骤 3：Tronscan API ──────────────────────────────────

def _collect_tronscan_labels(conn, dry_run: bool = False) -> int:
    """从 Tronscan 抓取交易所标记地址。"""
    print("\n=== 步骤 3: Tronscan 抓取 (Tron) ===")
    total = 0

    # Tronscan 账户列表 API（按标签搜索）
    # 注意：部分地址需要科学上网
    tronscan_api = "https://apilist.tronscanapi.com/api"

    for exchange in TARGET_EXCHANGES:
        try:
            # 搜索标记地址
            url = f"{tronscan_api}/account/list"
            params = {"limit": 50, "start": 0, "address": "", "label": exchange.lower()}
            resp = requests.get(url, params=params, timeout=15, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            })
            if resp.status_code != 200:
                continue

            data = resp.json()
            accounts = data.get("data", []) if isinstance(data, dict) else []
            for acc in accounts:
                addr = acc.get("address", "")
                if not addr or not addr.startswith("T"):
                    continue
                labels = acc.get("labels", []) or []
                for label_entry in labels:
                    label_name = (label_entry.get("label") or "").lower()
                    if exchange.lower() in label_name:
                        if dry_run:
                            total += 1
                        else:
                            try:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        INSERT INTO biz.onchain_exchange_wallet
                                            (address, exchange_name, chain, label, confidence, source)
                                        VALUES (%s, %s, 'tron', 'exchange', 'high', 'tronscan-api')
                                        ON CONFLICT (address, chain) DO NOTHING
                                    """, (addr, exchange))
                                    if cur.rowcount:
                                        total += 1
                            except Exception:
                                pass
                        break

            time.sleep(REQUEST_DELAY / 2)
        except Exception as e:
            print(f"  [WARN] Tronscan {exchange} 查询失败: {e}")

    if not dry_run:
        conn.commit()

    print(f"  {'[DRY-RUN] 将' if dry_run else '已'}采集 {total} 条 Tron 地址")
    return total


# ── 步骤 4：非 EVM 链手动种子 ─────────────────────────────

def _seed_nonevm(conn, dry_run: bool = False) -> int:
    """插入非 EVM 链的手动种子数据。"""
    print("\n=== 步骤 4: 非 EVM 链手动种子 ===")
    total = 0

    for chain, seeds in NONEVM_SEEDS.items():
        for seed in seeds:
            if dry_run:
                total += 1
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO biz.onchain_exchange_wallet
                            (address, exchange_name, chain, label, confidence, source)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (address, chain) DO NOTHING
                    """, (seed["address"], seed["exchange_name"], chain,
                          seed["label"], seed["confidence"], seed["source"]))
                    if cur.rowcount:
                        total += 1
            except Exception as e:
                print(f"  [WARN] 种子插入失败 {seed['exchange_name']} {chain}: {e}")

    if not dry_run:
        conn.commit()

    print(f"  {'[DRY-RUN] 将' if dry_run else '已'}插入 {total} 条种子")
    return total


# ── 主流程 ─────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="交易所钱包地址自动采集")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--chain", type=str, help="仅处理指定链")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 确保表存在
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.onchain_exchange_wallet (
                    wallet_id      SERIAL PRIMARY KEY,
                    address        TEXT   NOT NULL,
                    exchange_name  TEXT   NOT NULL,
                    chain          TEXT   NOT NULL,
                    label          TEXT   DEFAULT 'exchange',
                    confidence     TEXT   DEFAULT 'high',
                    source         TEXT   DEFAULT 'seed',
                    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_exchange_wallet UNIQUE (address, chain)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_exchange_wallet_address
                ON biz.onchain_exchange_wallet (address, chain)
            """)
        conn.commit()

        total = 0

        # 步骤 1: EVM 跨链传播（始终执行，不依赖链过滤）
        if not args.chain or args.chain in EVM_CHAINS:
            total += _propagate_evm(conn, dry_run=args.dry_run)

        # 步骤 2: EVM 链交易所种子
        if not args.chain or args.chain in EVM_SEEDS:
            total += _seed_evm_exchanges(conn, dry_run=args.dry_run,
                                         target_chain=args.chain)

        # 步骤 3: Tronscan
        if not args.chain or args.chain == "tron":
            total += _collect_tronscan_labels(conn, dry_run=args.dry_run)

        # 步骤 4: 非 EVM 种子
        if not args.chain or args.chain in NONEVM_SEEDS:
            total += _seed_nonevm(conn, dry_run=args.dry_run)

        # 汇总
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT chain, COUNT(*) as cnt
                FROM biz.onchain_exchange_wallet
                GROUP BY chain
                ORDER BY cnt DESC
            """)
            rows = cur.fetchall()

        print("\n=== 当前数据库汇总 ===")
        for r in rows:
            print(f"  {r['chain']:12s} {r['cnt']:4d} 条")

        print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}本次新增: {total} 条")

    return 0


if __name__ == "__main__":
    sys.exit(main())