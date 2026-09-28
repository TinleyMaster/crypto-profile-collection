#!/usr/bin/env python3
"""CoinGlass V4 跨所衍生品快照 → biz.coinglass_derivatives_snapshot（CGV4-003）。

关联：工单_CoinGlass_V4接入_按优先级_2026-09-28.md（CGV4-003）/
      04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md。

目标：用 CoinGlass V4 **跨所聚合**衍生品数据（OI 聚合/分所/币本位 + 各所资金费率 + 多空比），
  绕开 Binance fapi 单所免费源在当前 IDC 出口被 418 封禁的结构性问题。
  ⚠️ 并行源：**不改动** 既有 binance fapi / biz.asset_derivatives 链路（工单红线⑤ 仅新增）。

取数成本（2026-09-28 HOBBYIST 实测）：
  - 资金费率：`funding-rate/exchange-list` **无参一次拉全**（1900+ 币）⇒ 1 次请求。
  - OI：`open-interest/exchange-list?symbol=<基码>` **每币 1 次**（一次拿全 All 聚合 + 各所 + 币本位）。
  - 多空比：3 接口 × 每币（默认**不接**，见下）。

⚠️ 多空比（L/S）默认不采：项目既有决议「多空比走 Binance 免费端点（`/futures/data/*`）」
  （见 coinglass_client.py 模块 docstring）——重复取无增益且 3 接口/币 × 池规模会吃掉额度。
  如需，用 `--with-ls` 显式开启（仅对 Binance 单所、池内币）。

节流与容错：
  - 串行 + `min_request_gap`（默认 2.5s ≈ 24 req/min，为 scan_daemon 的 coin-list 留余量）。
  - 429 / code != 0 走指数退避（1→2→4→8s，上限 3 次）后跳过该币，**不整轮失败**。
  - 资金费率取不到 ⇒ 降级为「仅 OI」继续（密钥/额度故障不当致命）。

用法：
  python ingest_coinglass_derivatives.py --probe                 # 套餐边界复验，不写库
  python ingest_coinglass_derivatives.py --dry-run               # 只算请求数与耗时
  python ingest_coinglass_derivatives.py --limit 200             # 只采前 200 池内币
  python ingest_coinglass_derivatives.py --symbols BTCUSDT,ETHUSDT
  python ingest_coinglass_derivatives.py --with-ls --limit 50    # 附带 Binance 多空比

退出码：0 = 无失败；1 = 有币种取数失败（部分完成）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.coinglass_client import CoinGlassClient  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 24 req/min（= 2.5s 间隔）；不得调小到 scan_daemon 的 0.3s（会与其争额度触发 429）
DEFAULT_MIN_GAP = 2.5
RETRY_BACKOFFS = (1, 2, 4, 8)
MAX_RETRIES = 3
POOL_LOOKBACK_DAYS = 7
LS_EXCHANGE_DEFAULT = "Binance"

UPSERT_SQL = """
    INSERT INTO biz.coinglass_derivatives_snapshot
        (symbol, exchange, ts, oi_usd, oi_quantity, oi_coin_margin_usd,
         oi_stablecoin_margin_usd, oi_change_24h_pct, funding_rate,
         funding_rate_interval_h, next_funding_time,
         ls_global_long_pct, ls_global_short_pct, ls_global_ratio,
         ls_top_account_long_pct, ls_top_account_short_pct, ls_top_account_ratio,
         ls_top_position_long_pct, ls_top_position_short_pct, ls_top_position_ratio,
         source, fetched_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'coinglass_v4',NOW())
    ON CONFLICT (symbol, exchange, ts) DO UPDATE SET
        oi_usd=COALESCE(EXCLUDED.oi_usd, biz.coinglass_derivatives_snapshot.oi_usd),
        oi_quantity=COALESCE(EXCLUDED.oi_quantity, biz.coinglass_derivatives_snapshot.oi_quantity),
        oi_coin_margin_usd=COALESCE(EXCLUDED.oi_coin_margin_usd, biz.coinglass_derivatives_snapshot.oi_coin_margin_usd),
        oi_stablecoin_margin_usd=COALESCE(EXCLUDED.oi_stablecoin_margin_usd, biz.coinglass_derivatives_snapshot.oi_stablecoin_margin_usd),
        oi_change_24h_pct=COALESCE(EXCLUDED.oi_change_24h_pct, biz.coinglass_derivatives_snapshot.oi_change_24h_pct),
        funding_rate=COALESCE(EXCLUDED.funding_rate, biz.coinglass_derivatives_snapshot.funding_rate),
        funding_rate_interval_h=COALESCE(EXCLUDED.funding_rate_interval_h, biz.coinglass_derivatives_snapshot.funding_rate_interval_h),
        next_funding_time=COALESCE(EXCLUDED.next_funding_time, biz.coinglass_derivatives_snapshot.next_funding_time),
        ls_global_long_pct=COALESCE(EXCLUDED.ls_global_long_pct, biz.coinglass_derivatives_snapshot.ls_global_long_pct),
        ls_global_short_pct=COALESCE(EXCLUDED.ls_global_short_pct, biz.coinglass_derivatives_snapshot.ls_global_short_pct),
        ls_global_ratio=COALESCE(EXCLUDED.ls_global_ratio, biz.coinglass_derivatives_snapshot.ls_global_ratio),
        ls_top_account_long_pct=COALESCE(EXCLUDED.ls_top_account_long_pct, biz.coinglass_derivatives_snapshot.ls_top_account_long_pct),
        ls_top_account_short_pct=COALESCE(EXCLUDED.ls_top_account_short_pct, biz.coinglass_derivatives_snapshot.ls_top_account_short_pct),
        ls_top_account_ratio=COALESCE(EXCLUDED.ls_top_account_ratio, biz.coinglass_derivatives_snapshot.ls_top_account_ratio),
        ls_top_position_long_pct=COALESCE(EXCLUDED.ls_top_position_long_pct, biz.coinglass_derivatives_snapshot.ls_top_position_long_pct),
        ls_top_position_short_pct=COALESCE(EXCLUDED.ls_top_position_short_pct, biz.coinglass_derivatives_snapshot.ls_top_position_short_pct),
        ls_top_position_ratio=COALESCE(EXCLUDED.ls_top_position_ratio, biz.coinglass_derivatives_snapshot.ls_top_position_ratio),
        fetched_at=NOW()
