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
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# 排除清单：错误/可疑的合约映射，快照会产生明显失真数据（如供应量亿倍于真实值）。
# 例：BTC(asset_id=2) 的 ethereum 合约 0x43fd... 实为某山寨代币，抓取会得到 2610 亿枚的
# 荒诞结果，直接写入会污染 BTC 行。此类原生币（BTC/ETH）本就无 EVM 合约，应跳过。
EXCLUDE_CONTRACTS = (
    "0x43fd9de06bb69ad771556e171f960a91c42d2955",  # BTC(asset_id=2) 错误映射的 ethereum 合约
    # SOL(asset_id=1814) 映射到 solana **原生 mint**（43 个 1），它不是 SPL token，
    # Helius getTokenSupply/getTokenLargestAccounts 恒返回 -32602 not a Token mint → 永不出快照。
    # 不在此剔除会每天白占一个待采集名额，且永远落进 fail 统计。
    "So11111111111111111111111111111111111111111",
)

# Solana 地址「结构护栏」：合法 Solana 公钥必为 base58（32~44 字符，字母表不含 0 O I l）。
# 背景（2026-09-27 审计）：`core.asset_contract` 的 solana 地址存在系统性污染 ——
#   ① 被降格成全小写（base58 **大小写敏感**：降格后或解码字节数不对 → Helius -32602 WrongSize，
#      或指向一个不存在的 mint → -32602 not a Token mint）；
#   ② 混入 Solscan URL 片段（如 `token/EdAhkb5…`，长度 50）；
#   ③ 把 EVM 地址贴到 solana 链（如 `0xadb2437e…`）。
# 这类行 `last_dt` 恒为 NULL ⇒ 永远排在「待采集最前」、永远失败、每天白占一个名额。
# 此处只做**零误伤**的结构过滤（不是 base58 字符串者铁定不是 Solana 公钥）；
# 「全小写但形态合法」的那批（GEOD/FOXSY/AIXCB/SAFA/BELG）**不在此过滤** —— 它们形态上无法
# 与真地址区分，须由数据侧修复地址本身（见 AGENTS.md「Solana 合约地址污染」留档）。
SOLANA_ADDR_RE = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"


def _sol_guard(chain_short: str) -> tuple[str, tuple]:
    """返回 (SQL 片段, 参数)。仅 solana 链追加 base58 结构护栏，其它链为空片段。"""
    if chain_short == "solana":
        return "AND c.contract_address ~ %s", (SOLANA_ADDR_RE,)
    return "", ()


