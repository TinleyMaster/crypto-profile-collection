#!/usr/bin/env python3
"""盘面异动扫描 · 信号日报邮件：biz.scan_signal → SMTP 摘要。

将最近 N 小时的主池/蓄势池信号（high/medium 置信度）整理成 HTML 邮件发送。
同币种去重展示最新一条，并统计窗口内触发次数。

用法：
    python send_scan_signal_brief.py                  # 发送最近 24h 信号邮件
    python send_scan_signal_brief.py --hours 12       # 最近 12h
    python send_scan_signal_brief.py --dry-run        # 只打印 HTML 不发送
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
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

CONF_ORDER = {"high": 0, "medium": 1}
CONF_COLOR = {"high": "#c0392b", "medium": "#e67e22"}
# ── 场景编号口径（两套编码共用 S1..S8 编号空间，同编号语义相反）──
# 生产扫描（scan_daemon._compute_l2）只用 (p_dir, oi_dir) 两维 → S1..S4；
# 设计口径（phase_scan_main_pool.py，未被 daemon 调度）含 cvd_dir 三维 → S1..S8。
# 生产行**也落 cvd_dir**，故判据是 PROD_SCENARIO_BY_DIMS 是否等于行内 scenario。
PROD_SCENARIO_BY_DIMS = {
    ("up", "up"): "S1", ("down", "up"): "S2",
    ("up", "down"): "S3", ("down", "down"): "S4",
}
PROD_SCENARIO_DESC = {
    "S1": "多头进攻", "S2": "空头扎实", "S3": "多头减仓", "S4": "空头兑现",
}
DESIGN_SCENARIO_BY_DIMS = {
    ("up", "up", "up"): "S1", ("up", "up", "down"): "S2",
    ("down", "up", "down"): "S3", ("down", "up", "up"): "S4",
    ("up", "down", "up"): "S5", ("up", "down", "down"): "S6",
    ("down", "down", "down"): "S7", ("down", "down", "up"): "S8",
}
DESIGN_SCENARIO_DESC = {
    "S1": "多头进攻", "S2": "诱多", "S3": "空头扎实", "S4": "诱空",
    "S5": "多头兑现", "S6": "修复反弹", "S7": "跌势衰竭", "S8": "见底反弹",
}
POOL_SCENARIO_DESC = {"ACC": "蓄势(吸筹?)", "BRK": "蓄势突破"}
SHOW_CONF = ("high", "medium")


def _scenario_label(r: dict) -> tuple[str, str]:
    """按行自身维度重算「编号, 文案」，不信任列内 `scenario` 的编码来源。

    直接用设计口径文案表渲染生产行，会把生产 S2（价↓+OI↑，真实空头）标成
    「诱多」——语义相反。
    """
    sc = (r.get("scenario") or "").strip()
    if sc in POOL_SCENARIO_DESC:
        return sc, POOL_SCENARIO_DESC[sc]
    if sc.startswith("SQZ"):
        return sc, ""
    p_dir, oi_dir = r.get("p_dir"), r.get("oi_dir")
    if p_dir in ("up", "down") and oi_dir in ("up", "down"):
        if sc == PROD_SCENARIO_BY_DIMS[(p_dir, oi_dir)]:
            return sc, PROD_SCENARIO_DESC[sc]
        cvd_dir = r.get("cvd_dir")
        if cvd_dir in ("up", "down"):
            sc8 = DESIGN_SCENARIO_BY_DIMS[(p_dir, oi_dir, cvd_dir)]
            return sc8, DESIGN_SCENARIO_DESC[sc8]
    return sc or "-", ""


def _load_signals(conn, hours: int) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                   vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate, confidence, context_tags
            FROM biz.scan_signal
            WHERE created_at > NOW() - make_interval(hours => %s)
            ORDER BY signal_ts DESC
            """,
            (hours,),
        )
        return cur.fetchall()


