"""
链上持仓快照批量采集脚本。
遍历所有有合约地址但尚无今日持仓快照的资产，按链分组批量采集。

用法:
    python phase_chain_holder_batch.py --chains bsc,eth,base,arb,solana
    python phase_chain_holder_batch.py --chains bsc --limit 100
    python phase_chain_holder_batch.py --all-chains
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(line_buffering=True)

import json

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.db.upsert import load_sql, fetch_one


# 支持的链（有可靠免费数据源的优先）
SUPPORTED_CHAINS = {
    "bsc": "Binplorer API (免费)",
    "eth": "Ethplorer API (免费)",
    "base": "Blockscout API (免费)",
    "arb": "Blockscout API (免费)",
    "solana": "Solscan 网页解析",
    "polygon": "Polygonscan 网页解析 (可能被CF拦截)",
    "avax": "Snowtrace 网页解析 (可能被CF拦截)",
    "opt": "Optimism Etherscan 网页解析 (可能被CF拦截)",
}

# 链名别名：数据库名 -> 脚本简称
CHAIN_ALIASES = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "bnb": "bsc",
    "binance": "bsc",
    "polygon": "polygon",
    "matic": "polygon",
    "arbitrum": "arb",
    "arb": "arb",
    "optimism": "opt",
    "op": "opt",
    "base": "base",
    "avalanche": "avax",
    "avax": "avax",
    "solana": "solana",
    "sol": "solana",
}


def get_pending_assets(conn, chain_short: str, limit: int) -> list[dict]:
    """获取指定链上有合约地址但尚无今日快照的资产列表。"""
    db_names = tuple(
        k for k, v in CHAIN_ALIASES.items() if v == chain_short
    )
    if not db_names:
        return []

    placeholders = ",".join(["%s"] * len(db_names))

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            SELECT c.asset_id, c.contract_address, c.chain,
                   a.canonical_symbol AS symbol, a.canonical_name AS name
            FROM core.asset_contract c
            JOIN core.asset a ON a.asset_id = c.asset_id
            WHERE c.chain IN ({placeholders})
              AND c.contract_address IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM biz.onchain_holder_snapshot s
                  WHERE s.asset_id = c.asset_id
                    AND s.chain = c.chain
                    -- 按北京时间判断"今日"，避免 UTC 时区下凌晨跑批被误判为已采集
                    AND s.snapshot_date >= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
              )
            ORDER BY c.asset_id ASC
            LIMIT %s
            """,
            (*db_names, limit),
        )
        return cur.fetchall()


