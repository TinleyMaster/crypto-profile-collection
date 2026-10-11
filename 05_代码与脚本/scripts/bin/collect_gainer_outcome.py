#!/usr/bin/env python3
"""涨幅榜信号 · 实际出场结算（scan_gainers_v2 → biz.scan_gainer_outcome）。

做什么
------
把 `biz.scan_gainer_signal` 里每条信号，按**回测同口径的实际出场**结算成
「方向对齐净收益」，幂等落 `biz.scan_gainer_outcome`。日报
（`build_gainer_daily_report.py`）只读这张表。

口径（与 `backtest_exit_profile.py` / `backtest_trade_params.py` 完全对齐）
--------------------------------------------------------------------------
连库取 1h K 线（`biz.asset_klines`，interval='1h'），入场价一律用信号表的
`price_usd`（**不是** K 线收盘）。入场当根 bar 用 `h0 = date_trunc('hour', scan_ts)`：
信号在整点后几秒触发，该 bar 是「入场后第一根」bar（回测里 entry=事件bar收盘，
h_off 从 1 起；线上 h0 ↔ 回测 h_off=1，故线上 offset k ↔ 回测 h_off=k+1）。

* LONG（SHORT_LONG / MID_LONG，方向 +1）——**TRAIL 3% 跟踪止盈**：
  `ph=None` 起逐根 bar（make 升序）：`ph is not None and low <= ph*0.97` → 触发，
  `exit_px=ph*0.97`（出场价 = **触达时刻的滚动峰值** ×0.97），`exit_ts=该 bar open_time`；
  否则 `ph=max(ph, high)`（第一根 ph=high，即首根不出场）。窗口 `[h0, h0+168h]`。
  168h 内未触发 → `exit_type='OPEN'`（censored，**不算亏损、不计入统计**，matured=false）。

* SHORT（TRAP_SHORT，方向 -1）——**FIX 12h / TP50% / SL10%**：逐条镜像
  `backtest_trade_params._returns_vec` 对 direction=-1 的判定顺序与成交价口径：
  做空「止盈」= 价格跌（`low <= entry*(1-0.50)`，对应回测 `sl50` 列）；
  做空「止损」= 价格涨（`high >= entry*(1+0.10)`，对应回测 `hp10` 列）；
  首次触达序号 `t_tp < t_sl` → 止盈；`t_sl <= t_tp` → 止损（同 bar 保守算止损）。
  窗口 12 根 bar（线上 offset 0..11 ↔ 回测 h_off 1..12）；12 根内都未触发 →
  以第 12 根 bar 的 close 了结（`exit_type='FIXCLOSE'`）。

净收益（**方向对齐后扣成本**，与 `_returns_vec` 的 `direction*(exit/entry-1)` 同口径）：
    ret_gross = direction * (exit_px/entry - 1)
    ret_net   = ret_gross - COST

用法
----
    python collect_gainer_outcome.py                      # 增量结算近 7 天
    python collect_gainer_outcome.py --days 30
    python collect_gainer_outcome.py --backfill-from 2026-10-07
    python collect_gainer_outcome.py --force              # 重算全部（口径修复后回填）
    python collect_gainer_outcome.py --dry-run --json     # 只算不写库
    python collect_gainer_outcome.py --selftest           # 回归自检（核对真实库 4 项）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

UTC = timezone.utc
COST = 0.003              # 往返成本（0.3%），对齐 data/exit_profile.json 的 cost0.3
TRAIL = 0.03              # 跟踪止盈回撤（做多）
TRAIL_WINDOW_H = 168     # 做多跟踪窗口（小时）
SHORT_WINDOW_BARS = 12   # 做空固定窗口（根数，=12h）
SHORT_TP = 0.50          # 做空止盈（价格下跌 50%）
SHORT_SL = 0.10          # 做空止损（价格上涨 10%）

# 做多信号类型（走 TRAIL 3% 跟踪止盈）
LONG_TYPES = ("SHORT_LONG", "MID_LONG")

# 场景表（biz.scan_gainer_outcome）：若不存在则建
DDL = """
CREATE TABLE IF NOT EXISTS biz.scan_gainer_outcome (
    signal_id   BIGINT PRIMARY KEY,
    symbol      TEXT,
    signal_type TEXT,
    direction   SMALLINT,
    scan_ts     TIMESTAMPTZ,
    entry_ts    TIMESTAMPTZ,
    entry_px    NUMERIC,
    exit_type   TEXT,
    exit_ts     TIMESTAMPTZ,
    exit_px     NUMERIC,
    hold_hours  NUMERIC,
    ret_gross   NUMERIC,
    ret_net     NUMERIC,
    cost        NUMERIC,
    matured     BOOLEAN,
    updated_at  TIMESTAMPTZ DEFAULT now()
)
"""

# 幂等守卫：已「matured（TRAIL/TP/SL/FIXCLOSE）」的行不再重算；censored(OPEN) 每次重算
# （未到期/未来可能触发，幂等无害）。`--force` 摘掉整段守卫（口径修复后回填历史）。
_GUARD = "AND (o.signal_id IS NULL OR o.matured = FALSE)"

# 候选信号 × 对应窗口 1h K 线（一次拉全，本地按 offset 切片）。
# ⚠️ IN 子句用 `= ANY(%s::text[])`；h0 用 AT TIME ZONE 'UTC' 截断，避免会话时区漂移。
_CAND_SQL = """
WITH cand AS (
    SELECT sg.id AS signal_id, sg.symbol, sg.signal_type, sg.direction,
           sg.scan_ts, sg.price_usd
      FROM biz.scan_gainer_signal sg
      LEFT JOIN biz.scan_gainer_outcome o ON o.signal_id = sg.id
     WHERE sg.scan_ts >= %s
       __GUARD__
)
SELECT c.signal_id, c.symbol, c.signal_type, c.direction, c.scan_ts, c.price_usd,
       (date_trunc('hour', c.scan_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS h0,
       k.open_time, k.high_px, k.low_px, k.close_px
  FROM cand c
  LEFT JOIN biz.asset_klines k
    ON k.symbol = c.symbol AND k.interval = '1h'
   AND k.open_time >= (date_trunc('hour', c.scan_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
   AND k.open_time <= (date_trunc('hour', c.scan_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
                       + INTERVAL '168 hours'
   AND k.high_px > 0 AND k.low_px > 0
 ORDER BY c.signal_id, k.open_time
"""

OUT_COLS = (
    "signal_id", "symbol", "signal_type", "direction", "scan_ts", "entry_ts", "entry_px",
    "exit_type", "exit_ts", "exit_px", "hold_hours", "ret_gross", "ret_net", "cost", "matured",
)


def cand_sql(force: bool = False) -> str:
    """候选 SQL：默认带幂等守卫；`--force` 摘掉守卫（重算全部）。"""
    return _CAND_SQL.replace("__GUARD__", "" if force else _GUARD)


# ──────────────────────────── 纯函数（可离线单测） ────────────────────────────

def _offset_hours(open_time: datetime, h0: datetime) -> int:
    """bar 相对入场整点的偏移（小时，向下取整）。"""
    return int((open_time - h0).total_seconds() // 3600)


def settle_long(bars: list[tuple]) -> dict:
    """做多 TRAIL 3%：bars 为 (open_time, high, low, close) 升序（含 h0 当根）。

    逐根：先用**上一根为止的滚动峰值** ph 判断 `low <= ph*0.97` 是否触发（首根 ph=None
    不判断），未触发再把 `ph = max(ph, high)`。窗口内未触发 → OPEN（censored）。
    """
    ph: float | None = None
    for ot, hi, lo, _cl in bars:
        if ph is not None and float(lo) <= ph * (1 - TRAIL):
            return {"exit_type": "TRAIL", "exit_ts": ot, "exit_px": ph * (1 - TRAIL),
                    "matured": True}
        ph = float(hi) if ph is None else max(ph, float(hi))
    return {"exit_type": "OPEN", "exit_ts": None, "exit_px": None, "matured": False}


def settle_short(entry: float, h0: datetime, bars: list[tuple], now: datetime) -> dict:
    """做空 FIX 12h / TP50% / SL10%：逐条镜像 `_returns_vec(direction=-1)`。

    * 止盈：`low <= entry*(1-TP)` 首次触达序号 t_tp（回测 `sl50` 列）；
    * 止损：`high >= entry*(1+SL)` 首次触达序号 t_sl（回测 `hp10` 列）；
    * 顺序：`t_tp < t_sl` → 止盈；`t_sl <= t_tp` → 止损（同 bar 保守算止损）；
    * 窗口 12 根；都未触发且窗口已走完 → 第 12 根 close 了结（FIXCLOSE）。
    """
    win = bars[:SHORT_WINDOW_BARS]
    t_tp: int | None = None
    t_sl: int | None = None
    for ot, hi, lo, _cl in win:
        off = _offset_hours(ot, h0)
        if t_tp is None and float(lo) <= entry * (1 - SHORT_TP):
            t_tp = off
        if t_sl is None and float(hi) >= entry * (1 + SHORT_SL):
            t_sl = off
    # 触发价
    if t_tp is not None and (t_sl is None or t_tp < t_sl):
        exit_ts = h0 + timedelta(hours=t_tp)
        return {"exit_type": "TP", "exit_ts": exit_ts,
                "exit_px": entry * (1 - SHORT_TP), "matured": True}
    if t_sl is not None and (t_tp is None or t_sl <= t_tp):
        exit_ts = h0 + timedelta(hours=t_sl)
        return {"exit_type": "SL", "exit_ts": exit_ts,
                "exit_px": entry * (1 + SHORT_SL), "matured": True}
    # 时间了结：窗口已走完才结算（避免对未到期信号提前收盘）
    if win and h0 + timedelta(hours=SHORT_WINDOW_BARS) <= now:
        last_ot, _hi, _lo, last_cl = win[-1]
        return {"exit_type": "FIXCLOSE", "exit_ts": last_ot + timedelta(hours=1),
                "exit_px": float(last_cl), "matured": True}
    return {"exit_type": "OPEN", "exit_ts": None, "exit_px": None, "matured": False}


def settle_row(sig: dict, bars: list[tuple], now: datetime, cost: float = COST) -> dict:
    """把一条信号（+窗口内 bars）结算成 `biz.scan_gainer_outcome` 写入行。"""
    direction = 1 if (sig.get("direction") == "LONG") else -1
    out: dict = {
        "signal_id": sig["signal_id"], "symbol": sig["symbol"],
        "signal_type": sig["signal_type"], "direction": direction,
        "scan_ts": sig["scan_ts"], "entry_ts": sig["scan_ts"],
        "entry_px": sig["price_usd"], "cost": cost,
    }
    entry = float(sig["price_usd"]) if sig.get("price_usd") is not None else 0.0
    h0 = sig["h0"]
    if entry <= 0 or not bars:
        out.update(exit_type="OPEN", exit_ts=None, exit_px=None,
                   hold_hours=None, ret_gross=None, ret_net=None, matured=False)
        return out

    res = (settle_long(bars) if sig["signal_type"] in LONG_TYPES
           else settle_short(entry, h0, bars, now))

    out["exit_type"] = res["exit_type"]
    out["exit_ts"] = res["exit_ts"]
    out["exit_px"] = res["exit_px"]
    out["matured"] = res["matured"]
    if res["exit_px"] is None:
        out.update(hold_hours=None, ret_gross=None, ret_net=None)
        return out
    ret_gross = direction * (float(res["exit_px"]) / entry - 1)
    out["ret_gross"] = ret_gross
    out["ret_net"] = ret_gross - cost
    out["hold_hours"] = ((res["exit_ts"] - sig["scan_ts"]).total_seconds() / 3600
                         if res["exit_ts"] is not None else None)
    return out


def _stats(rets: list) -> dict | None:
    """一组「方向对齐净收益」（分数）的统计：n/胜率/赔率/PF/均值/中位/最差。"""
    xs = [float(r) for r in rets if r is not None]
    if not xs:
        return None
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    return {
        "n": len(xs), "win_rate": len(wins) / len(xs),
        "payoff": abs(avg_win / avg_loss) if avg_loss else float("inf"),
        "pf": gross_w / gross_l if gross_l else float("inf"),
        "mean": sum(xs) / len(xs), "median": statistics.median(xs), "worst": min(xs),
    }


# ──────────────────────────── 落库 ────────────────────────────

def ensure_table(conn) -> None:
    """建表（若不存在）；DDL 后立即 commit，避免长时间持锁。"""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def table_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('biz.scan_gainer_outcome') IS NOT NULL")
        return bool(cur.fetchone()[0])


def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(OUT_COLS)
    placeholders = ",".join(["%s"] * len(cols))
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "signal_id")
    sql = (f"INSERT INTO biz.scan_gainer_outcome ({','.join(cols)}, updated_at) "
           f"VALUES ({placeholders}, NOW()) "
           f"ON CONFLICT (signal_id) DO UPDATE SET {updates}, updated_at=NOW()")
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(sql, tuple(r.get(c) for c in cols))
    conn.commit()
    return len(rows)


def load_settled(conn, sql: str, start: datetime, now: datetime, cost: float) -> list[dict]:
    """执行候选 SQL，把「信号 × 窗口 bars」聚合成结算行列表。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, (start,))
        raw = cur.fetchall()
    grouped: dict[int, dict] = {}
    order: list[int] = []
    for r in raw:
        sid = r["signal_id"]
        if sid not in grouped:
            grouped[sid] = {"sig": r, "bars": []}
            order.append(sid)
        if r["open_time"] is not None:
            grouped[sid]["bars"].append((r["open_time"], r["high_px"], r["low_px"], r["close_px"]))
    return [settle_row(grouped[sid]["sig"], grouped[sid]["bars"], now, cost) for sid in order]


def run_selftest(conn, now: datetime, cost: float = COST) -> bool:
    """回归自检：核对真实库 4 项（BRU/MET/OGN + SHORT_LONG 汇总）。全一致返回 True。"""
    rows = load_settled(conn, cand_sql(force=True), datetime(2020, 1, 1, tzinfo=UTC), now, cost)
    by_id = {r["signal_id"]: r for r in rows}
    ok = True

    def _chk(label: str, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got} want={want}")

    def _round(v):
        return None if v is None else round(float(v), 4)

    # 1~3：单信号
    _chk("id=20 BRUSDT exit_type", by_id[20]["exit_type"], "TRAIL")
    _chk("id=20 BRUSDT ret_net", _round(by_id[20]["ret_net"]), -0.0169)
    _chk("id=47 METUSDT exit_type", by_id[47]["exit_type"], "TRAIL")
    _chk("id=47 METUSDT ret_net", _round(by_id[47]["ret_net"]), -0.0279)
    _chk("id=53 OGNUSDT exit_type", by_id[53]["exit_type"], "TRAIL")
    _chk("id=53 OGNUSDT ret_net", _round(by_id[53]["ret_net"]), 0.0884)

    # 4：全部 SHORT_LONG 汇总
    st = _stats([r["ret_net"] for r in rows
                 if r["signal_type"] == "SHORT_LONG" and r["matured"]])
    st = st or {}
    print(f"  SHORT_LONG 汇总: n={st.get('n')} 胜率={st.get('win_rate',0):.4f} "
          f"赔率={st.get('payoff',0):.4f} PF={st.get('pf',0):.4f} "
          f"均收益={st.get('mean',0):.4f} 最差={st.get('worst',0):.4f}")
    _chk("SHORT_LONG n", st.get("n"), 15)
    _chk("SHORT_LONG 胜率", round(st.get("win_rate", 0), 4), 0.6000)
    _chk("SHORT_LONG 赔率", round(st.get("payoff", 0), 2), 2.77)
    _chk("SHORT_LONG PF", round(st.get("pf", 0), 2), 4.15)
    _chk("SHORT_LONG 均收益", round(st.get("mean", 0), 4), 0.0178)
    _chk("SHORT_LONG 最差", round(st.get("worst", 0), 4), -0.0279)
    print(f"[selftest] {'全部一致 ✓' if ok else '存在不一致 ✗'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="涨幅榜信号实际出场结算")
    parser.add_argument("--days", type=int, default=7, help="回看天数（默认 7）")
    parser.add_argument("--backfill-from", type=str, default="",
                        help="回填起点 YYYY-MM-DD（覆盖 --days）")
    parser.add_argument("--cost", type=float, default=COST, help="往返成本（默认 0.003）")
    parser.add_argument("--force", action="store_true", help="重算已结算的行（口径修复后回填）")
    parser.add_argument("--dry-run", action="store_true", help="只结算不写库")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    parser.add_argument("--selftest", action="store_true", help="回归自检（核对真实库 4 项）")
    args = parser.parse_args()

    now = datetime.now(UTC)
    if args.backfill_from:
        start = datetime.fromisoformat(args.backfill_from).replace(tzinfo=UTC)
    else:
        start = now - timedelta(days=args.days)

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        if args.selftest:
            ensure_table(conn)
            return 0 if run_selftest(conn, now, args.cost) else 1
        if not args.dry_run:
            ensure_table(conn)
        # 表尚不存在（如首次 dry-run）时去掉 join 守卫，否则引用未建表报错
        force = args.force or not table_exists(conn)
        settled = load_settled(conn, cand_sql(force), start, now, args.cost)
        written = 0 if args.dry_run else upsert(conn, settled)

    by_type: dict[str, list] = {}
    by_exit: dict[str, int] = {}
    for r in settled:
        by_exit[r["exit_type"]] = by_exit.get(r["exit_type"], 0) + 1
        if r["matured"]:
            by_type.setdefault(r["signal_type"], []).append(r["ret_net"])

    summary = {
        "candidates": len(settled), "written": written,
        "exit_type": by_exit,
        "by_type": {t: _stats(v) for t, v in sorted(by_type.items())},
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[outcome] 候选 {len(settled)} 条 → 写入 {written} 行｜出场分布 "
              + " ".join(f"{k}={v}" for k, v in sorted(by_exit.items())))
        for t, v in sorted(by_type.items()):
            st = _stats(v)
            print(f"  {t:<12} n={st['n']:<4} 胜率 {st['win_rate']:.1%} "
                  f"PF {st['pf']:.2f} 均 {st['mean']*100:+.2f}% 最差 {st['worst']*100:+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())