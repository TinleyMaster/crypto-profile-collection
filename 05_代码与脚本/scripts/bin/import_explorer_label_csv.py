#!/usr/bin/env python3
"""
区块浏览器标签页 CSV 导入工具
=============================

用途：从 Etherscan/BaseScan/PolygonScan 等区块浏览器标签页导出的 CSV 文件，
批量导入地址标签到 biz.onchain_address_label 表。

使用场景：
- Playwright 绕 Cloudflare 不稳定时，手动从标签页下载 CSV，用这个脚本导入
- 浏览器插件导出的 CSV 也能用（格式兼容）

CSV 格式（Etherscan / BaseScan / PolygonScan 标签页通用）：
  第 0 列: 地址链接（https://.../address/0x...）
  第 1 列: 地址前缀（0x...）
  第 2 列: 地址后缀（...）
  第 3 列: Name Tag （部分行可能在第 6 列，因为列错位）
  第 4 列: 余额
  ...
  标签名规律: "交易所名: xxx" 或 "交易所名 Dep: xxx" 等

支持的链: eth / base / polygon / bsc / arbitrum / optimism / avalanche

用法:
  # 导入单个 CSV
  python scripts/bin/import_explorer_label_csv.py --chain eth --exchange Kraken --csv /path/to/etherscan-Kraken.csv

  # 导入目录下所有 CSV（文件名格式: {site}-{label}-{date}.csv）
  python scripts/bin/import_explorer_label_csv.py --chain eth --dir /path/to/csvs

  # dry-run，只预览不写入
  python scripts/bin/import_explorer_label_csv.py --chain eth --exchange Kraken --csv xxx.csv --dry-run
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

# 把 src 目录加到 sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# ─── 标签分类规则（与 explorer_label_fetcher.py 保持一致） ──────────────────

# 交易所关键词 → 标准化名称
EXCHANGE_NAME_MAP: dict[str, str] = {
    "binance": "Binance",
    "okx": "OKX",
    "okex": "OKX",
    "bybit": "Bybit",
    "coinbase": "Coinbase",
    "kraken": "Kraken",
    "huobi": "Huobi",
    "gate.io": "Gate.io",
    "gateio": "Gate.io",
    "mexc": "MEXC",
    "kucoin": "KuCoin",
    "bitget": "Bitget",
    "bitfinex": "Bitfinex",
    "upbit": "Upbit",
    "bithumb": "Bithumb",
    "gemini": "Gemini",
    "ftx": "FTX",
    "crypto.com": "Crypto.com",
    "cryptocom": "Crypto.com",
    "bitstamp": "Bitstamp",
    "poloniex": "Poloniex",
    "cex.io": "CEX.io",
    "deribit": "Deribit",
    "dydx": "dYdX",
    "woo": "WOO X",
    "bitmex": "BitMEX",
    "coinex": "CoinEx",
    "lbank": "LBank",
    "htx": "HTX",
}

# 非交易所高价值标签（关键词 → 类型）
NON_EXCHANGE_LABEL_TYPES: list[tuple[str, str]] = [
    # smart_money
    ("smart money", "smart_money"),
    ("smartmoney", "smart_money"),
    ("whale", "whale"),
    # MEV
    ("mev", "mev_bot"),
    ("searcher", "mev_bot"),
    ("flashbot", "mev_bot"),
    # 做市商
    ("market maker", "market_maker"),
    ("marketmaker", "market_maker"),
    ("wintermute", "market_maker"),
    ("jump trading", "market_maker"),
    ("gts", "market_maker"),
    ("kairon", "market_maker"),
    ("wizard", "market_maker"),
    # DEX / LP
    ("liquidity pool", "dex"),
    ("liquidity provider", "dex"),
    ("lp token", "dex"),
    ("uniswap", "dex"),
    ("sushiswap", "dex"),
    ("pancakeswap", "dex"),
    # 跨链桥
    ("bridge", "bridge"),
    # 项目方/基金会
    ("foundation", "project_team"),
    ("treasury", "project_team"),
    ("team", "project_team"),
]


def classify_label(label_text: str) -> str | None:
    """分类标签，返回 label_type；不匹配高价值类型返回 None（不存）。"""
    if not label_text:
        return None
    low = label_text.lower().strip()

    # 1. 交易所匹配
    for kw in EXCHANGE_NAME_MAP:
        if kw in low:
            return "exchange"

    # 2. 非交易所高价值标签匹配
    for kw, label_type in NON_EXCHANGE_LABEL_TYPES:
        if kw in low:
            return label_type

    # 3. 不存
    return None


# ─── 地址提取 ───────────────────────────────────────────────────────────

def extract_address(row: list[str]) -> str | None:
    """从 CSV 行里提取完整的 0x 地址。

    优先从第 0 列（URL）提取，最靠谱。
    """
    for col in row:
        if not col:
            continue
        m = re.search(r'0x[a-fA-F0-9]{40}', col)
        if m:
            return m.group(0).lower()
    return None


def extract_name_tag(row: list[str]) -> str | None:
    """从 CSV 行里提取 Name Tag。

    Etherscan 标签页 CSV 列有时候会错位（地址列数量不定），
    所以扫一遍所有列，找看起来像标签的内容。
    """
    # 跳过纯地址/纯数字/纯链接的列
    for col in row:
        if not col or not col.strip():
            continue
        col = col.strip()
        # 跳过地址（0x 开头的十六进制）
        if re.match(r'^0x[a-fA-F0-9]+$', col):
            continue
        # 跳过纯十六进制短串（地址后缀，如 E87734040）
        if re.match(r'^[a-fA-F0-9]{5,20}$', col):
            continue
        # 跳过链接
        if col.startswith('http'):
            continue
        # 跳过纯数字（余额、tx 数等）
        if re.match(r'^[\d.,]+\s*(ETH|BNB|MATIC|AVAX|ETH\.|ARB|OP|SOL|TRX)?$', col, re.I):
            continue
        # 有实际内容的就是标签名
        if len(col) > 2 and not col.startswith('$'):
            return col
    return None


# ─── 主逻辑 ──────────────────────────────────────────────────────────────

def import_csv(csv_path: Path, chain: str, exchange_hint: str,
               dry_run: bool = False) -> dict:
    """导入单个 CSV 文件。"""
    results = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader, None)  # 跳过表头

        for i, row in enumerate(reader, 2):
            address = extract_address(row)
            if not address:
                continue

            name_tag = extract_name_tag(row) or exchange_hint
            label_type = classify_label(name_tag)

            # 只存高价值类型，不匹配的跳过（避免 other 类噪声）
            if not label_type:
                continue

            results.append({
                "address": address,
                "name_tag": name_tag,
                "label_type": label_type,
            })

    # 按地址去重（同一个地址可能有多行）
    seen = set()
    unique = []
    for r in results:
        if r["address"] not in seen:
            seen.add(r["address"])
            unique.append(r)

    print(f"  文件: {csv_path.name}")
    print(f"  提取地址: {len(unique)} 条（去重后）")

    # 统计类型分布
    type_counts = {}
    for r in unique:
        t = r["label_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"  类型分布: {type_counts}")

    if dry_run or not unique:
        return {"total": len(unique), "inserted": 0, "type_counts": type_counts}

    # 写入数据库
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        inserted = 0
        for r in unique:
            raw_meta = json.dumps({"csv_file": csv_path.name}, ensure_ascii=False)
            # ON CONFLICT 跳过重复
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO biz.onchain_address_label
                        (address, chain, label_type, label_name,
                         confidence, source, raw_meta, created_at)
                    VALUES (%s, %s, %s, %s, 'high', 'explorer_label_page', %s, NOW())
                    ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
                    RETURNING address
                    """,
                    (r["address"], chain, r["label_type"], r["name_tag"], raw_meta),
                )
                if cur.fetchone():
                    inserted += 1
        conn.commit()

    print(f"  新增入库: {inserted} 条")
    return {"total": len(unique), "inserted": inserted, "type_counts": type_counts}