"""


def _num(v) -> float | None:
    """接口数值可能为字符串（OHLC）/ number ⇒ 统一转 float；缺失留 None（≠0）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts_from_ms(ms) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def floor_minute(dt: datetime) -> datetime:
    """快照时刻按分钟对齐（同一轮采集共享同一 ts ⇒ PK 幂等）。"""
    return dt.replace(second=0, microsecond=0)


def pool_symbols(conn, lookback_days: int = POOL_LOOKBACK_DAYS) -> list[str]:
    """默认宇宙 = 现役扫描池的**合约码**（与 biz.liquidation_snapshot 同口径，≈527 币）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.liquidation_snapshot "
            "WHERE ts >= NOW() - make_interval(days => %s) ORDER BY symbol",
            (lookback_days,))
        return [r[0] for r in cur.fetchall()]


def base_code(contract_symbol: str) -> str:
    """合约码 → 币种基码（CoinGlass OI/funding 请求口径）。BTCUSDT→BTC，1000PEPEUSDT→1000PEPE。"""
    s = contract_symbol.upper()
    for quote in ("USDT", "USDC", "BUSD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def fetch_with_retry(fn, *args, **kwargs):
    """带指数退避的取数（1→2→4→8s，上限 3 次重试）。返回 (结果, 失败原因)。"""
    last_err: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs), None
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            if attempt >= MAX_RETRIES:
                break
            wait = RETRY_BACKOFFS[min(attempt, len(RETRY_BACKOFFS) - 1)]
            time.sleep(wait)
    return None, last_err


def build_funding_map(raw: list[dict]) -> dict[str, dict[str, dict]]:
    """funding-rate/exchange-list → {基码: {交易所: {rate, interval_h, next_ts}}}。

    接口每币含 `stablecoin_margin_list` 与 `coin_margin_list`；本表以**稳定币本位**为准
    （与 OI 的 `exchange` 命名一致），币本位暂不落（如后续需要再扩列）。
    """
    out: dict[str, dict[str, dict]] = {}
    for row in raw:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        per_ex: dict[str, dict] = {}
        for item in (row.get("stablecoin_margin_list") or []):
            ex = str(item.get("exchange") or "")
            if not ex:
                continue
            per_ex[ex] = {
                "funding_rate": _num(item.get("funding_rate")),
                "interval_h": _num(item.get("funding_rate_interval")),
                "next_ts": _ts_from_ms(item.get("next_funding_time")),
            }
        out[sym] = per_ex
    return out


def oi_rows_for(oi_raw: list[dict]) -> dict[str, dict]:
    """open-interest/exchange-list → {交易所: {oi 字段}}（含 'All' 聚合行）。"""
    out: dict[str, dict] = {}
    for row in oi_raw:
        ex = str(row.get("exchange") or "")
        if not ex:
            continue
        out[ex] = {
            "oi_usd": _num(row.get("open_interest_usd")),
            "oi_qty": _num(row.get("open_interest_quantity")),
            "oi_coin_margin": _num(row.get("open_interest_by_coin_margin")),
            "oi_stable_margin": _num(row.get("open_interest_by_stable_coin_margin")),
            "oi_chg24h": _num(row.get("open_interest_change_percent_24h")),
        }
    return out


def ls_rows(client: CoinGlassClient, contract: str, exchange: str, interval: str,
            limit: int) -> tuple[dict, str | None]:
    """取 Binance 单所三类多空比的最新一点，合并为一行 dict（缺失留 None）。"""
    out: dict = {}
    getters = [
        (client.global_long_short_account_ratio_history,
         ("global_account_long_percent", "global_account_short_percent",
          "global_account_long_short_ratio"),
         ("ls_global_long_pct", "ls_global_short_pct", "ls_global_ratio")),
        (client.top_long_short_account_ratio_history,
         ("top_account_long_percent", "top_account_short_percent",
          "top_account_long_short_ratio"),
         ("ls_top_account_long_pct", "ls_top_account_short_pct", "ls_top_account_ratio")),
        (client.top_long_short_position_ratio_history,
         ("top_position_long_percent", "top_position_short_percent",
          "top_position_long_short_ratio"),
         ("ls_top_position_long_pct", "ls_top_position_short_pct", "ls_top_position_ratio")),
    ]
    for fn, (k_long, k_short, k_ratio), (c_long, c_short, c_ratio) in getters:
        raw, err = fetch_with_retry(fn, exchange, contract, interval=interval, limit=limit)
        if err:
            return out, err
        if raw:
            last = raw[-1]
            out[c_long] = _num(last.get(k_long))
            out[c_short] = _num(last.get(k_short))
            out[c_ratio] = _num(last.get(k_ratio))
    return out, None


def probe(client: CoinGlassClient) -> int:
    """套餐边界复验（不写库）：逐接口打 1 次，打印 code/msg/条数。"""
    cases: list[tuple[str, callable]] = [
        ("OI exchange-list(BTC)",
         lambda: client.open_interest_exchange_list("BTC")),
        ("OI aggregated-history(BTC)",
         lambda: client.open_interest_aggregated_history("BTC", interval="4h", limit=3)),
        ("funding-rate/exchange-list",
         lambda: client.funding_rate_exchange_list()),
        ("funding-rate/accumulated(1d)",
         lambda: client.funding_rate_accumulated_exchange_list("1d")),
        ("Global L/S(Binance BTCUSDT)",
         lambda: client.global_long_short_account_ratio_history("Binance", "BTCUSDT", interval="4h", limit=3)),
    ]
    bad = 0
    for label, fn in cases:
        try:
            data = fn()
            n = len(data) if isinstance(data, list) else None
            print(f"[probe] {label}: OK n={n} rate_limit={client.last_rate_limit}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"[probe] {label}: FAIL {type(e).__name__}: {e}")
    print(f"\n[probe] 完成；失败项 {bad} 个")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CoinGlass V4 跨所衍生品快照 → biz.coinglass_derivatives_snapshot")
    ap.add_argument("--probe", action="store_true", help="套餐边界复验，不写库")
    ap.add_argument("--dry-run", action="store_true", help="只算请求数与预计耗时，不写库")
    ap.add_argument("--limit", type=int, default=0, help="只采前 N 个池内币（0 = 全池）")
    ap.add_argument("--symbols", default="", help="指定币（逗号分隔，本库合约码）；默认全池")
    ap.add_argument("--with-ls", action="store_true", help="附带 Binance 单所多空比（3 接口/币，默认不采）")
    ap.add_argument("--ls-interval", default="4h", help="多空比粒度（HOBBYIST 下限 4h）")
    ap.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP,
                    help=f"请求最小间隔秒（默认 {DEFAULT_MIN_GAP} = 24 req/min；勿调小）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
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
        print(f"[warn] --min-gap {args.min_gap}s 低于 2.5s（24 req/min）⇒ 可能与 daemon 争额度",
              file=sys.stderr)

    ts = floor_minute(datetime.now(timezone.utc))

    with get_connection(settings.database_url) as conn:
        symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                   if args.symbols else pool_symbols(conn))
        symbols = sorted(set(symbols))
        if args.limit and args.limit > 0:
            symbols = symbols[: args.limit]

        n_req = 1 + len(symbols) * (4 if args.with_ls else 1)
        plan = {"ts": ts.isoformat(), "universe": len(symbols), "with_ls": args.with_ls,
                "min_gap_sec": args.min_gap, "requests": n_req,
                "est_minutes": round(n_req * args.min_gap / 60, 1)}
        print(f"[cg-deriv] ts={ts.isoformat()} 宇宙 {len(symbols)} 币，with_ls={args.with_ls}，"
              f"预计请求 {n_req} 次 × {args.min_gap}s ≈ {plan['est_minutes']} 分钟")

        if args.dry_run:
            out = dict(plan, dry_run=True)
            print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else
                  "[dry-run] 不写库。资金费率 1 次拉全；OI 每币 1 次" +
                  ("；多空比 3 次/币" if args.with_ls else ""))
            return 0

        # ── 1) 资金费率：无参一次拉全（失败降级为仅 OI）──────────────────
        funding_map: dict[str, dict[str, dict]] = {}
        fund_err = None
        raw_fund, fund_err = fetch_with_retry(client.funding_rate_exchange_list)
        if fund_err:
            print(f"[warn] funding-rate/exchange-list 失败（{fund_err}）⇒ 降级为仅 OI",
                  file=sys.stderr)
        else:
            funding_map = build_funding_map(raw_fund or [])
            print(f"[cg-deriv] 资金费率：{len(funding_map)} 币（1 次请求）")

        # ── 2) 逐币 OI（+ 可选多空比）→ 合并 upsert ─────────────────────
        rows_written = 0
        ok = failed = empty = 0
        failures: list[dict] = []
        ls_failed = 0
        t0 = time.time()
        for i, sym in enumerate(symbols, 1):
            base = base_code(sym)
            oi_raw, err = fetch_with_retry(client.open_interest_exchange_list, base)
            if err:
                failed += 1
                failures.append({"symbol": sym, "error": err})
                print(f"[fail] {sym} OI 取数失败：{err}", file=sys.stderr)
                continue
            oi_by_ex = oi_rows_for(oi_raw or [])
            fund_by_ex = funding_map.get(base, {})
            if not oi_by_ex and not fund_by_ex:
                empty += 1
                print(f"[skip] {sym}({base}) OI 与费率均空 ⇒ 不写库", file=sys.stderr)
                continue

            ls_by_ex: dict[str, dict] = {}
            if args.with_ls:
                ls_data, ls_err = ls_rows(client, sym, LS_EXCHANGE_DEFAULT, args.ls_interval, 1)
                if ls_err:
                    ls_failed += 1
                elif ls_data:
                    ls_by_ex[LS_EXCHANGE_DEFAULT] = ls_data

            exchanges = set(oi_by_ex) | set(fund_by_ex) | set(ls_by_ex)
            rows: list[tuple] = []
            for ex in exchanges:
                oi = oi_by_ex.get(ex, {})
                fu = fund_by_ex.get(ex, {})
                ls = ls_by_ex.get(ex, {})
                rows.append((
                    base, ex, ts,
                    oi.get("oi_usd"), oi.get("oi_qty"), oi.get("oi_coin_margin"),
                    oi.get("oi_stable_margin"), oi.get("oi_chg24h"),
                    fu.get("funding_rate"), fu.get("interval_h"), fu.get("next_ts"),
                    ls.get("ls_global_long_pct"), ls.get("ls_global_short_pct"), ls.get("ls_global_ratio"),
                    ls.get("ls_top_account_long_pct"), ls.get("ls_top_account_short_pct"), ls.get("ls_top_account_ratio"),
                    ls.get("ls_top_position_long_pct"), ls.get("ls_top_position_short_pct"), ls.get("ls_top_position_ratio"),
                ))
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, rows)
            conn.commit()
            rows_written += len(rows)
            ok += 1
            if i % 25 == 0 or i == len(symbols):
                el = time.time() - t0
                print(f"[cg-deriv] {i}/{len(symbols)} 币，{rows_written} 行，已用 {el / 60:.1f} 分钟")

        out = dict(plan, ok=ok, failed=failed, empty=empty, ls_failed=ls_failed,
                   rows_written=rows_written, failures=failures[:20],
                   funding_symbols=len(funding_map),
                   elapsed_min=round((time.time() - t0) / 60, 1))
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"\n[cg-deriv] 完成：成功 {ok} 币 / 失败 {failed} / 空集 {empty}，"
                  f"落库 {rows_written} 行，用时 {out['elapsed_min']} 分钟")
            print("⚠️ 口径提醒：exchange='All' 为接口给的跨所聚合行，与分所行严禁相加；"
                  "本表为并行源，不改既有 binance fapi / biz.asset_derivatives 链路。")
        return 1 if (failed or ls_failed) else 0


if __name__ == "__main__":
    sys.exit(main())