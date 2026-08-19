"""
社交热度批量采集脚本。
遍历所有有 CoinGecko 映射但尚无社交热度数据的资产，批量采集。

用法:
    python phase_c_social_heat_batch.py --limit 100
    python phase_c_social_heat_batch.py --limit 0  # 全量
"""
from __future__ import annotations

import argparse
import json
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
    """获取有 CG 映射但尚无社交热度的资产列表。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (a.asset_id)
                   a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
                   asm.source_asset_key AS coingecko_id
            FROM core.asset a
            JOIN core.asset_source_map asm
                ON asm.asset_id = a.asset_id AND asm.source_code = 'cg'
            WHERE asm.source_asset_key IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM biz.asset_social_heat sh
                  WHERE sh.asset_id = a.asset_id
              )
            ORDER BY a.asset_id ASC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_total_pending(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM core.asset_source_map asm
            WHERE asm.source_code = 'cg' AND asm.source_asset_key IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM biz.asset_social_heat sh
                  WHERE sh.asset_id = asm.asset_id
              )
            """
        )
        return cur.fetchone()[0]


def run_single(asset_id: int, timeout: int = 60) -> tuple[bool, str]:
    """运行单币社交热度采集，返回 (是否成功, 状态信息)。"""
    script = SCRIPT_DIR / "phase_c_social_heat.py"
    try:
        result = subprocess.run(
            [
                sys.executable, "-u", str(script),
                "--asset-id", str(asset_id),
                "--save",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(SCRIPT_DIR),
        )
        if result.returncode != 0:
            return False, f"exit={result.returncode}"

        # 解析 stdout 最后一行 JSON
        stdout_lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        if not stdout_lines:
            return False, "no output"

        try:
            data = json.loads(stdout_lines[-1])
            status = data.get("status", "unknown")
            if status == "ok":
                score = data.get("score", "?")
                return True, f"score={score}"
            else:
                return False, f"status={status}"
        except (json.JSONDecodeError, ValueError):
            return False, "parse_error"

    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, f"error={e}"


def main():
    parser = argparse.ArgumentParser(description="社交热度批量采集")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多采集数量 (0=不限，全量)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="单币超时时间（秒）")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每币之间延迟（秒，避免触发CG限流）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    print("=" * 60)
    print("社交热度批量采集")
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
    t0 = time.time()

    for i, asset in enumerate(assets, 1):
        asset_id = asset["asset_id"]
        symbol = asset.get("symbol", "?")
        print(f"  [{i}/{len(assets)}] asset_id={asset_id} {symbol} ... ",
              end="", flush=True)

        ok, info = run_single(asset_id, timeout=args.timeout)
        if ok:
            success += 1
            print(f"OK ({info})")
        else:
            fail += 1
            print(f"FAIL ({info})")

        if i < len(assets) and args.delay > 0:
            time.sleep(args.delay)

        # 每 50 个打印一次进度摘要
        if i % 50 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(assets) - i) / rate if rate > 0 else 0
            print(f"  -- 进度 {i}/{len(assets)} ({i/len(assets)*100:.1f}%), "
                  f"速度 {rate:.1f}/min, 预计剩余 {eta/60:.1f}min --")

    elapsed = time.time() - t0
    print("\n" + "=" * 60)
    print(f"全部完成，耗时 {elapsed:.1f}s ({elapsed/60:.1f}min)")
    print(f"总计: 成功 {success}, 失败 {fail}")
    print(f"平均速度: {len(assets)/elapsed*60:.1f} 币/分钟" if elapsed > 0 else "")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
