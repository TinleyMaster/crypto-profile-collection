#!/usr/bin/env python3
"""告警邮件「纯盘面无共振」审计 B1/B3/B4 修复探针
（审计_盘面异动告警邮件_纯盘面无共振_2026-09-22.md）。

运行：python workbench/test_scan_alert_header_regime.py（纯离线）

覆盖：
  B1 头部「市场环境」= 批次级全局 L0 regime，**不得**泄漏 BRK 原始 token；
     BRK 状态人文化后并列展示（批次条数 + 卡片「触发根」）；
  B3 OI 持平时明示「强度条偏低」（避免与「高置信」同框困惑）；
  B4 CVD 缺值统一为 `n/a`（与图例一致，不再用未定义的「未知」）。
另附 AST 断言：task_scan_alert 计算全局 regime 并传入渲染层。
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


def _res(bull=0, bear=0, neut=0, latest=None, stale=0, fresh=None, linked=True):
    return {"event": [], "catalyst": [], "kol": [],
            "catalyst_dir": {"bullish": bull, "bearish": bear, "neutral": neut},
            "catalyst_dir_fresh": ({"bullish": fresh[0], "bearish": fresh[1],
                                    "neutral": fresh[2]} if fresh else {}),
            "catalyst_raw": bull + bear + neut, "catalyst_latest": latest,
            "catalyst_stale": stale,
            "catalyst_all": [], "asset_linked": linked}


def _body(html: str) -> str:
    """卡片/正文部分（去掉图例与页脚）——图例文本会干扰卡片级断言。"""
    return html.split("图例：")[0]


def _brk_sig():
    return {
        "id": 2, "symbol": "SOPHUSDT", "pool": "accumulation", "scenario": "BRK",
        "timeframe": "1h", "p_dir": "down", "price_chg_pct": None,
        "vol_ratio": 29.93, "oi_dir": None, "oi_chg_pct": None,
        "cvd_dir": None, "cvd_usd": None, "cvd_ratio": None,
        "funding_rate": -0.00961608, "confidence": "high", "status": "confirmed",
        "context_tags": ["brk_down", "vol_x=29.9", "bar=2026-09-22T06"],
        "trigger_price": 0.003857, "stop_loss_pct": 8.0, "breakout_px": 0.003857,
    }


def _main_sig():
    return {
        "id": 3, "symbol": "TUTUSDT", "pool": "main", "scenario": "S1",
        "timeframe": "15m", "p_dir": "up", "price_chg_pct": 3.20,
        "vol_ratio": 2.01, "oi_dir": "up", "oi_chg_pct": 0.29,
        "cvd_dir": "down", "cvd_usd": -31684.0, "cvd_ratio": -0.144,
        "funding_rate": 0.00005, "confidence": "high", "status": "active",
        "context_tags": ["btc_1h=up(+0.06%)", "fgi=72(Greed)",
                         "cap_trend=+5.07%", "空头环境受限（市值+5.07%）", "lv2_15m"],
        "trigger_price": 0.02384, "stop_loss_pct": 8.0, "breakout_px": 0.024,
    }


REGIME = ["btc_1h=up(+0.06%)", "fgi=72(Greed)", "cap_trend=+5.07%",
          "空头环境受限（市值+5.07%）"]

# BRK 强度 29.93×3.0=89.8 > 主池 2.01×0.29≈0.58 ⇒ BRK 必为 items[0]（复现审计场景）
items = [{"signal": _brk_sig(), "resonance": _res()},
         {"signal": _main_sig(), "resonance": _res()}]

print("\n【B1】头部市场环境 = 全局 L0 regime（不泄漏 BRK token）")
html = sd._render_alert_email(items, REGIME)
head = html.split("本批含蓄势池突破")[0]
check("btc_1h=up(+0.06%)" in head, "头部含传入的全局 regime（btc_1h）")
check("fgi=72(Greed)" in head and "cap_trend=+5.07%" in head,
      "头部含 fgi / cap_trend")
for bad in ("brk_down", "vol_x=", "bar="):
    check(bad not in html.split("图例：")[0], f"正文/头部不含 BRK 原始 token `{bad}`")

print("\n【B1】BRK 状态人文化并列展示")
check("本批含蓄势池突破（BRK）1 条" in html, "头部并列「本批含蓄势池突破（BRK）1 条」")
check("触发根 09/22 06:00" in html, "BRK 卡片标「触发根 09/22 06:00」（来自 bar= 标签）")

print("\n【B1】未传 regime_tags 的兜底：仍不得泄漏 token")
html_fb = sd._render_alert_email(items)  # items[0]=BRK，无 L0 标签可抽
body_fb = html_fb.split("图例：")[0]
for bad in ("brk_down", "vol_x=", "bar="):
    check(bad not in body_fb, f"兜底渲染也不泄漏 `{bad}`")
check("市场环境" not in body_fb,
      "BRK 无 L0 标签 ⇒ 兜底不渲染「市场环境」（宁缺勿错）")

print("\n【B3】OI 持平时明示强度条偏低")
check("（OI 持平，强度条偏低）" in html, "主池 OI=+0.29% → 明示「强度条偏低」")
_wide = _main_sig(); _wide["oi_chg_pct"] = 8.0
h2 = sd._render_alert_email([{"signal": _wide, "resonance": _res()}], REGIME)
check("强度条偏低" not in h2, "OI 明显扩张（+8%）时不加该提示")

print("\n【B4】CVD 缺值统一为 n/a")
check("CVD n/a" in html, "BRK 无 CVD → 渲染「CVD n/a」")
check("CVD 未知" not in html, "不再出现图例未定义的「未知」")

print("\n【护栏】HTML 无 markdown 强调符 `**`")
check("**" not in html, "渲染结果无 `**`")

print("\n【AST】task_scan_alert 计算全局 regime 并传入渲染层")
_tree = ast.parse(open(sd.__file__, encoding="utf-8").read())
_fn = next((n for n in _tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "task_scan_alert"), None)
_src = ast.unparse(_fn) if _fn else ""
check("_build_regime(conn)" in _src, "task_scan_alert 调 _build_regime(conn)")
check("_render_alert_email(to_alert, regime_tags)" in _src,
      "把 regime_tags 传入 _render_alert_email（与 context_tags 解耦）")

# ═══════════════════════════════════════════════════════════════
#  N-pure3-1：btc_1h 只用**已收盘** 1h 条（live 价不可复现 + 图例措辞）
# ═══════════════════════════════════════════════════════════════

print("\n【N-pure3-1】btc_1h 用已收盘 1h 条，非未收盘当前小时的 live 价")


class _Cur:
    def __init__(self, conn):
        self.conn, self.mode = conn, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = sql.lower()
        self.mode = ("btc" if "btcusdt" in s else
                     "fgi" if "fear_greed_daily" in s else
                     "cap" if "global_metric_daily" in s else None)

    def fetchall(self):
        return {"btc": self.conn["btc"], "cap": self.conn["cap"]}.get(self.mode, [])

    def fetchone(self):
        return self.conn["fgi"] if self.mode == "fgi" else None


class _Conn:
    def __init__(self, data):
        self.data = data

    def cursor(self, *a, **k):
        return _Cur(self.data)


from datetime import datetime, timedelta, timezone  # noqa: E402

_now = datetime.now(timezone.utc)
# DESC（最新在前）：当前小时未收盘（live 105）、70min 前已收盘（100）、130min 前已收盘（80）
_data = {
    "btc": [
        {"close_px": 105.0, "open_time": _now - timedelta(minutes=10)},
        {"close_px": 100.0, "open_time": _now - timedelta(minutes=70)},
        {"close_px": 80.0, "open_time": _now - timedelta(minutes=130)},
    ],
    "fgi": {"value": 50, "value_classification": "Neutral"},
    "cap": [{"metric_date": "d1", "total_market_cap": 1000},
            {"metric_date": "d0", "total_market_cap": 1000}],
}
reg = sd._build_regime(_Conn(_data))
check("btc_1h=up(+25.00%)" in reg["tags"],
      "已收盘两根：(100−80)/80=+25.00%",
      f"tags={reg['tags']}")
check("btc_1h=up(+5.00%)" not in reg["tags"],
      "不再采用未收盘当前小时的 live 价（(105−100)/100=+5.00%）")

# 全未收盘 → 无 btc_1h 标签（宁缺勿错）
_data2 = dict(_data)
_data2["btc"] = [{"close_px": 105.0, "open_time": _now - timedelta(minutes=5)},
                 {"close_px": 104.0, "open_time": _now - timedelta(minutes=20)}]
reg2 = sd._build_regime(_Conn(_data2))
check(not any(str(t).startswith("btc_1h=") for t in reg2["tags"]),
      "无已收盘条时不产出 btc_1h 标签（不拿 live 冒充收盘）")

# ═══════════════════════════════════════════════════════════════
#  OPT-1~6（审计_盘面异动告警邮件_含共振3封_2026-09-23）
# ═══════════════════════════════════════════════════════════════

print("\n【OPT-1】标题补覆盖币数（共振集中度）")
two = [{"signal": _main_sig(), "resonance": _res(bull=3, fresh=(3, 0, 0))},
       {"signal": _brk_sig(), "resonance": _res()}]
t2 = sd._alert_title(two)
check("（1/2 币）" in t2, "标题并列覆盖币数「1/2 币」（5币11条实为1币的误读）", t2)
t1 = sd._alert_title([{"signal": _main_sig(), "resonance": _res(bull=1, fresh=(1, 0, 0))}])
check("（1/1 币）" in t1, "单币批次「1/1 币」", t1)

print("\n【N-786-1】标题净多用**新鲜**口径（陈旧不推高 conviction）")
# 全量净多10（11-1），新鲜净多4（5-1）——标题须报新鲜
t_stale = sd._alert_title([{"signal": _main_sig(),
                            "resonance": _res(bull=11, bear=1, neut=5,
                                              stale=10, fresh=(5, 1, 0))}])
check("净多4" in t_stale and "净多10" not in t_stale,
      "标题报新鲜净多4（非全量净多10）", t_stale)
check("已剔除陈旧" in t_stale, "标题方向段标「已剔除陈旧」", t_stale)

print("\n【N-786-2】覆盖币数按新鲜计（只有陈旧催化剂的币不算有共振）")
only_stale = [{"signal": _main_sig(),
               "resonance": _res(bull=3, stale=3, fresh=(0, 0, 0))},
              {"signal": _brk_sig(), "resonance": _res()}]
t_onlystale = sd._alert_title(only_stale)
check("无新鲜共振" in t_onlystale and "纯盘面信号" not in t_onlystale,
      "全陈旧 → 标题「无新鲜共振…」而非「纯盘面信号，无共振」（事实错误）", t_onlystale)
check("净多" not in t_onlystale, "全陈旧时不报净多（避免把陈旧当方向）")
# 有新鲜共振的另一币共存时，方向段用新鲜口径并显式标「已剔除陈旧」
mixed = [{"signal": _main_sig(), "resonance": _res(bull=3, stale=3, fresh=(0, 0, 0))},
         {"signal": dict(_main_sig(), id=9),
          "resonance": _res(bull=2, fresh=(2, 0, 0))}]
t_mixed = sd._alert_title(mixed)
check("已剔除陈旧" in t_mixed and "净多2" in t_mixed,
      "混合批次：标题用新鲜口径并标「已剔除陈旧」", t_mixed)

print("\n【N-786-3】卡片全陈旧 → 「无新鲜条目」（不说「多空持平」）")
h_allstale = _body(sd._render_alert_email([{
    "signal": _main_sig(),
    "resonance": _res(bull=17, bear=0, neut=0, stale=17, fresh=(0, 0, 0))}]))
check("剔除陈旧后无新鲜条目" in h_allstale, "全陈旧卡片「剔除陈旧后无新鲜条目」")
check("多空持平" not in h_allstale, "不再误述「多空持平」（含义相反）")

print("\n【OPT-2】陈旧催化剂不推高净多（并列剔除后净值）")
h_stale = _body(sd._render_alert_email([{
    "signal": _main_sig(),
    "resonance": _res(bull=11, bear=1, neut=5, latest="2026-09-22",
                      stale=10, fresh=(4, 1, 2))}]))
check("剔除陈旧后净多3" in h_stale, "含陈旧时并列「剔除陈旧后净多3」")
h_nostale = _body(sd._render_alert_email([{
    "signal": _main_sig(), "resonance": _res(bull=3, stale=0, fresh=(3, 0, 0))}]))
check("剔除陈旧" not in h_nostale, "无陈旧条时不渲染「剔除陈旧」")

print("\n【OPT-3】BRK 无先验时显式标注（不再静默省略）")
h_brk = _body(sd._render_alert_email([{"signal": _brk_sig(), "resonance": _res()}]))
check("BRK 暂无历史先验" in h_brk, "BRK 无 prior → 显式「BRK 暂无历史先验」")
h_main = _body(sd._render_alert_email([{"signal": _main_sig(), "resonance": _res()}]))
check("暂无历史先验" not in h_main, "主池无 prior 时不加 BRK 文案")

print("\n【OPT-5/6】图例补 BRK 设计说明 + 强度条不含催化剂强度")
_leg2 = sd._render_alert_email([{"signal": _main_sig(), "resonance": _res()}], REGIME)
check("BRK 判定只用价+量" in _leg2, "图例说明 BRK 不落 OI/CVD（n/a 是设计）")
check("不含催化剂强度" in _leg2, "图例声明强度条不含催化剂强度")
check("**" not in _leg2, "图例无 markdown 强调符 `**`")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
