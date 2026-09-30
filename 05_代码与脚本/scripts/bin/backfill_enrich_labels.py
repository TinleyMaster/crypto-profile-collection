"""地址标签批量富化 — 本地回填脚本。

从转账记录表中捞出没有标签的陌生地址，去区块浏览器爬取标签，
写入 onchain_address_label 并回填 onchain_transfer_log。

设计目的：服务器 IP 被 Cloudflare 拦截，无法在服务器上实时爬取，
因此在本地（IP 正常）批量跑，把结果写回数据库。

支持增量：只查 onchain_transfer_log 中出现过、但 onchain_address_label 中没有的地址。
幂等：已有标签的地址跳过，可重复执行。
支持并发：ThreadPoolExecutor 多线程并发爬取，默认 5 线程。

用法：
    # 预览（不爬取、不写入，只看有多少陌生地址）
    python backfill_enrich_labels.py --dry-run --chain eth

    # 实际执行（默认 eth 链，5 并发，每次最多 100 个地址）
    python backfill_enrich_labels.py --chain eth --limit 100

    # 高并发跑全量（10 线程，注意别太猛被封）
    python backfill_enrich_labels.py --chain eth --limit 0 --concurrency 10

    # 多条链一起跑
    python backfill_enrich_labels.py --chain eth,base,polygon --limit 200
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.explorer_label_fetcher import ExplorerLabelFetcher
from crypto_research.clients.label_enricher import LabelEnricher, ENRICH_SOURCE, ALLOWED_LABEL_TYPES
from crypto_research.clients.address_label_resolver import AddressLabelResolver

# 支持 HTML 爬取的链（Etherscan 系列）
ENRICH_SUPPORTED_CHAINS = {"eth", "base", "polygon"}
# 大小写敏感链
CASE_SENSITIVE_CHAINS = {"solana", "tron", "ton", "sui", "aptos"}
# 爬取尝试表名
FETCH_ATTEMPT_TABLE = "biz.onchain_label_fetch_attempt"

# 免 key 公共 JSON-RPC（用于批量判定 EOA/合约），已用真实合约+EOA 地址交叉验证
# 支持 JSON-RPC batch：N 个地址只发 1 次 HTTP 请求，代价远低于逐个调 Etherscan API
PUBLIC_RPC = {
    # 每个备用端点都已用「已知 EOA + 已知合约」交叉验证过 getCode 判定正确。
    # 注意：cloudflare-eth.com 对 batch 请求静默返回无 result 的错误体
    # （实测判定结果 ['?','?']），会伪装成「unknown」而保留全部地址，已剔除。
    # ankr 不支持 JSON-RPC batch，llamarpc 常返 525，均不可用。
    "eth": ["https://ethereum-rpc.publicnode.com",
            "https://eth-mainnet.public.blastapi.io"],
    "base": ["https://base-rpc.publicnode.com",
             "https://base-mainnet.public.blastapi.io"],
    "polygon": ["https://polygon-bor-rpc.publicnode.com",
                "https://polygon.rpc.thirdweb.com"],
}


def ensure_attempt_table(conn) -> None:
    """确保爬取尝试记录表存在。"""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {FETCH_ATTEMPT_TABLE} (
                address TEXT NOT NULL,
                chain TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'explorer_html',
                status TEXT NOT NULL,
                attempt_count INT NOT NULL DEFAULT 1,
                last_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY (address, chain, source)
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_label_attempt_chain_status
            ON {FETCH_ATTEMPT_TABLE} (chain, status)
        """)
    conn.commit()


def get_unlabeled_addresses(conn, chain: str, limit: int, skip_attempted: bool = True,
                            min_count: int = 1, since_hours: float = 0) -> list[str]:
    """从转账记录中捞出没有地址标签的地址（按出现频次倒序，优先爬高频地址）。

    只查 from_address / to_address 中出现过、但 onchain_address_label 中没有的地址。
    skip_attempted=True 时，跳过已经爬过（无论成功失败）的地址。
    min_count>1 时，只保留出现次数 >= min_count 的地址（过滤一次性散户长尾）。
    since_hours>0 时只统计最近 N 小时出现过的地址（热跑口径，避免陈年长尾占坑）；
    0 = 不限窗口（每日全量口径）。
    """
    case_sensitive = chain in CASE_SENSITIVE_CHAINS

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if case_sensitive:
            addr_col_from = "from_address"
            addr_col_to = "to_address"
            compare = "="
        else:
            addr_col_from = "LOWER(from_address)"
            addr_col_to = "LOWER(to_address)"
            compare = "="

        # 增量窗口：热跑时只捞最近 N 小时的新转账地址，防止和陈年长尾互相占坑
        since_clause = ""
        since_params: list = []
        if since_hours and since_hours > 0:
            since_clause = "AND block_timestamp >= NOW() - (%s * INTERVAL '1 hour')"
            since_params = [since_hours]

        # 已尝试过的地址（无标签/失败的都算）
        attempt_clause = ""
        params = [chain, *since_params, chain, *since_params, chain, chain]
        if skip_attempted:
            attempt_clause = f"""
                AND NOT EXISTS (
                    SELECT 1 FROM {FETCH_ATTEMPT_TABLE} fa
                    WHERE {'LOWER(fa.address)' if not case_sensitive else 'fa.address'} {compare}
                          {'LOWER(ac.addr)' if not case_sensitive else 'ac.addr'}
                      AND fa.chain = %s
                )
            """
            params.append(chain)

        min_clause = ""
        if min_count > 1:
            min_clause = "AND ac.cnt >= %s"
            params.append(min_count)

        params.append(limit)

        cur.execute(f"""
            WITH all_addrs AS (
                SELECT {addr_col_from} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s
                  AND from_address IS NOT NULL
                  {since_clause}
                UNION ALL
                SELECT {addr_col_to} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s
                  AND to_address IS NOT NULL
                  {since_clause}
            ),
            addr_counts AS (
                SELECT addr, COUNT(*) AS cnt
                FROM all_addrs
                WHERE addr IS NOT NULL AND addr <> ''
                GROUP BY addr
            ),
            labeled AS (
                SELECT address AS addr
                FROM biz.onchain_address_label
                WHERE chain = %s
                UNION
                SELECT address AS addr
                FROM biz.onchain_exchange_wallet
                WHERE chain = %s
            )
            SELECT ac.addr, ac.cnt
            FROM addr_counts ac
            LEFT JOIN labeled l
              ON {'LOWER(ac.addr)' if not case_sensitive else 'ac.addr'} {compare}
                 {'LOWER(l.addr)' if not case_sensitive else 'l.addr'}
            WHERE l.addr IS NULL
              {attempt_clause}
              {min_clause}
            ORDER BY ac.cnt DESC
            LIMIT %s
        """, tuple(params))

        rows = cur.fetchall()

    return [r["addr"] for r in rows]


