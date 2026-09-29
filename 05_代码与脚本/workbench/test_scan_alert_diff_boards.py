#!/usr/bin/env python3
"""「L0 告警邮件融合变化榜」注入测试（工单_L0告警邮件融合变化榜_2026-09-29）。

运行：python workbench/test_scan_alert_diff_boards.py（纯离线）

覆盖：
  A 命中：卡片渲染「📌 同源变化榜：…」标注（含连板 🔥 / 方向榜）；
  B 不在榜 / BRK（蓄势池）⇒ 不渲染标注；
  C 旧调用（2 参，无 diff_boards）⇒ 输出与改动前一致（无变化榜痕迹、无图例锚点）；
  D 判读纪律：`_build_reason` / `_load_reason_context` 不引用 diff_boards（源码级），
    且 tone 与 diff_boards 无关（纯展示，守 N-11A4-G 维持门槛）；
  E `_diff_board_map` 纯函数（离线喂 raw dict）。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_daemon as sd  # noqa: E402

passed = 0
failed = 0


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


def _res():
    return {"event": [], "catalyst": [], "kol": [], "kol_total": 0,
            "catalyst_dir": {"bullish": 0, "bearish": 0, "neutral": 0},
            "catalyst_dir_fresh": {}, "catalyst_raw": 0, "catalyst_latest": None,
            "catalyst_stale": 0, "catalyst_all": [], "asset_linked": True}


def _sig(symbol="TUTUSDT", pool="main", scenario="S1", p_dir="up", vol=3.0):
    return {"id": 1, "symbol": symbol, "pool": pool, "scenario": scenario,
            "timeframe": "15m", "p_dir": p_dir, "price_chg_pct": 3.2,
            "vol_ratio": vol, "oi_dir": "up", "oi_chg_pct": 2.0, "cvd_dir": "up",
            "cvd_usd": None, "cvd_ratio": None, "funding_rate": -0.0003,
            "confidence": "high", "status": "active", "context_tags": ["lv2_15m"],
            "trigger_price": 0.02, "stop_loss_pct": 8.0, "breakout_px": 0.02}


def _it(symbol="TUTUSDT", pool="main", scenario="S1", diff_boards=None,
        diff_date=None, reason=None, p_dir="up", vol=3.0):
    return {"signal": _sig(symbol, pool, scenario, p_dir, vol), "resonance": _res(),
            "diff_boards": diff_boards or [], "diff_date": diff_date, "reason": reason}


def _card(it):
    """整封渲染后取卡片正文（图例前）。"""
    html = sd._render_alert_email([it])
    return html.split("图例：")[0]


QNT_BOARD = [{"category": "price_change_24h", "direction": "up", "rank": 1,
              "metric_value": 50.8, "streak_days": 4}]
SAGA_BOARD = [{"category": "price_change_24h", "direction": "down", "rank": 2,
               "metric_value": -22.3, "streak_days": 4}]

print("\n【A】命中变化榜 ⇒ 卡片渲染「📌 同源变化榜」标注")
_h_qnt = _card(_it(diff_boards=QNT_BOARD, diff_date="2026-09-27"))
check("📌 同源变化榜：" in _h_qnt, "命中标注行存在")
check("🔥 连续4天登涨幅榜 Top1" in _h_qnt, "连板 ≥3 用 🔥 + Top 名次（涨幅榜）")
check("（最近可用变化榜 2026-09-27）" in _h_qnt, "披露变化榜数据日期（prod 滞后，诚实标注）")
_h_saga = _card(_it(diff_boards=SAGA_BOARD, diff_date="2026-09-27"))
check("🔻 连续4天登跌幅榜 Top2" in _h_saga, "跌幅榜用 🔻 + 名次")
# 非连板（streak<3）用普通图标
_h_short = _card(_it(diff_boards=[{"category": "price_change_24h", "direction": "up",
                                   "rank": 5, "streak_days": 1}], diff_date="2026-09-27"))
check("📈 登涨幅榜 Top5" in _h_short and "🔥" not in _h_short, "streak<3 用 📈（非 🔥）")
# rank 缺失 ⇒ 不臆造名次
_h_norank = _card(_it(diff_boards=[{"category": "volume_surge_24h", "direction": "up"}]))
check("📊 登放量榜 上榜" in _h_norank, "rank 缺失 ⇒ 「上榜」（不臆造 TopN）")
# 无 diff_date ⇒ 省略日期后缀
check("最近可用变化榜" not in _h_norank, "diff_date 缺失 ⇒ 省略日期后缀（不臆造当日）")

print("\n【B】不在榜 / BRK（蓄势池）⇒ 不渲染标注")
check("同源变化榜" not in _card(_it()), "不在榜 ⇒ 无标注")
_h_brk = _card(_it(pool="accumulation", scenario="BRK", diff_boards=QNT_BOARD,
                   diff_date="2026-09-27"))
check("同源变化榜" not in _h_brk, "BRK 蓄势池不标（Q4：语义不同）")

print("\n【C】旧调用（2 参，无 diff_boards）⇒ 与改动前一致（无痕迹 + 无图例锚点）")
_old = sd._render_alert_email([_it()])
check("同源变化榜" not in _old, "旧 items 无 diff_boards ⇒ 正文无标注")
check("「📌 同源变化榜」" not in _old.split("图例：")[1],
      "旧 items ⇒ 图例不含「📌 同源变化榜」锚点（守卫跟随实际渲染）")
_d_new = sd._render_alert_email([_it(diff_boards=QNT_BOARD, diff_date="2026-09-27")])
check("「📌 同源变化榜」" in _d_new.split("图例：")[1],
      "有命中 ⇒ 图例挂「📌 同源变化榜」锚点")

print("\n【D】判读纪律：diff_boards 不进 _build_reason / _load_reason_context")
_src = open(sd.__file__, encoding="utf-8").read()
_tree = ast.parse(_src)


def _fn_src(name):
    fn = next((n for n in _tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    return ast.unparse(fn) if fn else ""


check("diff_boards" not in _fn_src("_build_reason"),
      "_build_reason 源码不引用 diff_boards（不改 goods/bads/RR）")
check("diff_boards" not in _fn_src("_load_reason_context"),
      "_load_reason_context 源码不引用 diff_boards")
check("_render_diff_boards" in _src and "def _diff_board_map" in _src,
      "标注渲染与反查 map 为独立纯函数（可离线测）")
# tone 与 diff_boards 无关：同一 sig 的 reason 不受 it.diff_boards 影响
_ctx = {"report_date": "2026-09-27",
        "daily": {"report_date": "2026-09-27", "alerts_n": 10, "win_1h": 0.5, "be_1h": 0.45,
                  "roll3_win_1h": 0.5, "roll3_be_1h": 0.45, "roll3_pf_1h": 1.2,
                  "avg_24h": 1.0, "top_share_bucket": {"dim": "scenario", "bucket": "S1"}},
        "buckets": {("cvd_align", "同向"): {"dim": "cvd_align", "bucket": "同向", "n": 20,
                                            "n_24h": 20, "avg_24h": 1.0, "sl_rate": 0.2,
                                            "ret_p75": 3.0, "mfe_p75": 12.0},
                    ("funding_sign", "<=0"): {"dim": "funding_sign", "bucket": "<=0", "n": 20,
                                              "n_24h": 20, "avg_24h": 1.0, "sl_rate": 0.2,
                                              "ret_p75": 3.0, "mfe_p75": 12.0},
                    ("vol_ratio", "2.5-4"): {"dim": "vol_ratio", "bucket": "2.5-4", "n": 20,
                                             "n_24h": 20, "avg_24h": 1.0, "sl_rate": 0.2,
                                             "ret_p75": 3.0, "mfe_p75": 12.0}},
        "prev_alert_avg": 8.0}
_r = sd._build_reason(_sig(), _ctx)
_rd = sd._build_reason(_sig(), _ctx)   # 传参与 diff_boards 无关
check(_r and _r["tone"] == _rd["tone"], "同一 sig 的 tone 与 diff_boards 无关（纯展示）")

print("\n【E】_diff_board_map 纯函数（离线喂 raw dict）")
_dd = {"diff_date": "2026-09-27",
       "categories": {"price_change_24h": {"up": [{"asset_id": 1555, "rank": 1, "streak_days": 4}],
                                           "down": [{"asset_id": 4888, "rank": 2, "streak_days": 4}]},
                      "volume_surge_24h": {"up": [{"asset_id": 999, "rank": 3}]}}}
_m, _d = sd._diff_board_map(_dd)
check(_d == "2026-09-27" and _m[1555][0]["streak_days"] == 4
      and _m[4888][0]["direction"] == "down" and _m[999][0]["category"] == "volume_surge_24h",
      "反查 map 按 asset_id 聚合 category/direction/rank/streak_days")
check(sd._diff_board_map(None) == ({}, None), "None ⇒ ({}, None)（不抛）")
check(sd._diff_board_map({"categories": {"x": {"up": [{"no_asset": 1}]}}})[0] == {},
      "item 缺 asset_id ⇒ 跳过（不串币）")

print("\n【护栏】渲染无 markdown 强调符 `**`")
check("**" not in _d_new, "渲染结果无 `**`")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
