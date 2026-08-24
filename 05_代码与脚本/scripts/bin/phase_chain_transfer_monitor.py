"""
Phase 1: 大额转账监控。
从 Etherscan/BSCScan API 拉取代币大额转账，标记转入交易所的潜在砸盘信号。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import psycopg
import psycopg.rows

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.etherscan_client import EtherscanClient, get_client
from crypto_research.clients.rpc_client import get_rpc_client
from crypto_research.clients.ethplorer_client import get_ethplorer_client
from crypto_research.clients.solana_client import get_solana_client
from crypto_research.clients.coingecko_client import CoinGeckoClient


# 大额转账阈值（美元）
LARGE_TRANSFER_THRESHOLD_USD = 50_000

# asset_contract_map 表中的链名（全称）-> 数据源客户端使用的内部短名
CHAIN_NAME_MAP = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "binance-smart-chain": "bsc",
    "solana": "solana",
    "sol": "solana",
    "polygon": "polygon",
    "matic": "polygon",
    "matic-network": "polygon",
    "arbitrum": "arbitrum",
    "arbitrum-one": "arbitrum",
    "base": "base",
    "optimism": "optimism",
    "op": "optimism",
    "avalanche": "avalanche",
    "avax": "avalanche",
    "avalanche-c-chain": "avalanche",
}

# 当前支持监控的链
SUPPORTED_CHAINS = ("eth", "bsc", "solana", "polygon", "arbitrum", "base", "optimism", "avalanche")

# 热门代币的参考价格（美元），用于粗略估算
# 实际使用时可通过 CoinGecko API 获取实时价格
FALLBACK_PRICES = {
    "eth": 2000.0,
    "weth": 2000.0,
    "usdt": 1.0,
    "usdc": 1.0,
    "dai": 1.0,
    "busd": 1.0,
    "wbnb": 300.0,
    "bnb": 300.0,
    "cake": 2.0,
    "uni": 5.0,
    "link": 15.0,
    "aave": 100.0,
    "matic": 0.5,
    "pol": 0.5,
}


def get_asset_contracts(conn, asset_id: int | None = None) -> list[dict]:
    """获取需要监控转账的资产及其合约地址。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if asset_id:
            cur.execute("""
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       m.chain, m.contract_address
                FROM core.asset a
                INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
                WHERE a.asset_id = %s AND a.status = 'active'
            """, (asset_id,))
        else:
            cur.execute("""
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       m.chain, m.contract_address
                FROM core.asset a
                INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
                WHERE a.status = 'active'
                ORDER BY COALESCE(a.market_cap, 0) DESC, a.asset_id
            """)
        return [dict(r) for r in cur.fetchall()]


