#!/usr/bin/env python3
"""BTC/ETH 爆仓极值日 → 自动化新闻归因 → biz.liq_event_attribution（2026-10-03）。

与回测框架配套（workbench/backtest_liq_cascade_btc_eth.py）：
  1. 按同一方法检测极值日（近 90 日相对分位 ≥ 90% × 方向主导 ≥70%），保证事件口径一致；
  2. 每个极值日自动归因：
     - **GDELT 2.0**（免费全历史）：当日 `bitcoin`/`ethereum` 新闻量 + 平均情感 + top 文章；
     - **catalyst 管线**（biz.asset_catalyst）：当日该币催化剂条数（历史稀疏，多数为 0）；
  3. 综合归因标签 `{bucket}:{news_tag}`（交互框架：爆仓极值 × 催化剂 → 趋势持续性）；
  4. UPSERT 落库（PK symbol+event_date+scope，幂等可重跑）。

⚠️ GDELT 环境限制（2026-10-03 实测）：
  共享/云 IP 会被 GDELT **持久 429**（冷却 10 分钟+仍 429，1 req/5s 限频对共享 IP 形同虚设）。
  本机全量回填 GDELT 段会全部落 status='gdelt_unavailable' 占位（缺数据 ≠ 0）。
  **全量 GDELT 回填请在正常网络 IP 执行**（`--resume` 会跳过已有行，不会重复请求）。
  `--gdelt-max-retries 0` 可让 GDELT 快速失败，用于本机演示/排障。

用法：
    python backfill_liq_event_attribution.py --dry-run              # 只打印事件计划
    python backfill_liq_event_attribution.py --limit 5 --gdelt-max-retries 0  # 本机演示（GDELT 快失败）
    python backfill_liq_event_attribution.py --with-articles --top-events 10  # top 事件附文章快照
    python backfill_liq_event_attribution.py --resume               # 正常 IP 全量回填
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = _SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.gdelt_client import GDELTClient  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

PCT_WINDOW = 90          # 分位窗口（交易日），与回测一致
PCT_THR = 0.90           # 极值分位阈值
LONG_SHARE_THR = 0.70    # 方向主导阈值
GDELT_TONE_BULL = 1.0    # AvgTone > +1 → news_bullish
GDELT_TONE_BEAR = -1.0   # AvgTone < -1 → news_bearish
GDELT_QUERY = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum"}

UPSERT_SQL = """
    INSERT INTO biz.liq_event_attribution
        (symbol, event_date, scope, bucket, liq_total, liq_ratio, pct, long_share,
         d1, fwd7, fwd14, gdelt_matched, gdelt_total, gdelt_avg_tone, gdelt_tag,
         gdelt_articles, catalyst_n, attribution, status, err, fetched_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, event_date, scope) DO UPDATE SET
        bucket=EXCLUDED.bucket, liq_total=EXCLUDED.liq_total, liq_ratio=EXCLUDED.liq_ratio,
        pct=EXCLUDED.pct, long_share=EXCLUDED.long_share, d1=EXCLUDED.d1,
        fwd7=EXCLUDED.fwd7, fwd14=EXCLUDED.fwd14,
        gdelt_matched=EXCLUDED.gdelt_matched, gdelt_total=EXCLUDED.gdelt_total,
        gdelt_avg_tone=EXCLUDED.gdelt_avg_tone, gdelt_tag=EXCLUDED.gdelt_tag,
        gdelt_articles=EXCLUDED.gdelt_articles, catalyst_n=EXCLUDED.catalyst_n,
        attribution=EXCLUDED.attribution, status=EXCLUDED.status, err=EXCLUDED.err,
        fetched_at=NOW()