def count_total_unlabeled(conn, chain: str, skip_attempted: bool = True,
                          min_count: int = 1, since_hours: float = 0) -> int:
    """估算总共有多少个无标签地址（用于预览）。

    与 get_unlabeled_addresses 保持同一口径：支持 skip_attempted / min_count / since_hours。
    """
    case_sensitive = chain in CASE_SENSITIVE_CHAINS

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if case_sensitive:
            addr_col_from = "from_address"
            addr_col_to = "to_address"
            compare = "="
        else:
            addr_col_from = "LOWER(from_address)"
            addr_col_to = "LOWER(to_address)"
            compare = "="

        since_clause = ""
        since_params: list = []
        if since_hours and since_hours > 0:
            since_clause = "AND block_timestamp >= NOW() - (%s * INTERVAL '1 hour')"
            since_params = [since_hours]

        attempt_clause = ""
        params = [chain, *since_params, chain, *since_params, chain, chain]
        if skip_attempted:
            attempt_clause = f"""
                AND NOT EXISTS (
                    SELECT 1 FROM {FETCH_ATTEMPT_TABLE} fa
                    WHERE LOWER(fa.address) = LOWER(ac.addr)
                      AND fa.chain = %s
                )
            """
            params.append(chain)

        min_clause = ""
        if min_count > 1:
            min_clause = "AND ac.cnt >= %s"
            params.append(min_count)

        cur.execute(f"""
            WITH all_addrs AS (
                -- 必须 UNION ALL（保留重复），否则 addr_counts.cnt 恒为 1，
                -- min_count 过滤会退化成「永远 0 条」（与 get_unlabeled_addresses 同口径）
                SELECT {addr_col_from} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND from_address IS NOT NULL
                  {since_clause}
                UNION ALL
                SELECT {addr_col_to} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND to_address IS NOT NULL
                  {since_clause}
            ),
            addr_counts AS (
                SELECT addr, COUNT(*) AS cnt
                FROM all_addrs
                WHERE addr IS NOT NULL AND addr <> ''
                GROUP BY addr
            ),
            labeled AS (
                SELECT LOWER(address) AS addr
                FROM biz.onchain_address_label
                WHERE chain = %s
                UNION
                SELECT LOWER(address) AS addr
                FROM biz.onchain_exchange_wallet
                WHERE chain = %s
            )
            SELECT COUNT(*) AS cnt
            FROM addr_counts ac
            LEFT JOIN labeled l ON LOWER(ac.addr) = LOWER(l.addr)
            WHERE l.addr IS NULL
              {attempt_clause}
              {min_clause}
        """, tuple(params))

        row = cur.fetchone()
    return row["cnt"] if row else 0


def count_medium_addresses(conn, chain: str, skip_attempted: bool = True,
                           source: str | None = None) -> int:
    """统计该链上「置信度 medium（待验证）」的地址数。

    口径与 get_medium_addresses 完全一致：
      - confidence = 'medium'
      - 同一 (address, chain) 上还没有 high 记录（已有 high 的不用再验证）
      - skip_attempted=True 时排除已爬过（attempt 表有记录）的，保证幂等
    """
    with conn.cursor() as cur:
        sql = f"""
            SELECT COUNT(*)
            FROM biz.onchain_address_label m
            WHERE m.chain = %s
              AND m.confidence = 'medium'
              AND NOT EXISTS (
                    SELECT 1 FROM biz.onchain_address_label h
                    WHERE h.chain = m.chain
                      AND LOWER(h.address) = LOWER(m.address)
                      AND h.confidence = 'high'
              )
        """
        params: list = [chain]
        if source:
            sql += " AND m.source = %s"
            params.append(source)
        if skip_attempted:
            sql += f"""
              AND NOT EXISTS (
                    SELECT 1 FROM {FETCH_ATTEMPT_TABLE} fa
                    WHERE fa.chain = m.chain
                      AND LOWER(fa.address) = LOWER(m.address)
              )
            """
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    return row[0] if row else 0


