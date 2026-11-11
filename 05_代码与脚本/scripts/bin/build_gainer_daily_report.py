#!/usr/bin/env python3
"""涨幅榜实盘策略 · 每日报告（邮件推送）。

做什么
------
读 `biz.scan_gainer_outcome`（由 `collect_gainer_outcome.py` 结算）+ `biz.v2_trade_log`
（账户真实成交），聚合出：
  1. **逐日表**（信号结算）：按 Asia/Shanghai 日期 × signal_type → n / 已结算 /
     censored(OPEN) / 胜率 / 赔率 / PF / 均收益 / 中位 / 最差；
  2. **累积追踪线**：近 7 天 / 近 N 天 / 全部，**「信号结算」与「真实成交」并排**；
  3. **三方对比**：信号结算 × 真实成交 × 回测基准（`data/exit_profile.json`）并排；
  4. **样本量警示**：任一分组 matured n<30 或真实成交 n<30 → 标注「⚠ 样本不足」；
  5. **动量闸门验证**（④）：用执行器同一个 `momentum_gate` 对窗口内信号按信号时刻
     重算放行/拦截，滚动对比两组收益并自动判断方向是否仍与样本外验证一致（✓/⚠），
     附实盘 momentum_decayed 拦截数——回答「闸门放的对不对」；
  6. **本轮改动生效看板**（⑤）：2026-11-11 三项改动（A 档弱动能档 dd 闸门 / B 档名义
     减半 / B 档 N 168h→24h）的「期望 vs 实测」核对表 + dd 闸门放行/拦截对照 + B 档
     改动前后成交校验——回答「改对了没有」；
  7. 顶部一句话结论（信号结算 A 档近 7 天 vs 回测基准 + 同期真实成交）；
  8. **实盘策略运行逻辑**（⑥）：流程 + 闸门 + 限价入场 + 止损/出场全链路，数字实时
     取自 execute_gainers_v2 模块常量（单一事实来源），执行器改动次日自动同步进日报。

口径（重要）
------------
* **实盘白名单 = A（SHORT_LONG）+ B（MID_LONG 右上角）**，来源为
  `bin/execute_gainers_v2.py:LIVE_SIGNALS`；**C（TRAP_SHORT）不实盘**，默认从所有统计中
  剔除（逐日表 / 累积追踪线 / 对比）。`--include-research` 可单列一张
  「研究观察（非实盘）」表，但**绝不并入累积追踪线**。
* **「信号结算」≠ 实盘业绩**：它是对全部信号的理想化回测结算（含被跳过/从未下单的
  信号、信号价入场、无延迟无滑点）；**「真实成交」才是账户业绩**（`biz.v2_trade_log`
  已平仓，盈亏 U 为账户口径，均收益 = 盈亏/名义，按信号日归属窗口）。两者并排展示。
* 告警邮件仅推 A（`scan_gainers_v2.py` 259-262）；B 在实盘白名单但不发告警。
* 净收益 `ret_net`（已扣 0.3% 往返成本）；**只统计 matured 行**（censored(OPEN)
  不计入胜率/PF，避免「还没回撤」的样本系统性高估）。

用法
----
    python build_gainer_daily_report.py                       # 发昨天（默认 email）
    python build_gainer_daily_report.py --date 2026-10-10     # 按天补跑
    python build_gainer_daily_report.py --signal-types SHORT_LONG,MID_LONG  # 覆盖白名单
    python build_gainer_daily_report.py --include-research    # 附 C 研究观察表（非实盘）
    python build_gainer_daily_report.py --no-email --dry-run  # 只打印 HTML 不发送
    python build_gainer_daily_report.py --to a@b.com          # 收件人覆盖
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
for _p in (str(PROJECT_SRC), str(SCRIPT_DIR)):   # bin 也入 path，便于复用 execute_gainers_v2
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

SH = timezone(timedelta(hours=8))     # 报告口径与展示时间统一用北京时间
MIN_N = 30                            # 样本量门槛（不足则警示）
EXIT_PROFILE = SCRIPT_DIR.parent / "data" / "exit_profile.json"
TYPE_LABEL = {"SHORT_LONG": "A 短线做多", "MID_LONG": "B 次档做多", "TRAP_SHORT": "C 诱多做空"}

# ── 实盘信号白名单（单一事实来源：优先复用 execute_gainers_v2.LIVE_SIGNALS）──
# execute_gainers_v2 顶层只有 import + 常量（get_settings/币安 client 均在函数内，
# 见其 187/196 行），import 不触发网络/读 .env/下单等副作用，故直接 import 复用，避免两处定义漂移。
# 若 import 失败（路径/环境异常）才回退到本地常量 —— ⚠️ 回退值须与
# bin/execute_gainers_v2.py:LIVE_SIGNALS 保持同步。
_LIVE_SIGNALS_FALLBACK = ("SHORT_LONG", "MID_LONG")
try:
    from execute_gainers_v2 import LIVE_SIGNALS as _LIVE_SIGNALS
    LIVE_SIGNALS: tuple[str, ...] = tuple(_LIVE_SIGNALS)
except Exception:                     # noqa: BLE001 — 任何 import 异常都回退，保证日报可用
    LIVE_SIGNALS = _LIVE_SIGNALS_FALLBACK

# 动量闸门：与执行器共用同一实现（单一事实来源，避免日报/实盘两处规则漂移）。
# 导入失败（路径/环境异常）→ 闸门验证节降级为提示，不影响日报其余部分。
try:
    from execute_gainers_v2 import momentum_gate as _momentum_gate
except Exception:                     # noqa: BLE001
    _momentum_gate = None

# ⑤ 策略运行逻辑节：引用整个执行器模块，常量（PARAMS/杠杆/名义/时效/限价/止损/保本）
# 实时读取——执行器改动后次日日报自动同步，避免日报文案与代码漂移。
try:
    import execute_gainers_v2 as _v2
except Exception:                     # noqa: BLE001
    _v2 = None

# 实盘 = A + B（B 为 MID_LONG 右上角子集）；C（TRAP_SHORT）不实盘。
# 告警邮件仅推 A（scan_gainers_v2.py 259-262），B 虽实盘但不发告警。
ALERT_SIGNALS = ("SHORT_LONG",)

OUTCOME_SQL = """
SELECT o.signal_id, o.symbol, o.signal_type, o.scan_ts, o.exit_type, o.matured, o.ret_net
  FROM biz.scan_gainer_outcome o
 ORDER BY o.scan_ts
"""

# 真实成交（账户口径）：origin='bot' 的实际下单记录，窗口按信号日 scan_ts 归属
TRADE_SQL = """
SELECT t.signal_type, t.scan_ts, t.close_ts, t.realized_pnl_usdt, t.notional_usdt
  FROM biz.v2_trade_log t
 WHERE t.origin = 'bot'
 ORDER BY t.scan_ts
"""

# 实盘闸门实际拦截数：执行器写进 params JSON 的 exec_reason（闸门部署后才有非零值）
GATE_SKIP_SQL = """
SELECT s.scan_ts
  FROM biz.scan_gainer_signal s
 WHERE s.signal_type = ANY(%s::text[])
   AND s.params->>'exec_reason' = 'momentum_decayed'
