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
from crypto_research.clients.tron_client import get_tron_client
from crypto_research.clients.ton_client import get_ton_client
from crypto_research.clients.sui_client import get_sui_client
from crypto_research.clients.aptos_client import get_aptos_client
from crypto_research.clients.address_label_resolver import AddressLabelResolver
from crypto_research.clients.explorer_label_fetcher import ExplorerLabelFetcher
from crypto_research.clients.label_enricher import LabelEnricher

# 支持 HTML 标签爬取的链（Etherscan 系列，服务端渲染，纯 HTTP 可达）
# 注意：bsc / arbitrum 被 Cloudflare 拦截，纯 requests 无法获取，暂不支持
ENRICH_SUPPORTED_CHAINS = frozenset({"eth", "base", "polygon"})
# 每处理多少个资产触发一次 enrich flush
ENRICH_FLUSH_EVERY_N_ASSETS = 10


# 大额转账阈值（美元）
LARGE_TRANSFER_THRESHOLD_USD = 50_000
# 小 Meme 放宽阈值（低于此市值的资产用更低下限，避免漏早期异动）
SMALL_MEME_THRESHOLD_USD = 5_000
# 市值分界线（低于此市值视为小 Meme，用 SMALL_MEME_THRESHOLD_USD）
SMALL_MEME_MCAP_FLOOR = 10_000_000  # $10M

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
    "tron": "tron",
    "trx": "tron",
    "ton": "ton",
    "the-open-network": "ton",
    "sui": "sui",
    "aptos": "aptos",
    "apt": "aptos",
}

# 大小写敏感链（地址不得 .lower()，否则会指向错误地址）
CASE_SENSITIVE_CHAINS = frozenset({"solana", "tron", "ton", "sui", "aptos"})
SUPPORTED_CHAINS = ("eth", "bsc", "solana", "polygon", "arbitrum", "base", "optimism", "avalanche",
                    "tron", "ton", "sui", "aptos")

# P2-2: 空/零地址集合（EVM 零地址 + 常见全 0 变体）
_ZERO_ADDRESSES = frozenset({
    "0x0", "0x00", "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
})

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
                       m.chain, m.contract_address, a.market_cap
                FROM core.asset a
                INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
                WHERE a.asset_id = %s AND a.status = 'active'
            """, (asset_id,))
        else:
            cur.execute("""
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       m.chain, m.contract_address, a.market_cap
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
    """获取指定链的交易所钱包地址 -> 交易所名称映射（仅 high 置信度参与净流标签）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT LOWER(address) AS address, exchange_name
            FROM biz.onchain_exchange_wallet
            WHERE chain = %s
              AND confidence = 'high'
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


def get_asset_supply_decimals(conn, asset_id: int, chain: str, contract_address: str) -> dict:
    """查询资产的权威供应量与合约精度（用于金额量纲 sanity check，P0-1）。

    返回 {total_supply, circulating_supply, decimals}。
    supply 优先取 core.asset（CMC 同步权威源），缺则用 biz.asset_tokenomics 兜底；
    decimals 优先 core.asset_contract.decimals（RPC 实测精度），缺则 18。
    """
    out = {"total_supply": None, "circulating_supply": None, "decimals": None}
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT a.total_supply, a.circulating_supply, c.decimals
                FROM core.asset a
                LEFT JOIN core.asset_contract c
                       ON LOWER(c.contract_address) = LOWER(%s)
                WHERE a.asset_id = %s
                LIMIT 1
            """, (contract_address, asset_id))
            row = cur.fetchone()
            if row:
                out["total_supply"] = float(row["total_supply"]) if row.get("total_supply") else None
                out["circulating_supply"] = float(row["circulating_supply"]) if row.get("circulating_supply") else None
                out["decimals"] = int(row["decimals"]) if row.get("decimals") is not None else None
    except Exception:
        pass
    return out


