#!/usr/bin/env python3
"""CoinGlass 盘口深度历史回填 → biz.orderbook_depth_history（SCAN-LIQ-DEPTH-001 阶段 A）。

关联工单：04_架构与代码方案/盘面扫描盘口深度接入工单_SCAN-LIQ-DEPTH-001_2026-09-30.md
触发链：Coinglass套餐数据接入方案 §4.5 P2「orderbook/ask-bids-history」触发条件成立
（真正开始做设计文档 §7 B8「价差/深度过滤」）+ SCAN-LIQ-FILTER-001 §9.10 验证上限。

⚠️ 口径（最重要的三条，勿混）：
  - 本表是「±range_pct 价格区间内的**累计挂单 USD**」（静态盘口**存量**），
    与 `biz.asset_klines.quote_vol`（成交额 = **流量**）**不同量纲**，严禁换算/相加。
  - 接口**无 best bid/ask** ⇒ 价差（spread）维度不可由本表得出（B8 只解「深度」半边）。
  - 本表**仅供标定/回测**消费（calib_*、backtest_*），**禁止接入** scan_daemon
    的任何实时判定分支（粒度错配：最小 4h vs 实时 5m/1h；且静态深度只是可吃量的上界）。

符号口径（2026-09-30 三轮 probe 实测，勿照抄其他端点）：
  - 单所端 `orderbook/ask-bids-history`：`symbol` 直传**合约码**（BTCUSDT；传基码 BTC ⇒ code=400）。
  - 聚合端 `aggregated-ask-bids-history`：`symbol` 必须传**币种基码**（BTC / 1000PEPE）
    ——传合约码 code=0 但**静默 0 行**；`exchange_list` **必填**，支持 'ALL'。
  - 响应 `code` 为**字符串** "0"：判成功用 `str(code) == "0"`（int 比较恒不等，已踩坑）。

接口实测边界（2026-09-30，勿重复探测）：
  - 粒度地板 4h（1h ⇒ code=403 upgrade_required=STANDARD，HTTP 恒 200）。
  - 历史范围 @4h = **180 天**（limit=2000 实回 1080 点）；`limit` 上限被服务端历史截断
    （500/1000/2000 分别回 500/1000/1080）⇒ 单请求 limit=2000 即覆盖全窗口。
  - `range` = **深度百分比**（0.25/0.5/0.75/1/2/3/5/10，缺省 1 ⇒ ±1%）；range=2 ⊃ range=1。
  - **`start_time`/`end_time` 生效**（与 liquidation/history 被忽略不同）⇒ 可精确补窗口。
  - ❌ `orderbook/large-limit-order` ⇒ code=401 Upgrade plan（Standard+，勿接）。

节流与容错（同 phase_backfill_liq_history 的既有纪律）：
  - 单进程**串行** + `min_request_gap = 2.5s`（≈24 req/min，为 daemon coin-list 留余量）。
  - 429 / `code != "0"` 走**指数退避**（1→2→4→8s，上限 3 次）后跳过该币，**不整轮失败**。
  - 游标表 `biz.ob_depth_backfill_cursor` + `--resume`（仅整币全区间成功后推进）。

用法：
    python phase_backfill_ob_depth.py --probe                     # 套餐边界复验，不写库
    python phase_backfill_ob_depth.py --dry-run --scope binance --days 180
    python phase_backfill_ob_depth.py --scope binance --days 180 --resume
    python phase_backfill_ob_depth.py --scope binance --range 1 --symbols BTCUSDT,ETHUSDT

退出码：
    0 = 无失败；1 = 有币种取数失败（部分完成，游标未推进）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.coinglass_client import (  # noqa: E402
    SUPPORTED_INTERVALS_HOBBYIST, CoinGlassClient)
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 24 req/min（= 2.5s 间隔），与 daemon 的 coin-list 共享 30 req/min 全局限额 ⇒ 勿调小
DEFAULT_MIN_GAP = 2.5
# 实测：limit 500/1000/2000 分别回 500/1000/1080 ⇒ @4h 服务端封顶 180 天（1080 点），
# limit=2000 单请求即覆盖全窗口（再大无意义，只被 1080 截断）
MAX_LIMIT = 2000
# 指数退避：1→2→4→8s，上限 3 次重试（之后跳过该币）
RETRY_BACKOFFS = (1, 2, 4, 8)
MAX_RETRIES = 3
# 池内币来源：biz.asset_klines 近 N 天有 1h 条的 symbol（= 扫描池，与 SCAN-LIQ-FILTER-001 同口径）
POOL_LOOKBACK_DAYS = 3
# scope → 落库的 exchange_scope 值（进 PK，禁混算）
SCOPE_BINANCE = "binance"
SCOPE_ALL = "all"
# interval → 小时数（对齐用）
INTERVAL_HOURS = {"4h": 4, "6h": 6, "8h": 8, "12h": 12, "1d": 24, "1w": 168}
# --resume 的跳过量容差（单位：interval 个数；同爆仓回填的教训：严格比较会永不命中）
RESUME_TOLERANCE_INTERVALS = 1

UPSERT_SQL = """
    INSERT INTO biz.orderbook_depth_history
        (symbol, interval, exchange_scope, range_pct, ts,
         bids_usd, asks_usd, bids_quantity, asks_quantity, fetched_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, interval, exchange_scope, range_pct, ts) DO UPDATE SET
        bids_usd=EXCLUDED.bids_usd,
        asks_usd=EXCLUDED.asks_usd,
        bids_quantity=EXCLUDED.bids_quantity,
        asks_quantity=EXCLUDED.asks_quantity,
        fetched_at=NOW()