"""

# 本轮改动（2026-11-11）生效看板 · dd 闸门对照：
#   dd_pct 优先取扫描器落库的 dd_24h_pct（闸门部署后才有）；历史信号则用 biz.asset_klines
#   1h 按 params.event_bar_ts 重算，口径与 bin/backtest_a_position.py 一致：
#   dd = 1 − 事件 bar 收盘 / max(事件 bar 及其前 23 根 1h 的 high)。
DD_AUDIT_SQL = """
SELECT s.id AS signal_id, s.scan_ts, s.signal_type, s.chg_24h_pct,
       s.params->>'exec_reason' AS exec_reason,
       COALESCE((s.params->>'dd_24h_pct')::numeric, dd.dd * 100) AS dd_pct,
       o.matured, o.exit_type, o.ret_net
  FROM biz.scan_gainer_signal s
  LEFT JOIN biz.scan_gainer_outcome o ON o.signal_id = s.id
  LEFT JOIN LATERAL (
      SELECT 1 - c.close_px / NULLIF(m.hi, 0) AS dd
        FROM biz.asset_klines c
        JOIN LATERAL (
            SELECT MAX(high_px) AS hi
              FROM biz.asset_klines x
             WHERE x.symbol = c.symbol AND x.interval = '1h'
               AND x.open_time BETWEEN c.open_time - INTERVAL '23 hours' AND c.open_time
        ) m ON true
       WHERE c.symbol = s.symbol AND c.interval = '1h'
         AND c.open_time = (s.params->>'event_bar_ts')::timestamptz
  ) dd ON true
 WHERE s.signal_type = ANY(%s::text[])
 ORDER BY s.scan_ts
"""

# 本轮改动生效看板 · B 档成交校验：名义是否落到 10U、计划持仓是否 24h、有无 168h 到期出场
MID_CHECK_SQL = """
SELECT t.open_ts, t.close_ts, t.notional_usdt, t.planned_hold_h, t.exit_reason
  FROM biz.v2_trade_log t
 WHERE t.origin = 'bot' AND t.signal_type = 'MID_LONG'
 ORDER BY t.open_ts
"""

# 本轮改动上线日（北京时间）：早于该日的成交属于「改动前」，用于前后对比。
# ⚠ 与 execute_gainers_v2.py 的改动日期保持一致；仅作展示分组，不参与任何交易决策。
ROLLOUT_DAY = date(2026, 11, 11)


# ──────────────────────────── 统计与格式化 ────────────────────────────

def _stats(rows: list[dict]) -> dict:
    """一组结算行 → {n, matured, censored, win_rate, payoff, pf, mean, median, worst}。"""
    n = len(rows)
    matured = [r for r in rows if r["matured"] and r["ret_net"] is not None]
    xs = [float(r["ret_net"]) for r in matured]
    out = {
        "n": n, "matured": len(matured),
        "censored": sum(1 for r in rows if r["exit_type"] == "OPEN"),
        "win_rate": None, "payoff": None, "pf": None, "mean": None, "median": None, "worst": None,
    }
    if not xs:
        return out
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gw, gl = sum(wins), abs(sum(losses))
    out.update(
        win_rate=len(wins) / len(xs),
        payoff=abs(avg_win / avg_loss) if avg_loss else float("inf"),
        pf=gw / gl if gl else float("inf"),
        mean=sum(xs) / len(xs), median=statistics.median(xs), worst=min(xs),
    )
    return out


def _pct(v, nd=1) -> str:
    return "-" if v is None else f"{float(v) * 100:.{nd}f}%"


def _num(v, nd=2) -> str:
    return "-" if v is None else f"{float(v):.{nd}f}"


def _sgn(v, nd=2) -> str:
    return "-" if v is None else f"{float(v) * 100:+.{nd}f}%"


def _u(v, nd=2) -> str:
    return "-" if v is None else f"{float(v):+.{nd}f}U"


def _trade_stats(rows: list[dict]) -> dict:
    """真实成交统计（仅已平仓；窗口按信号日 scan_ts 归属）。

    → {n, open_n, win_rate, pf, mean, pnl}
      - win_rate / pnl：账户口径（realized_pnl_usdt）
      - mean：均收益率 = 每笔 realized/notional（与信号结算 ret_net 同为价格收益率口径，
        可与回测/结算的百分比并排；缺 notional 的行不计入）
    """
    closed = [r for r in rows if r.get("close_ts") is not None
              and r.get("realized_pnl_usdt") is not None]
    out = {"n": len(closed),
           "open_n": sum(1 for r in rows if r.get("close_ts") is None),
           "win_rate": None, "pf": None, "mean": None, "pnl": None}
    if not closed:
        return out
    pnls = [float(r["realized_pnl_usdt"]) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    rets = [r["ret"] for r in closed if r.get("ret") is not None]
    out.update(
        win_rate=len(wins) / len(pnls),
        pf=(gw / gl if gl else (float("inf") if wins else None)),
        mean=(sum(rets) / len(rets) if rets else None),
        pnl=sum(pnls),
    )
    return out


def load_exit_profile() -> dict:
    if not EXIT_PROFILE.exists():
        return {}
    try:
        return json.loads(EXIT_PROFILE.read_text(encoding="utf-8")).get("signals", {})
    except Exception:
        return {}


# ──────────────────────────── 取数与聚合 ────────────────────────────

def load(conn) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(OUTCOME_SQL)
        rows = cur.fetchall()
    for r in rows:
        r["day"] = r["scan_ts"].astimezone(SH).date()
    return rows


def load_trades(conn) -> list[dict]:
    """真实成交行（origin='bot'），补充 day（信号日）与 ret（盈亏/名义，价格收益率口径）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(TRADE_SQL)
        rows = cur.fetchall()
    out = []
    for r in rows:
        if r.get("scan_ts") is None:      # 早期手工/异常行无信号时间，无法归入任何窗口
            continue
        r["day"] = r["scan_ts"].astimezone(SH).date()
        pnl, notional = r.get("realized_pnl_usdt"), r.get("notional_usdt")
        r["ret"] = (float(pnl) / float(notional)
                    if pnl is not None and notional else None)
        out.append(r)
    return out


def load_gates(conn, settings, rows: list[dict]) -> list[dict]:
    """对每条已结算信号用**执行器同一 momentum_gate** 重算放行/拦截（复用连接）。

    rows：已过滤到实盘白名单、matured 且有 ret_net 的结算行。
    返回追加了 pass / gate_info 的行（fail_open 也计为放行，info 里带 gate 原因）。
    """
    if _momentum_gate is None:
        return []
    out = []
    for r in rows:
        sig = {"symbol": r["symbol"], "scan_ts": r["scan_ts"]}
        try:
            ok, info = _momentum_gate(settings, sig, conn=conn)
        except Exception as e:  # noqa: BLE001 — 日报其余部分不应被闸门故障拖垮
            ok, info = True, {"gate": f"error_{type(e).__name__}"}
        out.append({**r, "pass": ok, "gate_info": info})
    return out


def load_gate_skips(conn, live_types: tuple[str, ...]) -> list[dict]:
    """实盘真正被 momentum_decayed 拦截的信号（params.exec_reason），带 day 供窗口过滤。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(GATE_SKIP_SQL, [list(live_types)])
        rows = cur.fetchall()
    for r in rows:
        r["day"] = r["scan_ts"].astimezone(SH).date()
    return rows


def load_change_audit(conn, live_types: tuple[str, ...]) -> tuple[list[dict], list[dict], bool]:
    """⑤ 本轮改动生效看板取数 → (dd 对照行, B 档成交行, position_scale 列是否存在)。

    dd 对照行：A 档信号 + 24h 回撤 dd_pct + 实盘拦截原因 + 理想化结算收益。
    B 档成交行：origin='bot' 的 MID_LONG 实际下单（名义 / 计划持仓 / 出场原因）。
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(DD_AUDIT_SQL, [list(live_types)])
        dd_rows = cur.fetchall()
        cur.execute(MID_CHECK_SQL)
        mid_rows = cur.fetchall()
        cur.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'biz' AND table_name = 'v2_trade_log' "
                    "AND column_name = 'position_scale'")
        has_scale = cur.fetchone() is not None
    for r in dd_rows:
        r["day"] = r["scan_ts"].astimezone(SH).date()
    for r in mid_rows:
        r["day"] = r["open_ts"].astimezone(SH).date()
    return dd_rows, mid_rows, has_scale


