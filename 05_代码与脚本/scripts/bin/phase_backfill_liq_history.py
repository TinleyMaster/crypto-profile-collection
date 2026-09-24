#!/usr/bin/env python3
"""CoinGlass 4h+ 爆仓历史回填 → biz.liquidation_history（P1，一次性 + 可重跑）。

关联方案：04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md §4.4 / §5.2 / §6.1

⚠️ 口径（最重要的一条，勿混）：
  - 本脚本落库的是**分段增量**（每个 4h 区间内新增的爆仓额），
    与 `biz.liquidation_snapshot` 的**滚动窗口绝对值**（`*_liq_usd_1h` 等）口径**不可换算、
    不可相加**。两套口径混用即重演 2026-09-21 审计 P1-1 的「假 0」缺陷。
  - 本表**仅供标定/回测**消费（`workbench/calib_squeeze_liq_thr.py` 等），
    **禁止接入** `scan_squeeze` / `squeeze_fuel` 的任何实时判定分支（粒度不匹配：4h vs 5m）。

符号口径（2026-09-24 `--probe` 实测，勿照抄 `scan_daemon._perp_alias_map()`）：
  - 本脚本是**请求侧**取数：`liquidation/history` 按**交易对**请求 ⇒ 直传本库合约码
    （`BTCUSDT` / `1000PEPEUSDT`），**不做别名映射**。
  - `--scope all` 走 `liquidation/aggregated-history`（**币种级**）：`symbol` 必须传
    **币种基码**（`BTC` / `1000PEPE`）——传合约码不报错但**返回 0 行**（静默空集）
    ⇒ 本脚本按「去 USDT 后缀」映射后请求，落库仍写回合约码；返回 0 行记为 `not_found`，
    **不得当 0 爆仓额**入库。

节流与容错（§4.4）：
  - 单进程**串行** + `min_request_gap = 2.5s`（≈24 req/min，为 daemon 的 coin-list 与手动
    调用留 6 req/min 余量）；**禁止**复用 `scan_daemon.CG_MIN_GAP = 0.3`。
  - 429 / `code != 0` 走**指数退避**（1→2→4→8s，上限 3 次）后跳过该币并记录，**不整轮失败**。
  - 作业必然跨容器执行窗口（全池 ≈ 22 min/口径 ≫ 实例寿命实测 ≈74.6 min 时更明显）
    ⇒ 游标表 + `--resume`（强约束，非可选）。

用法：
    python phase_backfill_liq_history.py --probe                      # 套餐边界复验，不写库
    python phase_backfill_liq_history.py --dry-run --scope binance --days 180
    python phase_backfill_liq_history.py --scope binance --days 180 --resume
    python phase_backfill_liq_history.py --scope all --days 30 --symbols BTCUSDT,1000PEPEUSDT

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

import psycopg.rows  # noqa: E402

from crypto_research.clients.coinglass_client import (  # noqa: E402
    SUPPORTED_INTERVALS_HOBBYIST, CoinGlassClient)
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 24 req/min（= 2.5s 间隔），见 §4.4「节流与容错」；**不得**调小到 daemon 的 0.3s
DEFAULT_MIN_GAP = 2.5
# 接口 limit 上限实测 4500；@4h 服务端最多回 180 天（1080 点）⇒ 单请求覆盖全窗口
MAX_LIMIT = 4500
# 指数退避：1→2→4→8s，上限 3 次重试（之后跳过该币）
RETRY_BACKOFFS = (1, 2, 4, 8)
MAX_RETRIES = 3
# 池内币来源：biz.liquidation_snapshot 近 N 天出现过的 symbol（= 现役扫描池，与早报/判定链同口径）
POOL_LOOKBACK_DAYS = 7
# scope → 落库的 exchange_scope 值（进 PK，禁混算）
SCOPE_BINANCE = "binance"
SCOPE_ALL = "all"
# interval → 小时数（对齐用）
INTERVAL_HOURS = {"4h": 4, "6h": 6, "8h": 8, "12h": 12, "1d": 24, "1w": 168}
# --resume 的跳过量容差（单位：interval 个数）。
# 为什么需要：`done_through` 存的是**已对齐的**最早 ts（接口按 4h 对齐返回），而请求窗口起点
# `NOW()-days` 一般不对齐 ⇒ 二者恒差 0~1 个 interval ⇒ 严格比较（`done_through <= start`）
# 会**永不命中**、--resume 形同虚设（实测：days=30 连跑两次都全量重取 360 行）。
# 容差 1 个 interval 的代价是「窗口最左可能少一格」（≤4h / 180 天），已在输出中披露。
RESUME_TOLERANCE_INTERVALS = 1

UPSERT_SQL = """
    INSERT INTO biz.liquidation_history
        (symbol, interval, exchange_scope, ts, long_liq_usd, short_liq_usd, fetched_at)
    VALUES (%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, interval, exchange_scope, ts) DO UPDATE SET
        long_liq_usd=EXCLUDED.long_liq_usd,
        short_liq_usd=EXCLUDED.short_liq_usd,
        fetched_at=NOW()
