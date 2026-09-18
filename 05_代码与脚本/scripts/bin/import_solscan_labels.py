"""Solscan 地址标签 Excel 导入脚本。

用法：
    python bin/import_solscan_labels.py --file /path/to/solscan-xxx.xlsx
    python bin/import_solscan_labels.py --file /path/to/solscan-xxx.xlsx --dry-run
    python bin/import_solscan_labels.py --file /path/to/solscan-xxx.xlsx --chain solana

Excel 格式（Solscan 页面复制导出）：
    第 1 列: 地址详情页 URL（含地址）
    第 2 列: 标签文本（如 "Binance 2"、"KuCoin Hot Wallet"）
    第 3 列: 地址

功能：
    - 自动去重
    - 自动分类标签类型（exchange / dex / market_maker / mev_bot / smart_money）
    - 写入 biz.onchain_address_label
    - 交易所同时写入 biz.onchain_exchange_wallet
    - 回填 biz.onchain_transfer_log 的标签列
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, 'src')

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

ENRICH_CONFIDENCE = "high"
ENRICH_SOURCE = "solscan_manual"

# 高价值标签类型白名单
ALLOWED_LABEL_TYPES = {"exchange", "smart_money", "whale", "mev_bot", "market_maker", "dex"}

# 交易所名归一化（与 explorer_label_fetcher.py 对齐）
EXCHANGE_NAME_MAP = {
    "binance": "Binance", "binance us": "Binance US", "binanceus": "Binance US",
    "coinbase": "Coinbase", "coinbase prime": "Coinbase Prime",
    "okx": "OKX", "okex": "OKX", "kraken": "Kraken", "bybit": "Bybit",
    "kucoin": "KuCoin",
    "gate.io": "Gate.io", "gateio": "Gate.io",
    "huobi": "Huobi", "htx": "HTX", "bitfinex": "Bitfinex", "bitget": "Bitget",
    "mexc": "MEXC", "crypto.com": "Crypto.com", "cryptocom": "Crypto.com",
    "upbit": "Upbit", "bithumb": "Bithumb", "gemini": "Gemini", "bitstamp": "Bitstamp",
    "poloniex": "Poloniex", "deribit": "Deribit", "bitmart": "BitMart", "lbank": "LBank",
    "xt.com": "XT.COM", "xtcom": "XT.COM", "bittrex": "Bittrex", "bitmex": "BitMEX",
    "korbit": "Korbit", "coinone": "Coinone", "ftx": "FTX", "hotbit": "Hotbit",
    "paxos": "Paxos", "circle": "Circle",
}

# DEX / 流动性池关键词
DEX_KEYWORDS = [
    "orca", "raydium", "jupiter", "serum", "pump.fun", "pumpfun",
    "dex", "amm", "liquidity pool", " lp", "lp ", "pool", "vault",
]

# 做市商关键词
MM_KEYWORDS = [
    "market maker", " mm ", " mm", "mm ", "jump ", "jump crypto", "wintermute",
    "gfx", "cumberland", "dv chain", "wintemute",
]

# MEV / 机器人关键词
MEV_KEYWORDS = [
    "mev", "bot", "arbitrage", "sniper", "snipe",
]

# Smart Money / KOL 关键词
SMART_MONEY_KEYWORDS = [
    "smart money", "smart_money", "whale", "kol", "vc ", " vc", "fund",
]


def classify_label(label_text: str) -> tuple[str, str | None]:
    """分类标签文本，返回 (label_type, normalized_name)。

    normalized_name 仅 exchange 类型有值。
    """
    l = label_text.lower().strip()

    # 1. 先判断交易所（匹配归一化表中的关键词）
    for key, normalized in EXCHANGE_NAME_MAP.items():
        if key in l:
            return "exchange", normalized

    # 2. DEX / 流动性池
    for kw in DEX_KEYWORDS:
        if kw in l:
            return "dex", None

    # 3. 做市商
    for kw in MM_KEYWORDS:
        if kw in l:
            return "market_maker", None

    # 4. MEV / 机器人
    for kw in MEV_KEYWORDS:
        if kw in l:
            return "mev_bot", None

    # 5. Smart Money / 巨鲸
    for kw in SMART_MONEY_KEYWORDS:
        if kw in l:
            return "smart_money", None

    # 6. 默认：如果含有 wallet/exchange/hot/cold 等词但没匹配上具体交易所，
    #    仍然归为 exchange（可能是小众交易所）
    if any(k in l for k in ["exchange", "hot wallet", "cold wallet", "wallet"]):
        return "exchange", None

    return "other", None


def extract_display_name(label_text: str, label_type: str, normalized_name: str | None) -> str:
    """从标签文本提取显示名。"""
    if label_type == "exchange" and normalized_name:
        return normalized_name
    # 去掉尾部的地址缩写（如 "(BmFdp)"）
    name = re.sub(r'\s*\([A-Za-z0-9]{3,8}\)\s*$', '', label_text).strip()
    # 去掉尾部的数字编号（如 " 2", " 3"）—— 但保留完整名字
    # 比如 "Binance 2" → 显示为 "Binance 2"（保留编号，区分不同钱包）
    return name or label_text


def parse_excel(file_path: str) -> list[dict]:
    """解析 Solscan 导出的 Excel，返回 [{address, label_text, label_type, display_name, normalized_name, is_exchange}]"""
    import pandas as pd

    df = pd.read_excel(file_path)
    cols = list(df.columns)
    print(f"  原始数据: {len(df)} 行, {len(cols)} 列")
    print(f"  列名: {cols}")

    # Solscan 导出的列名是 CSS class，我们按位置识别：
    #   第 1 列: URL
    #   第 2 列: 标签文本
    #   第 3 列: 地址
    if len(cols) < 3:
        raise ValueError(f"Excel 至少需要 3 列，实际只有 {len(cols)} 列")

    url_col, label_col, addr_col = cols[0], cols[1], cols[2]

    results = []
    seen = set()  # 去重

    for _, row in df.iterrows():
        addr = str(row[addr_col]).strip()
        label = str(row[label_col]).strip()

        # 跳过空值
        if not addr or addr == "nan" or not label or label == "nan":
            continue
        # 跳过明显不是地址的
        if len(addr) < 32 or len(addr) > 48:
            continue

        key = (addr, label)
        if key in seen:
            continue
        seen.add(key)

        label_type, normalized_name = classify_label(label)
        display_name = extract_display_name(label, label_type, normalized_name)
        is_exchange = label_type == "exchange"

        results.append({
            "address": addr,
            "label_text": label,
            "label_type": label_type,
            "display_name": display_name,
            "normalized_name": normalized_name,
            "is_exchange": is_exchange,
        })

    return results


def import_to_db(conn, labels: list[dict], chain: str = "solana",
                 dry_run: bool = False) -> dict:
    """导入标签到数据库。"""
    # 过滤高价值标签
    high_value = [l for l in labels if l["label_type"] in ALLOWED_LABEL_TYPES]
    skipped = len(labels) - len(high_value)
    if skipped > 0:
        print(f"  跳过 {skipped} 个非高价值标签")

    if not high_value:
        return {"total": 0, "inserted": 0, "exchange_inserted": 0, "backfilled": 0}

    if dry_run:
        print(f"\n  [dry-run] 将导入 {len(high_value)} 个标签：")
        type_counts: dict[str, int] = {}
        for l in high_value:
            t = l["label_type"]
            type_counts[t] = type_counts.get(t, 0) + 1
            print(f"    [{l['label_type']}] {l['address'][:20]}... → {l['display_name']}")
        print(f"\n  类型统计: {type_counts}")
        return {"total": len(high_value), "inserted": 0, "exchange_inserted": 0, "backfilled": 0}

    # 1. 写入 onchain_address_label
    inserted = 0
    with conn.cursor() as cur:
        for l in high_value:
            raw_meta = json.dumps({
                "original_label": l["label_text"],
                "source": ENRICH_SOURCE,
                "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            cur.execute("""
                INSERT INTO biz.onchain_address_label
                    (address, chain, label_type, label_name, display_name,
                     confidence, source, raw_meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
            """, (
                l["address"], chain, l["label_type"],
                l["display_name"], l["display_name"],
                ENRICH_CONFIDENCE, ENRICH_SOURCE, raw_meta,
            ))
            if cur.rowcount:
                inserted += 1

        # 2. 交易所也写入 onchain_exchange_wallet
        exchange_inserted = 0
        for l in high_value:
            if not l["is_exchange"]:
                continue
            ex_name = l["normalized_name"] or l["display_name"]
            cur.execute("""
                INSERT INTO biz.onchain_exchange_wallet
                    (address, exchange_name, chain, label, confidence, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (address, chain) DO UPDATE
                SET confidence = 'high',
                    exchange_name = EXCLUDED.exchange_name,
                    label = CASE
                        WHEN biz.onchain_exchange_wallet.label IS NULL
                             OR biz.onchain_exchange_wallet.label = ''
                        THEN EXCLUDED.label
                        ELSE biz.onchain_exchange_wallet.label
                    END,
                    source = COALESCE(NULLIF(biz.onchain_exchange_wallet.source, ''), '')
                               || ';solscan_manual'
                WHERE biz.onchain_exchange_wallet.confidence != 'high'
            """, (
                l["address"], ex_name, chain,
                l["label_text"], ENRICH_CONFIDENCE, ENRICH_SOURCE,
            ))
            if cur.rowcount:
                exchange_inserted += 1

    conn.commit()
    print(f"  ✅ 写入 onchain_address_label: {inserted} 条新标签")
    print(f"  ✅ 写入 onchain_exchange_wallet: {exchange_inserted} 条")

    # 3. 回填转账记录
    backfilled = 0
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE tmp_solscan_labels (
                address TEXT PRIMARY KEY,
                label_types TEXT[],
                label_names TEXT[]
            ) ON COMMIT DROP
        """)

        # 按地址聚合标签类型和名称（一个地址可能有多条标签记录）
        addr_labels: dict[str, tuple[list[str], list[str]]] = {}
        for l in high_value:
            if l["address"] not in addr_labels:
                addr_labels[l["address"]] = ([], [])
            types, names = addr_labels[l["address"]]
            if l["label_type"] not in types:
                types.append(l["label_type"])
            if l["display_name"] not in names:
                names.append(l["display_name"])

        rows = [
            (addr, types, names)
            for addr, (types, names) in addr_labels.items()
        ]
        cur.executemany("""
            INSERT INTO tmp_solscan_labels (address, label_types, label_names)
            VALUES (%s, %s, %s)
        """, rows)

        # 更新发件方
        cur.execute("""
            UPDATE biz.onchain_transfer_log t
            SET from_labels = e.label_types,
                from_label_names = e.label_names
            FROM tmp_solscan_labels e
            WHERE t.chain = %s
              AND t.from_address = e.address
              AND (t.from_labels IS NULL OR t.from_labels = ARRAY['unknown']::TEXT[])
        """, (chain,))
        from_up = cur.rowcount

        # 更新收件方
        cur.execute("""
            UPDATE biz.onchain_transfer_log t
            SET to_labels = e.label_types,
                to_label_names = e.label_names
            FROM tmp_solscan_labels e
            WHERE t.chain = %s
              AND t.to_address = e.address
              AND (t.to_labels IS NULL OR t.to_labels = ARRAY['unknown']::TEXT[])
        """, (chain,))
        to_up = cur.rowcount

        backfilled = from_up + to_up
        conn.commit()
        print(f"  ✅ 回填转账记录：{backfilled} 条（from: {from_up}, to: {to_up}）")

    return {
        "total": len(high_value),
        "inserted": inserted,
        "exchange_inserted": exchange_inserted,
        "backfilled": backfilled,
    }


def main():
    parser = argparse.ArgumentParser(description="Solscan 地址标签 Excel 导入")
    parser.add_argument("--file", type=str, required=True, help="Excel 文件路径")
    parser.add_argument("--chain", type=str, default="solana")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        sys.exit(1)

    settings = get_settings(require_database=True)

    print("=" * 60)
    print(f"Solscan 标签导入")
    print(f"  文件: {file_path.name}")
    print(f"  链: {args.chain}")
    print("=" * 60)
    print()

    # 1. 解析 Excel
    print("[1/3] 解析 Excel...")
    labels = parse_excel(str(file_path))
    print(f"  解析到 {len(labels)} 个唯一标签")

    # 类型统计
    type_counts: dict[str, int] = {}
    for l in labels:
        t = l["label_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"  类型分布: {dict(sorted(type_counts.items(), key=lambda x: -x[1]))}")
    print()

    # 2. 导入数据库
    print("[2/3] 写入数据库...")
    with get_connection(settings.database_url) as conn:
        stats = import_to_db(conn, labels, args.chain, args.dry_run)

    print()
    print("[3/3] 完成")
    print(f"  总标签数: {stats['total']}")
    if not args.dry_run:
        print(f"  新写入: {stats['inserted']}")
        print(f"  交易所钱包: {stats['exchange_inserted']}")
        print(f"  回填转账: {stats['backfilled']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