def window_rows(rows: list[dict], anchor: date, days: int | None) -> list[dict]:
    """近 days 天（含 anchor）的行；days=None 表示全部。"""
    if days is None:
        return rows
    lo = anchor - timedelta(days=days - 1)
    return [r for r in rows if lo <= r["day"] <= anchor]


def by_type(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["signal_type"], []).append(r)
    return out


# ──────────────────────────── 渲染 ────────────────────────────

def _daily_table(rows: list[dict]) -> str:
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["day"], r["signal_type"]), []).append(r)
    trs = []
    for (day, sig) in sorted(groups, reverse=True):
        st = _stats(groups[(day, sig)])
        trs.append(
            f"<tr><td>{day}</td><td>{TYPE_LABEL.get(sig, sig)}</td><td>{st['n']}</td>"
            f"<td>{st['matured']}</td><td>{st['censored']}</td><td>{_pct(st['win_rate'])}</td>"
            f"<td>{_num(st['payoff'])}</td><td>{_num(st['pf'])}</td><td>{_sgn(st['mean'])}</td>"
            f"<td>{_sgn(st['median'])}</td><td>{_sgn(st['worst'], 1)}</td></tr>")
    return f"""
    <p style="margin:14px 0 4px"><b>① 逐日表 · 信号结算</b>（A+B；Asia/Shanghai，按信号类型分组）
      <span style="color:#777;font-weight:normal">— 理想化结算，含未成交信号，非账户业绩</span></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th>日期</th><th>信号</th><th>n</th><th>已结算</th>
          <th>剔除(OPEN)</th><th>胜率</th><th>赔率</th><th>PF</th><th>均收益</th><th>中位</th><th>最差</th></tr>
      {''.join(trs) or '<tr><td colspan="11">无数据</td></tr>'}
    </table>
    <p style="margin:6px 0 0;color:#777;font-size:12px">
      列出全部有结算的日期。「剔除(OPEN)」为 censored（窗口内未触发跟踪止盈/未到期），
      <b>不计入</b>胜率/PF。</p>"""


def _track_table(rows: list[dict], trades: list[dict], anchor: date, days: int) -> str:
    """累积追踪线：左块=信号结算（理想化），右块=真实成交（账户），并排。"""
    wins = [("近 7 天", 7), (f"近 {days} 天", days), ("全部", None)]
    trs = []
    for label, w in wins:
        st = _stats(window_rows(rows, anchor, w))
        ts = _trade_stats(window_rows(trades, anchor, w))
        trs.append(
            f"<tr><td>{label}</td>"
            f"<td>{st['matured']}</td><td>{_pct(st['win_rate'])}</td>"
            f"<td>{_num(st['pf'])}</td><td>{_sgn(st['mean'])}</td>"
            f"<td>{ts['n']}</td><td>{_pct(ts['win_rate'])}</td>"
            f"<td>{_num(ts['pf'])}</td><td>{_sgn(ts['mean'])}</td><td>{_u(ts['pnl'])}</td></tr>")
    return f"""
    <p style="margin:14px 0 4px"><b>② 累积追踪线 · 信号结算 vs 真实成交</b>（<b>仅实盘 A+B</b>；C 不计入）</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th rowspan="2">窗口</th>
          <th colspan="4" style="background:#eef2ff">信号结算（理想化，含未成交）</th>
          <th colspan="5" style="background:#ecfdf5">真实成交（账户，仅已平仓）</th></tr>
      <tr style="background:#f5f5f5"><th>n</th><th>胜率</th><th>PF</th><th>均收益</th>
          <th>n</th><th>胜率</th><th>PF</th><th>均收益</th><th>盈亏</th></tr>
      {''.join(trs)}
    </table>
    <p style="margin:6px 0 0;color:#777;font-size:12px">
      真实成交按信号日归属窗口；「均收益」= 每笔盈亏/名义（价格收益率口径），与结算/回测的
      百分比可比；「盈亏」为账户实际 USDT 盈亏。</p>"""


def _bench_table(rows: list[dict], trades: list[dict], anchor: date, days: int,
                 profiles: dict) -> tuple[str, str]:
    """三方对比：信号结算 × 真实成交 × 回测基准（近 days 天，按类型）。返回 (html, 警示html)。"""
    trs, warns = [], []
    by_trade = by_type(trades)
    for sig in sorted({r["signal_type"] for r in rows}):
        st = _stats(window_rows(by_type(rows).get(sig, []), anchor, days))
        ts = _trade_stats(window_rows(by_trade.get(sig, []), anchor, days))
        prof = profiles.get(sig, {})
        c3 = prof.get("cost0.3", {}) if prof else {}
        trs.append(
            f"<tr><td>{TYPE_LABEL.get(sig, sig)}</td>"
            f"<td>{st['matured']}</td><td>{_pct(st['win_rate'])}</td>"
            f"<td>{_num(st['payoff'])}</td><td>{_num(st['pf'])}</td><td>{_sgn(st['mean'])}</td>"
            f"<td>{ts['n']}</td><td>{_pct(ts['win_rate'])}</td>"
            f"<td>{_num(ts['pf'])}</td><td>{_sgn(ts['mean'])}</td><td>{_u(ts['pnl'])}</td>"
            f"<td>{_pct(c3.get('win_rate'))}</td><td>{_num(c3.get('pf'))}</td>"
            f"<td>{_sgn(c3.get('mean'))}</td></tr>")
        if st["matured"] < MIN_N:
            warns.append(f"<li>{TYPE_LABEL.get(sig, sig)}：信号结算近 {days} 天已结算 n={st['matured']}"
                         f"（&lt;{MIN_N}）</li>")
        if ts["n"] < MIN_N:
            warns.append(f"<li>{TYPE_LABEL.get(sig, sig)}：真实成交近 {days} 天 n={ts['n']}"
                         f"（&lt;{MIN_N}）</li>")
    html = f"""
    <p style="margin:14px 0 4px"><b>③ 信号结算 vs 真实成交 vs 回测基准</b>
      （近 {days} 天 A+B，回测含 0.3% 成本）</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th rowspan="2">信号</th>
          <th colspan="5" style="background:#eef2ff">信号结算（理想化）</th>
          <th colspan="5" style="background:#ecfdf5">真实成交（账户）</th>
          <th colspan="3" style="background:#fefce8">回测基准</th></tr>
      <tr style="background:#f5f5f5"><th>n</th><th>胜率</th><th>赔率</th><th>PF</th><th>均收益</th>
          <th>n</th><th>胜率</th><th>PF</th><th>均收益</th><th>盈亏</th>
          <th>胜率</th><th>PF</th><th>均收益</th></tr>
      {''.join(trs) or '<tr><td colspan="14">无数据</td></tr>'}
    </table>"""
    warn_html = ""
    if warns:
        warn_html = (f'<div style="margin:8px 0 0;padding:8px 10px;background:#fff7ed;'
                     f'border-left:4px solid #e67e22;font-size:12px;color:#7c2d12">'
                     f'⚠ 样本不足，不具统计显著性，勿据此下结论：<ul style="margin:4px 0 0">'
                     + "".join(warns) + "</ul></div>")
    return html, warn_html


