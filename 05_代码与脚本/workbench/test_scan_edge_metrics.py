#!/usr/bin/env python3
"""告警胜率赔率日报 · 指标口径复验探针。

运行：python workbench/test_scan_edge_metrics.py
      （离线纯函数恒可跑；能连生产库时自动追加**只读**库不变量校验，连不上只跳过、不判失败）

为什么需要它
------------
「胜率 37%、PF 0.84」这类数字一旦口径写错（如把未到期窗口当已结算、把 beta 收益
算成 alpha），日报会给出**方向相反**的结论。本探针把口径钉成可执行判据：

  * 离线：`aligned_ret` 方向对齐与扣费、`mae_mfe` 最差偏移夹负、`matured_windows`
    未到期闸门、`agg` 的赔率/盈亏平衡线/PF 边界（单侧为空不得记 ∞）、`bucket_of`
    桶边界、`classify_regime` 行情判据、`decide` 规则 A~E 与样本闸门；
  * 库层（只读）：结算表与聚合表的**跨表一致性**（日报 alerts_n 必须等于当日
    告警结算行数）与**闸门不变量**（已过 24h 的行不得仍为 pending）。
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import collect_scan_outcome as co  # noqa: E402
import build_scan_edge_report as be  # noqa: E402

passed = 0
failed = 0
skipped = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \u2713 {name}")
    else:
        failed += 1
        print(f"  \u2717 {name}")
        if detail:
            print(f"    {detail}")


def skip(name, reason):
    global skipped
    skipped += 1
    print(f"  \u2013 跳过 {name}：{reason}")


UTC = timezone.utc

# ═══════════════════════════════════════════════════════════════
#  一、P0 结算口径（离线纯函数）
# ═══════════════════════════════════════════════════════════════

print("\n【P0】方向对齐净收益 `aligned_ret`")
check(co.aligned_ret(100, 102, "up") == 1.9, "up：+2% 原始收益扣 0.1% 费 = +1.9%",
      f"got={co.aligned_ret(100, 102, 'up')}")
check(co.aligned_ret(100, 98, "down") == 1.9,
      "down：-2% 原始收益方向对齐后扣 0.1% 费 = +1.9%（扣费在对齐之后）",
      f"got={co.aligned_ret(100, 98, 'down')}")
check(co.aligned_ret(100, 98, "up") == -2.1, "up 且下跌 = 负收益（-2.1%）",
      f"got={co.aligned_ret(100, 98, 'up')}")
check(co.aligned_ret(100, 100, "up") == -0.1,
      "平盘 = 仅扣费（-0.1%）⇒ 平盘计负，不虚增胜率",
      f"got={co.aligned_ret(100, 100, 'up')}")
check(co.aligned_ret(None, 100, "up") is None, "缺基线价 → None（不兜底为 0）")
check(co.aligned_ret(100, 101, None) is None, "p_dir 为空 → None（不计入统计）")

print("\n【P0】24h 途中风险 `mae_mfe`")
worst, best = co.mae_mfe(100, 90, 110, "up")
check(worst == -10.0 and best == 10.0, "up：最差 -10% / 最好 +10%",
      f"got=({worst}, {best})")
worst, best = co.mae_mfe(100, 90, 110, "down")
check(worst == -10.0 and best == 10.0, "down：最差 -10% / 最好 +10%（方向对齐）",
      f"got=({worst}, {best})")
worst, _ = co.mae_mfe(100, 105, 110, "up")
check(worst == 0.0, "up 且窗口内未跌破基线 → 最差夹到 0（否则「未触及止损」被误判）",
      f"got={worst}")

print("\n【P0】未到期闸门 `matured_windows`")
_t0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
check(co.matured_windows(_t0, _t0 + timedelta(hours=5)) == [1, 4],
      "告警后 5h → 只有 1h/4h 到期（12h/24h 不计入）",
      f"got={co.matured_windows(_t0, _t0 + timedelta(hours=5))}")
check(co.matured_windows(_t0, _t0 + timedelta(hours=24)) == [1, 4, 12, 24],
      "告警后 24h → 四窗口全到期（边界含等号）",
      f"got={co.matured_windows(_t0, _t0 + timedelta(hours=24))}")

print("\n【P0】整行结算 `settle_row`")
_raw = {
    "signal_id": 1, "symbol": "ETHUSDT", "pool": "main", "scenario": "S1",
    "timeframe": "1h", "p_dir": "up", "alerted_at": _t0, "stop_loss_pct": 8,
    "iv": "5m", "base_time": _t0 - timedelta(minutes=5), "base_px": 100,
    "px_1h": 102, "px_4h": 104, "px_12h": 108, "px_24h": 120,
    "btc_base_px": 200, "btc_px_1h": 202, "btc_px_4h": 204,
    "btc_px_12h": 208, "btc_px_24h": 220,
    "low_24h": 95, "high_24h": 125, "bars_n": 24,
}
_r = co.settle_row(_raw, _t0 + timedelta(hours=5))
check(_r["outcome_state"] == "pending" and _r["last_window"] == 4,
      "5h 时点结算：last_window=4、state=pending",
      f"got={_r['outcome_state']}/{_r['last_window']}")
check(_r["aligned_ret_1h"] is not None and _r["aligned_ret_12h"] is None
      and _r["aligned_ret_24h"] is None,
      "未到期窗口写 NULL（不得提前写入未来收益）")
check(_r["excess_1h"] == round(_r["aligned_ret_1h"] - _r["btc_ret_1h"], 10) or
      abs(_r["excess_1h"] - (_r["aligned_ret_1h"] - _r["btc_ret_1h"])) < 1e-9,
      "超额 = 信号收益 − BTC 同向收益",
      f"got excess={_r['excess_1h']} vs {_r['aligned_ret_1h']}-{_r['btc_ret_1h']}")
check(_r["kline_iv"] == "5m", "取价周期落库（5m 主 / 1h 兜底，可审计口径）")

_r24 = co.settle_row(_raw, _t0 + timedelta(hours=25))
check(_r24["outcome_state"] == "resolved" and _r24["last_window"] == 24,
      "25h 时点结算：last_window=24、state=resolved",
      f"got={_r24['outcome_state']}/{_r24['last_window']}")
check(_r24["sl_hit_24h"] is False,
      "24h 最差偏移 -5% 未达 8% 失效位 ⇒ sl_hit=False（不得把未触及当触及）",
      f"got={_r24['sl_hit_24h']} mae={_r24['mae_24h']}")
_r24b = co.settle_row({**_raw, "low_24h": 90}, _t0 + timedelta(hours=25))
check(_r24b["sl_hit_24h"] is True,
      "24h 最低价 90（-10%）跌破 8% 失效位 ⇒ sl_hit=True",
      f"got={_r24b['sl_hit_24h']} mae={_r24b['mae_24h']}")

_nodata = co.settle_row({**_raw, "base_px": None}, _t0 + timedelta(hours=25))
check(_nodata["outcome_state"] == "no_data" and _nodata["last_window"] == 0,
      "无基线 K 线 → no_data（不污染胜率分母）",
      f"got={_nodata['outcome_state']}")
_nodir = co.settle_row({**_raw, "p_dir": None}, _t0 + timedelta(hours=25))
check(_nodir["outcome_state"] == "no_data", "p_dir 为空 → no_data")

# ═══════════════════════════════════════════════════════════════
#  二、P1 聚合口径（离线纯函数）
# ═══════════════════════════════════════════════════════════════

print("\n【P1】指标聚合 `agg`")
a = be.agg([2.0, -1.0, 3.0, -1.0])
check(a["n"] == 4 and a["win"] == 0.5, "胜率 = net>0 占比（4 条中 2 条为正）",
      f"got={a}")
check(abs(a["odds"] - 2.5) < 1e-9, "赔率 = 平均盈利 2.5 ÷ |平均亏损 1.0| = 2.5",
      f"got={a['odds']}")
check(abs(a["be"] - 1 / 3.5) < 1e-9, "盈亏平衡胜率 = 1/(1+赔率) = 28.6%",
      f"got={a['be']}")
check(abs(a["pf"] - 2.5) < 1e-9, "PF = 总盈利 5 ÷ |总亏损| 2 = 2.5", f"got={a['pf']}")
check(a["win"] > a["be"], "50% > 28.6% ⇒ 正期望（判定方向正确）")

b = be.agg([1.0, 2.0])
check(b["odds"] is None and b["be"] is None, "无亏损样本 → 赔率记 NULL（不得记 ∞）",
      f"got={b}")
c = be.agg([-1.0, -2.0])
check(c["odds"] is None and c["win"] == 0.0, "无盈利样本 → 赔率 NULL、胜率 0%",
      f"got={c}")
check(be.agg([])["n"] == 0 and be.agg([])["win"] is None,
      "空样本 → n=0 且各指标 NULL（不是 0%）")
check(be.agg([None, None])["n"] == 0, "全 NULL → n=0（未到期窗口不拉低胜率）")
check(be.agg([0.0, 1.0])["win"] == 0.5, "净收益恰为 0 计负（平盘=手续费亏损）")

print("\n【P1】分桶 `bucket_of`（阈值对齐 scan_daemon）")
check(be.bucket_of("vol_ratio", 2.0) == "<2.5" and be.bucket_of("vol_ratio", 2.5) == "2.5-4",
      "量比桶边界 [2.5) 左闭右开",
      f"got={be.bucket_of('vol_ratio', 2.5)}")
check(be.bucket_of("vol_ratio", -3.0) == "2.5-4", "量比取绝对值后分桶（负值不落入 <2.5）",
      f"got={be.bucket_of('vol_ratio', -3.0)}")
check(be.bucket_of("price_chg", 3.5) == "3-5" and be.bucket_of("price_chg", 9) == ">8",
      "涨幅桶 3-5 / >8",
      f"got={be.bucket_of('price_chg', 3.5)}/{be.bucket_of('price_chg', 9)}")
check(be.bucket_of("oi_chg", 0.5) == "<1", "OI 桶 <1（含 0 附近的小变动）",
      f"got={be.bucket_of('oi_chg', 0.5)}")
check(be.bucket_of("vol_ratio", None) is None, "缺值 → None（不进任何桶）")

print("\n【P1】行情环境 `classify_regime`")
check(be.classify_regime(6.49, 0.44) == "trend", "09-18 实测（振幅 6.49%/占比 44%）→ trend",
      f"got={be.classify_regime(6.49, 0.44)}")
check(be.classify_regime(2.71, 0.16) == "range", "09-22 实测（振幅 2.71%/占比 16%）→ range",
      f"got={be.classify_regime(2.71, 0.16)}")
check(be.classify_regime(1.01, 0.0) == "range", "09-23 实测（振幅 1.01%/占比 0%）→ range",
      f"got={be.classify_regime(1.01, 0.0)}")
check(be.classify_regime(4.0, 0.20) == "mixed", "高振幅但小时波动占比低 → mixed（不硬判）",
      f"got={be.classify_regime(4.0, 0.20)}")
check(be.classify_regime(None, None) == "mixed", "缺行情数据 → mixed")

print("\n【P1】失配判定 `decide`（规则 A~E）")
_base = dict(roll3_alerts_avg=50, prev7_alerts_avg=30, roll3_win_1h=0.40, roll3_be_1h=0.45,
             roll3_pf_1h=0.85, regime_label="mixed", alerts_n=50, edge_buckets=[],
             tf15_prev_edge=False, btc_win_24h=0.60, win_24h=0.60,
             excess_avg_24h=1.0, sample_ready=True)
flag, rules, sev = be.decide(**_base)
check(flag and "A" in rules and sev == "high",
      "A：告警量 50 > 前 7 日均 30×1.5 且滚动胜率 < 平衡线 → HIGH",
      f"got={rules}/{sev}")
check("C" not in rules, "无边缘桶时不得触发 C", f"got={rules}")
f2, r2, _ = be.decide(**{**_base, "roll3_alerts_avg": 30})
check(not f2 and r2 == [], "告警量无异动 → 不触发 A", f"got={r2}")
f3, r3, _ = be.decide(**{**_base, "regime_label": "range", "alerts_n": 30,
                         "roll3_pf_1h": 0.9, "roll3_alerts_avg": 30})
check("B" in r3, "B：横盘 + 告警不减 + PF<1 → 触发", f"got={r3}")
f4, r4, _ = be.decide(**{**_base, "roll3_alerts_avg": 30,
                         "edge_buckets": [{"dim": "vol_ratio", "bucket": "<2.5",
                                           "win_1h": 0.2, "share": 0.4}]})
check("C" in r4 and "D" not in r4, "C：有边缘桶；15m 非边缘则不触发 D", f"got={r4}")
f5, r5, _ = be.decide(**{**_base, "roll3_alerts_avg": 30, "tf15_prev_edge": True,
                         "edge_buckets": [{"dim": "timeframe", "bucket": "15m",
                                           "win_1h": 0.28, "share": 0.3}]})
check("D" in r5, "D：15m 当日+前一日均为边缘桶 → 连续 2 日触发", f"got={r5}")
f6, r6, _ = be.decide(**{**_base, "roll3_alerts_avg": 30, "btc_win_24h": 0.62,
                         "win_24h": 0.60, "excess_avg_24h": 0.2})
check("E" in r6 and _ == "watch", "E：T+24h 胜率≈BTC 且超额<0.5% ⇒ 纯 beta → WATCH",
      f"got={r6}")
f7, r7, s7 = be.decide(**{**_base, "sample_ready": False})
check(not f7 and r7 == [] and s7 == "ok",
      "样本未达门槛 → 只展示不判定（n<10 时不得报警）", f"got={r7}/{s7}")

print("\n【P1】结论文案 `build_conclusion`")
con = be.build_conclusion(severity="ok", rules=[], alerts_n=20, prev7_alerts_avg=25,
                          regime_label="trend", btc_amp_pct=6.0, gt05_ratio=0.5,
                          roll3_win_1h=0.55, roll3_be_1h=0.45, roll3_pf_1h=1.3,
                          edge_buckets=[])
check("未见阈值-行情失配" in con, "无规则 → 明确输出「未见失配」（不空转）", f"got={con}")
con2 = be.build_conclusion(severity="high", rules=["A", "B"], alerts_n=73,
                           prev7_alerts_avg=30, regime_label="range", btc_amp_pct=2.71,
                           gt05_ratio=0.167, roll3_win_1h=0.391, roll3_be_1h=0.43,
                           roll3_pf_1h=0.85,
                           edge_buckets=[{"dim": "vol_ratio", "bucket": "<2.5",
                                          "win_1h": 0.2, "share": 0.4}])
check("规则 A/B" in con2 and "vol_ratio=<2.5" in con2 and "2.71" in con2,
      "有规则 → 列出规则号 + 行情 + 边缘桶坐标（可直接定位阈值）", f"got={con2}")

print("\n【P1】邮件主题 `build_subject`")
try:
    import send_scan_edge_report as se

    sub = se.build_subject({"report_date": date(2026, 9, 22), "severity": "high",
                            "win_1h": 0.37, "pf_1h": 0.84})
    check(sub == "【告警质量日报】09-22 T+1h 胜率 37.0% · PF 0.84 · HIGH",
          "主题含日期/胜率/PF/判定等级（可被邮件客户端检索）", f"got={sub}")
    check(se.build_subject({"report_date": date(2026, 9, 22), "severity": "ok",
                            "win_1h": None, "pf_1h": None}).endswith("OK"),
          "指标缺失时主题不崩（渲染为 -）")
except Exception as exc:  # noqa: BLE001
    check(False, "send_scan_edge_report 可导入且主题可渲染", f"{type(exc).__name__}: {exc}")

# ═══════════════════════════════════════════════════════════════
#  三、库层不变量（只读；连不上则跳过）
# ═══════════════════════════════════════════════════════════════

print("\n【库层】结算/聚合表不变量（只读）")

try:
    import psycopg.rows  # noqa: E402

    with co.get_connection(co.get_settings(require_database=True).database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM biz.scan_signal_outcome")
            total = cur.fetchone()["n"]
            cur.execute("SELECT kline_iv, COUNT(*) AS n FROM biz.scan_signal_outcome "
                        "GROUP BY kline_iv ORDER BY n DESC")
            iv_rows = cur.fetchall()
            cur.execute("SELECT signal_id, alerted_at, outcome_state, last_window, p_dir "
                        "FROM biz.scan_signal_outcome "
                        "WHERE alerted_at + INTERVAL '24 hours' <= NOW()")
            due = cur.fetchall()
            cur.execute("SELECT report_date, alerts_n, matured_n, pending_n, sample_ready, "
                        "       mismatch_flag, mismatch_rules, severity FROM biz.scan_edge_daily "
                        "ORDER BY report_date")
            dailies = cur.fetchall()
            cur.execute("SELECT report_date, dim, bucket, n, win_1h, be_1h, share, edge "
                        "FROM biz.scan_edge_bucket WHERE edge")
            edge_rows = cur.fetchall()
            cur.execute("""
                SELECT b.report_date, b.alerts_n,
                       (SELECT COUNT(*) FROM biz.scan_signal_outcome o
                          JOIN biz.scan_signal s ON s.id = o.signal_id
                         WHERE (o.alerted_at AT TIME ZONE 'Asia/Shanghai')::date = b.report_date
                       ) AS real_n
                  FROM biz.scan_edge_daily b ORDER BY b.report_date""")
            cross = cur.fetchall()
except Exception as exc:  # noqa: BLE001
    skip("库层不变量", f"无法连库：{type(exc).__name__}: {exc}")
    total = None

if total is not None:
    iv_desc = ", ".join("{}={}".format(r["kline_iv"], r["n"]) for r in iv_rows)
    print(f"    （结算表 {total} 行；取价周期分布：{iv_desc}）")
    check(total > 0, "结算表非空（P0 已产出）", "biz.scan_signal_outcome 为空 ⇒ 先跑 collect_scan_outcome.py")
    check(any(r["kline_iv"] == "5m" for r in iv_rows),
          "存在 5m 取价行（可交易口径，非 1h 已收盘棒）",
          f"分布={[(r['kline_iv'], r['n']) for r in iv_rows]}")

    bad_state = [(r["signal_id"], r["outcome_state"], r["last_window"]) for r in due
                 if r["p_dir"] in ("up", "down") and r["outcome_state"] == "pending"]
    check(not bad_state,
          "已过 24h 且方向已知的行不得仍为 pending（结算不滞后）",
          f"滞后行：{bad_state[:5]}")

    bad_lw = [(r["signal_id"], r["last_window"]) for r in due
              if r["last_window"] not in (0, 1, 4, 12, 24)]
    check(not bad_lw, "last_window 只取 0/1/4/12/24（窗口枚举未被污染）",
          f"越界：{bad_lw[:5]}")

    check(all(r["severity"] in ("ok", "watch", "high") for r in dailies),
          "日报 severity 只取 ok/watch/high",
          f"越界：{[(r['report_date'], r['severity']) for r in dailies if r['severity'] not in ('ok','watch','high')]}")
    check(all(bool(r["mismatch_rules"]) == bool(r["mismatch_flag"]) for r in dailies),
          "mismatch_flag 与 mismatch_rules 同真同假（无「有旗无据」）",
          f"不一致：{[(r['report_date'], r['mismatch_flag'], r['mismatch_rules']) for r in dailies if bool(r['mismatch_rules']) != bool(r['mismatch_flag'])]}")
    check(all(r["sample_ready"] == (r["matured_n"] >= 10) for r in dailies),
          "sample_ready ⇔ matured_n >= 10（闸门口径一致）",
          f"不一致：{[(r['report_date'], r['matured_n'], r['sample_ready']) for r in dailies if r['sample_ready'] != (r['matured_n'] >= 10)]}")
    check(all(r["alerts_n"] == r["matured_n"] + r["pending_n"] for r in dailies),
          "alerts_n = matured_n + pending_n（样本账平）",
          f"不平：{[(r['report_date'], r['alerts_n'], r['matured_n'], r['pending_n']) for r in dailies if r['alerts_n'] != r['matured_n'] + r['pending_n']]}")

    drift = [(r["report_date"], r["alerts_n"], r["real_n"]) for r in cross
             if r["alerts_n"] != r["real_n"]]
    check(not drift,
          "日报 alerts_n 等于当日结算行数（聚合口径与结算口径未漂移）",
          f"漂移：{drift}")

    bad_edge = [(r["report_date"], r["dim"], r["bucket"]) for r in edge_rows
                if not (r["n"] >= 5 and r["win_1h"] is not None and r["be_1h"] is not None
                        and float(r["win_1h"]) < float(r["be_1h"])
                        and r["share"] is not None and float(r["share"]) >= 0.2)]
    check(not bad_edge, "边缘桶必须满足 n≥5 且 胜率<平衡线 且 占比≥20%",
          f"越界：{bad_edge[:5]}")
    print(f"    （日报 {len(dailies)} 天，边缘桶 {len(edge_rows)} 个）")

print(f"\n结果：{passed} 通过 / {failed} 失败 / {skipped} 跳过")
sys.exit(1 if failed else 0)