def _dedupe(rows: list[dict]) -> list[dict]:
    """按 (symbol, pool, scenario) 取最新一条，附触发次数。"""
    by_key: dict[tuple, dict] = {}
    cnt: dict[tuple, int] = defaultdict(int)
    for r in rows:
        key = (r["symbol"], r["pool"], r["scenario"])
        cnt[key] += 1
        if key not in by_key:
            by_key[key] = r
    out = []
    for key, r in by_key.items():
        r = dict(r)
        r["n_hits"] = cnt[key]
        out.append(r)
    out.sort(key=lambda x: (CONF_ORDER.get(x["confidence"], 9), x["signal_ts"]), reverse=True)
    return out


def _fmt_funding(v) -> str:
    if v is None:
        return "-"
    pct = float(v) * 100
    return f"{pct:+.4f}%"


def render_html(signals: list[dict], hours: int, low_count: int) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    head = f"""
    <h2 style="margin:0 0 4px">盘面异动信号 · 日报</h2>
    <p style="margin:0 0 12px;color:#666;font-size:13px">近 {hours} 小时 · 生成于 {now} · high/medium 置信度 · 净收益口径见设计方案 §8</p>"""
    if not signals:
        body = '<p style="color:#999">窗口内无 high/medium 信号。</p>'
    else:
        trs = []
        for r in signals:
            sc, desc = _scenario_label(r)
            conf_color = CONF_COLOR.get(r["confidence"], "#888")
            pool = "蓄势" if r["pool"] == "accumulation" else "主池"
            tags = " ".join(r["context_tags"] or [])
            n = f' <span style="color:#999">×{r["n_hits"]}</span>' if r["n_hits"] > 1 else ""
            trs.append(f"""
            <tr>
              <td>{str(r['signal_ts'])[5:16]}</td>
              <td><b>{r['symbol']}</b>{n}</td>
              <td>{pool}</td>
              <td><b style="color:{conf_color}">{sc}</b> {desc}</td>
              <td>{r['timeframe']}</td>
              <td>{r['p_dir']} {r['price_chg_pct'] if r['price_chg_pct'] is not None else '-'}%</td>
              <td>{r['vol_ratio'] if r['vol_ratio'] is not None else '-'}x</td>
              <td>OI {r['oi_dir'] if r['oi_dir'] else '-'}{f" {r['oi_chg_pct']}%" if r['oi_chg_pct'] is not None else ''}</td>
              <td>CVD {r['cvd_dir'] if r['cvd_dir'] else '-'}</td>
              <td>{_fmt_funding(r['funding_rate'])}</td>
              <td style="font-size:12px;color:#777">{tags}</td>
            </tr>""")
        body = f"""
        <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px;width:100%">
          <tr style="background:#f5f5f5">
            <th>时间</th><th>币种</th><th>池</th><th>场景</th><th>周期</th>
            <th>价格</th><th>量比</th><th>OI</th><th>CVD</th><th>费率</th><th>环境</th>
          </tr>
          {''.join(trs)}
        </table>"""
    footnote = f'<p style="color:#999;font-size:12px">另有 {low_count} 条低置信度信号（仅记录不预警）。本邮件为盘面数据分析参考，不构成投资建议。</p>'
    return f"""<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif">{head}{body}{footnote}</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="盘面信号日报邮件")
    parser.add_argument("--hours", type=int, default=24, help="回溯窗口小时（默认 24）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 HTML 不发送")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        rows = _load_signals(conn, args.hours)
    shown = [r for r in rows if r["confidence"] in SHOW_CONF]
    low_count = len(rows) - len(shown)
    signals = _dedupe(shown)

    html = render_html(signals, args.hours, low_count)
    n_sym = len({r["symbol"] for r in signals})
    print(f"[brief] 窗口 {args.hours}h：原始 {len(rows)} 条 → high/medium {len(shown)} 条 → "
          f"去重后 {len(signals)} 条（{n_sym} 个币），low {low_count} 条")
    if args.dry_run:
        print(html)
        return 0
    if not signals:
        print("[brief] 无信号，不发送邮件")
        return 0

    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("[WARN] SMTP 未配置，跳过发送")
        print(html)
        return 0
    ok, msg = notifier.send(
        f"📊 盘面异动信号日报（{n_sym} 币 · {len(signals)} 条）",
        html,
        from_name="盘面信号扫描",
    )
    print(f"[brief] 发送结果: {'成功' if ok else '失败'} - {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