def get_asset_price(conn, asset_id: int, symbol: str) -> float:
    """从数据库获取代币最新价格（多源 fallback）。

    优先级：
    1. biz.asset_market_daily 最新日收盘价（最准确）
    2. src_cmc.cmc_asset_quote_snapshot 最新快照
    3. core.asset.market_cap / circulating_supply 推算
    4. FALLBACK_PRICES 硬编码（兜底）
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 1. 日级行情表
        cur.execute("""
            SELECT price_usd FROM biz.asset_market_daily
            WHERE asset_id = %s AND price_usd IS NOT NULL
            ORDER BY market_date DESC LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if row and row["price_usd"]:
            return float(row["price_usd"])

        # 2. CMC 快照表
        cur.execute("""
            SELECT cqs.price_usd
            FROM src_cmc.cmc_asset_quote_snapshot cqs
            INNER JOIN biz.coin_basic cb ON cb.cmc_id = cqs.cmc_id
            WHERE cb.asset_id = %s AND cqs.price_usd IS NOT NULL
            ORDER BY cqs.quote_time DESC LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if row and row["price_usd"]:
            return float(row["price_usd"])

        # 3. core.asset 市值/流通量推算
        cur.execute("""
            SELECT market_cap, circulating_supply
            FROM core.asset WHERE asset_id = %s
        """, (asset_id,))
        row = cur.fetchone()
        if row and row["market_cap"] and row["circulating_supply"] and float(row["circulating_supply"]) > 0:
            return float(row["market_cap"]) / float(row["circulating_supply"])

    # 4. 硬编码兜底
    return FALLBACK_PRICES.get(symbol.lower(), 0.0)


def get_exchange_map(conn, chain: str) -> dict[str, str]:
    """获取指定链的交易所钱包地址 -> 交易所名称映射。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT LOWER(address) AS address, exchange_name
            FROM biz.onchain_exchange_wallet
            WHERE chain = %s
        """, (chain,))
        return {r["address"]: r["exchange_name"] for r in cur.fetchall()}


def get_last_block(conn, chain: str, contract_address: str) -> int:
    """获取上次扫描到的区块号。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT MAX(block_number) AS last_block
            FROM biz.onchain_transfer_log
            WHERE chain = %s AND contract_address = %s
        """, (chain, contract_address))
        row = cur.fetchone()
        return row["last_block"] or 0 if row else 0


def save_transfers(conn, transfers: list[dict]) -> int:
    """批量保存转账记录。"""
    written = 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for t in transfers:
            try:
                cur.execute("""
                    INSERT INTO biz.onchain_transfer_log (
                        asset_id, chain, contract_address, tx_hash,
                        from_address, to_address, value, value_usd,
                        from_label, to_label, from_exchange, to_exchange,
                        block_number, block_timestamp, is_to_exchange
                    ) VALUES (
                        %(asset_id)s, %(chain)s, %(contract_address)s, %(tx_hash)s,
                        %(from_address)s, %(to_address)s, %(value)s, %(value_usd)s,
                        %(from_label)s, %(to_label)s, %(from_exchange)s, %(to_exchange)s,
                        %(block_number)s, %(block_timestamp)s, %(is_to_exchange)s
                    )
                    ON CONFLICT (chain, tx_hash, contract_address, from_address, to_address) DO NOTHING
                """, t)
                if cur.rowcount:
                    written += 1
            except Exception:
                continue
    conn.commit()
    return written


_latest_block_cache: dict[str, tuple[float, int]] = {}


def _get_latest_block_approx(client, client_type: str) -> int:
    """获取最新区块号（带缓存，避免每次都查）。"""
    cache_key = client_type
    cached = _latest_block_cache.get(cache_key)
    if cached and time.time() - cached[0] < 60:  # 缓存 60 秒
        return cached[1]

    block_num = 0
    try:
        if client_type == "rpc" and hasattr(client, "get_block_number"):
            block_num = client.get_block_number()
        else:
            # Etherscan 模式下用 eth_blockNumber 也可以，但我们直接用估算
            block_num = 20000000  # 粗略值，不影响大额判断
    except Exception:
        block_num = 20000000

    _latest_block_cache[cache_key] = (time.time(), block_num)
    return block_num


def collect_transfers(
    conn,
    client,
    asset: dict,
    exchange_map: dict[str, str],
    dry_run: bool = False,
    alarm_only: bool = False,
    client_type: str = "explorer",
    price_usd: float | None = None,
) -> dict:
    """采集单个资产的大额转账。

    alarm_only=True: 存储双向大额转账（保证 netflow 计算完整），
    但仅对转入交易所的记录标记告警关注。
    client_type: 'explorer' / 'etherscan' / 'rpc'，影响时间戳等字段处理。
    三者均通过 client.get_token_transfers(...) 拉取，返回字段归一化一致。"""
    asset_id = asset["asset_id"]
    symbol = asset["canonical_symbol"]
    chain = asset["chain"]
    contract_address = asset["contract_address"]

    if not contract_address:
        return {"asset_id": asset_id, "symbol": symbol, "processed": 0, "written": 0}

    # 获取上次扫描到的区块号，从该区块之后开始扫描
    last_block = get_last_block(conn, chain, contract_address)

    # 获取代币价格（从数据库多源 fallback，最后用硬编码兜底）
    price_usd = get_asset_price(conn, asset_id, symbol)

    all_transfers = []
    total_processed = 0
    seen_raw = set()          # 已见过的 tx_hash
    overlap_pages = 0         # 连续完全重叠的页数

    # 分页拉取转账记录
    for page in range(1, 11):  # 最多 10 页 = 上限 1000 条转账
        transfers = client.get_token_transfers(
            contract_address,
            page=page,
            offset=100,
            sort="desc",
            start_block=last_block + 1 if last_block > 0 else 0,
        )
        if not transfers:
            break
        # 高频币（如 USDT）的分页按"操作"而非"交易"，会出现整页都是已见过的重复，
        # 且呈现"重叠→前进→重叠"交替。仅当连续 2 页完全重叠（真正到历史末尾）才停止，
        # 避免误停漏掉后续新数据，也避免无谓翻页。
        if all(t.get("hash") in seen_raw for t in transfers):
            overlap_pages += 1
            if overlap_pages >= 2:
                break
            continue
        overlap_pages = 0
        for t in transfers:
            seen_raw.add(t.get("hash"))

        total_processed += len(transfers)

        for tx in transfers:
            try:
                value_raw = float(tx.get("value", 0))
                decimals = int(tx.get("tokenDecimal", 18))
                value = value_raw / (10 ** decimals)

                # 估算美元价值
                value_usd = round(value * price_usd, 2) if price_usd > 0 else None

                # 过滤：只保留大额转账
                if value_usd is not None and value_usd < LARGE_TRANSFER_THRESHOLD_USD:
                    continue

                from_addr = (tx.get("from", "") or "").lower()
                to_addr = (tx.get("to", "") or "").lower()

                from_exchange = exchange_map.get(from_addr)
                to_exchange = exchange_map.get(to_addr)

                from_label = "exchange" if from_exchange else "unknown"
                to_label = "exchange" if to_exchange else "unknown"

                is_to_exchange = to_exchange is not None

                block_ts_raw = int(tx.get("timeStamp", 0))
                if block_ts_raw > 0:
                    block_ts = datetime.fromtimestamp(block_ts_raw, tz=timezone.utc)
                else:
                    # RPC 模式下日志不含时间戳，用区块号估算（按 12s/block）
                    block_num = int(tx.get("blockNumber", 0))
                    estimated_ts = int(time.time()) - max(0, _get_latest_block_approx(client, client_type) - block_num) * 12
                    block_ts = datetime.fromtimestamp(estimated_ts, tz=timezone.utc)

                all_transfers.append({
                    "asset_id": asset_id,
                    "chain": chain,
                    "contract_address": contract_address,
                    "tx_hash": tx.get("hash", ""),
                    "from_address": from_addr,
                    "to_address": to_addr,
                    "value": value,
                    "value_usd": value_usd,
                    "from_label": from_label,
                    "to_label": to_label,
                    "from_exchange": from_exchange,
                    "to_exchange": to_exchange,
                    "block_number": int(tx.get("blockNumber", 0)),
                    "block_timestamp": block_ts,
                    "is_to_exchange": is_to_exchange,
                })
            except (ValueError, TypeError):
                continue

        if len(transfers) < 100:
            break

    if dry_run:
        large_count = len(all_transfers)
        to_exchange_count = sum(1 for t in all_transfers if t["is_to_exchange"])
        label = "告警" if alarm_only else "大额"
        print(f"  [{symbol}] {chain}: {total_processed} 条转账, {large_count} 条{label}"
              f"（其中 {to_exchange_count} 条转入交易所） (dry-run)")
        return {"asset_id": asset_id, "symbol": symbol, "processed": total_processed,
                "written": 0, "large": large_count}

    written = save_transfers(conn, all_transfers)
    to_exchange_count = sum(1 for t in all_transfers if t["is_to_exchange"])
    label = "告警" if alarm_only else "大额"
    print(f"  [{symbol}] {chain}: {total_processed} 条转账, {to_exchange_count} 条{label}, 写入 {written} 条")

    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "processed": total_processed,
        "written": written,
        "large": sum(1 for t in all_transfers if t["is_to_exchange"]) if alarm_only
                 else len(all_transfers),
    }


def _build_chain_sources(source: str) -> list[str]:
    """按 --source 展开为该链尝试数据源的降级顺序。

    explorer: 免 Key 免费源优先，公共 RPC 兜底。
    etherscan: 付费 Key 主源，公共 RPC 兜底。
    rpc: 仅公共 RPC。
    auto: explorer → etherscan(若有有效Key) → rpc。
    """
    if source == "explorer":
        return ["explorer", "rpc"]
    if source == "etherscan":
        return ["etherscan", "rpc"]
    if source == "rpc":
        return ["rpc"]
    return ["explorer", "etherscan", "rpc"]  # auto


def _init_chain_client(chain: str, source: str, settings=None):
    """按单个数据源类型初始化客户端，返回 (client, client_type)。

    client_type ∈ {"explorer", "etherscan", "rpc", "helius"}。
    client 为 None 表示该类型无可用数据源（如 etherscan 未配置 Key）。
    """
    # Solana 统一走 Helius RPC（无论 --source 选啥，转账/持仓均走 Helius）
    if chain == "solana":
        return get_solana_client(settings.helius_api_key if settings else None), "helius"
    if source == "rpc":
        return get_rpc_client(chain), "rpc"
    if source == "etherscan":
        return get_client(chain), "etherscan"
    # explorer（免 Key 免费源，无需付费 Key，默认主链路）
    return get_ethplorer_client(chain), "explorer"


def _print_source_banner(chain: str, client_type: str) -> None:
    """打印当前链实际采用的数据源横幅。"""
    if client_type == "rpc":
        msg = "使用公共 RPC 节点（免 API Key，最终兜底）"
    elif client_type == "etherscan":
        msg = "使用 Etherscan API（需付费 Key）"
    elif client_type == "helius":
        msg = "使用 Helius RPC（Solana 链，免费档）"
    else:  # explorer
        msg = "使用 Ethplorer/Binplorer 免 Key 免费源（默认主链路）"
    print(f"  [{chain}] {msg}")


def _get_solana_price_usd(settings, mint: str) -> float:
    """通过 CoinGecko 按合约地址查询 Solana 代币 USD 价格（失败回退 0）。"""
    try:
        cg = CoinGeckoClient(settings)
        data = cg.get_token_price("solana", [mint])
        price = data.get(mint, {}).get("usd")
        return float(price) if price else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def main():
    parser = argparse.ArgumentParser(description="链上大额转账监控")
    parser.add_argument("--asset-id", type=int, default=None, help="指定资产 ID")
    parser.add_argument("--chain", type=str, default=None, help="指定链（eth/bsc/solana/polygon/arbitrum/base/optimism/avalanche）")
    parser.add_argument("--limit", type=int, default=50, help="单轮最大处理资产数")
    parser.add_argument("--offset", type=int, default=0, help="资产列表起始偏移（自动循环分批扫描用）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写入")
    parser.add_argument("--alarm-only", action="store_true", help="告警模式：只存储转入交易所的大额转账")
    parser.add_argument("--source", type=str, default="explorer",
                        choices=["auto", "explorer", "etherscan", "rpc"],
                        help="转账数据源：explorer=免Key免费源(默认)；etherscan=需付费Key；"
                             "rpc=公共RPC兜底；auto=explorer→etherscan→rpc 自动降级")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        assets = get_asset_contracts(conn, args.asset_id)

        # 归一化链名（asset_contract_map 用 'ethereum' 等全称），并过滤暂不支持的链
        for a in assets:
            a["chain"] = CHAIN_NAME_MAP.get(a["chain"], a["chain"])
        before = len(assets)
        assets = [a for a in assets if a["chain"] in SUPPORTED_CHAINS]
        if before - len(assets) > 0:
            print(f"（跳过 {before - len(assets)} 个暂不支持的链资产，当前支持 {', '.join(SUPPORTED_CHAINS)}）")

        if args.chain:
            assets = [a for a in assets if a["chain"] == args.chain]
        if args.limit > 0:
            # 自动循环分批：从 offset 起取 limit 个资产；未指定 offset 则从头取
            if args.offset > 0:
                assets = assets[args.offset:args.offset + args.limit]
            else:
                assets = assets[:args.limit]

        print(f"共 {len(assets)} 个资产待监控\n")

        chain_sources = _build_chain_sources(args.source)
        chain_clients = {}     # chain -> (client, client_type)，首个成功返回数据的源
        chain_exchanges = {}

        total_processed = 0
        total_written = 0
        total_large = 0
        t0 = time.time()

        for i, asset in enumerate(assets, 1):
            chain = asset["chain"]

            # 该链尚未锁定数据源：按降级链依次尝试，锁定第一个能返回数据的源。
            # lock_result 非空表示本次已为该资产采集过，避免重复调用 API。
            lock_result = None
            # Solana 走 CoinGecko 按合约查价（用于大额转账 USD 估值）
            price_usd = (
                _get_solana_price_usd(settings, asset["contract_address"])
                if chain == "solana" else None
            )
            if chain not in chain_clients:
                exchanges = None
                for stype in chain_sources:
                    client = _init_chain_client(chain, stype, settings)[0]
                    if not client:
                        continue
                    if exchanges is None:
                        exchanges = get_exchange_map(conn, chain)
                    result = collect_transfers(
                        conn, client, asset, exchanges,
                        dry_run=args.dry_run,
                        alarm_only=args.alarm_only,
                        client_type=stype,
                        price_usd=price_usd,
                    )
                    if result.get("processed", 0) > 0:
                        chain_clients[chain] = (client, stype)
                        chain_exchanges[chain] = exchanges
                        _print_source_banner(chain, stype)
                        lock_result = result
                        break
                    # 该源无返回（如免费源该代币近期无转账 / Etherscan Key 失效），尝试下一源
                    print(f"  [{chain}] {stype} 无返回，尝试下一数据源")
                if chain not in chain_clients:
                    print(f"  [{i}/{len(assets)}] 跳过 {chain}: 所有数据源均无返回")
                    continue

            client, client_type = chain_clients[chain]
            exchanges = chain_exchanges[chain]

            if lock_result is not None:
                # 锁定数据源时已经为该资产采集过，直接复用
                result = lock_result
            else:
                result = collect_transfers(
                    conn, client, asset, exchanges,
                    dry_run=args.dry_run,
                    alarm_only=args.alarm_only,
                    client_type=client_type,
                    price_usd=price_usd,
                )

            total_processed += result.get("processed", 0)
            total_written += result.get("written", 0)
            total_large += result.get("large", 0)

        elapsed = time.time() - t0
        label = "告警" if args.alarm_only else "大额"
        written_note = "" if args.dry_run else f", 写入 {total_written} 条"
        print(f"\n完成: 处理 {total_processed} 条转账, {label} {total_large} 条{written_note}, 耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()