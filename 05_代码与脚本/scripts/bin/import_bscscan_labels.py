# -*- coding: utf-8 -*-
"""
从浏览器插件爬取的 BSCScan 标签页 CSV 导入交易所地址到地址标签库。

数据源：BSCScan Labeled Accounts 页面（浏览器插件导出的 CSV）
        例：bscscan-2026-09-09.csv，含 1000 条带 name tag 的地址

导入目标（双表写入，渐进式扩展）：
  1. biz.onchain_exchange_wallet —— 现有交易所地址表，立即可参与净流计算
  2. biz.onchain_address_label    —— 新通用地址标签表，为后续多类型标签铺路

置信度规则：
  - 来自 BSCScan 官方标签的交易所地址 → medium（单源权威标注）
  - 已有记录不动（不降级、不覆盖已有 high）

用法：
  python import_bscscan_labels.py --csv bscscan-2026-09-09.csv            # dry-run
  python import_bscscan_labels.py --csv bscscan-2026-09-09.csv --apply    # 正式写入
  python import_bscscan_labels.py --csv xxx.csv --chain bsc               # 指定链（默认 bsc）
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# ── 配置 ──────────────────────────────────────────────────

# 交易所名称归一化（与 collect_exchange_wallets.py 保持同步）
EXCHANGE_NAME_MAP = {
    "binance": "Binance", "binance us": "Binance US", "binanceus": "Binance US",
    "coinbase": "Coinbase", "coinbase prime": "Coinbase Prime",
    "okx": "OKX", "okex": "OKX", "kraken": "Kraken", "bybit": "Bybit",
    "kucoin": "KuCoin", "gate": "Gate.io", "gate.io": "Gate.io", "gateio": "Gate.io",
    "huobi": "Huobi", "htx": "HTX", "bitfinex": "Bitfinex", "bitget": "Bitget",
    "mexc": "MEXC", "crypto.com": "Crypto.com", "cryptocom": "Crypto.com",
    "upbit": "Upbit", "bithumb": "Bithumb", "gemini": "Gemini", "bitstamp": "Bitstamp",
    "poloniex": "Poloniex", "deribit": "Deribit", "bitmart": "BitMart", "lbank": "LBank",
    "xt.com": "XT.COM", "xtcom": "XT.COM", "bittrex": "Bittrex", "bitmex": "BitMEX",
    "korbit": "Korbit", "coinone": "Coinone", "coinbit": "Coinbit",
    "ftx": "FTX", "hotbit": "Hotbit",
    "blockchain": "Blockchain.com", "nexo": "Nexo", "swissborg": "SwissBorg",
    "maskex": "MaskEX", "nominex": "Nominex", "weex exchange": "WEEX",
    "coindcx": "CoinDCX", "fixedfloat": "FixedFloat", "azbit": "Azbit",
    "bitdiamond": "BitDiamond", "indoex": "IndoEx",
    "bitkeep": "BitKeep", "bingx": "BingX", "ascendex": "AscendEX",
    "whitebit": "WhiteBIT", "youhodler": "YouHodler", "coinex": "CoinEx",
    "coinstore": "Coinstore", "coinsbit": "Coinsbit", "kucoin": "KuCoin",
    "biconomy": "Biconomy", "paxos": "Paxos", "ceffu": "Ceffu",
    "transak": "Transak", "tokocrypto": "Tokocrypto", "coinzix": "Coinzix",
    "difx": "DIFX", "finxflo": "Finxflo", "lordtoken": "LordToken",
    "oobit": "Oobit", "byex": "BYEX", "darkex": "Darkex",
    "qmall": "Qmall", "stake.com": "Stake.com", "steam exchange": "Steam Exchange",
    "tbcc": "TBCC", "tidex": "Tidex", "txbit": "Txbit",
    "xeggex": "XeggeX", "cryptounity": "CryptoUnity", "coinspot": "CoinSpot",
    "blockfolio": "Blockfolio", "keysecure": "KeySecure",
    "bitsten": "Bitsten", "coinfield": "CoinField",
    "bazar exchange": "Bazar Exchange", "brasil bitcoin": "Brasil Bitcoin",
    "ourbit": "Ourbit", "hotbit": "Hotbit",
    "lbank": "LBank", "xt.com": "XT.COM", "bittrex": "Bittrex",
    "bitmex": "BitMEX", "korbit": "Korbit", "coinone": "Coinone",
    "coinbit": "Coinbit", "nominex": "Nominex", "weex": "WEEX",
    "coindcx": "CoinDCX", "fixedfloat": "FixedFloat", "azbit": "Azbit",
    "maskex": "MaskEX", "swissborg": "SwissBorg", "nexo": "Nexo",
    "blockchain": "Blockchain.com",
    "btcturk": "BtcTurk", "bitso": "Bitso", "robinhood": "Robinhood",
    "ace": "ACE", "sideshift": "SideShift", "xt": "XT.COM",
    "bybit": "Bybit",
}

# 大小写敏感链（地址不得 .lower()）
CASE_SENSITIVE_CHAINS = frozenset({"solana", "tron", "ton", "sui", "aptos"})

# 地址 URL 正则：支持 /address/ (EVM系) 和 /account/ (Solana系)
# EVM: 0x + 40 hex
# Solana: base58 地址（32-44位字母数字，不含 0OIl）
RE_ADDR_URL = re.compile(
    r"/(?:address|account)/([1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40})"
)
RE_EVM = re.compile(r"^0x[a-fA-F0-9]{40}$")
RE_SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

DEFAULT_SOURCE = "auto_bscscan_csv"
DEFAULT_CONFIDENCE = "high"


# ── 工具函数 ──────────────────────────────────────────────

def norm_address(addr: str, chain: str) -> str:
    """地址标准化：EVM 小写，大小写敏感链保留原样。"""
    if not addr:
        return ""
    if chain not in CASE_SENSITIVE_CHAINS:
        return addr.lower()
    return addr


def extract_exchange_display_name(label: str) -> str:
    """从 BSCScan 标签文本提取交易所显示名。

    规则：取冒号前的部分（如有），否则取空格+数字前的部分。
    例：
      'Binance 47' -> 'Binance'
      'Binance: Hot Wallet 10' -> 'Binance'
      'BitDiamond: IndoEx Exchange' -> 'BitDiamond'
      'FixedFloat: Hot Wallet' -> 'FixedFloat'
    """
    if not label:
        return ""
    # 冒号优先
    if ":" in label:
        prefix = label.split(":")[0].strip()
    else:
        # 去掉末尾数字编号
        m = re.match(r"^(.+?)\s+\d+$", label)
        prefix = m.group(1).strip() if m else label.strip()
    return prefix


def normalize_exchange(raw_display: str) -> str:
    """用 EXCHANGE_NAME_MAP 归一化交易所名；未命中返回原值。

    策略：
      1. 先完整匹配 EXCHANGE_NAME_MAP
      2. 再尝试"交易所名 + 子类型"的模式（如 OKX Dep, Coinbase Cold, Bybit Hot Wallet 等）
         → 归到主交易所名
    """
    if not raw_display:
        return ""
    raw = raw_display.strip()
    key = raw.lower()
    if key in EXCHANGE_NAME_MAP:
        return EXCHANGE_NAME_MAP[key]

    # 子类型后缀（命中则归到主交易所名）
    SUB_TYPES = (
        "dep", "deposit", "deposit funder",
        "cold", "cold wallet", "cold storage",
        "hot", "hot wallet",
        "wallet",
        "futures",
        "earn",
        "staking",
        "savings",
        "commerce",
        "internal",
        "withdrawal",
        "fee",
    )

    # 尝试从已知交易所名开头的字符串中提取主名
    # 遍历 EXCHANGE_NAME_MAP 的 key，找最长匹配的前缀
    best_match = None
    best_len = 0
    for ex_key in EXCHANGE_NAME_MAP:
        if key.startswith(ex_key + " ") and len(ex_key) > best_len:
            # 检查去掉前缀后剩下的部分是否是子类型
            rest = key[len(ex_key):].strip()
            # 去掉可能的数字后缀（"OKX Dep 3" → "okx dep"）
            rest_no_num = re.sub(r"\s+\d+$", "", rest).strip()
            if rest_no_num in SUB_TYPES:
                best_match = EXCHANGE_NAME_MAP[ex_key]
                best_len = len(ex_key)

    if best_match:
        return best_match

    # 未命中：保持原样
    return raw


# 非交易所类标签关键词（命中则排除）
NON_EXCHANGE_KEYWORDS = (
    " token",              # "BUSD Token" / "TKO Token" 等合约地址
    ": deployer",          # 部署者地址
    " deployer",
    ": liquidity pool",    # 流动性池
    "fee recipient",       # 手续费接收者
    "bridge:",             # 跨链桥合约
    ": router",            # DEX 路由
    ": factory",           # 合约工厂
    ": staking",           # 质押合约
    ": vault",             # 金库合约
    ": treasury",          # 国库合约
    "governance",          # 治理合约
    ": timelock",          # 时间锁合约
    "multisig",            # 多签（非交易所类）
    ": gnosis safe",       # Gnosis Safe 多签
    "airdrops",            # 空投地址
    ": faucet",            # 水龙头
    "validator",           # 验证者
    ": miner",             # 矿工
    "charity",             # 慈善基金（非交易所业务）
    "foundation",          # 基金会
    "grant",               # 资助
    ": peg",               # Peg 代币合约（Binance-Peg 系列）
    "token swap",          # 代币交换合约
    ": ae token swap",     # AE Token Swap 之类
    ": jex",               # JEX 不是交易所核心地址
    ": pool",              # 矿池/资金池（非交易所钱包）
)


def is_exchange_label(label: str) -> bool:
    """判断标签是否属于交易所类。

    策略：这批 CSV 来自 BSCScan 标签页，大部分是交易所。
    采用"默认是，排除法"策略：
      1. 空标签 → 不是（无法判断）
      2. 命中非交易所关键词 → 不是
      3. 其他情况 → 是（保守但覆盖率高，BSCScan 标签页的标注可信度足够）
    """
    if not label:
        return False
    low = label.lower()
    for kw in NON_EXCHANGE_KEYWORDS:
        if kw in low:
            return False
    return True


# ── CSV 解析 ──────────────────────────────────────────────

def _detect_columns(header: list, first_rows: list) -> tuple:
    """从表头和前几行数据自动推断列位置。

    返回: (url_col, label_col, bal_cols)
      url_col: 地址 URL 列索引
      label_col: 标签名列索引
      bal_cols: 余额相关列索引列表（按出现顺序）
    """
    n_cols = len(header)

    # 1. URL 列：表头含 href，且值里有 /address/ 或 /account/
    url_col = None
    for i, col in enumerate(header):
        col_l = col.strip().lower()
        if "href" in col_l:
            # 验证一下第一行数据是不是地址链接
            for row in first_rows:
                if i < len(row) and ("/address/" in row[i] or "/account/" in row[i]):
                    url_col = i
                    break
            if url_col is not None:
                break

    # 如果表头找不到，直接扫数据列
    if url_col is None:
        for i in range(n_cols):
            for row in first_rows:
                if i < len(row) and ("/address/" in row[i] or "/account/" in row[i]):
                    url_col = i
                    break
            if url_col is not None:
                break

    if url_col is None:
        return None, None, []

    # 2. 标签列：排除 URL 列、图片列（http/https 开头且不是 /address/）、
    #    地址截断列（0x 开头短字符串），找含交易所名模式的列
    label_col = None

    # 先看表头：sorting_1 是 Etherscan 系的标签列
    for i, col in enumerate(header):
        if col.strip().lower() in ("sorting_1", "sorting"):
            label_col = i
            break

    # 再扫数据找标签列
    if label_col is None:
        # 标签特征：包含冒号（"Binance: Hot Wallet"）或
        #         匹配 "名称 数字" 模式（"Binance 10"）或
        #         全是英文+空格，不含 $/数字/小数点
        candidate_scores = []
        for i in range(n_cols):
            if i == url_col:
                continue
            score = 0
            for row in first_rows:
                if i >= len(row):
                    continue
                val = row[i].strip()
                if not val:
                    continue
                # 排除图片 URL
                if val.startswith("http") and "/address/" not in val:
                    score -= 10
                    break
                # 排除纯数字（带逗号可能是金额或计数）
                if re.match(r"^[\d,]+\.?\d*$", val):
                    score -= 5
                    break
                # 排除 $ 开头的金额
                if val.startswith("$"):
                    score -= 5
                    break
                # 排除截断地址
                if val.startswith("0x") and "..." in val:
                    score -= 5
                    break
                # 冒号加分（典型标签格式）
                if ":" in val:
                    score += 3
                # "名称 数字" 模式加分
                if re.search(r"\s+\d+$", val):
                    score += 2
                # 纯文本加分
                if re.match(r"^[A-Za-z][A-Za-z0-9 :.\-']+$", val):
                    score += 1
            candidate_scores.append((i, score))

        # 取分最高的
        candidate_scores.sort(key=lambda x: -x[1])
        if candidate_scores and candidate_scores[0][1] > 0:
            label_col = candidate_scores[0][0]

    # 3. 余额列：表头含 odd/even，或值含币单位（ETH/BNB/AVAX/MATIC 等）或 $ 开头
    bal_cols = []
    # 先按表头找
    for i, col in enumerate(header):
        col_l = col.strip().lower()
        if "odd" in col_l or "even" in col_l:
            bal_cols.append(i)
    # 如果没找到，扫数据找含币单位或$的列
    if not bal_cols:
        for i in range(n_cols):
            if i == url_col or i == label_col:
                continue
            for row in first_rows:
                if i >= len(row):
                    continue
                val = row[i].strip()
                if not val:
                    continue
                # $ 开头 或 含币单位 或 纯大数字带逗号
                if val.startswith("$") or re.search(r"\s+(ETH|BNB|AVAX|MATIC|ARB|OP|SOL|TRX)\b", val, re.I):
                    if i not in bal_cols:
                        bal_cols.append(i)
                    break
                if re.match(r"^[\d,]+\.?\d*\s+[A-Z]+$", val):
                    if i not in bal_cols:
                        bal_cols.append(i)
                    break

    return url_col, label_col, bal_cols


def parse_label_csv(csv_path: Path, chain: str) -> list[dict]:
    """解析浏览器插件导出的区块浏览器标签页 CSV（多链通用）。

    自动从表头和数据推断列位置，支持 Etherscan 系 / BSCScan / Arbiscan /
    PolygonScan / Snowtrace 等各种变体。

    返回每条记录：
      {address, label_text, display_name, balance_coin, balance_usd, is_exchange}
    """
    records = []
    seen = set()
    with csv_path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []

        # 预读前 10 行用于列检测
        first_rows = []
        for _ in range(10):
            row = next(reader, None)
            if row:
                first_rows.append(row)

        url_col, label_col, bal_cols = _detect_columns(header, first_rows)

        if url_col is None:
            print(f"  [FATAL] 无法识别 URL 列，表头: {header}")
            return []
        if label_col is None:
            print(f"  [FATAL] 无法识别标签列，表头: {header}")
            print(f"  [DEBUG] 候选余额列: {bal_cols}")
            return []

        # 重置到数据开头（先处理预读的 10 行，再处理剩下的）
        all_rows = first_rows + list(reader)

        for i, row in enumerate(all_rows, start=2):
            if not row or len(row) <= max(url_col, label_col):
                continue

            url = row[url_col].strip()
            label = row[label_col].strip() if len(row) > label_col else ""

            # 从 URL 提取完整地址
            m = RE_ADDR_URL.search(url)
            if not m:
                # 跳过不是地址链接的行
                continue
            addr = norm_address(m.group(1), chain)

            # 去重（同一地址可能出现多次）
            if addr in seen:
                continue
            seen.add(addr)

            # 余额列（按顺序取非空值）
            bal_values = []
            for ci in bal_cols:
                if ci < len(row) and row[ci].strip():
                    bal_values.append(row[ci].strip())
            bal_coin = bal_values[0] if len(bal_values) > 0 else ""
            bal_usd = bal_values[1] if len(bal_values) > 1 else ""

            exchange_raw = extract_exchange_display_name(label)
            display_name = normalize_exchange(exchange_raw)
            is_exch = is_exchange_label(label)

            records.append({
                "address": addr,
                "label_text": label,
                "display_name": display_name,
                "balance_coin": bal_coin,
                "balance_usd": bal_usd,
                "is_exchange": is_exch,
            })

    return records


# 兼容旧函数名
parse_bscscan_csv = parse_label_csv


# ── 数据库对比 ────────────────────────────────────────────

def diff_against_db(conn, records: list[dict], chain: str) -> dict:
    """对比数据库已有数据，输出 diff 统计。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 查老表（交易所地址）
        cur.execute("""
            SELECT address, exchange_name, confidence
            FROM biz.onchain_exchange_wallet
            WHERE chain = %s
        """, (chain,))
        old_existing = {r["address"].lower(): dict(r) for r in cur.fetchall()}

        # 查新表（通用标签）
        cur.execute("""
            SELECT address, label_type, label_name, confidence
            FROM biz.onchain_address_label
            WHERE chain = %s
        """, (chain,))
        new_existing = {(r["address"].lower(), r["label_type"], r["label_name"]): dict(r)
                        for r in cur.fetchall()}

    new_exchange = [r for r in records if r["is_exchange"]]
    new_other = [r for r in records if not r["is_exchange"]]

    # 老表 diff
    old_new = []
    old_duplicate = []
    for r in new_exchange:
        addr = r["address"].lower()
        if addr in old_existing:
            old_duplicate.append(r)
        else:
            old_new.append(r)

    # 新表 diff
    new_table_new = []
    new_table_duplicate = []
    for r in records:
        addr = r["address"].lower()
        label_type = "exchange" if r["is_exchange"] else "other"
        key = (addr, label_type, r["label_text"])
        if key in new_existing:
            new_table_duplicate.append(r)
        else:
            new_table_new.append(r)

    # 按交易所统计新增
    ex_counter = Counter(r["display_name"] for r in old_new)

    return {
        "total": len(records),
        "exchange_count": len(new_exchange),
        "other_count": len(new_other),
        "old_table_new": len(old_new),
        "old_table_duplicate": len(old_duplicate),
        "new_table_new": len(new_table_new),
        "new_table_duplicate": len(new_table_duplicate),
        "exchange_breakdown": ex_counter.most_common(),
        "old_new_records": old_new,
        "new_table_new_records": new_table_new,
    }