def get_total_pending(conn, chain_short: str) -> int:
    """获取指定链待采集总数。"""
    db_names = tuple(
        k for k, v in CHAIN_ALIASES.items() if v == chain_short
    )
    if not db_names:
        return 0

    placeholders = ",".join(["%s"] * len(db_names))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM core.asset_contract c
            WHERE c.chain IN ({placeholders})
              AND c.contract_address IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM biz.onchain_holder_snapshot s
                  WHERE s.asset_id = c.asset_id
                    AND s.chain = c.chain
                    -- 按北京时间判断"今日"
                    AND s.snapshot_date >= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
              )
            """,
            db_names,
        )
        return cur.fetchone()[0]


def run_single(asset_id: int, chain: str, timeout: int = 30) -> tuple[bool, str]:
    """运行单币持仓快照采集，返回 (是否成功, 失败原因)。"""
    script = SCRIPT_DIR / "phase_chain_holder_scrape.py"
    try:
        result = subprocess.run(
            [
                sys.executable, "-u", str(script),
                "--asset-id", str(asset_id),
                "--chain", chain,
                "--save",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(SCRIPT_DIR),
        )
        if result.returncode == 0:
            return True, ""
        # 取 stderr 最后 200 字符作为失败原因
        err = (result.stderr or result.stdout or "").strip()[-200:]
        return False, f"exit={result.returncode} {err}"
    except subprocess.TimeoutExpired:
        return False, f"timeout {timeout}s"
    except Exception as e:
        return False, f"exception: {str(e)[:100]}"


def main():
    parser = argparse.ArgumentParser(description="链上持仓快照批量采集")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--chains", type=str,
                   help="要采集的链，逗号分隔，如 bsc,eth,base,arb,solana")
    g.add_argument("--all-chains", action="store_true", help="采集所有支持的链")
    parser.add_argument("--limit", type=int, default=0,
                        help="每链最多采集数量 (0=不限)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="单币超时时间（秒）")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每币之间延迟（秒，避免触发限流）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    if args.all_chains:
        chains = list(SUPPORTED_CHAINS.keys())
    else:
        chains = [c.strip() for c in args.chains.split(",") if c.strip()]
        invalid = [c for c in chains if c not in SUPPORTED_CHAINS]
        if invalid:
            print(f"ERROR: 不支持的链: {invalid}")
            print(f"支持的链: {list(SUPPORTED_CHAINS.keys())}")
            return 1

    print("=" * 60)
    print("链上持仓快照批量采集")
    print(f"目标链: {chains}")
    print(f"每链限制: {args.limit if args.limit > 0 else '不限'}")
    print("=" * 60)

    total_success = 0
    total_fail = 0
    total_skipped = 0
    t0 = time.time()

    # ingest_run 审计记录（与 CMC 流水线对齐，方便监控面板统一查询）
    insert_ingest_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_sql = load_sql("sys/finish_ingest_run.sql")
    run_id = None
    workflow_name = "WF_CHAIN_HOLDER_SNAPSHOT"

    # ingest_run 审计记录：用独立连接写入，避免写入失败污染主采集事务。
    # 修复 P1-3：原本与外键缺失共同导致整条管线在 get_total_pending 处崩溃。
    try:
        with get_connection(settings.database_url) as wconn:
            run_row = fetch_one(
                wconn,
                insert_ingest_sql,
                (
                    "onchain",
                    "holder_snapshot",
                    workflow_name,
                    json.dumps(
                        {"chains": chains, "limit": args.limit, "timeout": args.timeout},
                        ensure_ascii=False,
                    ),
                    f"chains:{','.join(chains)}",
                ),
            )
            run_id = run_row["run_id"]
    except Exception as e:
        print(f"[WARN] ingest_run 记录写入失败（不影响采集）: {e}")

    with get_connection(settings.database_url) as conn:
        for chain in chains:
            total_pending = get_total_pending(conn, chain)
            limit = args.limit if args.limit > 0 else total_pending
            if limit == 0:
                print(f"\n[{chain}] 待采集: 0 (全部完成或无合约)，跳过")
                total_skipped += 1
                continue

            print(f"\n[{chain}] 待采集总数: {total_pending}，本次处理: {limit}")
            print(f"  数据源: {SUPPORTED_CHAINS[chain]}")

            assets = get_pending_assets(conn, chain, limit)
            if not assets:
                print(f"  无待采集资产")
                total_skipped += 1
                continue

            chain_success = 0
            chain_fail = 0

            for i, asset in enumerate(assets, 1):
                asset_id = asset["asset_id"]
                symbol = asset.get("symbol", "?")
                print(f"  [{i}/{len(assets)}] asset_id={asset_id} {symbol} ... ",
                      end="", flush=True)

                ok, reason = run_single(asset_id, chain, timeout=args.timeout)
                if ok:
                    chain_success += 1
                    print("OK")
                else:
                    chain_fail += 1
                    print(f"FAIL ({reason})")

                if i < len(assets) and args.delay > 0:
                    time.sleep(args.delay)

            total_success += chain_success
            total_fail += chain_fail
            print(f"  本链完成: 成功 {chain_success}, 失败 {chain_fail}")

    elapsed = time.time() - t0
    total_processed = total_success + total_fail
    # 状态判定：全失败 → failed；部分失败 → partial；全成功 → success；无待处理 → success（空跑）
    if total_processed == 0:
        status = "success"
        error_msg = "无待采集资产"
    elif total_fail == 0:
        status = "success"
        error_msg = None
    elif total_success == 0:
        status = "failed"
        error_msg = f"全部失败 ({total_fail}/{total_processed})"
    else:
        status = "partial"
        error_msg = f"部分失败 ({total_fail}/{total_processed})"

    # 写 ingest_run 结束记录
    if run_id:
        try:
            with get_connection(settings.database_url) as conn:
                fetch_one(
                    conn,
                    finish_ingest_sql,
                    (
                        status,
                        200 if status != "failed" else 500,
                        total_processed,
                        total_success,
                        total_fail,
                        error_msg,
                        run_id,
                    ),
                )
        except Exception as e:
            print(f"[WARN] ingest_run 结束记录写入失败: {e}")

    print("\n" + "=" * 60)
    print(f"全部完成，耗时 {elapsed:.1f}s")
    print(f"总计: 成功 {total_success}, 失败 {total_fail}, 跳过 {total_skipped}")
    print(f"状态: {status}")
    print("=" * 60)

    # JSON 行输出，供 TaskManager 解析 stats
    print(json.dumps({
        "status": status,
        "success": total_success,
        "fail": total_fail,
        "skipped": total_skipped,
        "elapsed_s": round(elapsed, 1),
        "chains": chains,
    }, ensure_ascii=False))

    # 全失败时 exit 1，让 TaskManager 标记为 failed 并触发告警
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
