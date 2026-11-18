#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账务审计：拉币安 income 流水，对账「权益 438.93 → 288.96（−150U）」缺口。

背景（2026-11-18）：用户报账户权益从 11-11 的 438.93 USDT 跌到 288.96 USDT，
差约 150U。但本地 biz.v2_trade_log 的 bot 成交累计只亏 18.08U（已平仓）
+ 0.54U 手续费 → 约 132U 的缺口不在 bot 成交表里。

本脚本用途：**在能直连币安的容器内跑一次**，把交易所侧权威流水落成 CSV，
再用本地分析归因缺口构成。纯只读（只用 GET 端点），不下单、不改账户。

产出：scripts/data/_equity_income_raw.csv
      scripts/data/_equity_account_snapshot.json

用法：
    "C:/python/python311/python.exe" -u bin/audit_equity_flow.py \
        --start 2026-11-01 --end 2026-11-19

容器内：
    python bin/audit_equity_flow.py --start 2026-11-01 --end 2026-11-19
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

DATA_DIR = SCRIPT_DIR.parent / "data"

# incomeType 中文说明（USDTⓈ-M 合约）
INCOME_TYPE_CN = {
    "REALIZED_PNL": "已实现盈亏",
    "COMMISSION": "手续费",
    "FUNDING_FEE": "资金费",
    "TRANSFER": "账户划转",
    "INTERNAL_TRANSFER": "内部划转",
    "WELCOME_BONUS": "新人奖励",
    "INSURANCE_CLEAR": "保险清算",
    "API_REBATE": "API 返佣",
    "CONTEST_REWARD": "竞赛奖励",
    "DELIVERED_SETTELMENT": "交割结算",
    "OPTIONS_PREMIUM_FEE": "期权权利金",
    "OPTIONS_SETTLE_PROFIT": "期权结算盈利",
    "AUTO_EXCHANGE": "自动兑换",
    "COIN_SWAP_DEPOSIT": "币兑换",
    "POSITION_LIMIT_INCREASE_FEE": "仓位限额增量费",
}


def _load_env(path: Path) -> dict:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _client():
    from crypto_research.clients.binance_futures import BinanceFuturesClient

    env = _load_env(SCRIPT_DIR.parent / ".env")
    key = env.get("BINANCE_API_KEY", "")
    secret = env.get("BINANCE_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("[FATAL] .env 缺 BINANCE_API_KEY / BINANCE_API_SECRET")
    return BinanceFuturesClient(api_key=key, api_secret=secret)


def fetch_income(client, start_ms: int, end_ms: int, chunk_days: int = 7) -> list[dict]:
    """按时间分片拉 income 流水（单端点上限 1000 条/次），按 tranId+time 去重。"""
    chunk = chunk_days * 24 * 3600 * 1000
    out: dict[tuple, dict] = {}
    cur = start_ms
    n_req = 0
    while cur < end_ms:
        nxt = min(cur + chunk, end_ms)
        for attempt in range(3):
            try:
                rows = client.get_income(start_ms=cur, end_ms=nxt, limit=1000)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"[warn] 分片 {cur}~{nxt} 拉取失败：{type(e).__name__}: {str(e)[:90]}")
                    rows = []
                else:
                    time.sleep(2 * (attempt + 1))
        n_req += 1
        for r in rows:
            key = (r.get("tranId"), r.get("time"), r.get("incomeType"), r.get("income"))
            out[key] = r
        # 推进游标：本分片最后一条时间 +1ms（防同毫秒丢数据）
        if rows:
            last_ts = max(int(r["time"]) for r in rows)
            cur = last_ts + 1 if last_ts >= nxt else nxt
        else:
            cur = nxt
        print(f"  [fetch] {n_req:3d} 片  累计 {len(out):,} 条  至 {datetime.fromtimestamp(cur/1000, timezone.utc):%Y-%m-%d %H:%M}")
    return list(out.values())