# ── 写入数据库 ────────────────────────────────────────────

def apply_import(conn, records: list[dict], chain: str, source: str,
                 confidence: str) -> dict:
    """正式写入两张表。返回写入统计。"""
    inserted_exchange = 0
    inserted_label = 0
    skipped_exchange = 0
    skipped_label = 0

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for r in records:
            addr = r["address"]
            label_text = r["label_text"]
            display_name = r["display_name"]
            is_exch = r["is_exchange"]

            # 原始元数据（存 JSONB）
            raw_meta = json.dumps({
                "balance_coin": r.get("balance_coin", r.get("balance_bnb", "")),
                "balance_usd": r.get("balance_usd", ""),
                "source_csv": source,
            })

            # 1. 写入通用标签表
            label_type = "exchange" if is_exch else "other"
            try:
                cur.execute("""
                    INSERT INTO biz.onchain_address_label
                        (address, chain, label_type, label_name, display_name,
                         confidence, source, raw_meta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
                """, (
                    addr, chain, label_type, label_text, display_name,
                    confidence, source, raw_meta,
                ))
                if cur.rowcount:
                    inserted_label += 1
                else:
                    skipped_label += 1
            except Exception as e:
                print(f"  [WARN] 写入 address_label 失败 {addr}: {str(e)[:120]}")
                skipped_label += 1

            # 2. 交易所类 → 也写入老表（立即可用）
            if is_exch:
                try:
                    cur.execute("""
                        INSERT INTO biz.onchain_exchange_wallet
                            (address, exchange_name, chain, label, confidence, source)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (address, chain) DO NOTHING
                    """, (
                        addr, display_name, chain, label_text, confidence, source,
                    ))
                    if cur.rowcount:
                        inserted_exchange += 1
                    else:
                        skipped_exchange += 1
                except Exception as e:
                    print(f"  [WARN] 写入 exchange_wallet 失败 {addr}: {str(e)[:120]}")
                    skipped_exchange += 1

    conn.commit()
    return {
        "inserted_exchange": inserted_exchange,
        "skipped_exchange": skipped_exchange,
        "inserted_label": inserted_label,
        "skipped_label": skipped_label,
    }


