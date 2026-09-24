#!/usr/bin/env python3
"""盘面告警 · 胜率赔率质量日报邮件（告警胜率赔率日报 P1）。

设计依据：04_架构与代码方案/告警胜率赔率日报方案_2026-09-23.md §6

做什么
------
读 `biz.scan_edge_daily`（当日一行）+ `biz.scan_edge_bucket`（分桶诊断），渲染成 HTML
邮件发出。**本脚本只读不写**，数据由 `build_scan_edge_report.py` 产出。

邮件要回答的问题（顺序即阅读顺序）：
  1. 昨天告警的**胜率 / 赔率 / 盈亏平衡线 / PF**（多窗口 T+1h~24h）——现在是否负期望；
  2. 行情环境是什么（趋势 / 横盘低波动）——**同一条阈值在不同 regime 下期望不同**；
  3. 负期望集中在哪个桶（`vol_ratio<2.5` / `price_chg 3-5` / `15m` …）——该收紧哪个阈值；
  4. 近 5 日趋势 —— 单日小样本噪声大（n=17~79，标准误 ±6~12pt），看趋势不看单日。

用法
----
    python send_scan_edge_report.py                  # 发最新一期（biz.scan_edge_daily 最大日）
    python send_scan_edge_report.py --date 2026-09-22
    python send_scan_edge_report.py --dry-run        # 只打印 HTML 不发送
"""
from __future__ import annotations

import argparse
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

WINDOWS = (1, 4, 12, 24)
SH = timezone(timedelta(hours=8))   # 报告口径与展示时间统一用北京时间（容器 TZ 为 UTC）
SEVERITY_STYLE = {
    "ok":    ("#27ae60", "正常"),
    "watch": ("#e67e22", "观察"),
    "high":  ("#c0392b", "失配"),
}
REGIME_TEXT = {"trend": "趋势", "range": "横盘低波动", "mixed": "混合"}
DIM_TEXT = {
    "vol_ratio": "量比", "price_chg": "涨幅", "oi_chg": "OI变动", "timeframe": "周期",
    "scenario": "场景", "confidence": "置信度", "pool": "池", "regime": "环境",
}
RULE_TEXT = {
    "A": "告警量激增且滚动期望转负", "B": "横盘低波动下告警不减且 PF<1",
    "C": "边缘桶负期望", "D": "15m 通道连续 2 日负期望", "E": "中长窗口正期望主要来自 beta",
}

DAILY_SQL = """
SELECT * FROM biz.scan_edge_daily WHERE report_date = %s
"""

BUCKET_SQL = """
SELECT dim, bucket, n, win_1h, win_4h, odds_1h, be_1h, pf_1h, avg_1h, avg_24h, share, edge
  FROM biz.scan_edge_bucket
 WHERE report_date = %s
 ORDER BY edge DESC, dim, share DESC NULLS LAST
"""

WINDOW_N_SQL = """
SELECT COUNT(aligned_ret_1h)  AS n_1h,  COUNT(aligned_ret_4h)  AS n_4h,
       COUNT(aligned_ret_12h) AS n_12h, COUNT(aligned_ret_24h) AS n_24h
  FROM biz.scan_signal_outcome
 WHERE (alerted_at AT TIME ZONE 'Asia/Shanghai')::date = %s
"""

HISTORY_SQL = """
SELECT report_date, alerts_n, win_1h, pf_1h, severity, regime_label
  FROM biz.scan_edge_daily
 WHERE report_date <= %s
 ORDER BY report_date DESC LIMIT %s
"""


# ──────────────────────────── 格式化 ────────────────────────────

def _pct(v, nd=1) -> str:
    return "-" if v is None else f"{float(v) * 100:.{nd}f}%"


def _num(v, nd=2) -> str:
    return "-" if v is None else f"{float(v):.{nd}f}"


def _signed(v, nd=2, suffix="%") -> str:
    return "-" if v is None else f"{float(v):+.{nd}f}{suffix}"