def _gate_table(gate_rows: list[dict], skips: list[dict], anchor: date, days: int) -> str:
    """④ 动量闸门验证：按信号时刻重算放行/拦截，滚动对比两组结算收益。

    gate_rows：已跑过 momentum_gate 的结算行（含 pass/ret_net）。
    skips：实盘 momentum_decayed 拦截行（params.exec_reason）。
    底部自动判断：全部窗口放行胜率 − 拦截胜率 > 0 → ✓ 方向与样本外验证一致。
    """
    if _momentum_gate is None:
        return ('<p style="margin:14px 0 4px"><b>④ 动量闸门验证</b>'
                '<span style="color:#b91c1c">— 闸门模块导入失败，本节不可用</span></p>')
    if not gate_rows:
        return ('<p style="margin:14px 0 4px"><b>④ 动量闸门验证</b>'
                '<span style="color:#777">— 窗口内无已结算信号，暂无数据</span></p>')

    def grp(sub: list[dict]) -> dict:
        xs = [float(g["ret_net"]) for g in sub]
        if not xs:
            return {"n": 0, "wr": None, "mean": None, "median": None}
        return {"n": len(xs),
                "wr": sum(1 for x in xs if x > 0) / len(xs),
                "mean": statistics.mean(xs), "median": statistics.median(xs)}

    def cell(g: dict) -> str:
        mark = " ⚠" if 0 < g["n"] < 15 else ""     # 小样本标记
        return (f"<td>{g['n']}{mark}</td><td>{_pct(g['wr'])}</td>"
                f"<td>{_sgn(g['mean'])}</td><td>{_sgn(g['median'])}</td>")

    wins = [("近 7 天", 7), (f"近 {days} 天", days), ("全部", None)]
    trs = []
    all_pass = all_blk = None
    for label, w in wins:
        sub = window_rows(gate_rows, anchor, w)
        gp, gb = grp([g for g in sub if g["pass"]]), grp([g for g in sub if not g["pass"]])
        n_skip = len(window_rows(skips, anchor, w))
        if w is None:
            all_pass, all_blk = gp, gb
        trs.append(f"<tr><td>{label}</td>{cell(gp)}{cell(gb)}<td>{n_skip}</td></tr>")

    fo = sum(1 for g in gate_rows if str(g["gate_info"].get("gate", "")).startswith("fail_open"))
    warns = ""
    verdict = ""
    if all_pass and all_blk and all_pass["n"] and all_blk["n"]:
        diff = (all_pass["wr"] - all_blk["wr"]) * 100
        if diff > 0:
            verdict = (f'<div style="margin:8px 0 0;padding:8px 10px;background:#ecfdf5;'
                       f'border-left:4px solid #059669;font-size:12px;color:#065f46">'
                       f'✓ 全部窗口：放行组胜率 {_pct(all_pass["wr"])} − 拦截组 {_pct(all_blk["wr"])}'
                       f' = <b>{diff:+.1f}pct</b>，方向与样本外验证一致（拦掉的确实更差）。</div>')
        else:
            verdict = (f'<div style="margin:8px 0 0;padding:8px 10px;background:#fff7ed;'
                       f'border-left:4px solid #e67e22;font-size:12px;color:#7c2d12">'
                       f'⚠ 全部窗口：放行组胜率 {_pct(all_pass["wr"])} ≤ 拦截组 {_pct(all_blk["wr"])}'
                       f'（{diff:+.1f}pct）——<b>闸门方向反转</b>，需关注是否市场状态变化，'
                       f'样本不足时勿据此单独下结论。</div>')
        if all_blk["n"] < 30:
            warns += (f'<div style="margin:6px 0 0;color:#b45309;font-size:12px">'
                      f'⚠ 拦截组全部窗口 n={all_blk["n"]}（&lt;30），判断易受个别样本扰动。</div>')
    return f"""
    <p style="margin:14px 0 4px"><b>④ 动量闸门验证 · 放行 vs 拦截</b>
      <span style="color:#777;font-weight:normal">— 与执行器同一 momentum_gate 按信号时刻重算
      （规则：最近30min收益≥0 OR 上根5m涨幅&gt;2%），结算收益口径同①</span></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th rowspan="2">窗口</th>
          <th colspan="4" style="background:#ecfdf5">放行（闸门会买入）</th>
          <th colspan="4" style="background:#fef2f2">拦截（闸门会跳过）</th>
          <th rowspan="2" style="background:#f5f5f5">实盘拦截数</th></tr>
      <tr style="background:#f5f5f5"><th>n</th><th>胜率</th><th>均收益</th><th>中位</th>
          <th>n</th><th>胜率</th><th>均收益</th><th>中位</th></tr>
      {''.join(trs)}
    </table>
    <p style="margin:6px 0 0;color:#777;font-size:12px">
      「实盘拦截数」= 执行器真实写入 exec_reason=momentum_decayed 的信号数（闸门部署前恒为 0）；
      fail-open 放行（K线不足/查询失败）{fo} 条计入放行组。⚠ = n&lt;15 小样本。
      本表每日重算，用于滚动验证闸门是否仍然有效。</p>
    {verdict}{warns}"""