def _resolve_block_timestamp(client, client_type: str, block_number: int) -> int | None:
    """按区块号反查真实区块时间戳（Unix 秒）。失败返回 None。"""
    if not block_number or block_number <= 0:
        return None
    try:
        # EVM RPC 客户端：eth_getBlockByNumber
        if client_type in ("rpc", "explorer") and hasattr(client, "get_block_timestamp"):
            return client.get_block_timestamp(block_number)
        # 其他客户端（etherscan 等）暂不支持按区块号反查时间
    except Exception:
        return None
    return None


def _resolve_timestamp(client, client_type: str, block_number: int, raw_ts: int) -> datetime | None:
    """解析转账时间戳（P0-2）。

    - raw_ts>0 且年份 >= 2015 → 直接使用（返回时间）
    - raw_ts 无效或年份 < 2015（脏时间）→ 尝试按 block_number 反查真实区块时间
    - 无 raw_ts（RPC 模式）→ 按 12s/block 估算，年份 < 2015 视为脏，尝试反查
    - 反查失败且无合理估算 → 返回 None（调用方跳过，脏时间不进表）
    """
    def _safe_from_ts(ts: int) -> datetime | None:
        if not ts or ts <= 0:
            return None
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt if dt.year >= 2015 else None
        except (OverflowError, OSError, ValueError):
            return None

    dt = _safe_from_ts(raw_ts)
    if dt:
        return dt

    # 有区块号：优先反查真实时间
    if block_number and block_number > 0:
        real_ts = _resolve_block_timestamp(client, client_type, block_number)
        if real_ts:
            dt = _safe_from_ts(real_ts)
            if dt:
                return dt

    # 无时间戳且无法反查：用 12s/block 估算兜底（仅接受合理年份）
    if raw_ts <= 0 and block_number and block_number > 0:
        estimated_ts = int(time.time()) - max(0, _get_latest_block_approx(client, client_type) - block_number) * 12
        dt = _safe_from_ts(estimated_ts)
        if dt:
            return dt

    return None