def build_subject(day: dict) -> str:
    """主题：`【告警质量日报】MM-DD 3日滚动 T+1h 胜率 xx% · PF x.xx · HIGH`。

    用 **3 日滚动**口径而非当日值：判定的 severity 就来自滚动口径，两者同源才不自相矛盾。
    当日值噪声极大且受样本构成影响（2026-09-23：当日混合口径 59.1% / PF 1.10，
    但其中 112 条是 accumulation-BRK 空头、main 池仅 35.9%，滚动口径 44.5% / PF 0.96
    ⇒ 曾经的主题写成「59.1% · PF 1.10 · HIGH」，收件人只看主题会以为系统健康）。
    当日值仍有完整展示，见正文多窗口表与分层表。
    """
    sev = (day.get("severity") or "ok").upper()
    return (f"【告警质量日报】{day['report_date']:%m-%d} "
            f"3日滚动 T+1h 胜率 {_pct(day.get('roll3_win_1h'))} · "
            f"PF {_num(day.get('roll3_pf_1h'))} · {sev}")


def render_html(day: dict, buckets: list[dict], win_n: dict, history: list[dict]) -> str:
    sev = day.get("severity") or "ok"
    color, sev_cn = SEVERITY_STYLE.get(sev, ("#888", sev))
    d: date = day["report_date"]

    # ── 1. 多窗口表 ──
    trs = []
    for w in WINDOWS:
        win, be = day.get(f"win_{w}h"), day.get(f"be_{w}h")
        bad = (win is not None and be is not None and float(win) < float(be))
        win_style = ' style="color:#c0392b;font-weight:bold"' if bad else ""
        trs.append(
            f"<tr><td>T+{w}h</td><td>{win_n.get(w, 0)}</td>"
            f"<td{win_style}>{_pct(win)}</td><td>{_num(day.get(f'odds_{w}h'))}</td>"
            f"<td>{_pct(be)}</td><td>{_num(day.get(f'pf_{w}h'))}</td>"
            f"<td>{_signed(day.get(f'avg_{w}h'))}</td></tr>")
    windows_tbl = f"""
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px">
      <tr style="background:#f5f5f5">
        <th>窗口</th><th>样本</th><th>胜率</th><th>赔率</th><th>盈亏平衡线</th><th>PF</th><th>均净收益</th>
      </tr>
      {''.join(trs)}
    </table>
    <p style="margin:6px 0 0;color:#777;font-size:12px">
      红色 = 胜率低于盈亏平衡线（期望为负）。净收益已扣 0.1% 双边费；
      「样本」为<b>该窗口</b>已到期条数（各窗口不同，长窗口样本更少）；
      未到期样本不计入该窗口，避免把未实现当成已实现。
    </p>"""

    # ── 1.5 分层（池口径）：整体值是多策略混合，必须分开看 ──
    pool_rows = [b for b in buckets if b["dim"] == "pool"]
    pool_rows.sort(key=lambda b: -(b["share"] or 0))
    pool_html = ""
    if len(pool_rows) > 1:
        ptrs = "".join(
            f"<tr><td><b>{b['bucket']}</b></td><td>{b['n']}</td>"
            f"<td>{_pct(b['win_1h'])}</td><td>{_pct(b['be_1h'])}</td>"
            f"<td>{_num(b['pf_1h'])}</td><td>{_pct(b['share'])}</td></tr>"
            for b in pool_rows)
        pool_html = f"""
        <p style="margin:14px 0 4px"><b>分层（池口径，T+1h）</b></p>
        <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px">
          <tr style="background:#f5f5f5"><th>池</th><th>样本</th><th>胜率</th>
              <th>盈亏平衡线</th><th>PF</th><th>占比</th></tr>
          {ptrs}
        </table>
        <p style="margin:6px 0 0;color:#777;font-size:12px">
          多窗口表的「整体」= 各池加权平均，而池间期望可正负相反（如单边下跌日：main 做多亏损、
          accumulation-BRK 做空盈利）⇒「整体为正」不代表各池都为正。
          实际失配的是哪个池，以下方「边缘桶」表为准。
        </p>"""

    # ── 2. 行情环境 ──
    regime = REGIME_TEXT.get(day.get("regime_label"), day.get("regime_label") or "-")
    env = (f"BTC 涨跌 {_signed(day.get('btc_chg_pct'))} · 振幅 {_num(day.get('btc_amp_pct'))}% · "
           f"小时|涨跌|&gt;0.5% 占比 {_pct(day.get('btc_1h_gt05_ratio'))} · "
           f"FGI {day.get('fgi') if day.get('fgi') is not None else '-'} · "
           f"市值趋势 {_signed(day.get('cap_trend_pct'))}")
    env_tbl = f"""
    <p style="margin:14px 0 4px"><b>行情环境：{regime}</b></p>
    <p style="margin:0;color:#555;font-size:13px">{env}</p>"""

    # ── 3. 滚动与 beta 对照 ──
    roll = (f"告警均 {_num(day.get('roll3_alerts_avg'), 1)} 条 · "
            f"T+1h 胜率 {_pct(day.get('roll3_win_1h'))} / 平衡线 {_pct(day.get('roll3_be_1h'))} / "
            f"PF {_num(day.get('roll3_pf_1h'))}")
    beta = (f"信号 T+24h 胜率 {_pct(day.get('win_24h'))}"
            f"（n={win_n.get(24, 0)}）vs BTC 同方向 "
            f"{_pct(day.get('btc_win_24h'))} · 平均超额 {_signed(day.get('excess_avg_24h'))}")
    roll_tbl = f"""
    <p style="margin:14px 0 4px"><b>近 3 日滚动</b>（失配判定口径，抗单日小样本噪声）</p>
    <p style="margin:0;color:#555;font-size:13px">{roll}</p>
    <p style="margin:8px 0 0"><b>beta 对照</b>（信号是否有 alpha）</p>
    <p style="margin:0;color:#555;font-size:13px">{beta}</p>"""

    # ── 4. 边缘桶 ──
    edges = [b for b in buckets if b["edge"]]
    if edges:
        etrs = "".join(
            f"<tr><td>{DIM_TEXT.get(b['dim'], b['dim'])}</td><td><b>{b['bucket']}</b></td>"
            f"<td>{b['n']}</td><td style='color:#c0392b'>{_pct(b['win_1h'])}</td>"
            f"<td>{_pct(b['be_1h'])}</td><td>{_num(b['pf_1h'])}</td>"
            f"<td>{_pct(b['share'])}</td></tr>" for b in edges)
        edge_html = f"""
        <p style="margin:14px 0 4px"><b>⚠ 边缘桶</b>（样本≥5、负期望、占比≥20%）</p>
        <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px">
          <tr style="background:#fdf2f2"><th>维度</th><th>桶</th><th>样本</th><th>胜率</th>
              <th>盈亏平衡线</th><th>PF</th><th>占比</th></tr>
          {etrs}
        </table>
        <p style="margin:6px 0 0;color:#777;font-size:12px">
          胜率/平衡线/PF 均为 <b>T+1h</b> 口径。已剔除两类不可操作项：
          上限开口桶（量比&gt;6、涨幅&gt;8% 等——收紧阈值只会裁掉低档，裁不到它们）与
          同义桶（同一批样本被两个维度重复报出，如 scenario=S1 与 pool=main 完全共线）。
          真正可收紧的是「低档边」桶（如 vol_ratio&lt;2.5），出现时会列在上表。
        </p>"""
    else:
        edge_html = ('<p style="margin:14px 0 0;color:#777;font-size:13px">'
                     '无边缘桶（未发现占比≥20% 且负期望的阈值区间）。</p>')

    # ── 5. 近 N 日趋势 ──
    htrs = "".join(
        f"<tr><td>{h['report_date']:%m-%d}</td><td>{h['alerts_n']}</td>"
        f"<td>{_pct(h['win_1h'])}</td><td>{_num(h['pf_1h'])}</td>"
        f"<td>{REGIME_TEXT.get(h['regime_label'], h['regime_label'] or '-')}</td>"
        f"<td style='color:{SEVERITY_STYLE.get(h['severity'], ('#888',))[0]}'>"
        f"{(h['severity'] or 'ok').upper()}</td></tr>" for h in history)
    hist_html = f"""
    <p style="margin:14px 0 4px"><b>近 {len(history)} 日趋势</b></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px">
      <tr style="background:#f5f5f5"><th>日</th><th>告警数</th><th>T+1h 胜率</th><th>PF</th>
          <th>环境</th><th>判定</th></tr>
      {htrs}
    </table>"""

    # ── 6. 结论 ──
    rules = day.get("mismatch_rules") or []
    rules_html = ""
    if rules:
        rules_html = ("<ul style='margin:6px 0 0;color:#555;font-size:13px'>"
                      + "".join(f"<li>规则 {r}：{RULE_TEXT.get(r, '')}</li>" for r in rules)
                      + "</ul>")
    conclusion = f"""
    <div style="margin-top:16px;padding:10px 12px;border-left:4px solid {color};background:#fafafa">
      <p style="margin:0;font-size:14px"><b style="color:{color}">判定 {sev.upper()}（{sev_cn}）</b></p>
      <p style="margin:6px 0 0;font-size:13px">{day.get('conclusion') or '-'}</p>
      {rules_html}
    </div>"""

    ready = day.get("sample_ready")
    warn = "" if ready else (
        f'<p style="margin:10px 0 0;color:#c0392b;font-size:12px">'
        f'样本未达门槛（T+1h 成熟 {day.get("matured_n")} 条 &lt; 10），本期只展示不判定。</p>')

    # 生成时间固定北京时间：容器 TZ=UTC，用 astimezone() 会让同一字段在容器/本机
    # 各自渲染成不同时刻（00:20 vs 11:45），且收件人无从判断时区。
    now = datetime.now(SH).strftime("%Y-%m-%d %H:%M")
    n_txt = " / ".join(f"T+{w}h {win_n.get(w, 0)}" for w in WINDOWS)
    return f"""<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#222">
      <h2 style="margin:0 0 4px">告警质量日报 · {d}</h2>
      <p style="margin:0 0 12px;color:#666;font-size:13px">
        当日告警 {day.get('alerts_n')} 条 · 各窗口已到期样本 {n_txt}
        · 生成于 {now}（北京时间）</p>
      {warn}{windows_tbl}{pool_html}{env_tbl}{roll_tbl}{edge_html}{hist_html}{conclusion}
      <p style="margin:14px 0 0;color:#999;font-size:12px">
        口径见 04_架构与代码方案/告警胜率赔率日报方案_2026-09-23.md §5。
        本邮件为盘面数据分析参考，不构成投资建议。</p>
    </body></html>"""


