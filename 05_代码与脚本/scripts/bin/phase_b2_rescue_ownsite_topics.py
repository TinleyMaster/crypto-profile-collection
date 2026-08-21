"""
自有站点主题抢救（第一步）：staging + 多轮深爬 + 按需 SPA 提升。

针对「有官网入口、但缺失自有站点主题」的目标资产，按以下顺序抢救：
  1. staging   从 select_ownsite_rescue_targets.sql 生成目标清单（缺失主题多者优先）。
  2. 多轮深爬  对每个资产重置官网 deep_crawled_at 后，用单资产放宽模式
               （require_doc_keyword=False，含 sitemap.xml 全站索引）反复深爬，
               直到不再有未爬的官网/文档入口，从而发现 /team /treasury /audit 等自有页面。
  3. 按需提升  深爬后若存在 needs_browser=TRUE 的 SPA 页面，才提升到 Playwright 浏览器爬取。

只针对「自有站点主题」（国库/多签、团队/VC、审计、漏洞赏金、交易所上线、公告），
不涉及第三方数据源，官网没有对应页面就跳过（不会强造数据）。

用法：
    python phase_b2_rescue_ownsite_topics.py --dry-run
    python phase_b2_rescue_ownsite_topics.py --limit 20
    python phase_b2_rescue_ownsite_topics.py --limit 20 --rounds 5 --sleep-min 10 --sleep-max 30

依赖环境变量（由 crypto_research.config 自动加载 scripts/.env）：
    DATABASE_URL
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

BIN_DIR = Path(__file__).resolve().parent
PROJECT_SRC = BIN_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

TARGET_SQL = BIN_DIR.parent / "sql" / "biz" / "select_ownsite_rescue_targets.sql"
DEFAULT_PROGRESS = BIN_DIR.parent / "rescue_ownsite_progress.jsonl"

# 自有站点主题：与 select_ownsite_rescue_targets.sql 保持一致
OWNSITE_TOPICS = [
    "treasury_multisig",   # 国库 / 多签
    "team_vc",             # 团队 / VC
    "audit",               # 审计
    "bug_bounty",          # 漏洞赏金
    "exchange_listing",    # 交易所上线
    "announcement",        # 公告
]

DEEP_ARGS = ["--limit", "50", "--workers", "1", "--timeout", "15"]
SPA_ARGS = ["--limit", "20", "--concurrency", "1"]


@contextmanager
def _connect(db_url: str):
    """连接数据库，带重试（PG 服务重启/瞬时抖动时自动重连）。"""
    from crypto_research.db.conn import get_connection

    last_err = None
    ctx = None
    for attempt in range(1, 6):
        try:
            ctx = get_connection(db_url)
            conn = ctx.__enter__()
            break
        except Exception as e:
            last_err = e
            print(f"      [DB] 连接失败（第 {attempt}/5 次），3s 后重试: {str(e)[:80]}")
            time.sleep(3)
    if ctx is None:
        raise last_err
    try:
        yield conn
    except BaseException as e:
        if not ctx.__exit__(type(e), e, e.__traceback__):
            raise
    else:
        ctx.__exit__(None, None, None)


def _load_targets(db_url: str) -> list[dict]:
    from psycopg.rows import dict_row

    sql = TARGET_SQL.read_text(encoding="utf-8")
    with _connect(db_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def _db_exec(db_url: str, sql: str, params: tuple) -> None:
    with _connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def _db_scalar(db_url: str, sql: str, params: tuple) -> int:
    with _connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return int(row[0]) if row else 0


def _reset_website(db_url: str, asset_id: int) -> None:
    """重置官网入口的爬取状态，让单资产放宽模式重新深爬（含 sitemap 全站索引）。"""
    _db_exec(
        db_url,
        """
        UPDATE biz.doc_source_entry
        SET deep_crawled_at = NULL, needs_browser = FALSE, spa_retry_count = 0
        WHERE asset_id = %s AND entity_type = 'asset' AND entry_type = 'official_website'
        """,
        (asset_id,),
    )


def _count_uncrawled(db_url: str, asset_id: int) -> int:
    return _db_scalar(
        db_url,
        """
        SELECT count(*) FROM biz.doc_source_entry
        WHERE asset_id = %s AND entity_type = 'asset'
          AND entry_type IN ('official_website', 'docs', 'docs_portal')
          AND deep_crawled_at IS NULL
        """,
        (asset_id,),
    )


def _count_needs_browser(db_url: str, asset_id: int) -> int:
    return _db_scalar(
        db_url,
        """
        SELECT count(*) FROM biz.doc_source_entry
        WHERE asset_id = %s AND entity_type = 'asset'
          AND needs_browser = TRUE
          AND COALESCE(spa_retry_count, 0) < 3
        """,
        (asset_id,),
    )


def _run_script(script: str, asset_id: int, extra_args: list[str], timeout: int) -> int:
    cmd = [sys.executable, "-u", str(BIN_DIR / script), "--asset-id", str(asset_id)] + extra_args
    print(f"      $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(BIN_DIR), timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        print(f"      [超时] {script} 超过 {timeout}s，跳过。")
        return -1


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


def _append_record(path: Path, asset_id: int, extra: dict) -> None:
    rec = {"asset_id": asset_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **extra}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="自有站点主题抢救（staging + 多轮深爬 + 按需 SPA）")
    parser.add_argument("--limit", type=int, default=20, help="本轮最多处理 N 个资产")
    parser.add_argument("--rounds", type=int, default=5, help="单资产深爬最大轮数")
    parser.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True,
                        help="深爬前重置官网 deep_crawled_at（默认开启）")
    parser.add_argument("--dry-run", action="store_true", help="仅列出待处理资产，不执行")
    parser.add_argument("--sleep-min", type=float, default=10.0, help="资产间随机休眠下限（秒）")
    parser.add_argument("--sleep-max", type=float, default=30.0, help="资产间随机休眠上限（秒）")
    parser.add_argument("--timeout", type=int, default=900, help="单脚本超时（秒）")
    parser.add_argument("--progress-file", type=str, default=str(DEFAULT_PROGRESS))
    args = parser.parse_args()

    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)
    db_url = settings.database_url

    targets = _load_targets(db_url)
    print(f"目标清单共 {len(targets)} 个资产（缺失自有站点主题）")

    progress = Path(args.progress_file)
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
        missing = t["missing_topics"] or []
        print(f"[{processed + 1}/{min(len(pending), args.limit)}] asset_id={aid} {sym} 缺{len(missing)}项: {','.join(missing)}")

        if args.dry_run:
            processed += 1
            continue

        fail = False

        # 1) staging：重置官网，准备放宽模式深爬
        if args.reset:
            _reset_website(db_url, aid)

        # 2) 多轮深爬
        for r in range(1, args.rounds + 1):
            rc = _run_script("phase_b2_deep_doc_discovery.py", aid, DEEP_ARGS, args.timeout)
            remaining = _count_uncrawled(db_url, aid)
            print(f"      第{r}轮深爬 exit={rc}，剩余未爬官网/文档入口: {remaining}")
            if rc != 0 or remaining == 0:
                break

        # 3) 按需提升：仅在存在未超限的 SPA 页面时启动浏览器
        spa_pending = _count_needs_browser(db_url, aid)
        if spa_pending > 0:
            print(f"      存在 {spa_pending} 个 SPA 页面，提升到浏览器爬取")
            rc = _run_script("phase_b2_spa_browser_crawl.py", aid, SPA_ARGS, args.timeout)
            if rc != 0:
                fail = True
        else:
            print(f"      无 SPA 页面，无需提升")

        processed += 1
        if fail:
            fail_count += 1
            _append_record(progress, aid, {"symbol": sym, "status": "fail", "missing": missing})
            print(f"    ✗ 完成 asset_id={aid}（部分失败）")
        else:
            ok_count += 1
            _append_record(progress, aid, {"symbol": sym, "status": "ok", "missing": missing})
            print(f"    ✓ 完成 asset_id={aid}")

        if processed < min(len(pending), args.limit):
            delay = random.uniform(sleep_min, sleep_max)
            print(f"    [节流] 休眠 {delay:.0f}s ...")
            time.sleep(delay)

    print(f"\n本轮结束：处理 {processed}，成功 {ok_count}，失败 {fail_count}")
    print(f"进度文件：{progress}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