def get_medium_addresses(conn, chain: str, limit: int, skip_attempted: bool = True,
                         source: str | None = None) -> list[str]:
    """捞出该链上 confidence='medium' 的待验证地址（跨链传播副本为主）。

    为什么需要它：evm_propagate 把 A 链的 high 标签复制到其他 EVM 链，
    副本置信度是 medium。这些地址在原脚本里永远不会被爬 —— 富化只捞
    「onchain_address_label 里没有」的地址，而副本已经有标签了。
    结果是 19k 条 medium 从未被区块浏览器真实验证过。

    本函数把它们捞出来送进爬取队列，命中即原地升级为 high（见 _write_batch）。

    source 可限定只验证某个来源（如 'evm_propagate'），None = 所有 medium。
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # SELECT 必须用 LOWER(m.address)：GROUP BY 的是表达式，
        # 直接选 m.address 会触发 "column must appear in the GROUP BY clause"。
        sql = f"""
            SELECT LOWER(m.address) AS address
            FROM biz.onchain_address_label m
            WHERE m.chain = %s
              AND m.confidence = 'medium'
              AND NOT EXISTS (
                    SELECT 1 FROM biz.onchain_address_label h
                    WHERE h.chain = m.chain
                      AND LOWER(h.address) = LOWER(m.address)
                      AND h.confidence = 'high'
              )
        """
        params: list = [chain]
        if source:
            sql += " AND m.source = %s"
            params.append(source)
        if skip_attempted:
            sql += f"""
              AND NOT EXISTS (
                    SELECT 1 FROM {FETCH_ATTEMPT_TABLE} fa
                    WHERE fa.chain = m.chain
                      AND LOWER(fa.address) = LOWER(m.address)
              )
            """
        # 同一地址在该链可能有多个 label_type/label_name 的 medium 行 → 去重
        sql += " GROUP BY LOWER(m.address) ORDER BY MIN(m.label_id)"
        if limit and limit > 0:
            sql += " LIMIT %s"
            params.append(limit)
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [r["address"] for r in rows]


def batch_filter_contracts(chain: str, addrs: list[str], batch_size: int = 100,
                           batch_delay: float = 0.25, max_retries: int = 2
                           ) -> tuple[list[str], list[str], dict[str, int]]:
    """批量剔除合约地址，只保留 EOA。

    原理：eth_getCode 返回 '0x' 即 EOA，返回字节码即合约。
    交易所热钱包必然是 EOA，代币合约/路由/金库都是合约 —— 后者爬 HTML 永远查不到
    交易所标签，属纯浪费请求，故前置剔除。

    用 JSON-RPC batch（100 地址/次 HTTP 请求）+ 免 key 公共节点，
    代价远低于逐个调 Etherscan 的 eth_getCode 代理接口。

    返回 (保留的 EOA 列表, 被剔除的合约列表, 统计字典)。
    判定失败的地址一律**保留**（保守，不误杀）。

    限流处理（2026-09-30）：免 key 公共节点对连续突发很敏感，16k 地址 161 批串行打过去
    会被 429 打到全线失败，结果是「全部保留」——合约过滤形同虚设，白爬 ~45% 的合约。
    故加 batch_delay 节流 + 失败退避重试；重试后仍失败的批次才保守全保留。
    """
    urls = PUBLIC_RPC.get(chain) or []
    stats = {"kept_eoa": 0, "dropped_contract": 0, "unknown": 0, "rpc_error": 0}
    dropped: list[str] = []
    if not urls or not addrs:
        stats["kept_eoa"] = len(addrs)
        if not urls:
            print(f"  ⚠️  {chain} 无可用公共 RPC，跳过合约过滤（全部保留）")
        return list(addrs), dropped, stats

    import json as _json
    import urllib.request

    # 显式禁用一切代理（P0，2026-09-30 实测踩坑）：
    # 启动器虽清了环境变量，但 Windows 上 urllib.urlopen 还会回落读
    # **注册表系统代理**（Clash 类工具开了系统代理就是 127.0.0.1:7890）。
    # 代理半死不活时每批 30s 超时 × 2 端点 × 2 重试，161 批能拖 5+ 小时
    # 且全程无日志输出（RPC 过滤在首条打印之前）。公共 RPC 节点必须直连。
    _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    kept: list[str] = []
    last_err = None
    for batch_no, start in enumerate(range(0, len(addrs), batch_size)):
        batch = addrs[start:start + batch_size]
        # 轮换首选端点：避免每批都先砸在同一个（可能正在限流的）节点上
        rot = batch_no % len(urls)
        ordered = urls[rot:] + urls[:rot]
        payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_getCode",
                    "params": [a, "latest"]} for i, a in enumerate(batch)]
        body = _json.dumps(payload).encode()

        by_id = None
        # 失败退避重试：公共节点突发 429 通常短暂，退避后重试成功率很高
        for attempt in range(max_retries):
            if attempt > 0:
                time.sleep(1.5 * attempt)   # 1.5s → 3s
            for url in ordered:              # 逐个端点兜底（已轮换起始位置）
                try:
                    req = urllib.request.Request(
                        url, data=body,
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "crypto-research/1.0"})
                    with _OPENER.open(req, timeout=30) as resp:
                        result = _json.loads(resp.read().decode())
                except Exception as e:
                    last_err = e
                    continue

                if not isinstance(result, list) or len(result) != len(batch):
                    last_err = ValueError("非 batch 响应")
                    continue

                # 关键：逐项校验 result 是否为非空字符串。
                # 实测 mainnet.base.org 返回 100 条响应但有效结果 0/100、
                # quiknode polygon 只有 20/100 有效 —— 这类「HTTP 200 但结果为空」
                # 若当成正常响应处理，空值会落到 else 分支被判成「合约」剔除，
                # 真实 EOA 会被误杀并以 status='contract' 入 attempt 表永久不再重爬。
                _map: dict[int, str | None] = {}
                _valid = 0
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    r = item.get("result")
                    if isinstance(r, str) and r:
                        _valid += 1
                    _map[item.get("id")] = r if isinstance(r, str) else None

                if _valid < len(batch) * 0.9:
                    last_err = ValueError(f"有效结果仅 {_valid}/{len(batch)}")
                    continue

                by_id = _map
                break
            if by_id is not None:
                break

        if batch_delay > 0:
            time.sleep(batch_delay)        # 节流，避免整批被 429

        # 每 20 批打一行进度：RPC 过滤在「合约过滤:」汇总行之前，
        # 不打中间进度的话几千地址会静默十几分钟，容易被误判卡死
        if (batch_no + 1) % 20 == 0:
            done = min(batch_no + 1, (len(addrs) + batch_size - 1) // batch_size)
            print(f"  RPC 判定进度: {min((batch_no + 1) * batch_size, len(addrs))}/{len(addrs)}")

        if by_id is None:
            # 整批判定失败 → 保守全保留，交给后续 HTML 爬取
            kept.extend(batch)
            stats["rpc_error"] += len(batch)
            print(f"  ⚠️  RPC 判定失败({last_err})，本批 {len(batch)} 个地址全部保留")
            continue

        # result 顺序可能与请求不一致，按 id 对齐
        for i, addr in enumerate(batch):
            code = by_id.get(i)
            if not code:
                # None / 空串 = 该地址判定失败，保守保留（绝不按合约剔除）
                kept.append(addr)
                stats["unknown"] += 1
            elif code == "0x":
                kept.append(addr)
                stats["kept_eoa"] += 1
            else:
                dropped.append(addr)             # 有字节码 = 合约，剔除
                stats["dropped_contract"] += 1
    return kept, dropped, stats


def main():
    parser = argparse.ArgumentParser(description="批量富化地址标签（从区块浏览器爬取并写回 DB）")
    parser.add_argument("--chain", type=str, default="eth",
                        help=f"链名，多个用逗号分隔（支持: {', '.join(sorted(ENRICH_SUPPORTED_CHAINS))}）")
    parser.add_argument("--limit", type=int, default=100,
                        help="每条链最多爬多少个地址（默认 100，优先爬高频地址）")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="每多少个地址写库一次（默认 50，并发模式下建议等于或大于 concurrency）")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每个请求的最小间隔秒数（默认 0.5 秒，并发模式下为每线程延迟）")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="并发爬取线程数（默认 5，建议不超过 10 避免触发反爬）")
    parser.add_argument("--min-count", type=int, default=3, dest="min_count",
                        help="只爬在转账记录中出现 >=N 次的地址（默认 3，过滤一次性散户长尾）；"
                             "设 0 或 1 表示不过滤")
    parser.add_argument("--include-contracts", action="store_true", dest="include_contracts",
                        help="不做合约前置剔除（默认会用 eth_getCode 剔除合约，只保留 EOA）")
    parser.add_argument("--verify-medium", action="store_true", dest="verify_medium",
                        help="把 confidence='medium' 的地址（evm_propagate 跨链传播副本）"
                             "也拉进爬取队列真爬验证，命中即原地升级为 high。"
                             "这些地址原本永远不会进入队列——富化只捞「没有标签」的地址，"
                             "而副本已经有标签了")
    parser.add_argument("--medium-limit", type=int, default=0, dest="medium_limit",
                        help="每条链最多验证多少个 medium 地址（默认 0=不限）")
    parser.add_argument("--medium-source", type=str, default=None, dest="medium_source",
                        help="只验证指定来源的 medium 地址（如 evm_propagate）；默认不限来源")
    parser.add_argument("--since-hours", type=float, default=0, dest="since_hours",
                        help="只捞最近 N 小时内出现的转账地址（默认 0=不限，即全量口径）。"
                             "本机热跑（每 30 分钟一次）应带窗口，例如 --since-hours 4："
                             "否则队列会被陈年长尾占满，新转账地址排不进来，榜单持续漏报")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式：只统计，不爬取、不写库")
    parser.add_argument("--db-url", type=str, default=None,
                        help="数据库连接串（默认从 settings 或 DATABASE_URL 环境变量读）")
    parser.add_argument("--proxy", type=str, default=None,
                        help="HTTP 代理地址（如 http://127.0.0.1:7890，配合 Clash 使用）")
    args = parser.parse_args()

    chains = [c.strip() for c in args.chain.split(",") if c.strip()]
    invalid = [c for c in chains if c not in ENRICH_SUPPORTED_CHAINS]
    if invalid:
        print(f"错误：不支持的链: {', '.join(invalid)}。支持的链: {', '.join(sorted(ENRICH_SUPPORTED_CHAINS))}")
        sys.exit(1)

    # 连接 DB 并执行
    settings = get_settings(require_database=True)
    db_url = args.db_url or settings.database_url
    with get_connection(db_url) as conn:
        _run_for_chains(conn, chains, args, db_url)

    print("\n全部完成。")


def _ensure_conn(conn, db_url: str):
    """确保数据库连接存活，断了就重连（返回连接对象，可能是新的）。"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        print("  🔄 数据库连接断开，正在重连...")
        # 关闭旧连接
        try:
            conn.close()
        except Exception:
            pass
        new_conn = psycopg.connect(
            db_url,
            connect_timeout=30,
            options="-c lock_timeout=30000",
            keepalives=1,
            keepalives_idle=15,
            keepalives_interval=5,
            keepalives_count=3,
        )
        new_conn.autocommit = False
        print("  ✅  已重连")
        return new_conn