"""

CURSOR_SQL = """
    INSERT INTO biz.liquidation_backfill_cursor
        (symbol, interval, exchange_scope, done_through, updated_at)
    VALUES (%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, interval, exchange_scope) DO UPDATE SET
        done_through=EXCLUDED.done_through, updated_at=NOW()
"""


def pool_symbols(conn, lookback_days: int = POOL_LOOKBACK_DAYS) -> list[str]:
    """默认宇宙 = 现役扫描池的合约码（与 biz.liquidation_snapshot / 判定链同口径）。

    不取 Binance `exchangeInfo` 全量：那是「全部 USDT 永续」，含大量不在扫描池内的长尾币
    ⇒ 会平白多花额度，且与 snapshot/早报的 527 池口径不一致（回填是为标定 P0-C 服务的）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.liquidation_snapshot "
            "WHERE ts >= NOW() - make_interval(days => %s) ORDER BY symbol",
            (lookback_days,))
        return [r[0] for r in cur.fetchall()]


def base_code(contract_symbol: str) -> str:
    """合约码 → 币种基码（仅用于 --scope all 的**请求参数**，落库仍写合约码）。

    `BTCUSDT` → `BTC`、`1000PEPEUSDT` → `1000PEPE`（实测聚合接口接受该写法）。
    ⚠️ 与 `scan_daemon._perp_alias_map()` 方向相反：那个是消费 coin-list 响应时的
    反向匹配（含 alias_bases 的「抢码」防抖），本场景（请求侧直传）用不上。
    """
    s = contract_symbol.upper()
    for quote in ("USDT", "USDC", "BUSD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def floor_ts(ms: int, interval: str) -> datetime:
    """接口返回的区间起点（ms）→ UTC datetime（按 interval 对齐，保证 PK 幂等）。"""
    return floor_dt(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc), interval)


