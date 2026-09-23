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
         event=0, kol=0, fresh=None):
    # 复验 N-786-1/2（2026-09-23）：标题方向段与覆盖币数改用**新鲜**口径 ⇒ 默认
    # `catalyst_dir_fresh` 与全量同值（stale=0 的常规 fixture 语义不变）；显式传
    # `fresh=(b,be,n)` 可构造「全量含陈旧」用例。
    return {
        "event": [f"e{i}" for i in range(event)],
        "catalyst": [],
        "catalyst_dir": {"bullish": bull, "bearish": bear, "neutral": neut},
        "catalyst_dir_fresh": ({"bullish": fresh[0], "bearish": fresh[1],
                                "neutral": fresh[2]} if fresh else
                               {"bullish": bull, "bearish": bear, "neutral": neut}),
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
#  P2-N8 —— 占位符标题（title='null'）不得计为催化剂
# ═══════════════════════════════════════════════════════════════

print("\n【P2-N8】催化剂占位符标题（prod 实测 177 条 title='null'）被丢弃")
for t, exp in [("null", True), ("NULL", True), (" none ", True), ("n/a", True),
               ("undefined", True), ("-", True), ("--", True), ("", True),
               (None, True), ("Arthur Hayes 看涨 ENA 至 0.5 美元", False),
               ("null 值不是标题的一部分", False)]:
    check(sd._is_placeholder_title(t) is exp,
          f"_is_placeholder_title({t!r}) == {exp}", f"got={sd._is_placeholder_title(t)}")
_res_fn = next((n for n in ast.parse(open(sd.__file__, encoding="utf-8").read()).body
                if isinstance(n, ast.FunctionDef) and n.name == "_get_resonance"), None)
check(_res_fn is not None and "_is_placeholder_title(" in ast.unparse(_res_fn),
      "_get_resonance 去重循环内调用该判据（源码 AST）",
      "未调用 ⇒ title='null' 仍会占一个方向名额")

# ═══════════════════════════════════════════════════════════════
#  P2-N9 —— CVD 机制断言需 OI 物性支撑（oi_dir 由 oi_chg>0 二值化）
# ═══════════════════════════════════════════════════════════════

print(f"\n【P2-N9】CVD 机制断言需 |OI 增速| ≥ {sd.OI_FLAT_PCT:g}%")
h_flat = _body(sd._render_alert_email([_item(sig=_sig(p_dir="up", cvd_dir="down",
                                                     oi_dir="up", oi_chg_pct=0.04))]))
check("杠杆驱动" not in h_flat, "OI +0.04%（噪声级微增）不得断言「杠杆驱动」",
      "实物 id=1286 ENAUSDT 即此情形")
check("未见同步扩张" in h_flat, "改述「OI 未见同步扩张」，只描述现货侧")
h_expand = _body(sd._render_alert_email([_item(sig=_sig(p_dir="up", cvd_dir="down",
                                                       oi_dir="up", oi_chg_pct=5.0))]))
check("杠杆驱动" in h_expand, "OI +5.0% → 仍判「杠杆驱动」（不误伤真实扩张）")
h_cover = _body(sd._render_alert_email([_item(sig=_sig(p_dir="up", cvd_dir="down",
                                                      oi_dir="down", oi_chg_pct=-3.0))]))
check("空头回补" in h_cover, "OI −3.0% → 「空头回补/多头离场推涨」（原分支不回归）")
h_unk = _body(sd._render_alert_email([_item(sig=_sig(p_dir="up", cvd_dir="down",
                                                     oi_dir=None, oi_chg_pct=None))]))
check("未见同步扩张" in h_unk, "OI 方向未知 → 同样不下机制结论")

# ═══════════════════════════════════════════════════════════════
#  P2-N10 —— OI 两列皆空渲染 `OI n/a`（原为 `OI - -`）
# ═══════════════════════════════════════════════════════════════

print("\n【P2-N10】OI 无从查询时渲染 `OI n/a`（与图例口径一致）")
h_brk = _body(sd._render_alert_email([_item(sig=_sig(pool="accumulation", scenario="BRK",
                                                     oi_dir=None, oi_chg_pct=None))]))
check("OI n/a" in h_brk, "BRK（oi_dir/oi_chg_pct 皆 NULL）→ `OI n/a`")
check("OI - -" not in h_brk, "不再渲染占位符残留 `OI - -`（实物 id=1292 龙虾USDT）")
h_oi = _body(sd._render_alert_email([_item(sig=_sig(oi_dir="up", oi_chg_pct=5.38))]))
check("OI up +5.4%" in h_oi, "有 OI 时仍渲染 `OI up +5.4%`（不误伤）")

# ═══════════════════════════════════════════════════════════════
#  P2-N11 —— 转载去重漏合并（正文内嵌源名 + 源库截断版）
# ═══════════════════════════════════════════════════════════════

print("\n【P2-N11a】正文内嵌源名（「据 HTX 行情数据」）应归一为同一条")
_T_PLAIN = ("火星财经消息，9 月 18 日，据 行情数据，随着美联储加息预期落地，加密市场"
            "集体回暖，比特币回升至 77,000 美元上方，山寨币出现普涨行情")
_T_HTX = ("BlockBeats 消息，9 月 18 日，据 HTX 行情数据，随着美联储加息预期落地，"
          "加密市场集体回暖，比特币回升至 77,000 美元上方，山寨币出现普涨行情")
check(sd._norm_title(_T_PLAIN) == sd._norm_title(_T_HTX),
      "「据 HTX 行情数据」与「据 行情数据」归一后相同（实物 AVAUSDT 利多那对）",
      f"{sd._norm_title(_T_PLAIN)!r}\n    vs {sd._norm_title(_T_HTX)!r}")
check("据现货数据" in sd._norm_title("PANews 消息，据币安现货数据显示，市场出现大幅波动"),
      "「据币安现货数据」→「据现货数据」")
check("币安" in sd._norm_title("Odaily 消息，币安将上线 NEWTOKEN 合约"),
      "正文（非「据…」句式）里的源名不得被删（防无关新闻误合并）")

print("\n【P2-N11b】源库截断版转载（短键是长键前缀 + 带 ... 标记）应判同一条")
_KEY_SHORT = sd._norm_title(
    "ChainCatcher 消息，据币安现货数据显示，市场出现大幅波动。"
    "LUNA 24 小时跌幅达 16.44%，AVA 跌幅 7.2%，LSK 跌幅 12.21...")
_KEY_LONG = sd._norm_title(
    "火星财经消息，据币安现货数据显示，市场出现大幅波动。"
    "LUNA 24 小时跌幅达 16.44%，AVA 跌幅 7.2%，LSK 跌幅 12.21%。同时，MASK...")
check(_KEY_LONG.startswith(_KEY_SHORT) and len(_KEY_SHORT) < len(_KEY_LONG),
      f"前置：短键（{len(_KEY_SHORT)}）是长键（{len(_KEY_LONG)}）前缀",
      f"short={_KEY_SHORT!r}\n    long ={_KEY_LONG!r}")
check(sd._is_repost_of_truncated(_KEY_LONG, "火星财经…完整原文",
                                 [(_KEY_SHORT, "…LSK 跌幅 12.21...")]),
      "长键遇到「以 ... 收尾的短键」→ 判为截断转载（实物 AVAUSDT 利空那对）")
check(sd._is_repost_of_truncated(_KEY_SHORT, "…LSK 跌幅 12.21...",
                                 [(_KEY_LONG, "火星财经…完整原文")]),
      "处理顺序反过来同样判为转载（不依赖入库次序）")
_TMPL_A = "accordingtotheannouncementfrombinancethefollowingtokenswillbelistedon"
_TMPL_B = _TMPL_A + "thenewspotmarketnextweek"
check(sd._is_repost_of_truncated(
        _TMPL_B,
        "According to the announcement from Binance, the following tokens will be "
        "listed on the new spot market next week",
        [(_TMPL_A, "According to the announcement from Binance, the following "
                   "tokens will be listed on")]) is False,
      "英文模板共享 69 字前缀但无 ... 标记 ⇒ 不合并（P1-N1 回归护栏）")
check(sd._is_repost_of_truncated("x" * 40, "x" * 40, [("y" * 40, "y" * 40)]) is False,
      "非前缀关系不合并")
check(sd._is_repost_of_truncated("ab" * 20, "ab" * 20 + "...",
                                 [("ab" * 3, "ab" * 3 + "...")]) is False,
      f"短键不足 MIN_TRUNC_DEDUP_LEN={sd.MIN_TRUNC_DEDUP_LEN} ⇒ 不合并（防短标题互并）")

print("\n【P2-N11c】OI 近乎持平时渲染「OI 持平 ±X%」（原直译 `OI up +0.2%`）")
h_flat_oi = _body(sd._render_alert_email([_item(sig=_sig(oi_dir="up", oi_chg_pct=0.19))]))
check("OI 持平 +0.2%" in h_flat_oi,
      "OI +0.19% → 「OI 持平 +0.2%」（实物 id=1303 AVAUSDT）", h_flat_oi)
check("OI up +0.2%" not in h_flat_oi,
      "不得再渲染 `OI up +0.2%`（读者会以为 OI 明显扩张、无法解释强度条 0.5 分）")
h_neg_oi = _body(sd._render_alert_email([_item(sig=_sig(oi_dir="down", oi_chg_pct=-0.3))]))
check("OI 持平 -0.3%" in h_neg_oi, "负向同样按持平渲染", h_neg_oi)
h_big_oi = _body(sd._render_alert_email([_item(sig=_sig(oi_dir="up", oi_chg_pct=3.9))]))
check("OI up +3.9%" in h_big_oi, "OI +3.9% 仍渲染方向（不误伤）")

print("\n【P2-N11d】图例补齐四处「看不懂」的口径")
_leg = sd._render_alert_email([_item()])
for frag in ("「市场环境」= 全局 regime",
             "「多头/空头环境受限」= 该方向信号在当轮被降级",
             "共振方向与结论一致 ×1.15",
             "CVD 同向 ×1.05",
             "「不含涨幅」",
             "「按场景跨币种聚合」",
             "「OI 持平 ±X%」",
             "负 = 空头付多头（对做多顺风）"):
    check(frag in _leg, f"图例含「{frag}」")

print("\n【P2-N12】confidence 徽章：查询须选该列，且缺失时不渲染空药丸")
_cand_fn = next((n for n in ast.parse(open(sd.__file__, encoding="utf-8").read()).body
                 if isinstance(n, ast.FunctionDef) and n.name == "_load_alert_candidates"), None)
check(_cand_fn is not None, "找到 scan_daemon._load_alert_candidates()")
_cand_sql = " ".join(ast.unparse(_cand_fn).split())
check("confidence" in _cand_sql.split("FROM biz.scan_signal")[0],
      "_load_alert_candidates 的 SELECT 含 confidence 列（原漏选 ⇒ 空药丸）")
h_no_conf = _body(sd._render_alert_email([_item(sig=_sig(confidence=None))]))
check("font-size:11px'></span>" not in h_no_conf,
      "confidence 为 None 时不渲染空药丸（原 `…font-size:11px'></span>`）", h_no_conf)
h_conf = _body(sd._render_alert_email([_item(sig=_sig(confidence="high"))]))
check(">HIGH</span>" in h_conf, "confidence='high' → 渲染 `HIGH` 药丸（实物 id=1303/1304）")
check(">HIGH</span>" in h_conf and h_conf.index(">HIGH</span>") < h_conf.index("量比"),
      "药丸位置在卡片头（量比行之前）")

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