"""

DDL = """
CREATE TABLE IF NOT EXISTS biz.liq_event_attribution (
    symbol text NOT NULL, event_date date NOT NULL,
    scope text NOT NULL DEFAULT 'binance',
    bucket text NOT NULL, liq_total numeric(24,2), liq_ratio numeric(16,8),
    pct numeric(8,4), long_share numeric(8,4), d1 numeric(12,6),
    fwd7 numeric(12,6), fwd14 numeric(12,6),
    gdelt_matched integer, gdelt_total integer, gdelt_avg_tone numeric(12,4),
    gdelt_tag text, gdelt_articles jsonb,
    catalyst_n integer NOT NULL DEFAULT 0,
    attribution text, status text NOT NULL DEFAULT 'ok', err text,
    fetched_at timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, event_date, scope)
)
"""


def load_daily(conn, contract: str, scope: str) -> dict:
    """日频K线 + 爆仓（与回测同一口径）。返回 {date: {...}}。"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, close_px, quote_vol FROM biz.asset_klines "
            "WHERE symbol=%s AND interval='1d' ORDER BY open_time", (contract,))
        for ot, close, vol in cur.fetchall():
            d = ot.replace(tzinfo=None).date()
            out[d] = {"close": float(close), "vol": float(vol or 0)}
        cur.execute(
            "SELECT ts, long_liq_usd, short_liq_usd FROM biz.liquidation_history "
            "WHERE symbol=%s AND interval='1d' AND exchange_scope=%s "
            "ORDER BY ts", (contract, scope))
        for ts, lon, sho in cur.fetchall():
            d = ts.replace(tzinfo=None).date()
            if d in out:
                out[d]["long_liq"] = float(lon or 0)
                out[d]["short_liq"] = float(sho or 0)
    return out


def detect_events(day_map: dict) -> list[dict]:
    """极值日检测（与 backtest_liq_cascade_btc_eth 同方法：pct≥90% × 方向主导）。"""
    days = sorted(day_map)
    closes = {d: day_map[d]["close"] for d in days}
    n = len(days)
    events = []
    for i, d in enumerate(days):
        rec = day_map[d]
        if "long_liq" not in rec:
            continue
        liq = rec["long_liq"] + rec["short_liq"]
        vol = rec["vol"]
        if liq <= 0 or vol <= 0:
            continue
        ratio = liq / vol
        d1 = (closes[d] / closes[days[i - 1]] - 1.0) if i >= 1 else 0.0
        win = [day_map[days[j]]["long_liq"] + day_map[days[j]]["short_liq"]
               for j in range(max(0, i - PCT_WINDOW + 1), i + 1)]
        win = [x / day_map[days[j]]["vol"] for j, x in zip(
            range(max(0, i - PCT_WINDOW + 1), i + 1), win) if day_map[days[j]]["vol"] > 0]
        pct = sum(1 for v in win if v <= ratio) / len(win) if win else 0.5
        long_share = rec["long_liq"] / liq
        if pct < PCT_THR:
            continue
        bucket = "EXT-MIX"
        if long_share >= LONG_SHARE_THR:
            bucket = "EXT-LONG"
        elif (1 - long_share) >= LONG_SHARE_THR:
            bucket = "EXT-SHORT"
        fwd = {}
        for h in (7, 14):
            if i + h < n:
                fwd[h] = closes[days[i + h]] / closes[d] - 1.0
        events.append({
            "date": d, "liq_total": liq, "liq_ratio": ratio, "pct": pct,
            "long_share": long_share, "d1": d1, "bucket": bucket,
            "fwd7": fwd.get(7), "fwd14": fwd.get(14),
        })
    return events


def resolve_asset_id(conn, symbol: str) -> int | None:
    """合约符号 → asset_id（与 scan_daemon._get_asset_id 同策略）。"""
    cands = [symbol, symbol[:-4]]
    if symbol.startswith("1000"):
        cands.append(symbol[4:-4])
    with conn.cursor() as cur:
        for cand in cands:
            cur.execute(
                "SELECT asset_id FROM core.asset WHERE canonical_symbol=%s "
                "ORDER BY market_cap_rank NULLS LAST, asset_id LIMIT 1", (cand,))
            r = cur.fetchone()
            if r:
                return r[0]
    return None


