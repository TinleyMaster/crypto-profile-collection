"""
代币解锁数据批量采集脚本。
从 tokenomist.ai / tokenomics.com 用 Playwright 爬取解锁时间表。

用法:
    python phase_chain_token_unlocks_batch.py --limit 100
    python phase_chain_token_unlocks_batch.py --limit 0  # 全量
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


def get_pending_assets(conn, limit: int) -> list[dict]:
    """获取有 CG 映射但尚无解锁数据的资产列表。

    优先处理高市值、非稳定币、非 meme 的资产，跳过已停用资产，
    提升 tokenomics.com 命中率和批量成功率。

    P1-1: not_found 墓碑 30 天冷却；parse_empty 视为待重试（不阻塞）。
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
                   asm.source_asset_key AS coingecko_id
            FROM core.asset a
            JOIN (
                SELECT DISTINCT ON (asset_id) asset_id, source_asset_key
                FROM core.asset_source_map
                WHERE source_code = 'cg'
                ORDER BY asset_id, source_asset_key
            ) asm ON asm.asset_id = a.asset_id
            WHERE a.status = 'active'
              AND asm.source_asset_key IS NOT NULL
              AND a.asset_type != 'stablecoin'
              AND a.primary_sector != 'meme'
              AND NOT EXISTS (
                  SELECT 1 FROM biz.asset_token_unlocks u
                  WHERE u.asset_id = a.asset_id
                    AND (u.crawl_status = 'ok'
                         OR (u.crawl_status = 'not_found'
                             AND u.last_attempt_at > NOW() - INTERVAL '30 day')))
            ORDER BY COALESCE(a.market_cap, 0) DESC, a.asset_id ASC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_total_pending(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT a.asset_id)
            FROM core.asset a
            JOIN (
                SELECT DISTINCT ON (asset_id) asset_id, source_asset_key
                FROM core.asset_source_map
                WHERE source_code = 'cg'
                ORDER BY asset_id, source_asset_key
            ) asm ON asm.asset_id = a.asset_id
            WHERE a.status = 'active'
              AND asm.source_asset_key IS NOT NULL
              AND a.asset_type != 'stablecoin'
              AND a.primary_sector != 'meme'
              AND NOT EXISTS (
                  SELECT 1 FROM biz.asset_token_unlocks u
                  WHERE u.asset_id = a.asset_id
                    AND (u.crawl_status = 'ok'
                         OR (u.crawl_status = 'not_found'
                             AND u.last_attempt_at > NOW() - INTERVAL '30 day')))
            """
        )
        return cur.fetchone()[0]


def run_single(asset_id: int, timeout: int = 60, allow_browser_search: bool = False) -> tuple[str, str]:
    """运行单币解锁采集，返回 (状态, 详情)。

    状态：ok / not_found / parse_empty / fail
    """
    script = SCRIPT_DIR / "phase_chain_token_unlocks.py"
    cmd = [
        sys.executable, "-u", str(script),
        "--asset-id", str(asset_id),
        "--save",
    ]
    if not allow_browser_search:
        cmd.append("--no-browser-search")  # 默认禁用浏览器首页搜索提速
    proc = None
    try:
        # start_new_session=True：超时后可用 killpg 清理整个进程组（含 Playwright chromium）
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(SCRIPT_DIR),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            return "fail", "timeout"
        if proc.returncode != 0:
            return "fail", f"exit={proc.returncode}"

        # 解析 stdout 最后一行 JSON
        stdout_lines = [l for l in stdout.strip().split("\n") if l.strip()]
        if not stdout_lines:
            return "fail", "no_output"

        try:
            data = json.loads(stdout_lines[-1])
            status = data.get("status", "unknown")
            if status == "ok":
                events = len(data.get("unlock_events", []))
                # P1-3: overview 有信号但事件空 → parse_empty，视为疑似失败
                if data.get("crawl_status") == "parse_empty":
                    return "fail", "parse_empty"
                return "ok", f"events={events}"
            elif status == "not_found":
                return "not_found", "not_found"
            else:
                return "fail", f"status={status}"
        except (json.JSONDecodeError, ValueError):
            return "fail", "parse_error"

    except Exception as e:
        return "fail", f"error={e}"


def main():
    parser = argparse.ArgumentParser(description="代币解锁数据批量采集")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多采集数量 (0=不限，全量)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="单币超时时间（秒）")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每币之间延迟（秒）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    print("=" * 60)
    print("代币解锁数据批量采集")
    print("=" * 60)

    with get_connection(settings.database_url) as conn:
        total_pending = get_total_pending(conn)
        limit = args.limit if args.limit > 0 else total_pending
        print(f"待采集总数: {total_pending}，本次处理: {limit}")

        if limit == 0:
            print("无待采集资产，退出")
            return 0

        assets = get_pending_assets(conn, limit)
        if not assets:
            print("无待采集资产")
            return 0

    success = 0
    fail = 0
    not_found = 0
    t0 = time.time()

    for i, asset in enumerate(assets, 1):
        asset_id = asset["asset_id"]
        symbol = asset.get("symbol", "?")
        print(f"  [{i}/{len(assets)}] asset_id={asset_id} {symbol} ... ",
              end="", flush=True)

        # P2-6: 每 50 个启用一次浏览器首页搜索兜底（提高 API 搜索被拦截时的命中率）
        allow_browser = (i % 50 == 0)
        status, info = run_single(asset_id, timeout=args.timeout,
                                  allow_browser_search=allow_browser)
        if status == "ok":
            success += 1
            print(f"OK ({info})")
        elif status == "not_found":
            not_found += 1
            print(f"NOT_FOUND ({info})")
        else:
            fail += 1
            print(f"FAIL ({info})")

        if i < len(assets) and args.delay > 0:
            time.sleep(args.delay)

        # 每 20 个打印一次进度摘要
        if i % 20 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(assets) - i) / rate if rate > 0 else 0
            print(f"  -- 进度 {i}/{len(assets)} ({i/len(assets)*100:.1f}%), "
                  f"成功 {success}, not_found {not_found}, 失败 {fail}, "
                  f"速度 {rate*60:.1f}/h, 预计剩余 {eta/60:.1f}min --")

    elapsed = time.time() - t0
    print("\n" + "=" * 60)
    print(f"全部完成，耗时 {elapsed:.1f}s ({elapsed/60:.1f}min)")
    print(f"总计: 成功 {success}, not_found {not_found}, 失败 {fail}")
    print(f"平均速度: {len(assets)/elapsed*60:.1f} 币/小时" if elapsed > 0 else "")
    print("=" * 60)

    # P2-5: fail > 0 返回 1，让调度器能感知失败率
    return 1 if fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