def _change_board(dd_rows: list[dict], mid_rows: list[dict], has_scale: bool,
                  anchor: date, days: int) -> str:
    """⑤ 本轮改动生效看板（2026-11-11 三项改动）：期望 vs 实测，回答「改对了没有」。

    dd_rows：A/B 信号 + dd_pct（落库值优先，缺失则按 event_bar_ts 重算）+ 结算收益。
    mid_rows：origin='bot' 的 B 档实际下单（名义 / 计划持仓 / 出场原因）。
    全部为**只读展示**：被闸门拦掉的信号没有真实成交，只能用理想化结算（ret_net）对照。
    """
    a_lo, a_hi = (getattr(_v2, "A_HALF_CHG24_RANGE", (50.0, 75.0)) if _v2 else (50.0, 75.0))
    dd_min = float(getattr(_v2, "DD_SKIP_MIN_PCT", 5.0)) if _v2 else 5.0

    def _f(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def low_chg(r: dict) -> bool:
        c = _f(r.get("chg_24h_pct"))
        return c is not None and a_lo <= c < a_hi

    def dd_of(r: dict) -> float | None:
        return _f(r.get("dd_pct"))

    def chip(kind: str, text: str) -> str:
        bg, bd, fg = {"ok": ("#ecfdf5", "#059669", "#065f46"),
                      "warn": ("#fff7ed", "#e67e22", "#7c2d12"),
                      "na": ("#f3f4f6", "#d1d5db", "#4b5563")}[kind]
        return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                f'background:{bg};border:1px solid {bd};color:{fg};font-size:12px">{text}</span>')

    # ── 表 A：dd 闸门（规则重算）放行 vs 拦截 ───────────────────────────
    def dd_cell(sub: list[dict]) -> str:
        st = _stats(sub)
        mark = " ⚠" if 0 < st["matured"] < 15 else ""
        return (f"<td>{st['matured']}{mark}</td><td>{_pct(st['win_rate'])}</td>"
                f"<td>{_sgn(st['mean'])}</td><td>{_num(st['pf'])}</td>")

    wins = [("近 7 天", 7), (f"近 {days} 天", days), ("全部", None)]
    dd_trs, blk_all, kept_all = [], None, None
    act_all = act_7 = 0
    for label, w in wins:
        sub = window_rows(dd_rows, anchor, w)
        blk = [r for r in sub if dd_of(r) is not None and low_chg(r) and dd_of(r) >= dd_min]
        kept = [r for r in sub if dd_of(r) is not None and low_chg(r) and dd_of(r) < dd_min]
        act = sum(1 for r in sub
                  if str(r.get("exec_reason") or "").startswith("dd_exceed_low_chg"))
        if w is None:
            blk_all, kept_all, act_all = blk, kept, act
        if w == 7:
            act_7 = act
        dd_trs.append(f"<tr><td>{label}</td>{dd_cell(blk)}{dd_cell(kept)}<td>{act}</td></tr>")

    # 强动能档（chg24≥75%）必须一条都不拦 —— 回归检查，防规则写错档位
    strong_blk = 0
    for r in dd_rows:
        c, d = _f(r.get("chg_24h_pct")), dd_of(r)
        if c is not None and c >= a_hi and d is not None and d >= dd_min:
            strong_blk += 1
    strong_act = sum(1 for r in dd_rows
                     if str(r.get("exec_reason") or "").startswith("dd_exceed_low_chg")
                     and (_f(r.get("chg_24h_pct")) or 0) >= a_hi)
    b_act = sum(1 for r in dd_rows
                if str(r.get("exec_reason") or "").startswith("dd_exceed_low_chg")
                and r["signal_type"] == "MID_LONG")

    sb, sk = _stats(blk_all or []), _stats(kept_all or [])
    if sb["matured"] == 0 or sk["matured"] == 0:
        dd_verdict = ('<div style="margin:8px 0 0;padding:8px 10px;background:#f3f4f6;'
                      'border-left:4px solid #9ca3af;font-size:12px;color:#4b5563">'
                      '— 规则重算的拦截组/放行组暂无已结算样本（dd 需信号带 event_bar_ts），'
                      '暂无法验证方向；样本出现后本节自动给出结论。</div>')
    else:
        diff = (sb["mean"] - sk["mean"]) * 100
        if diff < 0:
            dd_verdict = (f'<div style="margin:8px 0 0;padding:8px 10px;background:#ecfdf5;'
                          f'border-left:4px solid #059669;font-size:12px;color:#065f46">'
                          f'✓ 全部窗口：被 dd 闸门拦掉的信号均收益 {_sgn(sb["mean"])} &lt; '
                          f'放行 {_sgn(sk["mean"])}（差 {diff:+.2f}pp，n={sb["matured"]} vs '
                          f'{sk["matured"]}）——拦掉的确实更差，方向与回测一致。</div>')
        else:
            dd_verdict = (f'<div style="margin:8px 0 0;padding:8px 10px;background:#fff7ed;'
                          f'border-left:4px solid #e67e22;font-size:12px;color:#7c2d12">'
                          f'⚠ 全部窗口：被拦组均收益 {_sgn(sb["mean"])} ≥ 放行 '
                          f'{_sgn(sk["mean"])}（差 {diff:+.2f}pp，n={sb["matured"]} vs '
                          f'{sk["matured"]}）——<b>与回测方向相反</b>，需复核规则或市场状态。</div>')

    # ── 表 B：B 档成交校验（改动前 vs 改动后）──────────────────────────
    def mid_grp(rows: list[dict]) -> dict:
        ns = [_f(r.get("notional_usdt")) for r in rows if _f(r.get("notional_usdt"))]
        holds = sorted({int(r["planned_hold_h"]) for r in rows
                        if r.get("planned_hold_h") is not None})
        closed = [r for r in rows if r.get("close_ts") is not None]
        dur = [(r["close_ts"] - r["open_ts"]).total_seconds() / 3600 for r in closed]
        return {"n": len(rows), "notional_mean": (statistics.mean(ns) if ns else None),
                "notional_min": (min(ns) if ns else None),
                "notional_max": (max(ns) if ns else None),
                "holds": holds,
                "hold_med": (statistics.median(dur) if dur else None),
                "expiry": sum(1 for r in closed if (r.get("exit_reason") or "") == "expiry")}

    mid_before = [r for r in mid_rows if r["day"] < ROLLOUT_DAY]
    mid_after = [r for r in mid_rows if r["day"] >= ROLLOUT_DAY]
    mb, ma = mid_grp(mid_before), mid_grp(mid_after)

    def _u1(v, unit: str) -> str:
        return "-" if v is None else f"{float(v):.1f}{unit}"

    def mid_row(label: str, g: dict) -> str:
        rng = (f"{g['notional_min']:.0f}~{g['notional_max']:.0f}U"
               if g["notional_min"] is not None else "-")
        holds = ("/".join(str(h) for h in g["holds"]) + "h") if g["holds"] else "-"
        return (f"<tr><td>{label}</td><td>{g['n']}</td><td>{_u1(g['notional_mean'], 'U')}</td>"
                f"<td>{rng}</td><td>{holds}</td><td>{_u1(g['hold_med'], 'h')}</td>"
                f"<td>{g['expiry']}</td></tr>")

    # ── 表 C：改动核对（期望 vs 实测 vs 判定）──────────────────────────
    if act_all > 0:
        dd_status = ("ok", f"✓ 已生效 {act_all} 条")
    elif blk_all:
        dd_status = ("na", "— 尚无实盘拦截")
    else:
        dd_status = ("na", "— 无样本")

    if ma["n"] == 0:
        notional_status = ("na", "— 改动后无成交")
    elif ma["notional_mean"] is not None and ma["notional_mean"] <= 15:
        notional_status = ("ok", f"✓ 已减半 {_num(ma['notional_mean'], 1)}U")
    else:
        notional_status = ("warn", f"⚠ 仍为 {_num(ma['notional_mean'], 1)}U")

    if ma["n"] == 0:
        hold_status = ("na", "— 改动后无成交")
    elif ma["holds"] and all(h == 24 for h in ma["holds"]):
        hold_status = ("ok", "✓ 全部 24h")
    else:
        hold_status = ("warn", "⚠ 仍见非 24h")

    scale_s = ""
    if not has_scale:
        scale_s = ('<div style="margin:8px 0 0;color:#b45309;font-size:12px">'
                   '⚠ biz.v2_trade_log 缺 position_scale 列 —— 迁移 '
                   '<b>fix_100_v2_trade_log_position_scale.sql</b> 尚未执行，'
                   '仓位系数未落库（名义仍可从 notional_usdt 校验）。</div>')

    rows_audit = [
        ("A 档弱动能档 dd 闸门",
         f"chg24∈[{a_lo:g}%,{a_hi:g}%) 且 dd≥{dd_min:g}% → skip，"
         f"exec_reason=dd_exceed_low_chg；chg24≥{a_hi:g}% 不过滤；B 档不受影响",
         f"实盘拦截 <b>{act_all}</b> 条（近 7 天 {act_7}）；规则重算应拦 "
         f"{len(blk_all or [])} 条（已结算 {sb['matured']}）；"
         f"强动能档(chg24≥{a_hi:g}%)实盘被拦 <b>{strong_act}</b> 条（应为 0；"
         f"该档 dd≥{dd_min:g}% 共 {strong_blk} 条，应全部放行）；"
         f"B 档被拦 {b_act} 条（应为 0）",
         chip(*dd_status)),
        ("B 档名义减半",
         "MID_LONG 名义 20U → 10U（position_scale=0.5，杠杆 1x 不变）",
         f"改动后 {ma['n']} 笔，名义均值 {_u1(ma['notional_mean'], 'U')}"
         f"（改动前 {mb['n']} 笔 / {_u1(mb['notional_mean'], 'U')}）",
         chip(*notional_status)),
        ("B 档持仓上限 168h → 24h",
         "新单 planned_hold_h=24，不再出现 168；到期出场应仍为 0 笔",
         f"改动后 planned_hold_h = {'/'.join(str(h) for h in ma['holds']) or '-'}；"
         f"到期出场 {ma['expiry']} 笔；实际持仓中位 {_u1(ma['hold_med'], 'h')}",
         chip(*hold_status)),
    ]
    audit_trs = "".join(
        f'<tr><td style="white-space:nowrap"><b>{k}</b></td>'
        f'<td style="color:#4b5563">{e}</td><td>{m}</td><td>{c}</td></tr>'
        for k, e, m, c in rows_audit)

    return f"""
    <p style="margin:14px 0 4px"><b>⑤ 本轮改动生效看板</b>
      <span style="color:#777;font-weight:normal">— 2026-11-11 三项改动「期望 vs 实测」，
      回答「改对了没有」；改动前 = open_ts 早于 {ROLLOUT_DAY}（北京时间）</span></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th>改动</th><th>期望</th><th>实测</th><th>判定</th></tr>
      {audit_trs}
    </table>
    {scale_s}
    <p style="margin:12px 0 4px;font-size:13px"><b>5.1 dd 闸门 · 放行 vs 拦截</b>
      <span style="color:#777;font-size:12px">（规则重算：A 档 chg24∈[{a_lo:g}%,{a_hi:g}%)
      按 dd≥{dd_min:g}% 分两组，结算口径同 ①；被拦信号无真实成交，仅理想化结算）</span></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th rowspan="2">窗口</th>
          <th colspan="4" style="background:#fef2f2">dd≥{dd_min:g}%（闸门会跳过）</th>
          <th colspan="4" style="background:#ecfdf5">dd&lt;{dd_min:g}%（闸门会买入）</th>
          <th rowspan="2">实盘拦截数</th></tr>
      <tr style="background:#f5f5f5"><th>已结算</th><th>胜率</th><th>均收益</th><th>PF</th>
          <th>已结算</th><th>胜率</th><th>均收益</th><th>PF</th></tr>
      {''.join(dd_trs)}
    </table>
    <p style="margin:6px 0 0;color:#777;font-size:12px">
      dd = 1 − 信号价 / 24h 阶段高点（1h 高点，口径同 bin/backtest_a_position.py）；
      落库值 dd_24h_pct 优先，缺失则按 event_bar_ts 重算。⚠ = 已结算 n&lt;15 小样本。</p>
    {dd_verdict}
    <p style="margin:12px 0 4px;font-size:13px"><b>5.2 B 档成交校验</b>
      <span style="color:#777;font-size:12px">（名义应 10U / 计划持仓应 24h；改动前后并排）</span></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th>分组</th><th>n</th><th>名义均值</th><th>名义区间</th>
          <th>计划持仓</th><th>实际持仓中位</th><th>到期出场</th></tr>
      {mid_row(f"改动前（&lt; {ROLLOUT_DAY}）", mb)}
      {mid_row(f"改动后（≥ {ROLLOUT_DAY}）", ma)}
    </table>
    <p style="margin:6px 0 0;color:#777;font-size:12px">
      到期出场 = exit_reason='expiry'；B 档实际持仓中位约 4h（回测口径），
      因此「到期出场 0 笔」也是正常，重点看 planned_hold_h 是否已写 24。</p>"""


