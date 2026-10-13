#!/usr/bin/env python3
"""扩容就绪检查（scale readiness）：跑一遍判断实盘是否够格加资金。

判定框架（2026-10-13 定稿，基于回测 vs 实盘差距 + 保护单事故复盘）：
  实盘必须先证明自己，才允许放大资金池。加钱不改变单笔风险占比
  （单笔最大亏损 ≈ 权益 6%），只放大总规模。

Tier 1（100U → 200~300U）须全部满足：
  ① 有效交易 ≥ 30 笔
  ② 实盘胜率 ≥ 回测基准 × 0.7（SHORT_LONG ≥48% / MID_LONG ≥46%）
  ③ 修复后（--since-fix 起）交易 ≥ 10 笔且无保护单显式异常
     ⚠️ 只能检测「DB 里 stop_loss/trailing_stop 显式 error 或缺标识」；
     DOSUSDT 型「撤旧止损成功、挂新失败、DB 仍留旧记录」无法静态检出
     （需要运行时日志），故必须靠修复后样本验证。
  ④ 单笔最大亏损 ≤ 该笔名义 × SL% × 2（2 倍滑点容差）—— 事故单会远超此线
  ⑤ 权益最大回撤 ≤ 7.5%

Tier 2（5~10 倍）：有效交易 ≥ 50 笔 + 胜率/期望/PF 与回测置信区间重叠
  + 连续 3 个月零事故（本脚本给出前两项，零事故需人工按时间核验）。

用法：
    python bin/scale_readiness.py                    # 默认按 100U 权益基准
    python bin/scale_readiness.py --equity 300       # 指定当前权益基准
    python bin/scale_readiness.py --since-fix "2026-10-13 16:00"   # 修复部署时刻
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# ── 回测基准（含 0.3% 成本；来源 exit_profile.json + 2026-10-07 收窄版 B 右上角回测）──
BACKTEST_WIN_RATE = {"SHORT_LONG": 0.686, "MID_LONG": 0.662}
BACKTEST_MEAN = {"SHORT_LONG": 0.053, "MID_LONG": 0.043}

TIER1_MIN_TRADES = 30
TIER1_MIN_TRADES_SINCE_FIX = 10
WIN_RATE_FACTOR = 0.7
SL_SLIP_MULT = 2.0          # 单笔亏损 vs 名义×SL% 的容差倍数
MAX_DRAWDOWN_PCT = 7.5      # 权益最大回撤上限 %
DEFAULT_SINCE_FIX = "2026-10-13 16:00"  # 保护单修复部署时刻（Asia/Shanghai）


def _tz8() -> timezone:
    from datetime import timedelta
    return timezone(timedelta(hours=8))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=100.0, help="当前权益基准 USDT")
    ap.add_argument("--since-fix", default=DEFAULT_SINCE_FIX,
                    help="保护单修复部署时刻（Asia/Shanghai，'YYYY-MM-DD HH:MM'）")
    args = ap.parse_args()
    equity = args.equity
    since_fix = datetime.strptime(args.since_fix, "%Y-%m-%d %H:%M").replace(tzinfo=_tz8())

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, signal_type, leverage, notional_usdt, sl_pct, open_ts, "
                "       realized_pnl_usdt, commission_usdt, exit_reason, protective_orders "
                "FROM biz.v2_trade_log WHERE origin='bot' ORDER BY open_ts")
            trades = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]

    # ── 统计 ──
    valid = []
    for t in trades:
        net = (float(t["realized_pnl_usdt"] or 0) + float(t["commission_usdt"] or 0))
        t["net"] = net
        if t["exit_reason"] != "aborted_protective" and abs(net) >= 0.0005:
            valid.append(t)

    def _win_rate(rows):
        if not rows:
            return None
        return sum(1 for r in rows if r["net"] > 0) / len(rows)

    by_type = {}
    for t in valid:
        by_type.setdefault(t["signal_type"], []).append(t)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in valid:
        cum += t["net"]
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    # 保护单显式异常（DB 可检）：stop_loss/trailing_stop 含 error、缺标识、或回滚
    def _prot_ok(v):
        if not v or "error" in v:
            return False
        return bool(v.get("orderId") or v.get("algoId") or v.get("clientAlgoId"))

    def _prot_issue(t):
        prot = t.get("protective_orders") or {}
        sl = prot.get("stop_loss") or {}
        trl = prot.get("trailing_stop") or {}
        if t["exit_reason"] == "aborted_protective":
            return "aborted_protective(保护单失败已回滚)"
        if (t.get("sl_pct") or 0) > 0 and not _prot_ok(sl):
            return "stop_loss 缺失/标记 error"
        if isinstance(trl, dict) and "error" in trl:
            return "trailing_stop 标记 error"
        return None

    issues = [(t, _prot_issue(t)) for t in valid]
    issues = [(t, m) for t, m in issues if m]
    since_fix_trades = [t for t in valid if t["open_ts"].astimezone(_tz8()) >= since_fix]
    since_fix_issues = [(t, m) for t, m in issues if t in since_fix_trades]

    # 单笔最大亏损 vs 名义×SL%×2
    worst = min(valid, key=lambda t: t["net"]) if valid else None
    worst_allowed = None
    if worst is not None:
        notional = float(worst["notional_usdt"] or 0)
        sl_pct = float(worst["sl_pct"] or 0)
        worst_allowed = notional * sl_pct * SL_SLIP_MULT

    # ── 逐项检查 ──
    checks = []
    n = len(valid)
    checks.append(("① 有效交易 ≥30 笔", n, 30, n >= 30,
                   f"{n} 笔（还差 {max(0, 30 - n)} 笔）"))
    sl_ok = True
    sl_detail = ""
    for st in ("SHORT_LONG", "MID_LONG"):
        wr = _win_rate(by_type.get(st, []))
        th = BACKTEST_WIN_RATE[st] * WIN_RATE_FACTOR
        ok = wr is not None and wr >= th
        sl_ok = sl_ok and ok
        sl_detail += f"{st}: 实盘{'%.0f' % (wr*100) if wr is not None else '无样本'}% / 门槛{th*100:.0f}%  {'✓' if ok else '✗'}  "
    checks.append(("② 实盘胜率 ≥ 回测×0.7", None, None, sl_ok, sl_detail))
    fix_ok = len(since_fix_trades) >= TIER1_MIN_TRADES_SINCE_FIX and len(since_fix_issues) == 0
    checks.append(("③ 修复后保护单验证", len(since_fix_trades), TIER1_MIN_TRADES_SINCE_FIX, fix_ok,
                   f"修复后 {len(since_fix_trades)} 笔 / 异常 {len(since_fix_issues)} 笔"
                   f"（需 ≥{TIER1_MIN_TRADES_SINCE_FIX} 笔且 0 异常）"))
    worst_ok = worst is None or worst_allowed is None or worst["net"] >= -worst_allowed
    checks.append(("④ 单笔亏损 ≤ 名义×SL×2", None, None, worst_ok,
                   (f"最差单笔 {worst['symbol']} 亏 {worst['net']:.2f}U"
                    f" / 容差上限 {worst_allowed:.2f}U" if worst else "无交易")))
    dd_pct = max_dd / equity * 100
    checks.append(("⑤ 权益最大回撤 ≤7.5%", f"{dd_pct:.1f}%", "7.5%", dd_pct <= MAX_DRAWDOWN_PCT,
                   f"回撤 {dd_pct:.1f}%（累计净 PnL {cum:+.2f}U / 权益 {equity:.0f}U）"))

    # ── 输出 ──
    print(f"==== 扩容就绪检查（equity={equity:.0f}U，修复起点 {args.since_fix} 上海）====")
    print(f"实盘统计：有效 {n} 笔 | 胜率 {_win_rate(valid)*100:.0f}% | "
          f"累计净 PnL {cum:+.2f}U（{cum/equity*100:+.1f}% 权益）| 最大回撤 {dd_pct:.1f}%")
    for st in ("SHORT_LONG", "MID_LONG"):
        rows = by_type.get(st, [])
        if rows:
            print(f"  {st:<11} {len(rows)} 笔 胜率 {_win_rate(rows)*100:.0f}% 净 {sum(r['net'] for r in rows):+.2f}U")
    print()
    for name, cur, target, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"        {detail}")
    if issues:
        print("  ⚠️ 历史保护单显式异常：")
        for t, m in issues:
            print(f"        {t['symbol']} ({t['signal_type']}): {m}")

    # 结论
    passed = sum(1 for c in checks if c[3])
    print()
    if all(c[3] for c in checks):
        t2_ok = n >= 50
        if t2_ok:
            print("✅ Tier 2 达标（样本≥50）：可考虑 5~10 倍扩容；零事故需人工按 3 个月核验")
        else:
            print("✅ Tier 1 达标：可考虑 100U → 200~300U（保持单笔风险占比不变）")
    else:
        print(f"⛔ 未达标（{passed}/{len(checks)} 项通过）→ 维持当前 {equity:.0f}U 规模")
        print("   优先补齐：③修复后样本验证 → ①样本量 → ②④⑤ 随实盘表现收敛")
    return 0


if __name__ == "__main__":
    sys.exit(main())
