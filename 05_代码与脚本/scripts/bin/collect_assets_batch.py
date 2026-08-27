"""
节流批量补齐「新币 + 热门赛道」目标资产的投研资料。

按 scripts/sql/biz/select_target_assets.sql 生成目标清单，逐个调用
collect_asset_materials.py（单 token 流水线）。资产之间随机休眠防封号，
进度写入 jsonl 支持断点续跑。

用法：
    python collect_assets_batch.py --limit 20
    python collect_assets_batch.py --limit 20 --dry-run          # 仅列出待处理资产
    python collect_assets_batch.py --limit 20 --sleep-min 30 --sleep-max 90
    python collect_assets_batch.py --limit 20 --stages deep,spa,ai_classify

依赖环境变量：
    DATABASE_URL
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
PROJECT_SRC = BIN_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

TARGET_SQL = BIN_DIR.parent / "sql" / "biz" / "select_target_assets.sql"
PIPELINE = BIN_DIR / "collect_asset_materials.py"

DEFAULT_PROGRESS = BIN_DIR.parent / "collect_assets_batch_progress.jsonl"
DEFAULT_FAILURES = BIN_DIR.parent / "collect_assets_batch_failures.jsonl"


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Missing required environment variable: DATABASE_URL")
    return url


def _load_done(path: Path) -> set[int]:
    if not path.exists():
        return set()
    done: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            done.add(int(json.loads(line)["asset_id"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return done


def _load_targets(db_url: str) -> list[dict]:
    from crypto_research.db.conn import get_connection
    from psycopg.rows import dict_row

    sql = TARGET_SQL.read_text(encoding="utf-8")
    with get_connection(db_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def _append_record(path: Path, asset_id: int, extra: dict) -> None:
    rec = {"asset_id": asset_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **extra}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _run_pipeline(asset_id: int, stages: str, timeout: int) -> int:
    cmd = [
        sys.executable, str(PIPELINE),
        "--asset-id", str(asset_id),
        "--stages", stages,
        "--timeout", str(timeout),
    ]
    print(f"    $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(BIN_DIR), timeout=None)
        return proc.returncode
    except Exception as e:  # noqa: BLE001
        print(f"    [异常] {e}")
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description="节流批量补齐目标资产投研资料")
    parser.add_argument("--limit", type=int, default=10, help="本轮最多处理 N 个未完成资产")
    parser.add_argument("--dry-run", action="store_true", help="仅列出待处理资产，不执行")
    parser.add_argument("--stages", type=str, default="entry_refresh,deep,spa,third_party,ai_classify",
                        help="透传给 collect_asset_materials.py 的阶段")
    parser.add_argument("--sleep-min", type=float, default=30.0, help="资产间随机休眠下限（秒）")
    parser.add_argument("--sleep-max", type=float, default=90.0, help="资产间随机休眠上限（秒）")
    parser.add_argument("--timeout", type=int, default=900, help="单资产流水线超时（秒）")
    parser.add_argument("--progress-file", type=str, default=str(DEFAULT_PROGRESS))
    parser.add_argument("--failure-file", type=str, default=str(DEFAULT_FAILURES))
    args = parser.parse_args()

    db_url = _db_url()
    targets = _load_targets(db_url)
    print(f"目标清单共 {len(targets)} 个资产")

    progress = Path(args.progress_file)
    failures = Path(args.failure_file)
    done = _load_done(progress)
    print(f"已完成（断点续跑跳过）: {len(done)}")

    pending = [t for t in targets if t["asset_id"] not in done]
    print(f"待处理: {len(pending)}，本轮最多 {args.limit}\n")

    sleep_min = min(args.sleep_min, args.sleep_max)
    sleep_max = max(args.sleep_min, args.sleep_max)

    processed = 0
    ok_count = 0
    fail_count = 0
    for t in pending:
        if processed >= args.limit:
            break
        aid = t["asset_id"]
        sym = t["canonical_symbol"]
        tag = "新币" if t["is_new"] else "赛道"
        secs = ",".join(t["sectors"] or [])
        print(f"[{processed + 1}/{min(len(pending), args.limit)}] asset_id={aid} {sym} [{tag}{'|' + secs if secs else ''}]")

        if args.dry_run:
            processed += 1
            continue

        rc = _run_pipeline(aid, args.stages, args.timeout)
        processed += 1
        if rc == 0:
            ok_count += 1
            _append_record(progress, aid, {"symbol": sym, "status": "ok"})
            print(f"    ✓ 完成 asset_id={aid}")
        else:
            fail_count += 1
            _append_record(failures, aid, {"symbol": sym, "status": f"exit_{rc}"})
            print(f"    ✗ 失败 asset_id={aid} (code={rc})")

        # 防封号：资产之间随机休眠（最后一个资产后不再休眠）
        if processed < min(len(pending), args.limit):
            delay = random.uniform(sleep_min, sleep_max)
            print(f"    [节流] 休眠 {delay:.0f}s ...")
            time.sleep(delay)

    print(f"\n本轮结束：处理 {processed}，成功 {ok_count}，失败 {fail_count}")
    print(f"进度文件：{progress}")
    print(f"失败文件：{failures}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