def _conclusion(rows: list[dict], trades: list[dict], anchor: date, profiles: dict) -> str:
    """顶部一句话结论：信号结算 A 档（SHORT_LONG）近 7 天 vs 回测基准 + 同期真实成交。

    rows = 已过滤后的信号结算行（仅 A+B）；trades = 真实成交行（仅 A+B）。
    """
    st = _stats(window_rows(by_type(rows).get("SHORT_LONG", []), anchor, 7))
    ts = _trade_stats(window_rows(by_type(trades).get("SHORT_LONG", []), anchor, 7))
    base = profiles.get("SHORT_LONG", {}).get("cost0.3", {})
    head = ("信号结算（理想化，含未成交）≠ 实盘业绩；真实成交=账户实际已平仓。"
            "实盘白名单 = A（SHORT_LONG）+ B（MID_LONG 右上角）；C（TRAP_SHORT）不交易；"
            "告警邮件仅推 A，B 虽实盘但无告警。")
    real_part = (f" 同期真实成交：n={ts['n']}，胜率 {_pct(ts['win_rate'])}，"
                 f"均收益 {_sgn(ts['mean'])}，盈亏 {_u(ts['pnl'])}"
                 + (f"（含未平 {ts['open_n']} 笔）" if ts["open_n"] else "") + "。")
    if st["matured"] == 0:
        return head + " A 档近 7 天无已结算样本，暂无法与回测基准对比。" + real_part
    base_pf = base.get("pf")
    if base_pf:
        rel = "强于" if (st["pf"] or 0) >= base_pf else "弱于"
        cmp_s = f"与回测基准 PF={base_pf:.2f} 相比{rel}"
    else:
        cmp_s = "（回测基准缺失）"
    warn = f"（样本不足 n={st['matured']}）" if st["matured"] < MIN_N else ""
    return (head + f" 信号结算 A 档近 7 天 n={st['matured']}，PF={_num(st['pf'])}，"
            f"胜率 {_pct(st['win_rate'])}，均收益 {_sgn(st['mean'])}，{cmp_s}{warn}。"
            + real_part)


