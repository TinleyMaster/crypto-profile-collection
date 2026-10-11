#!/usr/bin/env python3
"""涨幅榜实盘策略 · 每日报告（邮件推送）。

做什么
------
读 `biz.scan_gainer_outcome`（由 `collect_gainer_outcome.py` 结算），聚合出：
  1. **逐日表**：按 Asia/Shanghai 日期 × signal_type → n / 已结算 / censored(OPEN) /
     胜率 / 赔率 / PF / 均收益 / 中位 / 最差；
  2. **累积追踪线**：近 7 天 / 近 N 天 / 全部 的累计 n、胜率、PF、均收益；
  3. **回测基准对比列**：读 `data/exit_profile.json`（各 signal_type 的 win_rate /
     payoff / pf / cost0.3），并排展示「实盘 vs 回测」；
  4. **样本量警示**：任一分组 matured n<30 → 标注「⚠ 样本不足，不具统计显著性」；
  5. 顶部一句话结论（近 7 天 A 档 vs 回测基准）。

口径：净收益 `ret_net`（已扣 0.3% 往返成本）；**只统计 matured 行**（censored(OPEN)
不计入胜率/PF，避免「还没回撤」的样本系统性高估）。

用法
----
    python build_gainer_daily_report.py                       # 发昨天（默认 email）
    python build_gainer_daily_report.py --date 2026-10-10     # 按天补跑
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
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

SH = timezone(timedelta(hours=8))     # 报告口径与展示时间统一用北京时间
MIN_N = 30                            # 样本量门槛（不足则警示）
EXIT_PROFILE = SCRIPT_DIR.parent / "data" / "exit_profile.json"
TYPE_LABEL = {"SHORT_LONG": "A 短线做多", "MID_LONG": "B 次档做多", "TRAP_SHORT": "C 诱多做空"}

OUTCOME_SQL = """
SELECT o.signal_id, o.symbol, o.signal_type, o.scan_ts, o.exit_type, o.matured, o.ret_net
  FROM biz.scan_gainer_outcome o
 ORDER BY o.scan_ts
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
    <p style="margin:14px 0 4px"><b>① 逐日表</b>（Asia/Shanghai，按信号类型分组）</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th>日期</th><th>信号</th><th>n</th><th>已结算</th>
          <th>剔除(OPEN)</th><th>胜率</th><th>赔率</th><th>PF</th><th>均收益</th><th>中位</th><th>最差</th></tr>
      {''.join(trs) or '<tr><td colspan="11">无数据</td></tr>'}
    </table>
    <p style="margin:6px 0 0;color:#777;font-size:12px">
      列出全部有结算的日期。「剔除(OPEN)」为 censored（窗口内未触发跟踪止盈/未到期），
      <b>不计入</b>胜率/PF。</p>"""


def _track_table(rows: list[dict], anchor: date, days: int) -> str:
    wins = [("近 7 天", 7), (f"近 {days} 天", days), ("全部", None)]
    trs = []
    for label, w in wins:
        scoped = window_rows(rows, anchor, w)
        st = _stats(scoped)
        trs.append(f"<tr><td>{label}</td><td>{st['n']}</td><td>{st['matured']}</td>"
                   f"<td>{_pct(st['win_rate'])}</td><td>{_num(st['pf'])}</td>"
                   f"<td>{_sgn(st['mean'])}</td></tr>")
    return f"""
    <p style="margin:14px 0 4px"><b>② 累积追踪线</b>（全部信号类型合计）</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th>窗口</th><th>n</th><th>已结算</th><th>胜率</th><th>PF</th><th>均收益</th></tr>
      {''.join(trs)}
    </table>"""


def _bench_table(rows: list[dict], anchor: date, days: int, profiles: dict) -> tuple[str, str]:
    """回测基准对比：实盘（近 days 天，按类型）× 回测（cost0.3）。返回 (html, 警示html)。"""
    trs, warns = [], []
    for sig in sorted({r["signal_type"] for r in rows}):
        st = _stats(window_rows(by_type(rows).get(sig, []), anchor, days))
        prof = profiles.get(sig, {})
        c3 = prof.get("cost0.3", {}) if prof else {}
        trs.append(
            f"<tr><td>{TYPE_LABEL.get(sig, sig)}</td>"
            f"<td>{st['matured']}</td><td>{_pct(st['win_rate'])}</td>"
            f"<td>{_num(st['payoff'])}</td><td>{_num(st['pf'])}</td><td>{_sgn(st['mean'])}</td>"
            f"<td>{_pct(c3.get('win_rate'))}</td><td>{_num(c3.get('pf'))}</td>"
            f"<td>{_sgn(c3.get('mean'))}</td></tr>")
        if st["matured"] < MIN_N:
            warns.append(f"<li>{TYPE_LABEL.get(sig, sig)}：实盘近 {days} 天已结算 n={st['matured']}"
                         f"（&lt;{MIN_N}）</li>")
    html = f"""
    <p style="margin:14px 0 4px"><b>③ 实盘 vs 回测基准</b>（实盘=近 {days} 天，回测含 0.3% 成本）</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:12px">
      <tr style="background:#f5f5f5"><th rowspan="2">信号</th><th colspan="5">实盘</th>
          <th colspan="3">回测基准</th></tr>
      <tr style="background:#f5f5f5"><th>n</th><th>胜率</th><th>赔率</th><th>PF</th><th>均收益</th>
          <th>胜率</th><th>PF</th><th>均收益</th></tr>
      {''.join(trs) or '<tr><td colspan="9">无数据</td></tr>'}
    </table>"""
    warn_html = ""
    if warns:
        warn_html = (f'<div style="margin:8px 0 0;padding:8px 10px;background:#fff7ed;'
                     f'border-left:4px solid #e67e22;font-size:12px;color:#7c2d12">'
                     f'⚠ 样本不足，不具统计显著性，勿据此下结论：<ul style="margin:4px 0 0">'
                     + "".join(warns) + "</ul></div>")
    return html, warn_html


def _conclusion(rows: list[dict], anchor: date, profiles: dict) -> str:
    """顶部一句话结论：A 档（SHORT_LONG）近 7 天 vs 回测基准。"""
    st = _stats(window_rows(by_type(rows).get("SHORT_LONG", []), anchor, 7))
    base = profiles.get("SHORT_LONG", {}).get("cost0.3", {})
    if st["matured"] == 0:
        return "A 档近 7 天无已结算样本，暂无法与回测基准对比。"
    base_pf = base.get("pf")
    if base_pf:
        rel = "强于" if (st["pf"] or 0) >= base_pf else "弱于"
        cmp_s = f"与回测基准 PF={base_pf:.2f} 相比{rel}"
    else:
        cmp_s = "（回测基准缺失）"
    warn = f"（样本不足 n={st['matured']}）" if st["matured"] < MIN_N else ""
    return (f"A 档近 7 天 n={st['matured']}，PF={_num(st['pf'])}，"
            f"胜率 {_pct(st['win_rate'])}，均收益 {_sgn(st['mean'])}，{cmp_s}{warn}。")


def render_html(rows: list[dict], anchor: date, days: int, profiles: dict) -> tuple[str, str]:
    conclusion = _conclusion(rows, anchor, profiles)
    bench, warns = _bench_table(rows, anchor, days, profiles)
    now = datetime.now(SH).strftime("%Y-%m-%d %H:%M")
    html = f"""<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#222">
      <h2 style="margin:0 0 4px">涨幅榜实盘策略日报 · {anchor}</h2>
      <p style="margin:0 0 8px;color:#666;font-size:13px">生成于 {now}（北京时间）</p>
      <div style="padding:10px 12px;border-left:4px solid #1e40af;background:#f8fafc">
        <p style="margin:0;font-size:14px"><b>结论：</b>{conclusion}</p>
      </div>
      {_daily_table(rows)}
      {_track_table(rows, anchor, days)}
      {bench}
      {warns}
      <p style="margin:14px 0 0;color:#999;font-size:12px">
        口径：净收益已扣 0.3% 往返成本；仅统计已结算（matured）样本，censored(OPEN) 不计入。
        出场口径对齐 backtest_exit_profile.py（做多 TRAIL 3%；做空 FIX 12h/TP50%/SL10%）。
        本邮件为数据分析参考，不构成投资建议。</p>
    </body></html>"""
    return html, conclusion


def build_subject(conclusion: str) -> str:
    return f"【涨幅榜实盘日报】{conclusion}"[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description="涨幅榜实盘策略日报邮件")
    parser.add_argument("--date", type=str, default="", help="报告日 YYYY-MM-DD（默认昨天）")
    parser.add_argument("--days", type=int, default=30, help="累积展示窗口天数（默认 30）")
    parser.add_argument("--email", dest="email", action="store_true", default=True, help="发送邮件")
    parser.add_argument("--no-email", dest="email", action="store_false", help="不发送邮件")
    parser.add_argument("--dry-run", action="store_true", help="只打印 HTML 不发送")
    parser.add_argument("--to", type=str, default="", help="收件人覆盖（逗号分隔）")
    args = parser.parse_args()

    anchor = (date.fromisoformat(args.date) if args.date
              else datetime.now(SH).date() - timedelta(days=1))

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        rows = load(conn)
    profiles = load_exit_profile()
    html, conclusion = render_html(rows, anchor, args.days, profiles)
    subject = build_subject(conclusion)
    print(f"[gainer-report] {anchor} · 结算 {len(rows)} 条 · {conclusion}")

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