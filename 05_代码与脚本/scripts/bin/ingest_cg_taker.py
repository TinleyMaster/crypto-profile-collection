#!/usr/bin/env python3
"""OFI 主动成交失衡采集 → biz.cg_taker_volume_hist（P1.8 资金流因子的 OFI 源）。

为什么有这个脚本
----------------
`biz.asset_flow_factor` 的资金流分有 40% 权重来自 OFI（主动成交失衡），源表是
`biz.cg_taker_volume_hist`。但 2026-10-22 审计发现：

  · 表内 919,191 行，ts 覆盖 2025-10-11 ~ 2026-10-07（整整一年）
  · 而 fetched_at 全部落在 2026-10-06 06:53 ~ 10-07 03:04 这 **8 小时**内
  · 全仓库**没有任何写入方**，只有 9 个读取方

⇒ 结论：这是一次性历史回填，**从未接入定时采集**。表停在 10-07，OFI 断更 15 天，
资金流因子静默用两周前的数据打分。

本脚本补上缺失的采集链路，以 `biz.liquidation_snapshot` 的现役扫描池（≈527 币）
为宇宙，与 `ingest_coinglass_derivatives.py` 同口径。

接口边界（`coinglass_client` docstring 已实测，勿重复探测）
--------------------------------------------------------
  · `taker-buy-sell-volume/history` @4h 服务端最多回 **180 天**（1080 点）
  · `startTime`/`endTime` **被忽略**，只能拿「最新 N 点」
  · 返回 `[{time(ms), taker_buy_volume_usd(str), taker_sell_volume_usd(str)}]`
  · 体积单位疑似百万 USD；消费侧只用比值（(buy-sell)/(buy+sell)）单位自然抵消

增量策略
--------
先一次性查出各 symbol 的 `max(ts)`，只插入比它更新的点 —— 避免每天重复
upsert 1080×527 行做无效冲突检查。`--full` 跳过该优化（回填用）。

用法
----
    python bin/ingest_cg_taker.py --dry-run            # 只算规模，不请求不写库
    python bin/ingest_cg_taker.py --limit 20           # 小批量试跑
    python bin/ingest_cg_taker.py                      # 常态增量（池内全量）
    python bin/ingest_cg_taker.py --full               # 全量回填（180 天）
    python bin/ingest_cg_taker.py --symbols UNIUSDT,BTCUSDT
    python bin/ingest_cg_taker.py --stale              # 只补「表内已断更 >1 天」的币

退出码：0 = 无真实失败（CoinGlass 未收录的跳过不计）；1 = 有真实取数失败
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.coinglass_client import CoinGlassClient, CoinGlassError  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

EXCHANGE = "Binance"
INTERVAL = "4h"
DEFAULT_MIN_GAP = 2.5          # 与其他 coinglass 采集一致，避免与 scan_daemon 争额度
RETRY_BACKOFFS = (1, 2, 4, 8)
MAX_RETRIES = 3
POOL_LOOKBACK_DAYS = 7
# 常态增量只需最近若干点：200 点 @4h ≈ 33 天，足够覆盖漏跑/短故障
DEFAULT_LIMIT_POINTS = 200
# --full 回填：服务端 @4h 封顶 1080 点（180 天），传更大值也会被截断
FULL_LIMIT_POINTS = 2000


# ═══════════════════════════════════════════════════════════════
#  小工具
# ═══════════════════════════════════════════════════════════════
def _ts_from_ms(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _to_num(v):
    """API 返回字符串数值；空串/None/非法 → None（不写 0 冒充数据）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_with_retry(fn, *args, **kwargs):
    """带指数退避的取数（1→2→4→8s，上限 3 次）。返回 (结果, 异常)。"""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs), None
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt >= MAX_RETRIES:
                break
            time.sleep(RETRY_BACKOFFS[min(attempt, len(RETRY_BACKOFFS) - 1)])
    return None, last_err


def is_unsupported_symbol(err) -> bool:
    """CoinGlass 未收录该币（code=400 + 'supported symbol'）⇒ 结构性跳过，不计失败。"""
    if isinstance(err, CoinGlassError) and err.code == "400":
        return "supported symbol" in str(err)
    return False


# ═══════════════════════════════════════════════════════════════
#  数据面
# ═══════════════════════════════════════════════════════════════
def pool_symbols(conn, lookback_days: int = POOL_LOOKBACK_DAYS) -> list[str]:
    """默认宇宙 = 现役扫描池的合约码（与 biz.liquidation_snapshot 同口径）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.liquidation_snapshot "
            "WHERE ts >= NOW() - make_interval(days => %s) ORDER BY symbol",
            (lookback_days,))
        return [r[0] for r in cur.fetchall()]


def last_ts_map(conn, interval: str) -> dict[str, datetime]:
    """各 symbol 在表内的最新 ts（增量边界）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, max(ts) FROM biz.cg_taker_volume_hist "
            "WHERE interval = %s GROUP BY symbol", (interval,))
        return {r[0]: r[1] for r in cur.fetchall()}


