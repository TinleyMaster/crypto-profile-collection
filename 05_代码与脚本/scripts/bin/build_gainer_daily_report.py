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
  6. 顶部一句话结论（信号结算 A 档近 7 天 vs 回测基准 + 同期真实成交）。

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
    <p style="margin:14px 0 4px"><b>⑤ 研究观察（非实盘）</b>
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
                gate_skips: list[dict] | None = None) -> tuple[str, str]:
    """渲染日报 HTML。rows=全部结算行、trades=全部真实成交行；内部按 live_types 拆分实盘/研究。

    gate_rows/gate_skips：动量闸门重算结果与实盘拦截记录（None → ④节显示不可用）。
    """
    live_rows = [r for r in rows if r["signal_type"] in live_types]
    research_rows = [r for r in rows if r["signal_type"] not in live_types]
    live_trades = [t for t in trades if t["signal_type"] in live_types]
    conclusion = _conclusion(live_rows, live_trades, anchor, profiles)
    bench, warns = _bench_table(live_rows, live_trades, anchor, days, profiles)
    research = _research_table(research_rows, anchor, days) if include_research else ""
    gate = _gate_table(gate_rows or [], gate_skips or [], anchor, days)
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
    live_n = sum(1 for r in rows if r["signal_type"] in live_types)
    profiles = load_exit_profile()
    html, conclusion = render_html(rows, trades, anchor, args.days, profiles,
                                   live_types, args.include_research,
                                   gate_rows, gate_skips)
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