"""

CURSOR_SQL = """
    INSERT INTO biz.ob_depth_backfill_cursor
        (symbol, interval, exchange_scope, range_pct, done_through, updated_at)
    VALUES (%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, interval, exchange_scope, range_pct) DO UPDATE SET
        done_through=EXCLUDED.done_through, updated_at=NOW()
"""


def pool_symbols(conn, lookback_days: int = POOL_LOOKBACK_DAYS) -> list[str]:
    """默认宇宙 = 扫描池合约码（biz.asset_klines 近 N 天有 1h 条，与 SCAN-LIQ-FILTER-001 同口径）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.asset_klines "
            "WHERE interval='1h' AND open_time >= NOW() - make_interval(days => %s) "
            "ORDER BY symbol",
            (lookback_days,))
        return [r[0] for r in cur.fetchall()]


def base_code(contract_symbol: str) -> str:
    """合约码 → 币种基码（仅用于 --scope all 的**请求参数**，落库仍写合约码）。"""
    s = contract_symbol.upper()
    for quote in ("USDT", "USDC", "BUSD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def floor_dt(dt: datetime, interval: str) -> datetime:
    """把任意时刻按 interval 向下对齐（与接口返回的区间起点同口径）。"""
    hours = INTERVAL_HOURS.get(interval)
    if not hours:
        return dt.replace(minute=0, second=0, microsecond=0)
    if hours >= 24:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(hour=(dt.hour // hours) * hours, minute=0, second=0, microsecond=0)


def floor_ts(ms: int, interval: str) -> datetime:
    """接口返回的区间起点（ms）→ UTC datetime（按 interval 对齐，保证 PK 幂等）。"""
    return floor_dt(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc), interval)


def _num(v) -> float | None:
    """数值统一转 float；缺失留 None（缺失≠0，**不得**用 0 冒充「无挂单」）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_history(client: CoinGlassClient, scope: str, symbol: str, interval: str,
                  range_pct: float, exchange_list: list[str] | None) -> list[dict]:
    """取单币深度历史；返回接口原始行（未解析）。"""
    if scope == SCOPE_BINANCE:
        return client.orderbook_ask_bids_history(
            "Binance", symbol, interval=interval, limit=MAX_LIMIT, range_=range_pct)
    return client.orderbook_aggregated_ask_bids_history(
        exchange_list or "ALL", base_code(symbol), interval=interval,
        limit=MAX_LIMIT, range_=range_pct)


def parse_rows(raw: list[dict], symbol: str, scope: str, interval: str,
               range_pct: float, start: datetime) -> list[tuple]:
    """接口行 → 落库元组（按 start 裁剪）。

    scope 不同 ⇒ 字段名不同（实测）：binance = `bids_usd`，all = `aggregated_bids_usd`。
    """
    pre = "" if scope == SCOPE_BINANCE else "aggregated_"
    out = []
    for r in raw:
        ms = r.get("time")
        if ms is None:
            continue
        ts = floor_ts(int(ms), interval)
        if ts < start:
            continue
        out.append((symbol, interval, scope, range_pct, ts,
                    _num(r.get(f"{pre}bids_usd")), _num(r.get(f"{pre}asks_usd")),
                    _num(r.get(f"{pre}bids_quantity")), _num(r.get(f"{pre}asks_quantity"))))
    return out


def fetch_with_retry(client: CoinGlassClient, scope: str, symbol: str, interval: str,
                     range_pct: float, exchange_list: list[str] | None,
                     ) -> tuple[list[dict], str | None]:
    """带指数退避的取数（1→2→4→8s，上限 3 次重试）。返回 (raw, 失败原因)。"""
    last_err: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fetch_history(client, scope, symbol, interval, range_pct, exchange_list), None
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            if attempt >= MAX_RETRIES:
                break
            wait = RETRY_BACKOFFS[min(attempt, len(RETRY_BACKOFFS) - 1)]
            print(f"[retry] {symbol} 第 {attempt + 1} 次失败（{last_err}），{wait}s 后重试",
                  file=sys.stderr)
            time.sleep(wait)
    return [], last_err


def probe(client: CoinGlassClient) -> int:
    """套餐边界复验（不写库）：逐接口打 1 次，含 2 个负向对照。

    ⚠️ 响应 `code` 是**字符串** "0"：判定一律 `str(code) == "0"`（int 0 比较恒不等）。
    """
    ob = "/api/futures/orderbook/ask-bids-history"
    ag = "/api/futures/orderbook/aggregated-ask-bids-history"
    cases: list[tuple[str, str, dict, str]] = [
        ("ask-bids-history · 合约码 · 4h", ob,
         {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3, "range": 1},
         "期望 code=0、n>0（主接口）"),
        ("ask-bids-history · 基码 BTC（取值域对照）", ob,
         {"exchange": "Binance", "symbol": "BTC", "interval": "4h", "limit": 3, "range": 1},
         "期望 code=400（单所端 symbol 须为合约码）"),
        ("ask-bids-history · range=0.25 细档", ob,
         {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3, "range": 0.25},
         "期望 code=0（深度百分比支持 0.25）"),
        ("aggregated · 基码 + ALL", ag,
         {"exchange_list": "ALL", "symbol": "BTC", "interval": "4h", "limit": 3, "range": 1},
         "期望 code=0、n>0（聚合端 symbol 为基码、exchange_list 支持 ALL）"),
        ("aggregated · 传合约码（静默空集对照）", ag,
         {"exchange_list": "ALL", "symbol": "BTCUSDT", "interval": "4h", "limit": 3, "range": 1},
         "期望 code=0 但 0 行（静默空集 ⇒ 必须映射为基码）"),
        ("aggregated · 缺 exchange_list", ag,
         {"symbol": "BTC", "interval": "4h", "limit": 3, "range": 1},
         "期望 code=400（exchange_list 必填）"),
        ("[负向对照] large-limit-order", "/api/futures/orderbook/large-limit-order",
         {"exchange": "Binance", "symbol": "BTC", "interval": "4h"},
         "期望 code=401 Upgrade plan（Standard+；若变可用须更新工单 §2）"),
        ("[负向对照] 低于 4h 粒度", ob,
         {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3, "range": 1},
         "期望 code=403 + upgrade_required=STANDARD（HTTP 仍是 200）"),
    ]
    bad = 0
    for label, path, params, expect in cases:
        body = client.get_raw(path, params)
        data = body.get("data")
        n = len(data) if isinstance(data, list) else None
        details = body.get("details") if isinstance(body.get("details"), dict) else {}
        print(f"\n[probe] {label}\n  params={params}\n  http={body.get('_http_status')} "
              f"code={body.get('code')} msg={str(body.get('msg'))[:100]} "
              f"upgrade_required={details.get('upgrade_required')} n={n}\n  期望：{expect}")
        if label.startswith("[负向对照]") and str(body.get("code")) == "0" and n:
            bad += 1
            print("  ⚠️ 负向对照意外可用 ⇒ 套餐边界已变，须更新工单 §2")
    print(f"\n[probe] 完成；意外可用项 {bad} 个")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CoinGlass 盘口深度历史回填 → biz.orderbook_depth_history")
    ap.add_argument("--probe", action="store_true", help="套餐边界复验，不写库")
    ap.add_argument("--dry-run", action="store_true", help="只算请求数与预计耗时，不写库、不落游标")
    ap.add_argument("--scope", choices=[SCOPE_BINANCE, SCOPE_ALL], default=SCOPE_BINANCE,
                    help="binance=单所（ask-bids-history，本期主用）；all=跨所聚合（aggregated-）")
    ap.add_argument("--interval", default="4h", choices=list(SUPPORTED_INTERVALS_HOBBYIST),
                    help="粒度（默认 4h；Hobbyist 下限 4h）")
    ap.add_argument("--range", dest="range_pct", type=float, default=1.0,
                    help="深度百分比带宽（默认 1.0 = ±1%；官方取值 0.25/0.5/0.75/1/2/3/5/10）")
    ap.add_argument("--days", type=int, default=180, help="回填窗口天数（@4h 官方上限 180 天）")
    ap.add_argument("--symbols", default="", help="指定币（逗号分隔，本库合约码）；默认全池")
    ap.add_argument("--resume", action="store_true", help="按游标表跳过已覆盖币种")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP,
                    help=f"请求最小间隔秒（默认 {DEFAULT_MIN_GAP} = 24 req/min；勿调小）")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    if not settings.coinglass_api_key:
        print("[fatal] COINGLASS_API_KEY 未配置", file=sys.stderr)
        return 1
    client = CoinGlassClient(settings.coinglass_api_key, base_url=settings.coinglass_base_url,
                             min_request_gap=args.min_gap)

    if args.probe:
        return probe(client)

    if args.min_gap < 2.0:
        print(f"[warn] --min-gap {args.min_gap}s 低于 2.5s ⇒ 可能与 daemon 争额度",
              file=sys.stderr)

    exchange_list: list[str] = []
    if args.scope == SCOPE_ALL:
        try:
            exchange_list = client.supported_exchanges()
        except Exception as e:  # noqa: BLE001
            print(f"[fatal] 取 supported-exchanges 失败：{e}", file=sys.stderr)
            return 1
        if not exchange_list:
            print("[fatal] supported-exchanges 返回空", file=sys.stderr)
            return 1

    now = datetime.now(timezone.utc)
    start = floor_dt(now - timedelta(days=args.days), args.interval)
    hours = INTERVAL_HOURS.get(args.interval) or 4
    resume_floor = start + timedelta(hours=hours * RESUME_TOLERANCE_INTERVALS)

    with get_connection(settings.database_url) as conn:
        symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                   if args.symbols else pool_symbols(conn))
        symbols = sorted(set(symbols))

        done: set[str] = set()
        if args.resume:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol FROM biz.ob_depth_backfill_cursor "
                    "WHERE interval=%s AND exchange_scope=%s AND range_pct=%s "
                    "AND done_through <= %s",
                    (args.interval, args.scope, args.range_pct, resume_floor))
                done = {r[0] for r in cur.fetchall()}
        todo = [s for s in symbols if s not in done]

        plan = {"scope": args.scope, "interval": args.interval, "range_pct": args.range_pct,
                "days": args.days, "window_start": start.isoformat(),
                "resume_tolerance_intervals": RESUME_TOLERANCE_INTERVALS,
                "universe": len(symbols), "resumed_skip": len(done), "todo": len(todo),
                "min_gap_sec": args.min_gap,
                "requests": len(todo) + (1 if args.scope == SCOPE_ALL else 0),
                "est_minutes": round((len(todo) + (1 if args.scope == SCOPE_ALL else 0))
                                     * args.min_gap / 60, 1),
                "max_limit": MAX_LIMIT}
        print(f"[ob-depth] scope={args.scope} interval={args.interval} range=±{args.range_pct}% "
              f"days={args.days} 宇宙 {len(symbols)}、resume 跳过 {len(done)}、待处理 {len(todo)}")
        print(f"[ob-depth] 预计请求 {plan['requests']} 次 × {args.min_gap}s ≈ "
              f"{plan['est_minutes']} 分钟")

        if args.dry_run:
            print(f"[dry-run] 不写库、不落游标。limit={MAX_LIMIT} 单请求覆盖 180 天 ⇒ 每币 1 次")
            out = dict(plan, dry_run=True)
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        # ── 串行回填（不并发，避免与 daemon 争额度）────────────────────
        rows_written = 0
        ok = failed = empty = 0
        failures: list[dict] = []
        not_found: list[str] = []
        t0 = time.time()
        for i, sym in enumerate(todo, 1):
            raw, err = fetch_with_retry(client, args.scope, sym, args.interval,
                                        args.range_pct, exchange_list)
            if err:
                failed += 1
                failures.append({"symbol": sym, "error": err})
                print(f"[fail] {sym} 取数失败：{err}", file=sys.stderr)
                continue
            rows = parse_rows(raw, sym, args.scope, args.interval, args.range_pct, start)
            if not rows:
                # 0 行 ⇒ not_found（scope=all 下多为基码映射不中 / 币安无该永续），
                # **不得**当 0 挂单入库
                empty += 1
                if len(not_found) < 20:
                    not_found.append(sym)
                print(f"[skip] {sym} 返回 {len(raw)} 行、裁剪后 0 行 ⇒ 记 not_found，不写库",
                      file=sys.stderr)
                continue
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, rows)
                # 游标仅在整币成功落库后推进（失败/截断不留游标，避免把半截当已完成）
                cur.execute(CURSOR_SQL, (sym, args.interval, args.scope, args.range_pct,
                                         min(r[4] for r in rows)))
            conn.commit()
            rows_written += len(rows)
            ok += 1
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"[ob-depth] {i}/{len(todo)} 币，{rows_written} 行，已用 {el / 60:.1f} 分钟")

        out = dict(plan, ok=ok, failed=failed, empty=empty, rows_written=rows_written,
                   failures=failures[:20], not_found=not_found,
                   elapsed_min=round((time.time() - t0) / 60, 1))
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"\n[ob-depth] 完成：成功 {ok} 币 / 失败 {failed} / 空集 {empty}，"
                  f"落库 {rows_written} 行，用时 {out['elapsed_min']} 分钟")
            print("⚠️ 口径提醒：本表为**静态盘口存量**（±range 累计挂单 USD），仅供标定/回测；"
                  "与成交额不同量纲，禁止接入 scan_daemon 实时判定。")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