def stale_symbols(conn, interval: str, max_lag_hours: int = 24) -> list[str]:
    """表内已断更超过阈值的币（含从未采过的池内币）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_lag_hours)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.symbol
            FROM (SELECT DISTINCT symbol FROM biz.liquidation_snapshot
                  WHERE ts >= NOW() - make_interval(days => %s)) s
            LEFT JOIN (SELECT symbol, max(ts) AS mx FROM biz.cg_taker_volume_hist
                       WHERE interval = %s GROUP BY symbol) h
              ON h.symbol = s.symbol
            WHERE h.mx IS NULL OR h.mx < %s
            ORDER BY s.symbol
            """, (POOL_LOOKBACK_DAYS, interval, cutoff))
        return [r[0] for r in cur.fetchall()]


def build_rows(symbol: str, interval: str, raw: list[dict],
               since: datetime | None) -> tuple[list[tuple], int]:
    """API 原始记录 → DB 行。仅保留比 since 更新的点。返回 (rows, n_skipped)。"""
    rows, skipped = [], 0
    for r in raw or []:
        ts = _ts_from_ms(r.get("time"))
        if ts is None:
            skipped += 1
            continue
        if since is not None and ts <= since:
            skipped += 1
            continue
        buy = _to_num(r.get("taker_buy_volume_usd"))
        sell = _to_num(r.get("taker_sell_volume_usd"))
        if buy is None and sell is None:
            skipped += 1
            continue
        rows.append((symbol, interval, ts, buy, sell))
    return rows, skipped


UPSERT_SQL = """
INSERT INTO biz.cg_taker_volume_hist
    (symbol, interval, ts, taker_buy_usd, taker_sell_usd, fetched_at)
VALUES (%s,%s,%s,%s,%s,NOW())
ON CONFLICT (symbol, interval, ts) DO UPDATE SET
    taker_buy_usd  = COALESCE(EXCLUDED.taker_buy_usd,  biz.cg_taker_volume_hist.taker_buy_usd),
    taker_sell_usd = COALESCE(EXCLUDED.taker_sell_usd, biz.cg_taker_volume_hist.taker_sell_usd),
    fetched_at     = NOW()
"""


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description="OFI 主动成交失衡采集（P1.8 OFI 源）")
    ap.add_argument("--limit", type=int, default=0, help="只采前 N 币（调试）")
    ap.add_argument("--symbols", default="", help="指定币（逗号分隔，合约码）")
    ap.add_argument("--full", action="store_true", help="全量回填（180 天），忽略增量边界")
    ap.add_argument("--stale", action="store_true", help="只补断更 >1 天的币")
    ap.add_argument("--dry-run", action="store_true", help="只算规模，不请求不写库")
    ap.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP)
    ap.add_argument("--interval", default=INTERVAL, help="K 线粒度（默认 4h）")
    args = ap.parse_args()

    iv = args.interval

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        if args.symbols:
            symbols = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
        elif args.stale and not args.full:
            symbols = stale_symbols(conn, iv)
        else:
            symbols = pool_symbols(conn)
        if args.limit:
            symbols = symbols[: args.limit]

        known = {} if args.full else last_ts_map(conn, iv)
        n_missing = sum(1 for s in symbols if s not in known)

        limit_points = FULL_LIMIT_POINTS if args.full else DEFAULT_LIMIT_POINTS
        est_min = len(symbols) * max(args.min_gap, 0.0) / 60.0

        print(f"[cg-taker] interval={iv} 宇宙 {len(symbols)} 币 "
              f"（其中 {n_missing} 币从未采过）｜{'全量回填' if args.full else '增量'} "
              f"｜limit={limit_points} 点 ｜ 预计 {est_min:.1f} 分钟")

        if args.dry_run:
            print("[cg-taker] dry-run：未发起请求、未写库")
            return 0

        if not symbols:
            print("[cg-taker] 无待采币种")
            return 0

        client = CoinGlassClient(settings.coinglass_api_key,
                                 min_request_gap=args.min_gap)

        ok = failed = empty = unsupported = 0
        rows_written = 0
        failures: list[dict] = []
        t0 = time.time()

        for i, sym in enumerate(symbols, 1):
            raw, err = fetch_with_retry(
                client.taker_buy_sell_volume_history,
                EXCHANGE, sym, interval=iv, limit=limit_points)

            if err is not None:
                if is_unsupported_symbol(err):
                    unsupported += 1
                else:
                    failed += 1
                    failures.append({"symbol": sym, "error": f"{type(err).__name__}: {err}"})
                continue

            rows, skipped = build_rows(sym, iv, raw, known.get(sym))
            if not rows:
                empty += 1
                continue

            try:
                with conn.cursor() as cur:
                    cur.executemany(UPSERT_SQL, rows)
                conn.commit()
                rows_written += len(rows)
                ok += 1
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                failed += 1
                failures.append({"symbol": sym, "error": f"DB {type(e).__name__}: {e}"})

            if i % 25 == 0 or i == len(symbols):
                el = (time.time() - t0) / 60.0
                print(f"[cg-taker] {i}/{len(symbols)} 币，写入 {rows_written} 行，已用 {el:.1f} 分钟")

        summary = {
            "interval": iv,
            "universe": len(symbols),
            "ok": ok, "empty": empty, "unsupported": unsupported, "failed": failed,
            "rows_written": rows_written,
            "elapsed_min": round((time.time() - t0) / 60.0, 1),
        }
        print(f"[cg-taker] 完成：写入 {rows_written} 行 ｜ ok={ok} empty={empty} "
              f"unsupported={unsupported} failed={failed} ｜ 用时 {summary['elapsed_min']} 分钟")
        for f in failures[:10]:
            print(f"  ✗ {f['symbol']}: {f['error']}")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
