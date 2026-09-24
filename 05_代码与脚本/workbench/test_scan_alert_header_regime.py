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


def _res(bull=0, bear=0, neut=0, latest=None, stale=0, fresh=None, linked=True,
         event=0, kol=0, cat=0, kol_total=None):
    # 2026-09-24：三段消息本体由 `str` 改为 `dict`（渲染层现在真的渲染它们）。
    return {"event": [{"dir": "bearish" if i % 2 == 0 else "neutral",
                       "kind": "🔓 解锁" if i % 2 == 0 else "🔄 链上转账",
                       "text": f"事件摘要{i}", "date": None}
                      for i in range(event)],
            "catalyst": [{"dir": "bullish" if i % 2 == 0 else "bearish",
                          "strength": "strong",
                          "text": f"催化剂摘要{i}", "date": "2026-09-21"}
                         for i in range(cat)],
            "kol": [{"dir": "bullish" if i % 2 == 0 else "bearish",
                     "text": f"KOL 看涨（T{i}USDT）",
                     "conf": 0.8, "date": "2026-09-21"} for i in range(kol)],
            "kol_total": kol if kol_total is None else kol_total,
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
check("触发根 09/22 14:00" in html,
      "BRK 卡片标「触发根 09/22 14:00」（bar= UTC 根 +8 转北京时间）")
check("生成于" in html and "（北京时间）" in html,
      "抬头生成时间标注北京时间（东八区）")

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
check("（1/2 币）" in t2, "多币批次并列覆盖币数「1/2 币」（5币11条实为1币的误读）", t2)
t1 = sd._alert_title([{"signal": _main_sig(), "resonance": _res(bull=1, fresh=(1, 0, 0))}])
# N-923-1：单币时「（1/1 币）」与条数重复 ⇒ 省略
check("1/1 币" not in t1, "单币批次省略「（1/1 币）」（N-923-1 冗余）", t1)

print("\n【N-923-1】方向段以「新鲜」前缀标口径，不再无条件印「已剔除陈旧」")
h_nostale_t = sd._alert_title([{"signal": _main_sig(),
                                "resonance": _res(bull=1, fresh=(1, 0, 0))}])
check("催化剂新鲜 1多/0空/0中" in h_nostale_t,
      "全新鲜（stale=0）→ 「催化剂新鲜 1多/0空/0中」（不再虚假暗示剔除）", h_nostale_t)
check("已剔除陈旧" not in h_nostale_t, "无陈旧可剔时不再印「已剔除陈旧」（46% 场景）")

print("\n【N-786-1】标题净多用**新鲜**口径（陈旧不推高 conviction）")
# 全量净多10（11-1），新鲜净多4（5-1）——标题须报新鲜
t_stale = sd._alert_title([{"signal": _main_sig(),
                            "resonance": _res(bull=11, bear=1, neut=5,
                                              stale=10, fresh=(5, 1, 0))}])
check("净多4" in t_stale and "净多10" not in t_stale,
      "标题报新鲜净多4（非全量净多10）", t_stale)
check("催化剂新鲜 5多/1空/0中" in t_stale, "标题方向段标「催化剂新鲜 …」口径", t_stale)

print("\n【N-786-2】覆盖币数按新鲜计（只有陈旧催化剂的币不算有共振）")
only_stale = [{"signal": _main_sig(),
               "resonance": _res(bull=3, stale=3, fresh=(0, 0, 0))},
              {"signal": _brk_sig(), "resonance": _res()}]
t_onlystale = sd._alert_title(only_stale)
check("无新鲜共振" in t_onlystale and "纯盘面信号" not in t_onlystale,
      "全陈旧 → 标题「无新鲜共振…」而非「纯盘面信号，无共振」（事实错误）", t_onlystale)
check("净多" not in t_onlystale, "全陈旧时不报净多（避免把陈旧当方向）")
# 有新鲜共振的另一币共存时，方向段用新鲜口径
mixed = [{"signal": _main_sig(), "resonance": _res(bull=3, stale=3, fresh=(0, 0, 0))},
         {"signal": dict(_main_sig(), id=9),
          "resonance": _res(bull=2, fresh=(2, 0, 0))}]
t_mixed = sd._alert_title(mixed)
check("催化剂新鲜" in t_mixed and "净多2" in t_mixed,
      "混合批次：标题用新鲜口径（「催化剂新鲜」前缀）", t_mixed)

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

print("\n【N-786-5】标题「含共振 N 条」= 全量（与卡片同口径），方向段用新鲜")
# ARB 式：全量 17（新鲜 6）⇒ 标题条数应为全量 17（非 6），方向段用新鲜
t_mix = sd._alert_title([{"signal": _main_sig(),
                          "resonance": _res(bull=11, bear=1, neut=5,
                                            stale=11, fresh=(5, 1, 0))}])
check("含共振 17 条" in t_mix, "标题条数为**全量** 17（与卡片「催化剂17」一致）", t_mix)
check("含共振 6 条" not in t_mix, "标题不再用新鲜口径的 6 条（消除跨层数字打架）")
check("净多4" in t_mix, "方向段仍用新鲜净多4")
_leg5 = sd._render_alert_email([{"signal": _main_sig(), "resonance": _res(bull=1)}], REGIME)
check("含共振 N 条" in _leg5 and "全量" in _leg5,
      "图例声明「含共振 N 条」为全量口径（N-786-5 口径变更须声明）")

print("\n【N-786-4】n_res 由 event/KOL 贡献、催化剂真为 0 时不得说「全部 >3 天」")
# 模拟 DOT/WLD：event=1、催化剂 0 条 ⇒ 标题方向段应写「无催化剂条目」
t_zero = sd._alert_title([{"signal": _main_sig(),
                           "resonance": _res(event=1, fresh=(0, 0, 0))}])
check("无催化剂条目" in t_zero, "催化剂真为 0 → 「催化剂新鲜条目 0（无催化剂条目）」", t_zero)
check("全部 >3 天" not in t_zero, "不误述「全部 >3 天」（卡片会显示催化剂0，自相矛盾）")
# 对照：确有全陈旧催化剂时仍写「全部 >3 天」
t_stl = sd._alert_title([{"signal": _main_sig(),
                          "resonance": _res(event=1, bull=3, stale=3, fresh=(0, 0, 0))}])
check("全部 >3 天" in t_stl, "确有全陈旧催化剂 → 仍写「全部 >3 天」", t_stl)

print("\n【N-416-1】混合批次（零催化剂币 + 全陈旧币）三态化，不得二选一失真")
# 币A=全陈旧(5条)、币B=event=1且催化剂0 → 旧实现 has_zero_cat 抢先 ⇒ 对币A误述「无催化剂条目」
mixed2 = [{"signal": _main_sig(),
           "resonance": _res(bull=5, stale=5, fresh=(0, 0, 0))},
          {"signal": dict(_main_sig(), id=8), "resonance": _res(event=1)}]
t_mix2 = sd._alert_title(mixed2)
check("含仅陈旧条目" in t_mix2 and "另有币无催化剂" in t_mix2,
      "混合批次 → 「含仅陈旧条目，另有币无催化剂」（三态化，不二选一失真）", t_mix2)
check("无催化剂条目）" not in t_mix2,
      "混合批次不对全陈旧币误述「无催化剂条目」（N-416-1 镜像失真）")

print("\n【N-416-3】标题条数与覆盖币数同窗（全量），不再两窗混搭")
# 币A=全陈旧(5条)、币B=event=1 → 全量条数 6、全量覆盖 2 币；旧实现覆盖用新鲜 → (1/2 币)
check("含共振 6 条（2/2 币）" in t_mix2,
      "条数 6 与覆盖币数 2/2 同为全量窗（消除「6 条（1/2 币）」混搭）", t_mix2)
check("1/2 币" not in t_mix2, "不再出现新鲜窗覆盖币数「1/2 币」")

print("\n【N-90EC-1】密封边界（n_res==0）也须三态化（近 7 天 6 批真实触发）")
# 币A=全陈旧(3条,无 event)、币B=纯盘面无共振 ⇒ n_res==0 且 has_stale and has_zero_cat
sealed_mix = [{"signal": _main_sig(),
               "resonance": _res(bull=3, stale=3, fresh=(0, 0, 0))},
              {"signal": dict(_main_sig(), id=7), "resonance": _res()}]
t_sealed = sd._alert_title(sealed_mix)
check("仅陈旧条目" in t_sealed and "另有币无催化剂" in t_sealed,
      "密封边界混合 → 「含仅陈旧条目，另有币无催化剂」（N-90EC-1 三态化）", t_sealed)
check("全部 >3 天" not in t_sealed,
      "密封边界不再断言「全部 >3 天」（对零催化剂币是事实错误）")
# 密封边界纯陈旧（无零催化剂币）→ 保持「全部 >3 天」
t_sealed_stale = sd._alert_title([{"signal": _main_sig(),
                                   "resonance": _res(bull=3, stale=3, fresh=(0, 0, 0))}])
check("全部 >3 天" in t_sealed_stale, "密封边界纯陈旧 → 保持「全部 >3 天」", t_sealed_stale)

print("\n【N-2B4D-1】密封侧新文案与图例/非密封侧**逐字**共用同一措辞核（复验第 4 次同型漏）")
# 旧：密封侧写「催化剂仅陈旧条目，…」、图例写「含仅陈旧条目，…」⇒ 只与非密封侧逐字对齐
check("含仅陈旧条目，另有币无催化剂" in t_sealed,
      "密封混合（S3）用「含仅陈旧条目，另有币无催化剂」（与图例逐字一致）", t_sealed)
check("含仅陈旧条目，另有币无催化剂" in t_mix2,
      "非密封混合（N4）沿用同一措辞核（三落点一致）", t_mix2)

print("\n【N-62B-1】资产未关联 ⇒ 催化剂/KOL 无从查询（n/a ≠ 0），标题不得断言「无共振/无催化剂」")
# 弱形式（线上 4/123 批可达）：未关联且无事件/KOL ⇒ n_res==0，旧实现断言「纯盘面信号，无共振」
t_unlinked = sd._alert_title([{"signal": _main_sig(), "resonance": _res(linked=False)}])
check("未关联资产" in t_unlinked and "无共振" not in t_unlinked,
      "密封边界未关联 → 披露「另有币未关联资产」，不再断言「无共振」", t_unlinked)
# 强形式：未关联币的 n_res 由事件段供给 ⇒ 走非密封方向段，须报 n/a 而非「无催化剂条目」
t_unlinked_ev = sd._alert_title([{"signal": _main_sig(),
                                  "resonance": _res(event=1, linked=False)}])
check("催化剂 n/a" in t_unlinked_ev and "无催化剂条目" not in t_unlinked_ev,
      "非密封未关联 → 「催化剂 n/a（未关联资产，非 0）」，不落空档也不误报 0", t_unlinked_ev)
# 对照：已关联且催化剂确为 0 条 → 仍报「无催化剂条目」（0 ≠ n/a 双向成立，勿一并收掉）
check("无催化剂条目" in t_zero, "已关联零催化剂币仍报「无催化剂条目」（0 ≠ n/a 双向成立）")

print("\n【N-2B4D-3 / N-62B-2】图例声明密封侧文案 + 单币批次省略「（k/M 币）」")
_leg_new = sd._render_alert_email([{"signal": _main_sig(), "resonance": _res()}], REGIME)
check("单币批次省略" in _leg_new, "图例声明单币批次省略「（k/M 币）」（实测 61% 邮件为单币）")
check("未关联资产" in _leg_new, "图例声明「未关联资产」= 催化剂/KOL 栏为 n/a（≠ 0）")
check("纯盘面信号" in _leg_new and "无新鲜共振" in _leg_new,
      "图例声明密封侧两种前缀（「纯盘面信号」/「无新鲜共振」）")
check("另有币无催化剂/未关联资产" not in _leg_new,
      "图例不再用「/」描述式句（N-0F7-3：无字面串可逐字比对）")
for _lit in ("「另有币无催化剂」", "「另有币未关联资产」", "「另有币无催化剂、未关联资产」"):
    check(_lit in _leg_new, f"图例声明并列字面串 {_lit}（N-0F7-3 顿号双形态）")
check("催化剂新鲜 X多/…」标口径" in _leg_new and "可并列「另有币…」子句" in _leg_new,
      "图例声明「催化剂新鲜 X多/…」段同样可并列「另有币…」（N-0F7-1 落点）")
check("**" not in _leg_new, "新增图例文案无 markdown 强调符 `**`")

print("\n【N-416-2 / N-90EC-2 / N-2B4D-2】`**` 护栏覆盖标题**全部**返回分支（表驱动夹具）")
_title_cases = {
    "密封-纯无共振": [{"signal": _main_sig(), "resonance": _res()}],
    "密封-纯陈旧": [{"signal": _main_sig(),
                     "resonance": _res(bull=3, stale=3, fresh=(0, 0, 0))}],
    "密封-混合": sealed_mix,
    "密封-未关联": [{"signal": _main_sig(), "resonance": _res(linked=False)}],
    "新鲜方向段": [{"signal": _main_sig(), "resonance": _res(bull=3, fresh=(3, 0, 0))}],
    "仅零催化剂": [{"signal": _main_sig(), "resonance": _res(event=1)}],
    "非密封-全陈旧": [{"signal": _main_sig(),
                       "resonance": _res(event=1, bull=3, stale=3, fresh=(0, 0, 0))}],
    "非密封-未关联": [{"signal": _main_sig(),
                       "resonance": _res(event=1, linked=False)}],
    "混合(非密封)": mixed2,
}

# ═══════════════════════════════════════════════════════════════
#  N-0F7-2：判据由「夹具数 = 文案数」改为「**代码可达路径穷举**」
#  —— 旧判据只对 9 条手工夹具断言 `**`，覆盖 33 条可达文案中的 9 条（27%）；
#     未覆盖的分支其护栏**从未被验证**（复验实测 9 夹具仅覆盖 56% / 归一化口径）。
#     现按「币形态」12 种（A 新鲜方向 4 变体 / B 仅陈旧 / C 已关联真 0 /
#     D 未关联 / E 未关联+陈旧，各含密封与非密封）× 非空子集 = 4095 批穷举，
#     由**全路径**驱动 `**` 护栏 + 文案集合快照 + 分支原子可达性。
#     快照数变化 ⇒ 说明返回文案集合被改动，需复核后同步本数字。
# ═══════════════════════════════════════════════════════════════
import itertools  # noqa: E402
import re as _re  # noqa: E402

_REACH_FORMS = {
    "A_netbull": _res(bull=2, fresh=(2, 0, 0)),
    "A_netbear": _res(bear=2, fresh=(0, 2, 0)),
    "A_tie": _res(bull=1, bear=1, fresh=(1, 1, 0)),
    "A_neutonly": _res(neut=2, fresh=(0, 0, 2)),
    "B_stale_sealed": _res(bull=3, stale=3, fresh=(0, 0, 0)),
    "B_stale_open": _res(event=1, bull=3, stale=3, fresh=(0, 0, 0)),
    "C_zero_sealed": _res(),
    "C_zero_open": _res(event=1),
    "D_unlinked_sealed": _res(linked=False),
    "D_unlinked_open": _res(event=1, linked=False),
    "E_ustale_sealed": _res(bull=3, stale=3, fresh=(0, 0, 0), linked=False),
    "E_ustale_open": _res(event=1, bull=3, stale=3, fresh=(0, 0, 0), linked=False),
}
_RN = list(_REACH_FORMS)


def _reachable_titles():
    out = set()
    for r in range(1, len(_RN) + 1):
        for combo in itertools.combinations(_RN, r):
            items = [{"signal": _main_sig(), "resonance": _REACH_FORMS[nm]}
                     for nm in combo]
            out.add(_re.sub(r"\d+", "#", sd._alert_title(items)))
    return out


_reach = _reachable_titles()
print(f"\n【N-0F7-2】可达路径穷举（{len(_RN)} 形态 → "
      f"{2 ** len(_RN) - 1} 批）→ {len(_reach)} 条唯一文案（归一化数字）")
check(all("**" not in _t for _t in _reach),
      f"全部 {len(_reach)} 条可达文案均无 markdown 强调符 `**`（旧判据仅覆盖 9 夹具）")
check(len(_reach) == 33, "可达文案集合 = 33 条（快照；变化即须复核返回文案空间）",
      f"实测 {len(_reach)} 条：\n" + "\n".join(f"      {_t}" for _t in sorted(_reach)))
_ATOMS = ["催化剂全部 ># 天，不计方向", "（全部 ># 天，不计方向）", "（无催化剂条目）",
          "纯盘面信号，无共振", "纯盘面信号；未关联资产，催化剂 n/a",
          "（未关联资产，非 #）", "含仅陈旧条目", "另有币无催化剂",
          "另有币未关联资产", "另有币无催化剂、未关联资产", "催化剂新鲜 #多/#空/#中"]
for _a in _ATOMS:
    check(any(_a in _t for _t in _reach), f"分支原子可达：{_a}")
# N-0F7-1 回归护栏：「有新鲜方向」支原为**唯一**漏读 kinds 的返回路径
check(any("催化剂新鲜" in _t and _t.endswith("，另有币无催化剂、未关联资产）")
          for _t in _reach),
      "「有新鲜方向」支并列披露零催化剂/未关联币（N-0F7-1：n/a 与真 0 不在标题层混同）")
check(any(_t.endswith("，另有币无催化剂）") and "催化剂新鲜" in _t for _t in _reach),
      "「有新鲜方向」支并列披露零催化剂币")
check(any(_t.endswith("，另有币未关联资产）") and "催化剂新鲜" in _t for _t in _reach),
      "「有新鲜方向」支并列披露未关联币（n/a 披露）")

for _nm, _case in _title_cases.items():
    _t = sd._alert_title(_case)
    check("**" not in _t, f"标题分支「{_nm}」无 `**`")
    check(_re.sub(r"\d+", "#", _t) in _reach,
          f"夹具「{_nm}」文案 ∈ 穷举集合（枚举 ⊇ 手工夹具）", _t)

# ═══════════════════════════════════════════════════════════════
#  共振消息明细块（2026-09-24：卡片内逐条展开事件/催化剂/KOL）
#  此前三段只渲染 len()，消息本体全仓从未被渲染 ⇒ 收件人看不到「是什么」。
# ═══════════════════════════════════════════════════════════════

print("\n【消息明细】三段逐条展开（此前只渲染条数）")


def _msg_body(**kw):
    return _body(sd._render_alert_email([{"signal": _main_sig(), "resonance": _res(**kw)}],
                                        REGIME))


_h3 = _msg_body(event=2, cat=2, kol=1)   # 三段皆非空，且覆盖利多/利空/中性三种徽章
check("📅 事件预置" in _h3, "渲染事件预置段头")
check("📰 催化剂" in _h3, "渲染催化剂段头")
check("🗣 KOL 预测" in _h3, "渲染 KOL 段头")
check("事件摘要0" in _h3, "事件消息本体（detail）出现在邮件里")
check("催化剂摘要0" in _h3 and "催化剂摘要1" in _h3,
      "催化剂消息本体（ai_summary）逐条出现")
check("KOL 看涨（T0USDT）" in _h3, "KOL 消息本体出现")
check("🔄 链上转账" in _h3, "事件类型标签（链上转账）出现")
check("conf 0.80" in _h3, "KOL 置信度渲染为 conf 0.80")
check("2026-09-21" in _h3, "消息日期渲染")

print("\n【消息明细】上限与「共 M 条，仅列最新 N 条」披露")
_h6 = _msg_body(cat=6, bull=4, bear=2)
check("催化剂摘要3" in _h6 and "催化剂摘要4" not in _h6,
      f"催化剂明细截断到 {sd.RESONANCE_MSG_MAX} 条（第 {sd.RESONANCE_MSG_MAX + 1} 条不渲染）")
check(f"共 6 条，仅列最新 {sd.RESONANCE_MSG_MAX} 条" in _h6,
      "超出上限时披露「共 6 条，仅列最新 4 条」（M 取截断前全量，与卡片主数字同源）")
_h2 = _msg_body(cat=2, bull=2)
check("仅列最新" not in _h2, "未超上限时不出现「仅列最新」（无虚假截断暗示）")
_hk = _msg_body(kol=4, kol_total=9)
check(f"共 9 条，仅列最新 {sd.RESONANCE_MSG_MAX} 条" in _hk,
      "KOL 段按截断前总数（kol_total=9）披露，而非明细条数")
_hev = _msg_body(event=6)
check(f"共 6 条，仅列最新 {sd.RESONANCE_MSG_MAX} 条" in _hev, "事件段同样按全量披露")

print("\n【消息明细】方向徽章配色（中文惯例：利多=红 / 利空=绿 / 中性=灰）")
check("background:#fee2e2;color:#ef4444" in _h3 and ">利多<" in _h3,
      "利多徽章红底红字（与卡片做多同色）")
check("background:#dcfce7;color:#22c55e" in _h3 and ">利空<" in _h3,
      "利空徽章绿底绿字（与卡片做空同色）")
check("background:#f1f5f9;color:#6b7280" in _h3 and ">中性<" in _h3,
      "中性徽章灰（链上转账方向不明，不臆断）")
check("KOL 预测" not in _h3.replace("🗣 KOL 预测", ""),
      "KOL 明细行不再重复段头文字（避免「KOL 预测 KOL 预测」）")

print("\n【消息明细】未关联资产不渲染催化剂/KOL 明细（无从查询 ≠ 0）")
_hu = _msg_body(linked=False, event=1)
check("📅 事件预置" in _hu, "未关联币仍渲染事件段（事件不依赖 asset_id）")
check("📰 催化剂" not in _hu and "🗣 KOL 预测" not in _hu,
      "未关联币不渲染催化剂/KOL 明细段（与「共振」行的 n/a 口径一致）")
check("📅 事件预置" not in _msg_body(), "三段皆空时不渲染空明细块（无空壳）")

print("\n【消息明细】转义 / 截断 / 旧快照兼容")
_x = _res(cat=1)
_x["catalyst"][0]["text"] = "<script>alert(1)</script> & <b>x</b>"
_hx = _body(sd._render_alert_email([{"signal": _main_sig(), "resonance": _x}], REGIME))
check("<script>" not in _hx and "&lt;script&gt;" in _hx,
      "消息文本经 html.escape（含 & 转义），不注入 HTML")
_long = _res(cat=1)
_long["catalyst"][0]["text"] = "长" * 200
_hl = _body(sd._render_alert_email([{"signal": _main_sig(), "resonance": _long}], REGIME))
check("长" * sd.RESONANCE_MSG_CHARS not in _hl
      and "长" * (sd.RESONANCE_MSG_CHARS - 1) + "…" in _hl,
      f"超长摘要截断到 {sd.RESONANCE_MSG_CHARS} 字并以 … 结尾")
_old = _res()
_old["event"] = ["🔓解锁: 旧快照字符串形态"]
_ho = _body(sd._render_alert_email([{"signal": _main_sig(), "resonance": _old}], REGIME))
check("旧快照字符串形态" in _ho, "兼容旧 resonance_snapshot 的纯字符串元素（不抛异常）")

print("\n【消息明细】图例声明（三落点逐字对齐）")
_leg_msg = sd._render_alert_email([{"signal": _main_sig(), "resonance": _res()}], REGIME)
_leg_msg = _leg_msg.split("图例：")[1]
for _lit in ("「共振」行下方的消息明细块", "📅 事件预置", "📰 催化剂", "🗣 KOL 预测",
             f"每段最多 {sd.RESONANCE_MSG_MAX} 条",
             f"单条摘要截断至 {sd.RESONANCE_MSG_CHARS} 字",
             f"「共 M 条，仅列最新 {sd.RESONANCE_MSG_MAX} 条」",
             "利多=红 / 利空=绿 / 中性=灰", "解锁事件记利空"):
    check(_lit in _leg_msg, f"图例声明 {_lit}")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