# ──────────────────────────── 取数 ────────────────────────────

def load(conn, d: date) -> tuple[dict | None, list[dict], dict, list[dict]]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(DAILY_SQL, (d,))
        day = cur.fetchone()
        if day is None:
            return None, [], {}, []
        cur.execute(BUCKET_SQL, (d,))
        buckets = cur.fetchall()
        cur.execute(WINDOW_N_SQL, (d,))
        n = cur.fetchone() or {}
        win_n = {w: int(n.get(f"n_{w}h") or 0) for w in WINDOWS}
        cur.execute(HISTORY_SQL, (d, 5))
        history = list(reversed(cur.fetchall()))
    return day, buckets, win_n, history


def latest_date(conn) -> date | None:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(report_date) FROM biz.scan_edge_daily")
        r = cur.fetchone()
    return r[0] if r else None


def main() -> int:
    parser = argparse.ArgumentParser(description="告警质量日报邮件")
    parser.add_argument("--date", type=str, default="", help="报告日 YYYY-MM-DD（默认最新一期）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 HTML 不发送")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        d = date.fromisoformat(args.date) if args.date else latest_date(conn)
        if d is None:
            print("[edge] biz.scan_edge_daily 无数据：先跑 build_scan_edge_report.py")
            return 1
        day, buckets, win_n, history = load(conn, d)

    if day is None:
        print(f"[edge] {d} 无日报数据：先跑 build_scan_edge_report.py --date {d}")
        return 1

    html = render_html(day, buckets, win_n, history)
    subject = build_subject(day)
    # 日志同时打印当日与滚动口径：主题用的是滚动值，只打当日值会让运维误以为主题算错。
    print(f"[edge] {d} · {day.get('severity', 'ok').upper()} · 告警 {day.get('alerts_n')} 条 "
          f"· 当日 T+1h 胜率 {_pct(day.get('win_1h'))} / 平衡线 {_pct(day.get('be_1h'))} "
          f"· PF {_num(day.get('pf_1h'))}"
          f" | 3 日滚动 T+1h 胜率 {_pct(day.get('roll3_win_1h'))} / 平衡线 "
          f"{_pct(day.get('roll3_be_1h'))} · PF {_num(day.get('roll3_pf_1h'))}"
          f" · 边缘桶 {sum(1 for b in buckets if b['edge'])} 个")
    if args.dry_run:
        print(subject)
        print(html)
        return 0

    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("[WARN] SMTP 未配置，跳过发送")
        print(html)
        return 0
    ok, msg = notifier.send(subject, html, from_name="盘面告警质量")
    print(f"[edge] 发送结果: {'成功' if ok else '失败'} - {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