# ── 主流程 ────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="导入 BSCScan 插件爬取的地址标签 CSV 到数据库")
    parser.add_argument("--csv", type=str, required=True, help="CSV 文件路径")
    parser.add_argument("--chain", type=str, default="bsc", help="链名（默认 bsc）")
    parser.add_argument("--source", type=str, default=DEFAULT_SOURCE,
                        help=f"来源标识（默认 {DEFAULT_SOURCE}）")
    parser.add_argument("--confidence", type=str, default=DEFAULT_CONFIDENCE,
                        choices=["high", "medium", "low"],
                        help=f"置信度（默认 {DEFAULT_CONFIDENCE}）")
    parser.add_argument("--apply", action="store_true",
                        help="正式写入数据库（默认 dry-run）")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[FATAL] CSV 文件不存在: {csv_path}")
        return 2

    chain = args.chain.strip().lower()
    print(f"[导入] CSV: {csv_path}")
    print(f"[导入] 链: {chain}")
    print(f"[导入] 模式: {'正式写入' if args.apply else 'dry-run（仅预览）'}")
    print(f"[导入] 置信度: {args.confidence}")
    print()

    # 1. 解析 CSV
    print("[1/3] 解析 CSV...")
    records = parse_bscscan_csv(csv_path, chain)
    print(f"  解析出 {len(records)} 条唯一地址标签")
    ex_count = sum(1 for r in records if r["is_exchange"])
    print(f"  其中交易所类: {ex_count}，其他类: {len(records) - ex_count}")

    # 打印前 10 条样例
    print(f"\n  前 10 条样例：")
    for r in records[:10]:
        tag = "exchange" if r["is_exchange"] else "other"
        print(f"    {r['address']}  |  {r['label_text']}  |  {r['display_name']}  [{tag}]")

    # 2. 对比数据库
    print("\n[2/3] 对比数据库...")
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        diff = diff_against_db(conn, records, chain)

        print(f"  总解析数: {diff['total']}")
        print(f"  交易所类: {diff['exchange_count']}")
        print()
        print(f"  老表 (onchain_exchange_wallet):")
        print(f"    新增: {diff['old_table_new']} 条")
        print(f"    已存在: {diff['old_table_duplicate']} 条")
        print()
        print(f"  新表 (onchain_address_label):")
        print(f"    新增: {diff['new_table_new']} 条")
        print(f"    已存在: {diff['new_table_duplicate']} 条")

        print(f"\n  新增交易所分布（Top 20）：")
        for name, cnt in diff["exchange_breakdown"][:20]:
            print(f"    {name}: {cnt}")

        if not args.apply:
            print("\n[DRY-RUN] 未写入数据库。加 --apply 执行正式导入。")
            return 0

        # 3. 正式写入
        print("\n[3/3] 正式写入数据库...")
        result = apply_import(conn, records, chain, args.source, args.confidence)

        print(f"  onchain_exchange_wallet: 新增 {result['inserted_exchange']}，跳过 {result['skipped_exchange']}")
        print(f"  onchain_address_label:   新增 {result['inserted_label']}，跳过 {result['skipped_label']}")
        print(f"\n[OK] 导入完成")

        # 验证：写后统计
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT confidence, COUNT(*) as cnt
                FROM biz.onchain_exchange_wallet
                WHERE chain = %s
                GROUP BY confidence ORDER BY cnt DESC
            """, (chain,))
            ex_stats = cur.fetchall()
            cur.execute("""
                SELECT label_type, confidence, COUNT(*) as cnt
                FROM biz.onchain_address_label
                WHERE chain = %s
                GROUP BY label_type, confidence ORDER BY cnt DESC
            """, (chain,))
            lbl_stats = cur.fetchall()

        print(f"\n[验证] 导入后 {chain} 链统计：")
        print(f"  交易所地址表：")
        for r in ex_stats:
            print(f"    {r['confidence']}: {r['cnt']}")
        print(f"  通用标签表：")
        for r in lbl_stats:
            print(f"    {r['label_type']} / {r['confidence']}: {r['cnt']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