def count_catalyst(conn, asset_id: int | None, d: date) -> int:
    """biz.asset_catalyst 当日（±1 天）条数。asset_id=None 或无表 → 0。"""
    if asset_id is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM biz.asset_catalyst "
                "WHERE asset_id=%s AND published_at::date BETWEEN %s AND %s",
                (asset_id, d - timedelta(days=1), d + timedelta(days=1)))
            return int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001 缺表/缺权限 → 0
        return 0


def gdelt_tag_of(matched: int | None, tone: float | None) -> str:
    if matched is None:
        return "gdelt_unavailable"
    if matched == 0:
        return "no_news"
    if tone is not None and tone > GDELT_TONE_BULL:
        return "news_bullish"
    if tone is not None and tone < GDELT_TONE_BEAR:
        return "news_bearish"
    return "news_neutral"


CACHE_DIR = Path(__file__).resolve().parent.parent / "data"


def load_gdelt_series_cache(symbol: str) -> dict[str, dict] | None:
    """读本地 GDELT 日频序列缓存（避免重复请求限频）。"""
    p = CACHE_DIR / f"gdelt_news_series_{symbol}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_gdelt_series_cache(symbol: str, series: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"gdelt_news_series_{symbol}.json"
    p.write_text(json.dumps(series, ensure_ascii=False), encoding="utf-8")


def build_gdelt_series(gdelt: GDELTClient, symbol: str, query: str,
                       first_day, last_day, refresh: bool) -> dict[str, dict]:
    """构建/加载该币 GDELT 日频新闻序列（timelinevolraw 文章数 + timelinetone 情感）。

    请求量：~6.5 年 / 180 天段 × 2 模式 ≈ 26 次/币（远低于逐事件 ~576 次）。
    断点续跑：每段完成后写本地缓存；重跑时已覆盖段跳过（共享代理 IP 限频下可多轮补完）。
    """
    cache_path = CACHE_DIR / f"gdelt_news_series_{symbol}.json"
    cached = None if refresh else load_gdelt_series_cache(symbol)
    if cached and len(cached) > 100:
        print(f"[gdelt] {symbol} 命中本地缓存: {len(cached)} 天，续跑未覆盖段")
    elif cached:
        print(f"[gdelt] {symbol} 缓存不完整（{len(cached)} 天），续跑补齐")
    else:
        cached = {}
    start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc)
    end = datetime(last_day.year, last_day.month, last_day.day, tzinfo=timezone.utc)
    series = gdelt.timeline_daily_series(query, start, end,
                                         cache_path=str(cache_path), initial=cached)
    save_gdelt_series_cache(symbol, series)
    print(f"[gdelt] {symbol} 序列现覆盖 {len(series)} 天 → 已缓存")
    return series