def get_pending_assets(conn, chain_short: str, limit: int) -> list[dict]:
    """获取指定链上有合约地址、今日尚未采快照的资产列表。

    排序（OBI-OPT-SNAPSHOT-FRESHNESS X3，2026-09-27）：先按**最近一次快照日升序、NULLS FIRST**，
    再按 market_cap_rank 升序。原实现只按 rank 排序 ⇒ 每次 run 都从头吃「前 N 名」，长尾
    （rank 靠后的合约）因单轮 `--limit` 截断而**永远轮不到**（实测滞后累积至 3 天）。
    改为「最久未采优先」后，未采过的资产（last_dt 为 NULL）恒排最前，已采资产按陈旧度轮转，
    长尾在数日内被自然覆盖 —— **无需放大 `--limit`**（放大反而会「跑到完」饿死 chain 槽位）。
    """
    db_names = tuple(
        k for k, v in CHAIN_ALIASES.items() if v == chain_short
    )
    if not db_names:
        return []

    placeholders = ",".join(["%s"] * len(db_names))
    exclude_ph = ",".join(["%s"] * len(EXCLUDE_CONTRACTS))
    sol_guard, sol_params = _sol_guard(chain_short)

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            SELECT c.asset_id, c.contract_address, c.chain,
                   a.canonical_symbol AS symbol, a.canonical_name AS name
            FROM core.asset_contract c
            JOIN core.asset a ON a.asset_id = c.asset_id
            LEFT JOIN LATERAL (
                SELECT MAX(s.snapshot_date) AS last_dt
                FROM biz.onchain_holder_snapshot s
                WHERE s.asset_id = c.asset_id
                  AND s.chain = c.chain
            ) ls ON TRUE
            WHERE c.chain IN ({placeholders})
              AND c.contract_address IS NOT NULL
              AND c.contract_address NOT IN ({exclude_ph})
              {sol_guard}
              AND NOT EXISTS (
                  SELECT 1 FROM biz.onchain_holder_snapshot s
                  WHERE s.asset_id = c.asset_id
                    AND s.chain = c.chain
                    -- 按北京时间判断"今日"，避免 UTC 时区下凌晨跑批被误判为已采集
                    AND s.snapshot_date >= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
              )
            ORDER BY ls.last_dt ASC NULLS FIRST,
                     a.market_cap_rank ASC NULLS LAST,
                     c.asset_id ASC
            LIMIT %s
            """,
            (*db_names, *EXCLUDE_CONTRACTS, *sol_params, limit),
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
    exclude_ph = ",".join(["%s"] * len(EXCLUDE_CONTRACTS))
    sol_guard, sol_params = _sol_guard(chain_short)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM core.asset_contract c
            WHERE c.chain IN ({placeholders})
              AND c.contract_address IS NOT NULL
              AND c.contract_address NOT IN ({exclude_ph})
              {sol_guard}
              AND NOT EXISTS (
                  SELECT 1 FROM biz.onchain_holder_snapshot s
                  WHERE s.asset_id = c.asset_id
                    AND s.chain = c.chain
                    -- 按北京时间判断"今日"
                    AND s.snapshot_date >= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
              )
            """,
            (*db_names, *EXCLUDE_CONTRACTS, *sol_params),
        )
        return cur.fetchone()[0]


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """连同子进程一起杀掉（Playwright 等会 spawn 后代进程）。"""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _tail(path: str, n: int = 200) -> str:
    """读取文件末尾 n 个字符，用于失败原因诊断。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()[-n:]
    except Exception:
        return ""


def _sweep_stale_temp_files(max_age_hours: int = 24) -> None:
    """清理本任务遗留的临时输出文件（审计 B8）。

    ``run_single`` 的 finally 清理在父进程被 SIGKILL 时不会执行，
    长期运行会在系统 temp 目录累积 ``chain_holder_*.out/.err``。
    仅清理本任务专属前缀且超过 max_age_hours 未修改的文件，避免误删在跑的兄弟任务。
    """
    cutoff = time.time() - max_age_hours * 3600
    tmp_dir = tempfile.gettempdir()
    try:
        entries = os.listdir(tmp_dir)
    except OSError:
        return
    for name in entries:
        if not (name.startswith("chain_holder_")
                and (name.endswith(".out") or name.endswith(".err"))):
            continue
        path = os.path.join(tmp_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _run_chain(chain: str, assets: list[dict], args) -> tuple[int, int, int]:
    """跑一条链的全部资产采集，返回 (success_count, fail_count, 本次触发熔断时的连续失败数)。

    按 args.workers 决定模式：
      workers == 1：顺序跑 —— 每币间 args.delay 延迟 + 连续失败熔断（与历史逻辑一致）
      workers >= 2：并发 —— ThreadPoolExecutor 跑多个 run_single 子进程；
                   按 "完成一个提交下一个" 的滑动窗口方式保持并发度，不预创建全部 future；
                   连续失败熔断仍生效（全局共享一个 consec_fail 计数器）。
    """
    n_assets = len(assets)
    timeout_base = args.timeout if chain != "solana" else max(args.timeout, 120)

    if args.workers <= 1:
        return _run_chain_sequential(chain, assets, args, timeout_base)
    return _run_chain_parallel(chain, assets, args, timeout_base)


def _run_chain_sequential(chain: str, assets: list[dict], args, timeout_base: int
                          ) -> tuple[int, int, int]:
    chain_success = 0
    chain_fail = 0
    consec_fail = 0
    for i, asset in enumerate(assets, 1):
        asset_id = asset["asset_id"]
        symbol = asset.get("symbol", "?")
        print(f"  [{i}/{len(assets)}] asset_id={asset_id} {symbol} ... ",
              end="", flush=True)
        ok, reason = run_single(asset_id, chain, timeout=timeout_base)
        if ok:
            chain_success += 1
            consec_fail = 0
            print("OK")
        else:
            chain_fail += 1
            consec_fail += 1
            print(f"FAIL ({reason})")
            if args.max_consec_fail > 0 and consec_fail >= args.max_consec_fail:
                print(f"\n  [熔断] {chain} 连续失败 {consec_fail} 次，"
                      f"判定上游故障，中止本轮（保留已成功 {chain_success} 条）")
                break
        if i < len(assets) and args.delay > 0:
            time.sleep(args.delay)
    return chain_success, chain_fail, consec_fail


def _run_chain_parallel(chain: str, assets: list[dict], args, timeout_base: int
                        ) -> tuple[int, int, int]:
    """并发模式：滑动窗口 + 熔断。

    每币完成一个就提交下一个，窗口大小 = workers；
    全局共享 consec_fail 计数器（需要线程锁）。
    """
    import threading
    lock = threading.Lock()
    success = fail = consec_fail = 0
    halted = threading.Event()

    def _one(i: int, asset: dict):
        nonlocal success, fail, consec_fail
        asset_id = asset["asset_id"]
        symbol = asset.get("symbol", "?")
        ok, reason = run_single(asset_id, chain, timeout=timeout_base)
        with lock:
            if ok:
                success += 1
                consec_fail = 0
                print(f"  [{i}/{len(assets)}] asset_id={asset_id} {symbol} OK", flush=True)
            else:
                fail += 1
                consec_fail += 1
                print(f"  [{i}/{len(assets)}] asset_id={asset_id} {symbol} FAIL ({reason})", flush=True)
                if args.max_consec_fail > 0 and consec_fail >= args.max_consec_fail:
                    halted.set()
        if args.delay > 0 and not halted.is_set():
            time.sleep(args.delay)
        return ok

    max_workers = max(2, args.workers)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        pending = iter(enumerate(assets, 1))
        futures = set()
        # 预填初始窗口
        for _ in range(max_workers):
            try:
                futures.add(ex.submit(_one, *next(pending)))
            except StopIteration:
                break

        # 外层 while：不断用 as_completed 收割，有空间就塞下一个
        while futures:
            # as_completed 返回迭代器，每次 yield 一个已完成 future
            for fut in as_completed(list(futures)):
                futures.discard(fut)
                try:
                    fut.result()
                except Exception as e:
                    print(f"  [worker] 异常: {e}", flush=True)
                    fail += 1
                if halted.is_set():
                    continue
                try:
                    i, a = next(pending)
                except StopIteration:
                    continue
                futures.add(ex.submit(_one, i, a))

    return success, fail, consec_fail


def run_single(asset_id: int, chain: str, timeout: int = 30) -> tuple[bool, str]:
    """运行单币持仓快照采集，返回 (是否成功, 失败原因)。

    审计 P0-3 真根因：子进程用 PIPE 捕获输出时，若采集脚本（含 Playwright）
    派生的后代进程在超时后仍持有写端 fd，父进程 acquire 管道 EOF 会永久阻塞
    → 90 分钟无日志被看护误杀。此处改为重定向到临时文件（父进程从不读管道），
    配合独立进程组 + killpg，彻底消除悬挂；失败时读临时文件尾部作为诊断。
    """
    script = SCRIPT_DIR / "phase_chain_holder_scrape.py"
    popen_kwargs: dict = dict(cwd=str(SCRIPT_DIR))
    # 独立进程组：超时时可连同后代进程一起回收
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    proc = None
    out_path = err_path = None
    try:
        out_f = tempfile.NamedTemporaryFile(
            mode="w", prefix="chain_holder_", suffix=".out", delete=False, encoding="utf-8")
        err_f = tempfile.NamedTemporaryFile(
            mode="w", prefix="chain_holder_", suffix=".err", delete=False, encoding="utf-8")
        out_path, err_path = out_f.name, err_f.name

        proc = subprocess.Popen(
            [
                sys.executable, "-u", str(script),
                "--asset-id", str(asset_id),
                "--chain", chain,
                "--save",
            ],
            stdout=out_f,
            stderr=err_f,
            **popen_kwargs,
        )
        out_f.close()
        err_f.close()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            return False, f"timeout {timeout}s"

        if proc.returncode == 0:
            return True, ""
        msg = _tail(err_path) or _tail(out_path)
        return False, f"exit={proc.returncode} {msg}"
    except Exception as e:
        if proc is not None:
            _kill_process_tree(proc)
        return False, f"exception: {str(e)[:100]}"
    finally:
        for p in (out_path, err_path):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass


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
                        help="每币之间延迟（秒，避免触发限流；并发模式下指 worker 内币间延迟）")
    parser.add_argument("--max-consec-fail", type=int, default=20,
                        help="单链连续失败超过该阈值即熔断本轮（防上游整体故障时整轮烧满 12h 槽位，0=禁用）")
    parser.add_argument("--workers", type=int, default=1,
                        help="每链并发 scrape worker 数（>1 时多个 phase_chain_holder_scrape.py 并行；"
                             "适合 base/arb 等 Blockscout 劣化严重的链，bsc/eth 保持 1 即可）")
    args = parser.parse_args()

    _sweep_stale_temp_files()

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
    print(f"每链并发 worker: {args.workers}")
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

    for chain in chains:
        # 每条链用独立短连接查询待采集资产后立刻关闭，避免连接在外层 subprocess 长循环中
        # 空闲过久被服务端/代理断开（修复 P1-3：长循环后 __exit__ 的 commit 偶发
        # "server closed the connection unexpectedly"，导致 ingest_run 无法终态化）。
        with get_connection(settings.database_url) as conn:
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

        chain_success, chain_fail, consec_fail = _run_chain(
            chain, assets, args)
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