def floor_dt(dt: datetime, interval: str) -> datetime:
    """把任意时刻按 interval 向下对齐（与接口返回的区间起点同口径）。"""
    hours = INTERVAL_HOURS.get(interval)
    if not hours:
        return dt.replace(minute=0, second=0, microsecond=0)
    if hours >= 24:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(hour=(dt.hour // hours) * hours, minute=0, second=0, microsecond=0)


def _num(v) -> float | None:
    """接口数值为**字符串**（history）或 number（aggregated）⇒ 统一转 float；缺失留 None。

    缺失≠0：整条记录缺该键时返回 None，**不得**用 0 冒充「无爆仓」。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_history(client: CoinGlassClient, scope: str, symbol: str, interval: str,
                  exchange_list: list[str] | None) -> list[dict]:
    """取单币爆仓历史；返回接口原始行（未解析）。"""
    if scope == SCOPE_BINANCE:
        return client.liquidation_history("Binance", symbol, interval=interval, limit=MAX_LIMIT)
    return client.liquidation_aggregated_history(
        exchange_list or [], base_code(symbol), interval=interval, limit=MAX_LIMIT)


def parse_rows(raw: list[dict], symbol: str, scope: str, interval: str,
               start: datetime) -> list[tuple]:
    """接口行 → 落库元组（按 start 裁剪）。

    scope 不同 ⇒ 字段名不同（实测）：binance = `long_liquidation_usd`，
    all = `aggregated_long_liquidation_usd`。两套都解析，避免静默全 None。
    """
    lk = "long_liquidation_usd" if scope == SCOPE_BINANCE else "aggregated_long_liquidation_usd"
    sk = "short_liquidation_usd" if scope == SCOPE_BINANCE else "aggregated_short_liquidation_usd"
    out = []
    for r in raw:
        ms = r.get("time")
        if ms is None:
            continue
        ts = floor_ts(int(ms), interval)
        if ts < start:
            continue
        out.append((symbol, interval, scope, ts, _num(r.get(lk)), _num(r.get(sk))))
    return out


def fetch_with_retry(client: CoinGlassClient, scope: str, symbol: str, interval: str,
                     exchange_list: list[str] | None) -> tuple[list[dict], str | None]:
    """带指数退避的取数（1→2→4→8s，上限 3 次重试）。返回 (raw, 失败原因)。"""
    last_err: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fetch_history(client, scope, symbol, interval, exchange_list), None
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
    """套餐边界复验（不写库）：逐接口打 1 次，打印 code/msg/upgrade_required/条数。

    含 2 个**负向对照**（若结果由失败转为可用 ⇒ 套餐或官方文档已变更，须更新方案 §2.2/§2.3）。
    """
    ex = client.supported_exchanges()
    print(f"[probe] supported-exchanges: n={len(ex)}  {ex[:6]}…")
    cases: list[tuple[str, str, dict, str]] = [
        ("liquidation/history", "/api/futures/liquidation/history",
         {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 5},
         "期望 code=0（scope=binance 的主接口）"),
        ("liquidation/aggregated-history", "/api/futures/liquidation/aggregated-history",
         {"exchange_list": ",".join(ex), "symbol": "BTC", "interval": "4h", "limit": 5},
         "期望 code=0（scope=all 的主接口；symbol 须为币种基码）"),
        ("liquidation/aggregated-history(缺 exchange_list)", "/api/futures/liquidation/aggregated-history",
         {"symbol": "BTC", "interval": "4h", "limit": 5},
         "期望 code=400（exchange_list 必填 ⇒ 无 all 快捷值）"),
        ("liquidation/aggregated-history(传合约码)", "/api/futures/liquidation/aggregated-history",
         {"exchange_list": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 5},
         "期望 code=0 但 0 行（静默空集 ⇒ 必须映射为币种基码）"),
        ("liquidation/coin-list", "/api/futures/liquidation/coin-list", {},
         "期望 code=0（现役接口，未受影响）"),
        ("liquidation/exchange-list", "/api/futures/liquidation/exchange-list", {"range": "24h"},
         "期望 code=0"),
        ("funding-rate/history", "/api/futures/funding-rate/history",
         {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 5},
         "期望 code=0（P2 候选，本轮不接）"),
        ("[负向对照] liquidation/map", "/api/futures/liquidation/map",
         {"symbol": "BTC"}, "期望非 0（Standard+ 专属；若变可用须更新方案 §2.2）"),
        ("[负向对照] 低于 4h 粒度", "/api/futures/liquidation/history",
         {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 5},
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
            print("  ⚠️ 负向对照意外可用 ⇒ 套餐边界已变，须更新方案 §2.2/§2.3")
    print(f"\n[probe] 完成；意外可用项 {bad} 个")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CoinGlass 4h+ 爆仓历史回填 → biz.liquidation_history")
    ap.add_argument("--probe", action="store_true", help="套餐边界复验，不写库")
    ap.add_argument("--dry-run", action="store_true", help="只算请求数与预计耗时，不写库、不落游标")
    ap.add_argument("--scope", choices=[SCOPE_BINANCE, SCOPE_ALL], default=SCOPE_BINANCE,
                    help="binance=单所（liquidation/history）；all=多所聚合（aggregated-history）")
    ap.add_argument("--interval", default="4h", choices=list(SUPPORTED_INTERVALS_HOBBYIST),
                    help="粒度（默认 4h；Hobbyist 下限 4h）")
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

    if args.interval not in SUPPORTED_INTERVALS_HOBBYIST:
        print(f"[fatal] 粒度 {args.interval} 不受支持（Hobbyist 下限 4h）", file=sys.stderr)
        return 1
    if args.min_gap < 2.0:
        print(f"[warn] --min-gap {args.min_gap}s 低于 2.5s（24 req/min）⇒ 可能与 daemon 争额度",
              file=sys.stderr)

    # 全所口径需 exchange_list（实测必填、无 'all' 快捷值）⇒ 动态取支持列表
    exchange_list: list[str] = []
    if args.scope == SCOPE_ALL:
        try:
            exchange_list = client.supported_exchanges()
        except Exception as e:  # noqa: BLE001
            print(f"[fatal] 取 supported-exchanges 失败：{e}", file=sys.stderr)
            return 1
        if not exchange_list:
            print("[fatal] supported-exchanges 返回空 ⇒ 无法拼 exchange_list", file=sys.stderr)
            return 1

    now = datetime.now(timezone.utc)
    # 窗口起点按 interval 向下对齐（与游标 done_through、接口区间起点同口径，见 RESUME_TOLERANCE_INTERVALS）
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
                    "SELECT symbol FROM biz.liquidation_backfill_cursor "
                    "WHERE interval=%s AND exchange_scope=%s AND done_through <= %s",
                    (args.interval, args.scope, resume_floor))
                done = {r[0] for r in cur.fetchall()}
        todo = [s for s in symbols if s not in done]

        plan = {"scope": args.scope, "interval": args.interval, "days": args.days,
                "window_start": start.isoformat(),
                "resume_tolerance_intervals": RESUME_TOLERANCE_INTERVALS,
                "universe": len(symbols), "resumed_skip": len(done), "todo": len(todo),
                "min_gap_sec": args.min_gap,
                "requests": len(todo) + (1 if args.scope == SCOPE_ALL else 0),
                "est_minutes": round((len(todo) + (1 if args.scope == SCOPE_ALL else 0))
                                     * args.min_gap / 60, 1),
                "max_limit": MAX_LIMIT}
        print(f"[liq-history] scope={args.scope} interval={args.interval} days={args.days} "
              f"宇宙 {len(symbols)}、resume 跳过 {len(done)}、待处理 {len(todo)}")
        print(f"[liq-history] 预计请求 {plan['requests']} 次 × {args.min_gap}s ≈ "
              f"{plan['est_minutes']} 分钟")

        if args.dry_run:
            # ⚠️ 计划里的「2 次/币」是未实测 limit 上限时的乐观估计；2026-09-24 实测
            #    limit 上限 4500 且 @4h 服务端封顶 180 天（1080 点）⇒ 实际 **1 次/币**。
            print(f"[dry-run] 不写库、不落游标。limit 上限实测 {MAX_LIMIT}，@4h 服务端封顶 "
                  f"180 天 ⇒ 每币 1 次请求即可覆盖全窗口")
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
            raw, err = fetch_with_retry(client, args.scope, sym, args.interval, exchange_list)
            if err:
                failed += 1
                failures.append({"symbol": sym, "error": err})
                print(f"[fail] {sym} 取数失败：{err}", file=sys.stderr)
                continue
            rows = parse_rows(raw, sym, args.scope, args.interval, start)
            if not rows:
                # 0 行 ⇒ not_found（scope=all 下多为基码映射不中），**不得**当 0 爆仓额入库
                empty += 1
                if len(not_found) < 20:
                    not_found.append(sym)
                print(f"[skip] {sym} 返回 {len(raw)} 行、裁剪后 0 行 ⇒ 记 not_found，不写库",
                      file=sys.stderr)
                continue
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, rows)
                # 游标仅在整币成功落库后推进（失败/截断不留游标，避免把半截当已完成）
                cur.execute(CURSOR_SQL, (sym, args.interval, args.scope, min(r[3] for r in rows)))
            conn.commit()
            rows_written += len(rows)
            ok += 1
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"[liq-history] {i}/{len(todo)} 币，{rows_written} 行，"
                      f"已用 {el / 60:.1f} 分钟")

        out = dict(plan, ok=ok, failed=failed, empty=empty, rows_written=rows_written,
                   failures=failures[:20], not_found=not_found,
                   elapsed_min=round((time.time() - t0) / 60, 1))
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"\n[liq-history] 完成：成功 {ok} 币 / 失败 {failed} / 空集 {empty}，"
                  f"落库 {rows_written} 行，用时 {out['elapsed_min']} 分钟")
            print("⚠️ 口径提醒：本表为**分段增量**，仅供标定/回测；"
                  "禁止接入 scan_squeeze / squeeze_fuel 实时判定。")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())