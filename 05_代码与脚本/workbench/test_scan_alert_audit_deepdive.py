#!/usr/bin/env python3
"""告警邮件「深层补刀」O2~O6 修复探针（审计_盘面异动告警邮件_3币1币_2026-09-22.md §八）。

运行：python workbench/test_scan_alert_audit_deepdive.py

覆盖面（全部离线，不连库）：
  O2 主题/卡片补「净多 = 多−空」，中性不计方向；
  O3 失效位触夹带上下限时不再写「2×ATR(14)」，改标「已触下限/上限」；
  O4「零催化 + 费率未覆盖 + 资产已关联」→ 纯技术面淡提示；
  O5 图例澄清「共振」= 事件+催化剂+KOL 三段聚合，非 catalyst_resonance 表；
  N1 催化剂新鲜度（最新日期 + >CATALYST_STALE_DAYS 天条数）；
  O6 task_scan_alert 把共振快照落 `detail`（AST 源码形状，回放用）。
另附「HTML 邮件不得含 markdown 强调符 `**`」护栏（重踩过的坑）。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
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


def _res(linked=True, bull=0, bear=0, neut=0, latest=None, stale=0,
         event=0, kol=0):
    return {
        "event": [f"e{i}" for i in range(event)],
        "catalyst": [],
        "catalyst_dir": {"bullish": bull, "bearish": bear, "neutral": neut},
        "catalyst_raw": bull + bear + neut,
        "catalyst_latest": latest,
        "catalyst_stale": stale,
        "catalyst_all": [],
        "kol": [f"k{i}" for i in range(kol)],
        "asset_linked": linked,
    }


def _sig(**kw):
    base = {
        "id": 1, "symbol": "TESTUSDT", "pool": "main", "scenario": "S1",
        "timeframe": "15m", "p_dir": "up", "price_chg_pct": 3.0,
        "vol_ratio": 3.0, "oi_dir": "up", "oi_chg_pct": 2.0,
        "cvd_dir": "up", "cvd_usd": 100.0, "cvd_ratio": 0.05,
        "funding_rate": 0.0001, "confidence": "high", "status": "active",
        "context_tags": ["lv2_15m"], "trigger_price": 100.0,
        "stop_loss_pct": 8.0, "breakout_px": 101.0,
    }
    base.update(kw)
    return base


def _item(sig=None, res=None, prior=None):
    return {"signal": sig or _sig(), "resonance": res or _res(), "prior": prior}


def _body(html: str) -> str:
    """卡片/正文部分（去掉图例与页脚）——图例文本会干扰卡片级断言。"""
    return html.split("图例：")[0]


# ═══════════════════════════════════════════════════════════════
#  O2 —— 净多/净空（主题 + 卡片）
# ═══════════════════════════════════════════════════════════════

print("\n【O2】主题与卡片补「净多 = 多−空」")
items = [_item(res=_res(bull=4, bear=0, neut=2))]
title = sd._alert_title(items)
check("净多4" in title, "_alert_title 含「净多4」（中性不计方向）", title)
check("催化剂 4多/0空/2中" in title, "_alert_title 保留原始多/空/中构成", title)
tie = sd._alert_title([_item(res=_res(bull=0, bear=0, neut=3))])
check("净" not in tie, "全中性（多=空）不输出净方向（避免「净多0」误读）", tie)
html_up = sd._render_alert_email([_item(res=_res(bull=3, bear=1, neut=2))])
check("净多2" in _body(html_up), "卡片括注含「净多2」（3−1）")

# ═══════════════════════════════════════════════════════════════
#  O3 —— 失效位触夹带上下限的标注
# ═══════════════════════════════════════════════════════════════

print("\n【O3】触夹带上下限时不再冒充「2×ATR」")
h_floor = _body(sd._render_alert_email([_item(sig=_sig(stop_loss_pct=sd.STOP_PCT_MIN))]))
check(f"已触下限 {sd.STOP_PCT_MIN:.0f}%" in h_floor,
      f"stop={sd.STOP_PCT_MIN} → 标注「已触下限 {sd.STOP_PCT_MIN:.0f}%」")
check("真实 2×ATR 更窄" in h_floor, "并说明真实 2×ATR 更窄（纠正波动误判）")
h_ceil = _body(sd._render_alert_email([_item(sig=_sig(stop_loss_pct=sd.STOP_PCT_MAX))]))
check(f"已触上限 {sd.STOP_PCT_MAX:.0f}%" in h_ceil,
      f"stop={sd.STOP_PCT_MAX} → 标注「已触上限 {sd.STOP_PCT_MAX:.0f}%」")
h_mid = _body(sd._render_alert_email([_item(sig=_sig(stop_loss_pct=12.0))]))
check("2×ATR(14)" in h_mid, "带内 stop=12% → 保留「2×ATR(14)」原文案")
check("已触" not in h_mid, "带内卡片不出现「已触下限/上限」")

# ═══════════════════════════════════════════════════════════════
#  P2-N7 —— 失效位幅度符号必须随方向（做空首次显形）
# ═══════════════════════════════════════════════════════════════

print("\n【P2-N7】失效位幅度符号随方向（做多「跌破 -」/ 做空「升破 +」）")
h_long = _body(sd._render_alert_email([_item(sig=_sig(p_dir="up", trigger_price=100.0,
                                                      stop_loss_pct=8.0))]))
check("跌破 92.000000" in h_long, "做多：失效位在入场下方（100 × (1−8%)）", h_long)
check("（-8.00%" in h_long, "做多：幅度带负号（价格下行）")
h_short = _body(sd._render_alert_email([_item(sig=_sig(p_dir="down", trigger_price=100.0,
                                                       stop_loss_pct=16.35))]))
check("升破 116.350000" in h_short, "做空：失效位在入场上方（100 × (1+16.35%)）", h_short)
check("（+16.35%" in h_short, "做空：幅度带正号（价格上行）")
check("（-16.35%" not in h_short,
      "做空不得渲染「升破 …（-16.35%）」——方向词与符号自相矛盾（实物 id=1292 龙虾USDT）")

# ═══════════════════════════════════════════════════════════════
#  O4 —— 纯技术面淡提示
# ═══════════════════════════════════════════════════════════════

print("\n【O4】「零催化 + 费率未覆盖」加纯技术面提示")
h_tech = _body(sd._render_alert_email([_item(sig=_sig(funding_rate=None), res=_res())]))
check("纯技术面信号" in h_tech, "cat=0 & funding=NULL & linked → 纯技术面提示")
h_has_cat = _body(sd._render_alert_email([_item(sig=_sig(funding_rate=None),
                                                 res=_res(bull=1))]))
check("纯技术面信号" not in h_has_cat, "有催化剂时不误标纯技术面")
h_unlinked = _body(sd._render_alert_email([_item(sig=_sig(funding_rate=None),
                                                  res=_res(linked=False))]))
check("纯技术面信号" not in h_unlinked,
      "资产未关联（催化剂无从查询）时不误判为「无催化」")
h_has_fund = _body(sd._render_alert_email([_item(sig=_sig(funding_rate=0.0001),
                                                 res=_res())]))
check("纯技术面信号" not in h_has_fund, "有费率时不误标纯技术面")

# ═══════════════════════════════════════════════════════════════
#  O5 / N1 —— 图例澄清 + 催化剂新鲜度
# ═══════════════════════════════════════════════════════════════

print("\n【O5】图例澄清「共振」语义")
html = sd._render_alert_email([_item()])
check("事件预置 + 催化剂 + KOL 三段聚合" in html, "图例写明共振=三段聚合")
check("catalyst_resonance" in html, "图例显式点出与 catalyst_resonance 表语义不同")

print("\n【N1】催化剂新鲜度披露")
h_fresh = _body(sd._render_alert_email([_item(
    res=_res(bull=3, neut=2, latest="2026-09-21", stale=1))]))
check("最新 2026-09-21" in h_fresh, "括注含最新催化剂日期")
check(f"含 1 条 >{sd.CATALYST_STALE_DAYS} 天" in h_fresh,
      f"括注含陈旧条数（>{sd.CATALYST_STALE_DAYS} 天）")
h_none = _body(sd._render_alert_email([_item(res=_res(bull=1, latest=None, stale=0))]))
check("最新" not in h_none, "无 published_at 时不渲染「最新」")
h_nostale = _body(sd._render_alert_email([_item(res=_res(bull=1, latest="2026-09-21"))]))
check("含 0 条" not in h_nostale, "陈旧条数为 0 时不渲染该字段")

# ═══════════════════════════════════════════════════════════════
#  O6 —— 共振快照落 detail（源码 AST）
# ═══════════════════════════════════════════════════════════════

print("\n【O6】task_scan_alert 落共振快照到 detail（源码 AST）")
_src = open(sd.__file__, encoding="utf-8").read()
_tree = ast.parse(_src)
_alert_fn = next((n for n in _tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "task_scan_alert"), None)
_alert_src = ast.unparse(_alert_fn) if _alert_fn else ""
check(_alert_fn is not None, "找到 scan_daemon.task_scan_alert()")
check("resonance_snapshot" in _alert_src, "快照键 resonance_snapshot 存在")
check("detail" in _alert_src and "COALESCE" in _alert_src,
      "以 COALESCE(detail,'{}') || 快照 写 detail（不覆盖 squeeze 池既有 metrics）")
check("captured_at" in _alert_src, "快照带 captured_at 时间戳")

print("\n【护栏】HTML 邮件不得含 markdown 强调符 `**`")
check("**" not in html, "渲染结果无 `**`（应使用 <b>/<span style>）",
      "发现 markdown 强调符会原样进入邮件正文")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