def import_dir(dir_path: Path, chain: str, dry_run: bool = False) -> dict:
    """导入目录下所有 CSV 文件。

    文件名约定: {browser}-{label}-{date}.csv，比如 etherscan-Kraken-2026-09-10.csv
    第二个字段作为 exchange_hint / 标签名。
    """
    csv_files = sorted(dir_path.glob("*.csv"))
    if not csv_files:
        print(f"  目录 {dir_path} 下没有 CSV 文件")
        return {"total": 0, "inserted": 0}

    print(f"找到 {len(csv_files)} 个 CSV 文件\n")

    total = 0
    total_inserted = 0

    for i, f in enumerate(csv_files, 1):
        # 从文件名提取标签名: etherscan-Kraken-2026-09-10.csv → Kraken
        name_parts = f.stem.split("-")
        exchange_hint = name_parts[1] if len(name_parts) >= 2 else f.stem

        print(f"[{i}/{len(csv_files)}] {f.name}")
        result = import_csv(f, chain, exchange_hint, dry_run=dry_run)
        total += result["total"]
        total_inserted += result.get("inserted", 0)
        print()

    print(f"{'─'*50}")
    print(f"总计: {total} 条地址，新增 {total_inserted} 条")
    return {"total": total, "inserted": total_inserted}


# ─── 入口 ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="区块浏览器标签页 CSV 导入工具")
    parser.add_argument("--chain", required=True,
                        help="链名: eth / base / polygon / bsc / arbitrum / optimism / avalanche")
    parser.add_argument("--csv", default="",
                        help="单个 CSV 文件路径")
    parser.add_argument("--dir", default="",
                        help="CSV 目录路径（批量导入）")
    parser.add_argument("--exchange", default="",
                        help="交易所/标签名提示（单文件导入时用，默认从文件名提取）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览不写入")
    args = parser.parse_args()

    if not args.csv and not args.dir:
        print("错误: 请指定 --csv 或 --dir")
        sys.exit(1)

    print(f"链: {args.chain}")
    print(f"模式: {'dry-run（只预览）' if args.dry_run else '正式写入'}\n")

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"错误: 文件不存在 {csv_path}")
            sys.exit(1)
        exchange_hint = args.exchange
        if not exchange_hint:
            # 从文件名提取
            parts = csv_path.stem.split("-")
            exchange_hint = parts[1] if len(parts) >= 2 else csv_path.stem
        import_csv(csv_path, args.chain, exchange_hint, dry_run=args.dry_run)

    elif args.dir:
        dir_path = Path(args.dir)
        if not dir_path.exists():
            print(f"错误: 目录不存在 {dir_path}")
            sys.exit(1)
        import_dir(dir_path, args.chain, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