def db_bot_symbols() -> tuple[set[str], float, int]:
    """从本地 DB 取 bot 交易过的 symbol 集合 + 已实现盈亏合计 + 笔数。"""
    try:
        import psycopg
        from crypto_research.config import get_settings

        s = get_settings(require_database=True)
        conn = psycopg.connect(s.database_url, connect_timeout=15)
        conn.execute("SET statement_timeout='60s'")
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM biz.v2_trade_log")
            syms = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT COALESCE(sum(realized_pnl_usdt),0), count(*) FILTER (WHERE close_ts IS NOT NULL) "
                "FROM biz.v2_trade_log")
            pnl, n = cur.fetchone()
        conn.close()
        return syms, float(pnl), int(n)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] DB 查询失败（不阻塞拉数）：{type(e).__name__}: {str(e)[:90]}")
        return set(), 0.0, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-11-01", help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", default="2026-11-19", help="结束日 YYYY-MM-DD（不含）")
    ap.add_argument("--out", default=str(DATA_DIR / "_equity_income_raw.csv"))
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    print("=" * 78)
    print("账务审计：币安 income 流水拉取（只读）")
    print(f"  区间 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}（UTC）")
    print("=" * 78)

    client = _client()
    print("\n[1/4] 拉取 income 流水 ...")
    rows = fetch_income(client, start_ms, end_ms)
    print(f"  ✅ 共 {len(rows):,} 条")

    # ── 落盘 ──
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["time", "time_iso", "tranId", "symbol", "incomeType", "incomeType_cn",
            "income", "asset", "info", "tradeId"]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(rows, key=lambda x: int(x.get("time", 0))):
            t = int(r.get("time", 0))
            w.writerow({
                "time": t,
                "time_iso": datetime.fromtimestamp(t / 1000, timezone.utc).isoformat(),
                "tranId": r.get("tranId"),
                "symbol": r.get("symbol", ""),
                "incomeType": r.get("incomeType", ""),
                "incomeType_cn": INCOME_TYPE_CN.get(r.get("incomeType", ""), ""),
                "income": r.get("income", ""),
                "asset": r.get("asset", ""),
                "info": r.get("info", ""),
                "tradeId": r.get("tradeId", ""),
            })
    print(f"  ✅ 原始流水 → {out_path}")

    # ── 账户快照 ──
    print("\n[2/4] 拉账户快照 ...")
    snap: dict = {"pulled_at": datetime.now(timezone.utc).isoformat()}
    try:
        acct = client.get_account()
        assets = {a["asset"]: a for a in acct.get("assets", [])}
        usdt = assets.get("USDT", {})
        snap["totalWalletBalance"] = acct.get("totalWalletBalance")
        snap["totalMarginBalance"] = acct.get("totalMarginBalance")
        snap["totalUnrealizedProfit"] = acct.get("totalUnrealizedProfit")
        snap["totalPositionInitialMargin"] = acct.get("totalPositionInitialMargin")
        snap["availableBalance"] = usdt.get("availableBalance")
        snap["walletBalance_usdt"] = usdt.get("walletBalance")
        snap["usdt_assets"] = {k: {kk: v.get(kk) for kk in
                                   ("walletBalance", "availableBalance", "marginBalance",
                                    "unrealizedProfit", "initialMargin", "maintMargin")}
                               for k, v in assets.items()
                               if float(v.get("walletBalance", 0) or 0) != 0}
        pos = client.get_position_risk()
        snap["positions"] = [
            {"symbol": p.get("symbol"), "positionAmt": p.get("positionAmt"),
             "entryPrice": p.get("entryPrice"), "markPrice": p.get("markPrice"),
             "unRealizedProfit": p.get("unRealizedProfit"),
             "leverage": p.get("leverage"), "initialMargin": p.get("initialMargin")}
            for p in pos if float(p.get("positionAmt", 0) or 0) != 0]
        print(f"  ✅ 权益 {acct.get('totalMarginBalance')} | 可用 {usdt.get('availableBalance')} "
              f"| 浮盈 {acct.get('totalUnrealizedProfit')}")
    except Exception as e:  # noqa: BLE001
        snap["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"  ⚠️ 账户快照失败：{type(e).__name__}: {str(e)[:100]}")

    snap_path = out_path.with_name("_equity_account_snapshot.json")
    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ 账户快照 → {snap_path}")

    # ── 本地 DB 对账基线 ──
    print("\n[3/4] 本地 DB 对账基线 ...")
    bot_syms, db_pnl, db_n = db_bot_symbols()
    print(f"  bot 交易过的 symbol: {len(bot_syms)} 个")
    print(f"  DB 已实现盈亏合计: {db_pnl:+.4f} USDT（{db_n} 笔已平仓）")

    # ── 汇总 ──
    print("\n[4/4] 汇总分析 ...")
    by_type: dict[str, float] = {}
    by_day: dict[str, float] = {}
    by_sym: dict[str, float] = {}
    for r in rows:
        try:
            inc = float(r.get("income", 0) or 0)
        except (TypeError, ValueError):
            continue
        t = r.get("incomeType", "")
        by_type[t] = by_type.get(t, 0.0) + inc
        day = datetime.fromtimestamp(int(r["time"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0.0) + inc
        sym = r.get("symbol", "") or "(无symbol)"
        by_sym[sym] = by_sym.get(sym, 0.0) + inc

    print("\n  ── 按 incomeType ──")
    for t, v in sorted(by_type.items(), key=lambda x: x[1]):
        print(f"    {t:<22s} {INCOME_TYPE_CN.get(t, ''):<12s} {v:+12.4f}")
    net = sum(by_type.values())
    print(f"    {'合计':<22s} {'':<12s} {net:+12.4f}")

    print("\n  ── 按天（累计净值变动）──")
    run = 0.0
    for d in sorted(by_day):
        run += by_day[d]
        print(f"    {d}  当日 {by_day[d]:+10.4f}   累计 {run:+11.4f}")

    bot_sum = sum(v for s, v in by_sym.items() if s in bot_syms)
    other_sum = sum(v for s, v in by_sym.items() if s not in bot_syms)
    print(f"\n  ── bot 交易过的 symbol 合计: {bot_sum:+.4f}")
    print(f"  ── 其他 symbol 合计:        {other_sum:+.4f}")
    print("\n  ── 按 symbol（绝对值前 15）──")
    for s, v in sorted(by_sym.items(), key=lambda x: x[1])[:15]:
        tag = "bot" if s in bot_syms else "其他"
        print(f"    {s:<18s} [{tag}] {v:+11.4f}")

    print("\n" + "=" * 78)
    print(f"完成。原始流水 {out_path.name}（{len(rows):,} 行）+ 快照 {snap_path.name}")
    print("下一步：在能读 CSV 的机器上做缺口归因（对比 11-11 权益 438.93 基准）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