def _strategy_logic() -> str:
    """⑥ 实盘策略运行逻辑：流程骨架 + 全链路参数（常量实时取自 execute_gainers_v2）。

    所有数字（白名单/杠杆/名义/时效/限价/初始止损/保本/硬顶）一律引用执行器模块常量，
    单一事实来源——执行器改动次日日报自动同步，供日后核对优化；不一致时以代码为准。
    """
    head = ('<p style="margin:14px 0 4px"><b>⑥ 实盘策略运行逻辑</b>'
            '<span style="color:#777;font-weight:normal">— 数字实时取自 '
            'bin/execute_gainers_v2.py 常量（单一事实来源），执行器改动次日自动同步</span></p>')
    if _v2 is None:
        return head + ('<p style="color:#b91c1c;font-size:12px">'
                       '执行器模块导入失败，本节不可用</p>')
    p, lev, notn = _v2.PARAMS, _v2.LEVERAGE_BY_SIGNAL, _v2.NOTIONAL_BY_SIGNAL

    # 仓位系数（2026-11-11）：A 档弱动能档半仓、B 档整体减半 —— 展示实际到手名义
    a_scale = float(getattr(_v2, "A_HALF_POSITION_SCALE", 0.5))
    b_scale = float(getattr(_v2, "B_POSITION_SCALE", 0.5))

    def _notional_s(code: str) -> str:
        nt = notn.get(code)
        if not isinstance(nt, (int, float)):
            return "-"
        if code == "SHORT_LONG":
            return f"{nt:g}U（chg24 50~75% 半仓 {nt * a_scale:g}U）"
        if code == "MID_LONG":
            return f"{nt:g}U×{b_scale:g}={nt * b_scale:g}U"
        return f"{nt:g}U"

    def _sig(code: str, cond: str, note: str = "") -> str:
        d = p.get(code, {})
        tp = f"TP {float(d['TP']) * 100:g}% / " if d.get("TP") else ""
        return (f'<li><b>{TYPE_LABEL.get(code, code)}</b>（{cond}）：'
                f'{lev.get(code, "-")}x / {_notional_s(code)} 名义，'
                f'持有 {d.get("N", "-")}h，{tp}TR {float(d.get("TR") or 0) * 100:g}% / '
                f'SL {float(d.get("SL") or 0) * 100:g}%{note}</li>')

    sig_list = ('<ul style="margin:2px 0;padding-left:18px">'
                + _sig("SHORT_LONG", "chg24 ≥ 50%")
                + _sig("MID_LONG", "chg24 30~50% 且 vr≥5.6（扫描器过滤）")
                + _sig("TRAP_SHORT", "chg24 &lt; 5%", " — <b>仅研究观察，不实盘</b>")
                + "</ul>")
    items = [
        ("流程",
         "扫描器 scan_gainers_v2（30min/轮，涨幅榜定位×强度确认，<b>告警邮件仅推 A</b>）"
         " → 执行器 execute_gainers_v2（--apply --watch 常驻，10min 轮询）"
         " → 入场闸门 → 限价挂单 + 保护单同步布防 → <b>limit_pending 状态机</b>"
         "（每轮先补录成交 / 超时撤单放弃，元数据落库、重启续跑）"
         " → 持仓管理（首小时初始止损 / EMA7 线破出场 / 保本上移）→ 到期强平"
         " → collect_gainer_outcome 结算 → 本日报"),
        ("信号白名单", sig_list),
        ("信号时效",
         f"scan_ts 距今 ≤ {_v2.MAX_SIGNAL_AGE_H}h，超时信号不执行"
         "（防停机重启后把陈旧信号当真单打下）"),
        ("入场闸门（按序）",
         "① 余额闸门：可用余额 &lt; 保证金×1.2 → skip(balance_low)；"
         "② 同币不叠仓：账户持仓按 symbol 去重（dust&lt;5U 过滤），含跨信号互斥、"
         "同币 limit_pending 期间不开第二张；"
         "③ 5m 动量闸门：最近 30min 收益 ≥0 或 上根 5m 涨幅 &gt;2%"
         "（K线不足 fail-open），否则 skip(momentum_decayed)；"
         "④ A 档弱动能档回撤闸门（2026-11-11）：A 档 chg24∈"
         f"[{float(getattr(_v2, 'A_HALF_CHG24_RANGE', (50.0, 75.0))[0]):g}%,"
         f"{float(getattr(_v2, 'A_HALF_CHG24_RANGE', (50.0, 75.0))[1]):g}%) 且 "
         f"dd(距 24h 阶段高点回撤)≥{float(getattr(_v2, 'DD_SKIP_MIN_PCT', 5.0)):g}% → "
         "skip(dd_exceed_low_chg)；<b>chg24 ≥75% 不过滤</b>（该档回撤是洗盘，右尾来源）；"
         "dd 缺失 fail-open；"
         "⑤ 价格守卫：现价 ≤ 限价×(1−SL)（信号后已崩）→ skip(price_below_sl_zone)"),
        ("入场方式（时间线 #10，2026-11-11）",
         f"5m 回撤限价：挂 <b>信号价×(1−{_v2.LIMIT_ENTRY_DISCOUNT_PCT:g}%)</b> GTC 限价单等回撤，"
         f"<b>{_v2.LIMIT_ENTRY_WAIT_MIN}min</b> 未成交 → 撤限价单+清保护单 → 放弃"
         "（回测 V1s 口径 <b>不追</b>，全历史配对 +49~57bp、t≈9~12）；SL 在挂单同一调用内布防"
         "（限价买单价必高于止损触发价，成交必然先于触发，<b>无裸奔窗口</b>）"),
        ("首小时初始止损",
         f"max(EMA{_v2.EMA_INIT_PERIOD}×(1−{_v2.EMA_INIT_BUF:.0%}), entry×(1−SL))，"
         f"线与现价须差 ≥{_v2.EMA_INIT_MIN_GAP:.0%} 否则回退固定 SL；常驻不撤；"
         "首小时不做出场逻辑，只靠该止损"),
        ("出场（第 2h 起）",
         f"已收盘 1h close 的 <b>EMA{_v2.EMA_TRAIL_PERIOD} 裸线</b>：现价破线 → 撤保护单 + "
         "市价按数量全平(exit_reason=trail)；与交易所侧止损互为兜底"
         "（软件防挂单链路坏，交易所单防进程挂）"),
        ("保本上移",
         f"浮盈 ≥{_v2.BREAKEVEN_TRIGGER_PCT:g}% → 止损重挂为 "
         f"max(entry±{_v2.BREAKEVEN_COST_COVER_PCT:g}%, EMA7 线) 取更紧者，"
         "只上移不回撤（0.4% 覆盖手续费+滑点）"),
        ("兜底与风控",
         f"SL {float(p.get('SHORT_LONG', {}).get('SL') or 0) * 100:g}% STOP_MARKET"
         "（algo closePosition）静默兜底 + 持有 N 小时到期市价强平；"
         f"单笔亏损硬顶 = 权益 {_v2.MAX_LOSS_EQUITY_PCT:g}%"),
        ("核对提示",
         "本节数字与 <b>bin/execute_gainers_v2.py</b> 同步渲染；改动史见"
         "《实盘策略改进链路与回测差距》分析材料（时间线 #0~#10）。"
         "若本节与执行器代码不一致，<b>以代码为准</b>。"),
    ]
    trs = "".join(
        f'<tr><td style="background:#f5f5f5;vertical-align:top;white-space:nowrap">'
        f'<b>{k}</b></td><td style="min-width:480px">{v}</td></tr>' for k, v in items)
    return (head + '<table border="1" cellspacing="0" cellpadding="6" '
            f'style="border-collapse:collapse;font-size:12px">{trs}</table>')


def _research_table(rows: list[dict], anchor: date, days: int) -> str:
    """研究观察（非实盘，如 C/TRAP_SHORT）：单列一张表，⚠ 绝不并入累积追踪线。"""
    if not rows:
        return ""
    trs = []
    for sig in sorted({r["signal_type"] for r in rows}):
        st = _stats(window_rows(by_type(rows).get(sig, []), anchor, days))
        trs.append(f"<tr><td>{TYPE_LABEL.get(sig, sig)}</td><td>{st['n']}</td>"
                   f"<td>{st['matured']}</td><td>{st['censored']}</td><td>{_pct(st['win_rate'])}</td>"
                   f"<td>{_num(st['pf'])}</td><td>{_sgn(st['mean'])}</td></tr>")
    return f"""
    <p style="margin:14px 0 4px"><b>⑦ 研究观察（非实盘）</b>
      <span style="color:#b91c1c">⚠ 不交易、不计入累积追踪线</span>（近 {days} 天）</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th>信号</th><th>n</th><th>已结算</th><th>剔除(OPEN)</th>
          <th>胜率</th><th>PF</th><th>均收益</th></tr>
      {''.join(trs)}
    </table>"""