def save_transfers(conn, transfers: list[dict]) -> int:
    """批量保存转账记录。

    ON CONFLICT 时更新交易所标签列 + 新标签数组列，
    使补全地址标签库后重跑能回填旧行的标签。
    """
    written = 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for t in transfers:
            try:
                cur.execute("""
                    INSERT INTO biz.onchain_transfer_log (
                        asset_id, chain, contract_address, tx_hash,
                        from_address, to_address, value, value_usd,
                        from_label, to_label, from_exchange, to_exchange,
                        block_number, block_timestamp, is_to_exchange,
                        is_suspect, threshold_used,
                        from_labels, to_labels, from_label_names, to_label_names
                    ) VALUES (
                        %(asset_id)s, %(chain)s, %(contract_address)s, %(tx_hash)s,
                        %(from_address)s, %(to_address)s, %(value)s, %(value_usd)s,
                        %(from_label)s, %(to_label)s, %(from_exchange)s, %(to_exchange)s,
                        %(block_number)s, %(block_timestamp)s, %(is_to_exchange)s,
                        %(is_suspect)s, %(threshold_used)s,
                        %(from_labels)s, %(to_labels)s, %(from_label_names)s, %(to_label_names)s
                    )
                    ON CONFLICT (chain, tx_hash, contract_address, from_address, to_address) DO UPDATE SET
                        value = EXCLUDED.value,
                        value_usd = EXCLUDED.value_usd,
                        from_label = EXCLUDED.from_label,
                        to_label = EXCLUDED.to_label,
                        from_exchange = EXCLUDED.from_exchange,
                        to_exchange = EXCLUDED.to_exchange,
                        is_to_exchange = EXCLUDED.is_to_exchange,
                        block_number = EXCLUDED.block_number,
                        block_timestamp = EXCLUDED.block_timestamp,
                        is_suspect = EXCLUDED.is_suspect,
                        threshold_used = EXCLUDED.threshold_used,
                        from_labels = EXCLUDED.from_labels,
                        to_labels = EXCLUDED.to_labels,
                        from_label_names = EXCLUDED.from_label_names,
                        to_label_names = EXCLUDED.to_label_names
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
    market_cap: float | None = None,
    label_resolver=None,
    label_enricher=None,
) -> dict:
    """采集单个资产的大额转账。

    alarm_only=True: 存储双向大额转账（保证 netflow 计算完整），
    但仅对转入交易所的记录标记告警关注。
    client_type: 'explorer' / 'etherscan' / 'rpc'，影响时间戳等字段处理。
    三者均通过 client.get_token_transfers(...) 拉取，返回字段归一化一致。
    label_resolver: AddressLabelResolver 实例（可选）。传入后会批量解析地址标签，
    填充 from_labels / to_labels / from_label_names / to_label_names 数组列。
    label_enricher: LabelEnricher 实例（可选）。传入后会收集无标签的陌生地址，
    供后续批量去区块浏览器查询并沉淀到地址标签库。
    """
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

    # P0-1: 权威 supply/decimals（一次查好，用于金额量纲 sanity check）
    supply_dec = get_asset_supply_decimals(conn, asset_id, chain, contract_address)
    supply_cap = supply_dec["total_supply"] or supply_dec["circulating_supply"]

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
                # P2-1: 原始值 <= 0 无意义，跳过
                value_raw = float(tx.get("value", 0))
                if value_raw <= 0:
                    continue

                # P0-1: decimals 兜底（源缺失/为 0 时用 DB 权威精度，再兜底 18）
                decimals = int(tx.get("tokenDecimal") or 0)
                if decimals <= 0 or decimals > 36:
                    decimals = supply_dec["decimals"] or 18
                value = value_raw / (10 ** decimals)

                # P0-1: 金额量纲 sanity check：value 远超流通量（>5×）→ 标 is_suspect
                is_suspect = False
                if supply_cap and supply_cap > 0 and value > supply_cap * 5:
                    is_suspect = True
                    print(f"[suspect] {symbol}/{chain} tx={tx.get('hash', '')[:12]} "
                          f"value={value:.4g} > supply×5({supply_cap:.4g}) → 标脏", file=sys.stderr)

                # 估算美元价值（P1-1: 价格缺失 → value_usd=None → 直接不入库）
                value_usd = round(value * price_usd, 2) if price_usd and price_usd > 0 else None

                # P1-2: INF/NaN 脏数据过滤（价格为 inf/nan 或 value 异常时会产生）
                if value_usd is not None:
                    import math
                    if math.isinf(value_usd) or math.isnan(value_usd):
                        print(f"[dirty-value] {symbol}/{chain} tx={tx.get('hash', '')[:12]} "
                              f"value_usd={value_usd} → 跳过", file=sys.stderr)
                        continue

                # 动态阈值：小市值资产用更低下限（Plan A）
                threshold = LARGE_TRANSFER_THRESHOLD_USD
                if market_cap and market_cap > 0 and market_cap < SMALL_MEME_MCAP_FLOOR:
                    threshold = SMALL_MEME_THRESHOLD_USD

                # P1-1: value_usd 缺失（价格缺失）或低于阈值 → 不当"大额"入库
                if value_usd is None or value_usd < threshold:
                    continue

                from_addr = (tx.get("from", "") or "")
                to_addr = (tx.get("to", "") or "")
                # EVM 链统一小写，大小写敏感链保留原样
                if chain not in CASE_SENSITIVE_CHAINS:
                    from_addr = from_addr.lower()
                    to_addr = to_addr.lower()

                # P2-2: 空/零地址过滤
                if (not from_addr or not to_addr
                        or from_addr in _ZERO_ADDRESSES or to_addr in _ZERO_ADDRESSES):
                    continue

                from_exchange = exchange_map.get(from_addr)
                to_exchange = exchange_map.get(to_addr)

                from_label = "exchange" if from_exchange else "unknown"
                to_label = "exchange" if to_exchange else "unknown"

                is_to_exchange = to_exchange is not None

                # P0-2: 时间戳处理（年份 < 2015 视为脏，反查区块时间，失败则跳过）
                block_num = int(tx.get("blockNumber", 0))
                block_ts = _resolve_timestamp(
                    client, client_type, block_num,
                    int(tx.get("timeStamp", 0)),
                )
                if block_ts is None:
                    print(f"[dirty-ts] {symbol}/{chain} tx={tx.get('hash', '')[:12]} "
                          f"脏时间戳且无法反查 → 跳过", file=sys.stderr)
                    continue

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
                    "block_number": block_num,
                    "block_timestamp": block_ts,
                    "is_to_exchange": is_to_exchange,
                    "is_suspect": is_suspect,
                    "threshold_used": threshold,
                })
            except (ValueError, TypeError):
                continue

        if len(transfers) < 100:
            break

    # ── 批量解析地址标签（数组列） ──
    if label_resolver is not None and all_transfers:
        # 收集所有出现过的地址，批量查询
        all_addrs = set()
        for t in all_transfers:
            all_addrs.add(t["from_address"])
            all_addrs.add(t["to_address"])
        label_resolver.resolve_batch(list(all_addrs))

        # 回填每条记录的标签数组
        for t in all_transfers:
            from_info = label_resolver.resolve(t["from_address"])
            to_info = label_resolver.resolve(t["to_address"])
            t["from_labels"] = from_info["types"] or None
            t["to_labels"] = to_info["types"] or None
            t["from_label_names"] = from_info["names"] or None
            t["to_label_names"] = to_info["names"] or None
    else:
        # 无 resolver：数组列留空（老行为）
        for t in all_transfers:
            t["from_labels"] = None
            t["to_labels"] = None
            t["from_label_names"] = None
            t["to_label_names"] = None

    # ── 收集无标签地址，供旁路 enrich（不阻塞主流程） ──
    if label_enricher is not None and all_transfers:
        unlabeled_addrs = set()
        for t in all_transfers:
            if not t.get("from_labels"):
                unlabeled_addrs.add(t["from_address"])
            if not t.get("to_labels"):
                unlabeled_addrs.add(t["to_address"])
        if unlabeled_addrs:
            label_enricher.collect(list(unlabeled_addrs))

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


def _build_chain_sources(source: str, chain: str = "eth") -> list[str]:
    """按 --source 展开为该链尝试数据源的降级顺序。

    explorer: 免 Key 免费源优先，公共 RPC 兜底。
    etherscan: 付费 Key 主源，公共 RPC 兜底。
    rpc: 仅公共 RPC。
    auto: explorer → etherscan(若有有效Key) → rpc。

    非 EVM 链（solana/tron/ton/sui/aptos）只有单一数据源，直接返回对应类型。
    """
    if chain in ("solana", "tron", "ton", "sui", "aptos"):
        return [chain]  # 这些链只有一种数据源类型，由 _init_chain_client 直接处理
    if source == "explorer":
        return ["explorer", "rpc"]
    if source == "etherscan":
        return ["etherscan", "rpc"]
    if source == "rpc":
        return ["rpc"]
    return ["explorer", "etherscan", "rpc"]  # auto


def _init_chain_client(chain: str, source: str, settings=None):
    """按单个数据源类型初始化客户端，返回 (client, client_type)。

    client_type ∈ {"explorer", "etherscan", "rpc", "helius", "trongrid", "toncenter", "sui_rpc", "aptos_rpc"}。
    client 为 None 表示该类型无可用数据源（如 etherscan 未配置 Key）。
    """
    # Solana 统一走 Helius RPC（无论 --source 选啥，转账/持仓均走 Helius）
    if chain == "solana":
        return get_solana_client(settings.helius_api_key if settings else None), "helius"
    # Tron 统一走 TronGrid API
    if chain == "tron":
        return get_tron_client(settings.trongrid_api_key if settings else None), "trongrid"
    # TON 统一走 TON Center API
    if chain == "ton":
        return get_ton_client(settings.toncenter_api_key if settings else None), "toncenter"
    # Sui 统一走 Sui 公共 RPC
    if chain == "sui":
        return get_sui_client(), "sui_rpc"
    # Aptos 统一走 Aptos 公共全节点
    if chain == "aptos":
        return get_aptos_client(), "aptos_rpc"
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
    elif client_type == "trongrid":
        msg = "使用 TronGrid API（Tron 链，免费档）"
    elif client_type == "toncenter":
        msg = "使用 TON Center API（TON 链，免费档）"
    elif client_type == "sui_rpc":
        msg = "使用 Sui 公共 RPC（Sui 链，免费免 Key）"
    elif client_type == "aptos_rpc":
        msg = "使用 Aptos 全节点 API（Aptos 链，免费免 Key）"
    else:  # explorer
        msg = "使用 Ethplorer/Binplorer 免 Key 免费源（默认主链路）"
    print(f"  [{chain}] {msg}")


def main():
    parser = argparse.ArgumentParser(description="链上大额转账监控")
    parser.add_argument("--asset-id", type=int, default=None, help="指定资产 ID")
    parser.add_argument("--chain", type=str, default=None, help="指定链（eth/bsc/solana/polygon/arbitrum/base/optimism/avalanche/tron/ton/sui/aptos）")
    parser.add_argument("--limit", type=int, default=0, help="单轮最大处理资产数 (0=不限量)")
    parser.add_argument("--offset", type=int, default=0, help="资产列表起始偏移（自动循环分批扫描用）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写入")
    parser.add_argument("--alarm-only", action="store_true",
                        help="告警模式：存储双向大额转账（保证 netflow 完整），仅标记转入交易所的为告警关注")
    parser.add_argument("--source", type=str, default="explorer",
                        choices=["auto", "explorer", "etherscan", "rpc"],
                        help="转账数据源：explorer=免Key免费源(默认)；etherscan=需付费Key；"
                             "rpc=公共RPC兜底；auto=explorer→etherscan→rpc 自动降级")
    parser.add_argument("--enrich", action="store_true",
                        help="开启地址标签旁路富化：遇到无标签地址时自动去区块浏览器查询并沉淀到地址标签库"
                             "（仅支持 eth/bsc/arbitrum/base/polygon，需注意爬取频率）")
    parser.add_argument("--enrich-delay", type=float, default=2.0,
                        help="富化爬取的请求间隔秒数（默认 2 秒，礼貌限速）")
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
        chain_resolvers = {}   # chain -> AddressLabelResolver
        chain_enrichers = {}   # chain -> LabelEnricher（仅 --enrich 且支持的链）
        total_enriched = {"fetched": 0, "inserted": 0, "backfilled": 0}

        total_processed = 0
        total_written = 0
        total_large = 0
        t0 = time.time()

        for i, asset in enumerate(assets, 1):
            chain = asset["chain"]

            # 该链尚未锁定数据源：按降级链依次尝试，锁定第一个能返回数据的源。
            # lock_result 非空表示本次已为该资产采集过，避免重复调用 API。
            lock_result = None
            # 非 EVM 链走本地行情库查价（日级行情→CMC快照→市值推算，零 API 配额）
            # 替代原 CoinGecko 按合约查价（CG 配额有限且 429 频发）
            price_usd = (
                get_asset_price(conn, asset["asset_id"], asset["canonical_symbol"])
                if chain in ("solana", "tron", "ton", "sui", "aptos") else None
            )
            if chain not in chain_clients:
                exchanges = None
                resolver = None
                enricher = None
                # 非 EVM 链用自己的源列表，EVM 链用通用 chain_sources
                sources = _build_chain_sources(args.source, chain)
                for stype in sources:
                    client = _init_chain_client(chain, stype, settings)[0]
                    if not client:
                        continue
                    if exchanges is None:
                        exchanges = get_exchange_map(conn, chain)
                        resolver = AddressLabelResolver(conn, chain)
                        # 初始化 enricher（仅 --enrich 且支持的链）
                        if args.enrich and chain in ENRICH_SUPPORTED_CHAINS:
                            try:
                                proxy = getattr(settings, 'https_proxy', None) or getattr(settings, 'http_proxy', None)
                                fetcher = ExplorerLabelFetcher(
                                    chain=chain, proxy=proxy, delay=args.enrich_delay)
                                enricher = LabelEnricher(
                                    conn, chain, fetcher=fetcher, resolver=resolver,
                                    dry_run=args.dry_run)
                                print(f"  [{chain}] ✨ 已开启地址标签旁路富化")
                            except Exception as e:
                                print(f"  [{chain}] ⚠️  富化初始化失败，跳过: {e}")
                                enricher = None
                    result = collect_transfers(
                        conn, client, asset, exchanges,
                        dry_run=args.dry_run,
                        alarm_only=args.alarm_only,
                        client_type=stype,
                        price_usd=price_usd,
                        market_cap=asset.get("market_cap"),
                        label_resolver=resolver,
                        label_enricher=enricher,
                    )
                    if result.get("processed", 0) > 0:
                        chain_clients[chain] = (client, stype)
                        chain_exchanges[chain] = exchanges
                        chain_resolvers[chain] = resolver
                        if enricher:
                            chain_enrichers[chain] = enricher
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
            resolver = chain_resolvers.get(chain)
            enricher = chain_enrichers.get(chain)

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
                    market_cap=asset.get("market_cap"),
                    label_resolver=resolver,
                    label_enricher=enricher,
                )

            total_processed += result.get("processed", 0)
            total_written += result.get("written", 0)
            total_large += result.get("large", 0)

            # ── 每 N 个资产触发一次 enrich flush（避免堆积太多） ──
            if args.enrich and i % ENRICH_FLUSH_EVERY_N_ASSETS == 0 and chain_enrichers:
                for en_chain, en in chain_enrichers.items():
                    stats = en.flush()
                    if stats["fetched"] > 0:
                        total_enriched["fetched"] += stats["fetched"]
                        total_enriched["inserted"] += stats["inserted"]
                        total_enriched["backfilled"] += stats["backfilled"]
                        print(f"  ✨ [{en_chain}] enrich: 新增标签 {stats['fetched']} 条, "
                              f"入库 {stats['inserted']} 条, 回填转账 {stats['backfilled']} 条")

        # ── 最终 flush：把剩余待富化地址处理完 ──
        if args.enrich and chain_enrichers:
            print("\n── 最终地址标签富化 ──")
            for en_chain, en in chain_enrichers.items():
                stats = en.flush()
                if stats["fetched"] > 0 or stats["collected"] > 0:
                    total_enriched["fetched"] += stats["fetched"]
                    total_enriched["inserted"] += stats["inserted"]
                    total_enriched["backfilled"] += stats["backfilled"]
                    print(f"  [{en_chain}] 本次收集 {stats['collected']} 个陌生地址, "
                          f"查到标签 {stats['fetched']} 条, "
                          f"入库 {stats['inserted']} 条, 回填转账 {stats['backfilled']} 条")

        elapsed = time.time() - t0
        label = "告警" if args.alarm_only else "大额"
        written_note = "" if args.dry_run else f", 写入 {total_written} 条"
        enrich_note = ""
        if args.enrich and total_enriched["fetched"] > 0:
            enrich_note = f", 富化新增标签 {total_enriched['fetched']} 条"
        print(f"\n完成: 处理 {total_processed} 条转账, {label} {total_large} 条{written_note}{enrich_note}, 耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()