def main() -> None:
    ap = argparse.ArgumentParser(description="爆仓极值日自动化新闻归因")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--scope", default="binance", choices=["binance", "all"])
    ap.add_argument("--limit", type=int, default=0, help="每币最多处理事件数（0=全部）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true", help="跳过已有 status='ok' 行")
    ap.add_argument("--refresh-gdelt", action="store_true", help="忽略本地缓存重拉 GDELT 序列")
    ap.add_argument("--with-articles", action="store_true", help="top 事件附 GDELT 文章快照")
    ap.add_argument("--top-events", type=int, default=10, help="--with-articles 的事件数")
    ap.add_argument("--gdelt-max-retries", type=int, default=4,
                    help="GDELT 429 重试次数（共享代理 IP 建议 ≥4）")
    args = ap.parse_args()

    s = get_settings()
    gdelt = GDELTClient()
    gdelt.max_retries = args.gdelt_max_retries
    gdelt.retry_backoffs = (8, 16, 32, 64, 120, 180)  # 共享代理 IP：耐心退避，段级自续跑

    with get_connection(s.database_url) as conn:
        # 幂等建表（对标 scan_daemon 的惰性 DDL）
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()

        # ── Phase 0：先构建全部币的 GDELT 日频序列（少请求量，一次搞定） ──
        gdelt_series: dict[str, dict[str, dict]] = {}
        for contract in args.symbols.split(","):
            contract = contract.strip()
            day_map = load_daily(conn, contract, args.scope)
            dates = sorted(d for d in day_map if "long_liq" in day_map[d])
            if not dates:
                continue
            query = GDELT_QUERY.get(contract, "bitcoin")
            gdelt_series[contract] = build_gdelt_series(
                gdelt, contract, query, dates[0], dates[-1], args.refresh_gdelt)

        # ── Phase 1：逐事件归因（GDELT 本地查表，零网络请求） ──
        for contract in args.symbols.split(","):
            contract = contract.strip()
            day_map = load_daily(conn, contract, args.scope)
            events = detect_events(day_map)
            events.sort(key=lambda e: e["liq_total"], reverse=True)
            if args.limit:
                events = events[: args.limit]
            print(f"\n===== {contract} [{args.scope}] 极值事件 n={len(events)} "
                  f"({events[0]['date'] if events else '-'} ~ "
                  f"{events[-1]['date'] if events else '-'}) =====")

            asset_id = resolve_asset_id(conn, contract)
            query = GDELT_QUERY.get(contract, "bitcoin")
            series = gdelt_series.get(contract, {})

            done = skip = no_gdelt = 0
            for idx, ev in enumerate(events):
                if args.resume:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT 1 FROM biz.liq_event_attribution "
                            "WHERE symbol=%s AND event_date=%s AND scope=%s AND status='ok'",
                            (contract, ev["date"], args.scope))
                        if cur.fetchone():
                            skip += 1
                            continue

                # ── GDELT 本地查表 ──
                g_err = None
                rec = series.get(ev["date"].isoformat(), {})
                matched = rec.get("count")
                tone = rec.get("tone")
                if matched is not None:
                    matched = int(matched)
                if tone is not None:
                    tone = float(tone)
                if matched is None:
                    no_gdelt += 1
                    g_err = "gdelt_series_missing"
                g_tag = gdelt_tag_of(matched, tone)

                articles = None
                if args.with_articles and idx < args.top_events and matched:
                    try:
                        arts = gdelt.day_articles(query, datetime(ev["date"].year,
                                                                  ev["date"].month,
                                                                  ev["date"].day,
                                                                  tzinfo=timezone.utc),
                                                  max_records=8)
                        articles = [{"url": a.get("url"), "title": a.get("title"),
                                     "domain": a.get("domain")} for a in arts if a.get("url")]
                    except Exception:  # noqa: BLE001
                        articles = None

                # ── catalyst 管线 ──
                c_n = count_catalyst(conn, asset_id, ev["date"])

                # ── 综合归因 ──
                attribution = f"{ev['bucket']}:{g_tag}"
                status = "ok" if g_tag != "gdelt_unavailable" else "gdelt_unavailable"

                if args.dry_run:
                    print(f"  [dry] {ev['date']} {ev['bucket']} "
                          f"liq={ev['liq_total']/1e6:,.0f}M pct={ev['pct']:.2f} "
                          f"→ {attribution} (gdelt={matched} tone={tone} cat={c_n})")
                    continue

                with conn.cursor() as cur:
                    cur.execute(UPSERT_SQL, (
                        contract, ev["date"], args.scope, ev["bucket"], ev["liq_total"],
                        ev["liq_ratio"], ev["pct"], ev["long_share"], ev["d1"],
                        ev["fwd7"], ev["fwd14"], matched, None, tone, g_tag,
                        json.dumps(articles) if articles else None,
                        c_n, attribution, status, g_err))
                done += 1
                print(f"  [{idx+1}/{len(events)}] {ev['date']} {ev['bucket']} "
                      f"→ {attribution} (gdelt={matched} tone={tone} cat={c_n})")

            print(f"  —— {contract}: 写入={done} 跳过={skip} 无gdelt数据={no_gdelt}")

    print("\n完成。")


if __name__ == "__main__":
    main()