def _record_attempts(conn, chain: str, ok_addrs: list[str], no_label_addrs: list[str],
                     source: str = "explorer_html",
                     contract_addrs: list[str] | None = None) -> int:
    """批量写入爬取尝试记录，返回写入条数。

    ok: 成功查到标签
    no_label: 页面正常但无标签
    contract: 前置判定为合约，从未爬取 HTML（语义上永不需要再爬）
    失败的（403/429/网络错误）不记，留给下次重试。
    """
    all_addrs = ([(a, "ok") for a in ok_addrs]
                 + [(a, "no_label") for a in no_label_addrs]
                 + [(a, "contract") for a in (contract_addrs or [])])
    if not all_addrs:
        return 0

    with conn.cursor() as cur:
        cur.executemany(f"""
            INSERT INTO {FETCH_ATTEMPT_TABLE} (address, chain, source, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (address, chain, source) DO UPDATE
            SET attempt_count = {FETCH_ATTEMPT_TABLE}.attempt_count + 1,
                last_attempt_at = NOW(),
                status = EXCLUDED.status
        """, [(addr, chain, source, status) for addr, status in all_addrs])
    conn.commit()
    return len(all_addrs)


def _write_batch(conn, db_url: str, chain: str, batch_results: dict, resolver,
                 inserted_total: int, backfilled_total: int) -> tuple:
    """写一批数据到 DB，带连接错误自动重连重试（最多 3 次）。
    返回 (conn, batch_inserted, batch_backfilled, inserted_total, backfilled_total)
    """
    import json
    from crypto_research.clients.label_enricher import (
        ENRICH_CONFIDENCE, ENRICH_SOURCE, ALLOWED_LABEL_TYPES,
        CASE_SENSITIVE_CHAINS as CS_CHAINS,
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 确保连接存活
            conn = _ensure_conn(conn, db_url)

            filtered = {
                a: info for a, info in batch_results.items()
                if info.get("label_type") in ALLOWED_LABEL_TYPES
            }

            batch_inserted = 0
            batch_backfilled = 0

            if not filtered:
                return conn, 0, 0, 0, inserted_total, 0, backfilled_total

            # 1. 写 address_label
            inserted = 0
            upgraded = 0
            with conn.cursor() as cur:
                for addr_l, info in filtered.items():
                    raw_meta = json.dumps({
                        "label_text": info["label_text"],
                        "fetched_from": "address_page",
                    }, ensure_ascii=False)
                    # ① 原地升级（medium/low → high）
                    #
                    # 这一步必须先于 INSERT，否则验证形同虚设：
                    # 唯一键是 (address, chain, label_type, label_name)，而
                    # evm_propagate 副本的 label_name 常常与浏览器标签名相同
                    # （例如都是 "Binance"）→ 原 INSERT ... DO NOTHING 会静默
                    # 跳过，medium 永远升不了 high，白爬一整晚。
                    # 这里把同 (address, chain, label_type) 下所有非 high 行
                    # 一次性升成 high，并统一采用浏览器真实标签名。
                    # LOWER(address) 匹配：EVM 链大小写混存时也能命中
                    # （索引换不来正确性，量级仅每批几十条）。
                    cur.execute("""
                        UPDATE biz.onchain_address_label
                        SET confidence = %s,
                            source = %s,
                            label_name = %s,
                            display_name = %s,
                            raw_meta = %s::jsonb,
                            updated_at = NOW()
                        WHERE chain = %s
                          AND LOWER(address) = LOWER(%s)
                          AND label_type = %s
                          AND confidence <> 'high'
                    """, (
                        ENRICH_CONFIDENCE, ENRICH_SOURCE,
                        info["display_name"], info["display_name"], raw_meta,
                        chain, addr_l, info["label_type"],
                    ))
                    if cur.rowcount:
                        upgraded += cur.rowcount
                        continue

                    # ② 没有可升级的旧行 → 插入新行
                    # 冲突时（同名不同源的旧记录）同样升级为 high，但绝不降级 high。
                    cur.execute("""
                        INSERT INTO biz.onchain_address_label
                            (address, chain, label_type, label_name, display_name,
                             confidence, source, raw_meta)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (address, chain, label_type, label_name) DO UPDATE
                        SET confidence = EXCLUDED.confidence,
                            source = EXCLUDED.source,
                            display_name = EXCLUDED.display_name,
                            raw_meta = EXCLUDED.raw_meta,
                            updated_at = NOW()
                        WHERE biz.onchain_address_label.confidence <> 'high'
                    """, (
                        addr_l, chain, info["label_type"],
                        info["display_name"], info["display_name"],
                        ENRICH_CONFIDENCE, ENRICH_SOURCE, raw_meta,
                    ))
                    if cur.rowcount:
                        inserted += 1

                # 也写 exchange_wallet（已存在且为 medium 的自动升级为 high）
                for addr_l, info in filtered.items():
                    if not info["is_exchange"]:
                        continue
                    # exchange_name 用标准化名（如 "Binance"），label 存完整原始标签
                    ex_name = info.get("normalized_name") or info["display_name"]
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
                                       || ';explorer_html'
                        WHERE biz.onchain_exchange_wallet.confidence != 'high'
                    """, (
                        addr_l, ex_name, chain,
                        info["label_text"], ENRICH_CONFIDENCE, ENRICH_SOURCE,
                    ))

            conn.commit()
            inserted_total += inserted
            batch_inserted = inserted
            batch_upgraded = upgraded

            # 2. 回填转账记录
            case_sensitive = chain in CS_CHAINS
            with conn.cursor() as cur:
                # 刷新 resolver 缓存
                resolver._no_label -= set(filtered.keys())
                addr_list = list(filtered.keys())
                resolver.resolve_batch(addr_list)

                # 临时表方式回填
                cur.execute("""
                    CREATE TEMP TABLE tmp_enrich_backfill (
                        address TEXT PRIMARY KEY,
                        label_types TEXT[],
                        label_names TEXT[]
                    ) ON COMMIT DROP
                """)
                rows = []
                for a in addr_list:
                    info = resolver.resolve(a)
                    if info["types"]:
                        rows.append((a, info["types"], info["names"]))
                if rows:
                    cur.executemany("""
                        INSERT INTO tmp_enrich_backfill (address, label_types, label_names)
                        VALUES (%s, %s, %s)
                    """, rows)

                # 更新发件方
                if case_sensitive:
                    from_cond = "t.from_address = e.address"
                    to_cond = "t.to_address = e.address"
                else:
                    from_cond = "LOWER(t.from_address) = LOWER(e.address)"
                    to_cond = "LOWER(t.to_address) = LOWER(e.address)"

                cur.execute(f"""
                    UPDATE biz.onchain_transfer_log t
                    SET from_labels = e.label_types,
                        from_label_names = e.label_names
                    FROM tmp_enrich_backfill e
                    WHERE t.chain = %s
                      AND {from_cond}
                      AND (t.from_labels IS NULL OR t.from_labels = ARRAY['unknown']::TEXT[])
                """, (chain,))
                from_up = cur.rowcount

                cur.execute(f"""
                    UPDATE biz.onchain_transfer_log t
                    SET to_labels = e.label_types,
                        to_label_names = e.label_names
                    FROM tmp_enrich_backfill e
                    WHERE t.chain = %s
                      AND {to_cond}
                      AND (t.to_labels IS NULL OR t.to_labels = ARRAY['unknown']::TEXT[])
                """, (chain,))
                to_up = cur.rowcount

            conn.commit()
            backfilled_total += from_up + to_up
            batch_backfilled = from_up + to_up

            return (conn, batch_inserted, batch_upgraded, batch_backfilled,
                    inserted_total, batch_upgraded, backfilled_total)

        except psycopg.OperationalError as e:
            # 连接错误：重连后重试
            print(f"  ⚠️  DB 连接错误（第 {attempt+1} 次）: {e}")
            if attempt < max_retries - 1:
                print(f"     等待 {2 ** attempt}s 后重连重试...")
                time.sleep(2 ** attempt)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = psycopg.connect(
                    db_url,
                    connect_timeout=30,
                    options="-c lock_timeout=30000",
                    keepalives=1,
                    keepalives_idle=15,
                    keepalives_interval=5,
                    keepalives_count=3,
                )
                conn.autocommit = False
                print(f"     ✅ 已重连")
            else:
                raise


def _run_for_chains(conn, chains: list[str], args, db_url: str) -> None:
    """对多条链依次执行富化（并发爬取 + 批量写库）。"""
    for chain in chains:
        print(f"\n{'=' * 60}")
        print(f"链: {chain}")
        print(f"{'=' * 60}")

        min_count = max(1, int(getattr(args, "min_count", 1)))
        exclude_contracts = not bool(getattr(args, "include_contracts", False))
        verify_medium = bool(getattr(args, "verify_medium", False))
        medium_source = getattr(args, "medium_source", None) or None
        since_hours = float(getattr(args, "since_hours", 0) or 0)

        # ── 队列一：medium 待验证地址（跨链传播副本，命中即升 high）──
        medium_addrs: list[str] = []
        total_medium = 0
        if verify_medium:
            total_medium = count_medium_addresses(conn, chain,
                                                  source=medium_source)
            med_limit = args.medium_limit if args.medium_limit > 0 else total_medium
            print(f"  medium 待验证地址: {total_medium} 个"
                  + (f"（source={medium_source}）" if medium_source else "")
                  + f"，本次计划爬 {min(med_limit, total_medium)} 个")

        # ── 队列二：无标签地址（原逻辑）──
        total_unlabeled = count_total_unlabeled(conn, chain, min_count=min_count,
                                               since_hours=since_hours)
        window_txt = f"，窗口近 {since_hours:g}h" if since_hours > 0 else "，窗口不限"
        print(f"  无标签地址总数（估算, cnt>={min_count}{window_txt}）: {total_unlabeled}")
        effective_limit = args.limit if args.limit > 0 else total_unlabeled
        print(f"  本次计划爬取: {min(effective_limit, total_unlabeled)} 个")
        print(f"  并发数: {args.concurrency}")
        print(f"  合约前置剔除: {'开（只留 EOA）' if exclude_contracts else '关'}"
              f"（medium 验证队列不做剔除）")

        if args.dry_run:
            print("  [dry-run] 跳过实际爬取")
            continue

        if total_unlabeled == 0 and total_medium == 0:
            print("  没有需要富化的地址，跳过")
            continue

        if verify_medium and total_medium:
            med_limit = args.medium_limit if args.medium_limit > 0 else total_medium
            medium_addrs = get_medium_addresses(conn, chain, med_limit,
                                                source=medium_source)
            print(f"  捞出 {len(medium_addrs)} 个 medium 待验证地址")

        # 捞出待爬地址
        addresses = get_unlabeled_addresses(conn, chain, effective_limit,
                                            min_count=min_count,
                                            since_hours=since_hours)

        # 前置剔除合约地址（obtained via eth_getCode），只保留 EOA
        dropped_contracts: list[str] = []
        if exclude_contracts and addresses:
            addresses, dropped_contracts, fstats = batch_filter_contracts(chain, addresses)
            print(f"  合约过滤: EOA {fstats['kept_eoa']} 个 / 剔除合约 "
                  f"{fstats['dropped_contract']} 个 / 判定失败保留 "
                  f"{fstats['rpc_error'] + fstats['unknown']} 个")

        # 合并队列：medium 优先（交易所副本价值远高于散户长尾）
        #
        # medium 队列**不做合约剔除**：这里的地址本来就带着「疑似交易所」标签，
        # 交易所的多签/金库常常是合约（有字节码），若走 eth_getCode 会被误删；
        # 验证的目的正是要确认它们，剔除等于自废武功。
        merged = list(dict.fromkeys(list(medium_addrs) + list(addresses)))
        n_med = len(medium_addrs)
        addresses = merged
        if not addresses:
            print("  过滤后无 EOA 地址，跳过")
            # 剔除的合约仍要落 attempt，避免以后重复 RPC 判定
            if dropped_contracts:
                ensure_attempt_table(conn)
                try:
                    n = _record_attempts(conn, chain, [], [],
                                         contract_addrs=dropped_contracts)
                    conn.commit()
                    print(f"  已记录 {n} 个合约地址（下次自动跳过）")
                except Exception as e:
                    print(f"  ⚠️  记录合约跳过失败: {e}")
            continue

        print(f"  合计 {len(addresses)} 个待爬地址"
              f"（medium 验证 {n_med} + 无标签 {len(addresses) - n_med}）")
        print(f"  前 5 个: {addresses[:5]}")

        # 初始化 resolver（DB 查询用，单线程安全）
        resolver = AddressLabelResolver(conn, chain)

        # 确保爬取尝试记录表存在
        ensure_attempt_table(conn)

        # 统计变量（多线程共享，用锁保护）
        stats_lock = Lock()
        result_map: dict[str, dict] = {}  # addr -> label_info
        stat_counts = {
            "ok": 0, "no_label": 0,
            "http_403": 0, "http_429": 0, "http_other": 0, "network_error": 0,
        }
        inserted_total = 0
        upgraded_total = 0
        backfilled_total = 0
        attempt_recorded = 0
        done_count = 0
        t0 = time.time()

        # 并发爬取函数：每个线程一个 fetcher（requests 非线程安全）
        def _fetch_one(addr: str, fetcher: ExplorerLabelFetcher):
            info, status = fetcher.fetch_with_status(addr)
            return addr, info, status

        # 用线程局部变量存每个线程的 fetcher
        thread_local = {}

        def _worker(addr: str):
            # 每个线程创建自己的 fetcher
            thread_id = _thread_id()
            if thread_id not in thread_local:
                thread_local[thread_id] = ExplorerLabelFetcher(
                    chain=chain, delay=args.delay, proxy=args.proxy)
            fetcher = thread_local[thread_id]
            return _fetch_one(addr, fetcher)

        # 分批提交 + 每批写完库再下一批（内存可控 + 避免连接池打爆）
        batch_size = args.batch_size
        for batch_start in range(0, len(addresses), batch_size):
            batch = addresses[batch_start:batch_start + batch_size]
            batch_results: dict[str, dict] = {}
            batch_no_label: list[str] = []
            batch_stats = {k: 0 for k in stat_counts}

            # 并发爬取本批
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {pool.submit(_worker, addr): addr for addr in batch}
                for future in as_completed(futures):
                    addr, info, status = future.result()
                    with stats_lock:
                        done_count += 1
                        if info:
                            batch_results[addr.lower()] = info
                            batch_stats["ok"] += 1
                        elif status == "no_label":
                            # 页面正常但无标签 —— 必须入 attempt 表，否则下轮重复爬。
                            # 注意：'no_label' 同时是 batch_stats 的键，
                            # 若放到 `elif status in batch_stats` 之后判断会被该分支截获，
                            # 导致 batch_no_label 恒为空、skip_attempted 形同虚设（历史 bug）。
                            batch_no_label.append(addr.lower())
                            batch_stats["no_label"] += 1
                        elif status in batch_stats:
                            # http_403 / http_429 / http_other / network_error：
                            # 按设计不入 attempt 表，留给下轮重试
                            batch_stats[status] += 1
                        else:
                            # 未知状态（含 invalid_address）：按无标签记账并跳过
                            batch_no_label.append(addr.lower())
                            batch_stats["no_label"] += 1

                    # 进度输出（每 10 个打一次）
                    if done_count % max(10, args.concurrency * 2) == 0 or done_count == len(addresses):
                        pct = done_count / len(addresses) * 100
                        elapsed = time.time() - t0
                        rate = done_count / elapsed if elapsed > 0 else 0
                        eta = (len(addresses) - done_count) / rate if rate > 0 else 0
                        print(f"  进度: {done_count}/{len(addresses)} ({pct:.0f}%) | "
                              f"已查到标签 {stat_counts['ok'] + batch_stats['ok']} 个 | "
                              f"速度 {rate:.1f}/s | 预计剩余 {eta/60:.1f} 分钟")

            # 本批写库（带连接断开自动重连重试）
            batch_inserted = 0
            batch_backfilled = 0
            batch_upgraded = 0
            if batch_results:
                try:
                    (conn, batch_inserted, batch_upgraded, batch_backfilled,
                     inserted_total, batch_upgraded_total, backfilled_total) = \
                        _write_batch(conn, db_url, chain, batch_results, resolver,
                                     inserted_total, backfilled_total)
                    upgraded_total += batch_upgraded_total
                except Exception as e:
                    print(f"  ⚠️  本批写库失败（重试后仍失败）: {e}")
                    import traceback
                    traceback.print_exc()

            # 记录爬取尝试（ok + no_label），避免下次重复爬
            try:
                ok_list = list(batch_results.keys())
                n = _record_attempts(conn, chain, ok_list, batch_no_label)
                attempt_recorded += n
            except Exception as e:
                print(f"  ⚠️  记录爬取尝试失败: {e}")

            # 累加到全局统计
            for k in batch_stats:
                stat_counts[k] += batch_stats[k]

            batch_end = min(batch_start + batch_size, len(addresses))
            pct = batch_end / len(addresses) * 100
            fail_parts = []
            if batch_stats["http_403"]:
                fail_parts.append(f"403×{batch_stats['http_403']}")
            if batch_stats["http_429"]:
                fail_parts.append(f"429×{batch_stats['http_429']}")
            if batch_stats["network_error"]:
                fail_parts.append(f"网络×{batch_stats['network_error']}")
            fail_str = f"，失败: {', '.join(fail_parts)}" if fail_parts else ""
            print(f"  ── 批次完成 {batch_end}/{len(addresses)} ({pct:.0f}%) ── "
                  f"本批查到标签 {batch_stats['ok']} 个, "
                  f"入库 {batch_inserted} 条, "
                  f"升级 medium→high {batch_upgraded} 条, "
                  f"回填 {batch_backfilled} 条{fail_str}")

        # 把本次前置剔除的合约地址落 attempt 表（status='contract'），下次直接跳过
        if dropped_contracts:
            try:
                n = _record_attempts(conn, chain, [], [],
                                     contract_addrs=dropped_contracts)
                conn.commit()
                attempt_recorded += n
            except Exception as e:
                print(f"  ⚠️  记录合约跳过失败: {e}")

        # 清理所有线程的 fetcher
        for f in thread_local.values():
            f.close()

        elapsed = time.time() - t0
        print(f"\n  ── {chain} 完成 ──")
        print(f"    总耗时: {elapsed:.1f}s ({elapsed/60:.1f} 分钟)")
        print(f"    爬取地址: {len(addresses)} 个")
        print(f"    查到标签: {stat_counts['ok']} 个")
        print(f"    无标签: {stat_counts['no_label']} 个")
        print(f"    爬取失败: {sum(stat_counts[k] for k in ['http_403','http_429','http_other','network_error'])} 个")
        print(f"    入库新标签: {inserted_total} 条")
        print(f"    升级 medium→high: {upgraded_total} 条")
        print(f"    回填转账记录: {backfilled_total} 条")
        print(f"    记录尝试: {attempt_recorded} 条（下次自动跳过）")
        print(f"    平均速度: {len(addresses)/elapsed:.1f} 地址/秒")


def _thread_id() -> int:
    import threading
    return threading.get_ident()


if __name__ == "__main__":
    main()