def render_html(rows: list[dict], trades: list[dict], anchor: date, days: int, profiles: dict,
                live_types: tuple[str, ...] = LIVE_SIGNALS,
                include_research: bool = False,
                gate_rows: list[dict] | None = None,
                gate_skips: list[dict] | None = None,
                audit: tuple[list[dict], list[dict], bool] | None = None) -> tuple[str, str]:
    """渲染日报 HTML。rows=全部结算行、trades=全部真实成交行；内部按 live_types 拆分实盘/研究。

    gate_rows/gate_skips：动量闸门重算结果与实盘拦截记录（None → ④节显示不可用）。
    audit：(dd 对照行, B 档成交行, position_scale 列是否存在)，None → ⑤节显示不可用。
    """
    live_rows = [r for r in rows if r["signal_type"] in live_types]
    research_rows = [r for r in rows if r["signal_type"] not in live_types]
    live_trades = [t for t in trades if t["signal_type"] in live_types]
    conclusion = _conclusion(live_rows, live_trades, anchor, profiles)
    bench, warns = _bench_table(live_rows, live_trades, anchor, days, profiles)
    research = _research_table(research_rows, anchor, days) if include_research else ""
    gate = _gate_table(gate_rows or [], gate_skips or [], anchor, days)
    board = (_change_board(audit[0], audit[1], audit[2], anchor, days) if audit else
             '<p style="margin:14px 0 4px"><b>⑤ 本轮改动生效看板</b> '
             '<span style="color:#b91c1c;font-size:12px">取数失败，本节不可用</span></p>')
    logic = _strategy_logic()
    now = datetime.now(SH).strftime("%Y-%m-%d %H:%M")
    html = f"""<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#222">
      <h2 style="margin:0 0 4px">涨幅榜实盘策略日报 · {anchor}</h2>
      <p style="margin:0 0 8px;color:#666;font-size:13px">生成于 {now}（北京时间）</p>
      <div style="padding:10px 12px;border-left:4px solid #1e40af;background:#f8fafc">
        <p style="margin:0;font-size:14px"><b>结论：</b>{conclusion}</p>
      </div>
      {_daily_table(live_rows)}
      {_track_table(live_rows, live_trades, anchor, days)}
      {bench}
      {gate}
      {board}
      {logic}
      {research}
      {warns}
      <p style="margin:14px 0 0;color:#999;font-size:12px">
        口径：<b>实盘白名单=A+B</b>，C(TRAP_SHORT) 不实盘（默认剔除；--include-research 单列不并入）；
        <b>「信号结算」</b>= scan_gainer_outcome 对<b>全部信号</b>的理想化回测结算
        （含被跳过/从未下单的信号、信号价入场、无延迟无滑点，<b>≠账户业绩</b>）；
        <b>「真实成交」</b>= biz.v2_trade_log 实际已平仓（origin=bot，按信号日归属窗口，
        盈亏为 USDT 账户口径，均收益=盈亏/名义，缺名义的行不含在均值里）。
        结算净收益已扣 0.3% 往返成本；仅统计已结算（matured）样本，censored(OPEN) 不计入。
        出场口径对齐 backtest_exit_profile.py（做多 TRAIL 3%；做空 FIX 12h/TP50%/SL10%）。
        本邮件为数据分析参考，不构成投资建议。</p>
    </body></html>"""
    return html, conclusion


def _subject_line(rows: list[dict], trades: list[dict], anchor: date, profiles: dict) -> str:
    """邮件主题用的紧凑摘要：信号结算 A 档近 7 天 + 同期真实成交（结论 head 太长会被截断）。"""
    st = _stats(window_rows(by_type(rows).get("SHORT_LONG", []), anchor, 7))
    ts = _trade_stats(window_rows(by_type(trades).get("SHORT_LONG", []), anchor, 7))
    base_pf = profiles.get("SHORT_LONG", {}).get("cost0.3", {}).get("pf")
    cmp_s = ("" if not base_pf or st["pf"] is None
             else f"，{'强于' if st['pf'] >= base_pf else '弱于'}回测PF={base_pf:.1f}")
    return (f"信号结算A近7天 n={st['matured']} 胜率{_pct(st['win_rate'])} "
            f"PF={_num(st['pf'])}{cmp_s} ｜ 真实成交 n={ts['n']} "
            f"胜率{_pct(ts['win_rate'])} 盈亏{_u(ts['pnl'])}")


def build_subject(summary: str) -> str:
    return f"【涨幅榜实盘日报】{summary}"[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description="涨幅榜实盘策略日报邮件")
    parser.add_argument("--date", type=str, default="", help="报告日 YYYY-MM-DD（默认昨天）")
    parser.add_argument("--days", type=int, default=30, help="累积展示窗口天数（默认 30）")
    parser.add_argument("--signal-types", type=str, default="",
                        help="实盘白名单覆盖（逗号分隔），默认取 execute_gainers_v2.LIVE_SIGNALS")
    parser.add_argument("--include-research", action="store_true",
                        help="额外单列 C 等非实盘信号（研究观察表，不并入累积追踪线）")
    parser.add_argument("--email", dest="email", action="store_true", default=True, help="发送邮件")
    parser.add_argument("--no-email", dest="email", action="store_false", help="不发送邮件")
    parser.add_argument("--dry-run", action="store_true", help="只打印 HTML 不发送")
    parser.add_argument("--to", type=str, default="", help="收件人覆盖（逗号分隔）")
    args = parser.parse_args()

    anchor = (date.fromisoformat(args.date) if args.date
              else datetime.now(SH).date() - timedelta(days=1))
    live_types = (tuple(s.strip().upper() for s in args.signal_types.split(",") if s.strip())
                  if args.signal_types else LIVE_SIGNALS)

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        rows = load(conn)
        trades = load_trades(conn)
        gate_skips = load_gate_skips(conn, live_types)
        # 闸门重算仅需实盘白名单、已结算（有 ret_net）的行
        gate_rows = load_gates(
            conn, settings,
            [r for r in rows if r["signal_type"] in live_types
             and r["matured"] and r["ret_net"] is not None])
        try:
            audit = load_change_audit(conn, live_types)
        except Exception as e:  # noqa: BLE001 — 看板故障不拖垮日报其余部分
            print(f"[warn] 改动看板取数失败: {type(e).__name__}: {e}")
            audit = None
    live_n = sum(1 for r in rows if r["signal_type"] in live_types)
    profiles = load_exit_profile()
    html, conclusion = render_html(rows, trades, anchor, args.days, profiles,
                                   live_types, args.include_research,
                                   gate_rows, gate_skips, audit)
    subject = build_subject(_subject_line(
        [r for r in rows if r["signal_type"] in live_types],
        [t for t in trades if t["signal_type"] in live_types], anchor, profiles))
    closed_n = sum(1 for t in trades if t["signal_type"] in live_types
                   and t.get("close_ts") is not None)
    gp_n = sum(1 for g in gate_rows if g["pass"])
    print(f"[gainer-report] {anchor} · 结算 {len(rows)} 条（实盘 {live_n} / 白名单 {list(live_types)}）"
          f" · 真实成交 {closed_n} 笔已平仓"
          f" · 闸门重算 放行 {gp_n} / 拦截 {len(gate_rows) - gp_n}"
          f"（实盘 momentum_decayed 拦截 {len(gate_skips)}）· {conclusion}")
    if audit:
        dd_act = sum(1 for r in audit[0]
                     if str(r.get("exec_reason") or "").startswith("dd_exceed_low_chg"))
        after_n = sum(1 for r in audit[1] if r["day"] >= ROLLOUT_DAY)
        print(f"[gainer-report] 改动看板 · dd 闸门实盘拦截 {dd_act} 条"
              f" · B 档改动后成交 {after_n} 笔"
              f" · position_scale 列 {'有' if audit[2] else '缺（迁移未执行）'}")

    if args.dry_run:
        print(subject)
        print(html)
        return 0
    if not args.email:
        print("[gainer-report] --no-email，跳过发送")
        return 0

    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("[WARN] SMTP 未配置，跳过发送")
        print(html)
        return 0
    ok, msg = notifier.send(subject, html, from_name="涨幅榜实盘策略",
                            to=(args.to or None))
    print(f"[gainer-report] 发送结果: {'成功' if ok else '失败'} - {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())