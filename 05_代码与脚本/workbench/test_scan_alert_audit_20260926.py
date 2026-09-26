#!/usr/bin/env python3
"""告警邮件「3 封」审计处置探针（audit_盘面异动告警邮件_3封_2026-09-26.md §七）。

运行：python workbench/test_scan_alert_audit_20260926.py

覆盖面（全部离线，不连库）：
  P0-1 卡片「⚠️ 共振相悖」改读**新鲜**口径；全陈旧时改印「未参与结论」（自相矛盾收口）；
  P0-2 `_alert_strength` 的 ×1.15 / ×0.75 改读**新鲜**口径（陈旧旧闻不加不扣）；
  P0-3 催化剂新鲜度对「预定动作」语义（下架/移除/上线/解锁/升级…）不判陈旧
       （ENJ 实测：09-25 已生效的币安下架被 published_at=09-22 误杀）；
  P0-4 `linker.py` 新增 `_EQUITY_TICKER_COLLISIONS` + `has_crypto_context()` 门禁
       （DoorDash（NASDAQ: DASH）新闻不得再连到加密 Dash）；
  P1-1 催化剂去重增加「交易所+动作+有序币种清单」实体（跨语种/跨截断长度转载合并为 1 条；
       清单取未截断的 `body_text`，判等用「同源截断 ⇒ 短者是长者的前缀」）。
复验（核验_盘面告警邮件修复_d442bd6_2026-09-26）：
  NEW-1 预定动作豁免须「动作词 ∧（实体 ∨ 将来语义）」，单靠 upgrade/list/上线 等词形不算
       （否则陈旧评论恒新鲜，OPT-2 被成规模架空）；
  NEW-2 新鲜条目只有中性时不得印「剔除陈旧后多空持平」（方向不存在），改印「无新鲜方向」。
另附「HTML 邮件不得含 markdown 强调符 `**`」护栏（重踩过的坑）。
"""
import ast
import datetime as _dt
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_daemon as sd  # noqa: E402


def _load_linker():
    """独立加载 `catalyst/linker.py`。

    它**无包内相对导入**（只 import re/logging），故可绕开 `catalyst/__init__` 的
    重依赖（pipeline/runner/sources），保持本探针「离线、不连库」的性质。
    """
    path = os.path.join(_HERE, "catalyst", "linker.py")
    spec = importlib.util.spec_from_file_location("catalyst_linker_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lk = _load_linker()

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
         event=0, kol=0, fresh=None, cat=0, cat_date="2026-09-21", kol_date="2026-09-21"):
    # `catalyst_dir_fresh` 默认与全量同值（stale=0 的常规 fixture 语义不变）；
    # 显式传 `fresh=(b,be,n)` 构造「全量含陈旧」用例。
    return {
        "event": [{"dir": "neutral", "kind": "🔄 链上转账", "text": f"事件摘要{i}",
                   "date": None} for i in range(event)],
        "catalyst": [{"dir": "bearish", "strength": "strong", "text": f"催化剂摘要{i}",
                      "date": cat_date} for i in range(cat)],
        "catalyst_dir": {"bullish": bull, "bearish": bear, "neutral": neut},
        "catalyst_dir_fresh": ({"bullish": fresh[0], "bearish": fresh[1],
                                "neutral": fresh[2]} if fresh else
                               {"bullish": bull, "bearish": bear, "neutral": neut}),
        "catalyst_raw": bull + bear + neut,
        "catalyst_latest": latest,
        "catalyst_stale": stale,
        "catalyst_all": [],
        "kol": [{"dir": "bullish", "text": f"KOL 看涨（T{i}USDT）", "conf": 0.8,
                 "date": kol_date} for i in range(kol)],
        "kol_total": kol,
        "asset_linked": linked,
    }


def _sig(**kw):
    base = {
        "id": 1, "symbol": "TESTUSDT", "pool": "main", "scenario": "S1",
        "timeframe": "15m", "p_dir": "up", "price_chg_pct": 2.03,
        "vol_ratio": 3.0, "oi_dir": "up", "oi_chg_pct": 2.0,
        "cvd_dir": "down", "cvd_usd": 100.0, "cvd_ratio": 0.05,
        "funding_rate": 0.0001, "confidence": "high", "status": "active",
        "context_tags": ["lv2_15m"], "trigger_price": 100.0,
        "stop_loss_pct": 8.0, "breakout_px": 101.0,
    }
    base.update(kw)
    return base


def _item(sig=None, res=None):
    return {"signal": sig or _sig(), "resonance": res or _res(), "prior": None}


def _body(html: str) -> str:
    """卡片/正文部分（去掉图例与页脚）——图例文本会干扰卡片级断言。"""
    return html.split("图例：")[0]


_SRC = open(sd.__file__, encoding="utf-8").read()
_TREE = ast.parse(_SRC)
_res_fn = next((n for n in _TREE.body
                if isinstance(n, ast.FunctionDef) and n.name == "_get_resonance"), None)
_res_src = ast.unparse(_res_fn) if _res_fn else ""

# ═══════════════════════════════════════════════════════════════
#  P0-1 —— 「不计方向」与「⚠️ 共振相悖」不得同框
# ═══════════════════════════════════════════════════════════════

print("\n【P0-1】相悖判定改读新鲜口径（全陈旧 ⇒ 不再判相悖）")
h_pure = _body(sd._render_alert_email([_item(
    sig=_sig(p_dir="up"),
    res=_res(bull=0, bear=1, neut=0, stale=1, latest="2026-09-22", fresh=(0, 0, 0)))]))
check("与做多结论相悖" not in h_pure,
      "全陈旧利空 + 做多 → 不再渲「⚠️ 与做多结论相悖」（B/DASH、C/ENJ 两封实况）")
check("共振方向无新鲜条目，未参与结论" in h_pure,
      "改印中性说明「共振方向无新鲜条目，未参与结论」", h_pure)
check("剔除陈旧后无新鲜条目" in h_pure, "卡片方向段仍披露「剔除陈旧后无新鲜条目」")
h_fresh = _body(sd._render_alert_email([_item(
    sig=_sig(p_dir="up"),
    res=_res(bull=0, bear=1, neut=0, stale=0, fresh=(0, 1, 0)))]))
check("与做多结论相悖" in h_fresh, "新鲜利空 + 做多 → 仍渲相悖警告（不误伤）")
check("未参与结论" not in h_fresh, "有新鲜条目时不渲「未参与结论」")
h_short = _body(sd._render_alert_email([_item(
    sig=_sig(p_dir="down"),
    res=_res(bull=1, bear=0, neut=0, stale=0, fresh=(1, 0, 0)))]))
check("与做空结论相悖" in h_short, "新鲜利多 + 做空 → 仍渲相悖警告（反向分支不回归）")

# ═══════════════════════════════════════════════════════════════
#  复验 NEW-2 —— 新鲜条目只有中性时不得印「多空持平」
# ═══════════════════════════════════════════════════════════════

print("\n【复验 NEW-2】新鲜 {0多,0空,2中} → 不得印「多空持平」")
h_neut = _body(sd._render_alert_email([_item(
    sig=_sig(p_dir="up"),
    res=_res(bull=0, bear=3, neut=0, stale=1, latest="2026-09-22",
             fresh=(0, 0, 2)))]))
check("多空持平" not in h_neut,
      "新鲜只有中性 → 不再印「（剔除陈旧后多空持平）」（N-786-3：方向不存在）", h_neut)
check("剔除陈旧后无新鲜方向" in h_neut,
      "改印「剔除陈旧后无新鲜方向」", h_neut)
check("仅 2 条中性" in h_neut,
      "中性条数如实披露（而非失真地说「无新鲜条目」）", h_neut)
h_mix = _body(sd._render_alert_email([_item(
    sig=_sig(p_dir="down"),
    res=_res(bull=0, bear=2, neut=0, stale=1, latest="2026-09-22",
             fresh=(0, 1, 1)))]))
check("剔除陈旧后净空1" in h_mix,
      "新鲜 0多/1空/1中 → 仍印「净空1」（有新鲜方向时不回归）", h_mix)

# ═══════════════════════════════════════════════════════════════
#  P0-2 —— 强度分的 ×1.15 / ×0.75 同样吃新鲜口径
# ═══════════════════════════════════════════════════════════════

print("\n【P0-2】_alert_strength 的共振加成/惩罚改读新鲜口径")
_base = abs(11.28 * 5.42)                       # ≈61.14（DASH 实况反算）
s_pure = sd._alert_strength({
    "signal": _sig(p_dir="up", vol_ratio=11.28, oi_chg_pct=5.42, cvd_dir="down"),
    "resonance": _res(bull=0, bear=1, stale=1, fresh=(0, 0, 0))})
check(abs(s_pure - _base) < 0.05,
      "全陈旧利空：强度不打折（11.28×5.42≈61.1，原为 ×0.75=45.9）", f"got={s_pure}")
s_pen = sd._alert_strength({
    "signal": _sig(p_dir="up", vol_ratio=11.28, oi_chg_pct=5.42, cvd_dir="down"),
    "resonance": _res(bull=0, bear=1, fresh=(0, 1, 0))})
check(abs(s_pen - _base * sd.STRENGTH_PENALTY_CONFLICT) < 0.05,
      "新鲜利空与做多相悖：仍 ×0.75（保留原惩罚，不误伤）", f"got={s_pen}")
s_ali = sd._alert_strength({
    "signal": _sig(p_dir="up", vol_ratio=11.28, oi_chg_pct=5.42, cvd_dir="down"),
    "resonance": _res(bull=1, bear=0, fresh=(1, 0, 0))})
check(abs(s_ali - _base * sd.STRENGTH_BONUS_ALIGNED) < 0.05,
      "新鲜利多与做多一致：仍 ×1.15（保留原加成）", f"got={s_ali}")

# ═══════════════════════════════════════════════════════════════
#  P0-3 —— 「预定动作」类催化剂不按发布日判陈旧
# ═══════════════════════════════════════════════════════════════

print("\n【P0-3】预定动作语义（下架/移除/上线/解锁/升级…）不参与陈旧剔除")
for txt, exp in [
        ("币安将于 9 月 25 日移除 ENJ/USDC 等现货交易对并停止交易", True),
        ("Binance Will Delist XYZUSDT Spot Trading Pair", True),
        ("Binance will list NEWUSDT on the spot market", True),
        ("某代币将于下周解锁 1200 万枚（Cliff Unlock）", True),
        ("ETH 网络升级（Hard Fork）时间确定", True),
        ("BTC 第四次减半倒计时", True),
        ("ENJ 价格突破关键阻力位，量比放大 7 倍", False),
        ("分析师看空 ENJ，目标价下调 20%", False)]:
    check(sd._is_scheduled_action(txt) is exp,
          f"_is_scheduled_action({txt[:24]!r}…) == {exp}",
          f"got={sd._is_scheduled_action(txt)}")
check("_is_scheduled_action(" in _res_src,
      "_get_resonance 新鲜度判定调用该判据（源码 AST）")

# ═══════════════════════════════════════════════════════════════
#  复验 NEW-1 —— 「动作词」不足以判豁免，须再有具体性证据
# ═══════════════════════════════════════════════════════════════

print("\n【复验 NEW-1】非预定动作的旧闻不得因含 upgrade/list/上线 字样而豁免")
for txt in [
        "The network upgrade improves throughput",
        "Protocol upgrade completed",
        "Top 100 holders list published",
        "A listing of new tokens this week",
        "链上活跃度升级",
        "该协议上线三个月",
        "Reviewing the impact of the halving"]:
    check(sd._is_scheduled_action(txt) is False,
          f"非预定动作 {txt[:30]!r}… → 不豁免（复验报告 7/7 误报）",
          f"got={sd._is_scheduled_action(txt)}")

print("\n【复验 NEW-1】真·预定动作仍豁免（实体 / 将来语义 两条通道）")
for txt, why in [
        ("币安将于 9 月 25 日 11:00 移除 ENJ/USDC 等现货交易对并停止交易", "实体+将于"),
        ("According to the announcement from Binance, the exchange will remove and "
         "cease trading on seven spot trading pairs", "英文截断版：will remove"),
        ("币安现货和闪兑平台将上线 bStocks 代币化证券 Axe Compute (AGPUB)", "将+动作"),
        ("Walrus (WAL) is scheduled to unlock about 38.33 million tokens at 10:00 AM",
         "scheduled to"),
        ("ChainCatcher 消息，RootData 数据显示，Walrus（WAL）将于北京时间 09 月 27 日 "
         "10 时解锁约 1.2 亿枚", "将于…解锁"),
        ("XRP Ledger 的 PermissionDelegationV1_1 升级已于 9 月 21 日进入 14 天激活倒计时",
         "倒计时")]:
    check(sd._is_scheduled_action(txt) is True,
          f"预定动作（{why}）{txt[:28]!r}… → 仍豁免",
          f"got={sd._is_scheduled_action(txt)}")
check(sd._is_scheduled_action(
    "Binance Will Delist XYZUSDT Spot Trading Pair") is True,
    "交易所公告（实体通道）：Binance 下架 XYZUSDT → 仍豁免")


def _now(days_ago: float) -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)


class _FakeCursor:
    """按 SQL 内容分派的最小游标：催化剂段给注入行，其余段给空集。"""

    def __init__(self, cat_rows):
        self._cat = list(cat_rows)
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql, _params=None):
        self._rows = self._cat if "catalyst_impact" in _sql else []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, cat_rows=()):
        self._cat = list(cat_rows)

    def cursor(self, **_kw):
        return _FakeCursor(self._cat)


row_stale_bear = {
    "title": "分析师看空 ENJ，目标价下调 20%", "ai_summary": "看空 ENJ",
    "published_at": _now(4), "impact_direction": "bearish", "impact_strength": "weak",
}
res_stale = sd._get_resonance(_FakeConn([row_stale_bear]), "ENJUSDT", 12345)
check(res_stale["catalyst_stale"] == 1
      and res_stale["catalyst_dir_fresh"]["bearish"] == 0,
      "非预定动作的 4 天前利空 → 仍判陈旧（豁免不外溢）",
      f"stale={res_stale['catalyst_stale']} fresh={res_stale['catalyst_dir_fresh']}")
row_delist = {
    "title": "币安将于 9 月 25 日 11:00 移除 ENJ/USDC 等现货交易对并停止交易",
    "ai_summary": "币安移除 ENJ/USDC 现货交易对",
    "published_at": _now(4), "impact_direction": "bearish", "impact_strength": "strong",
}
res_delist = sd._get_resonance(_FakeConn([row_delist]), "ENJUSDT", 12345)
check(res_delist["catalyst_stale"] == 0
      and res_delist["catalyst_dir_fresh"]["bearish"] == 1,
      "4 天前发布、09-25 已生效的币安下架 → 按「预定动作」计入新鲜方向（ENJ 实况）",
      f"stale={res_delist['catalyst_stale']} fresh={res_delist['catalyst_dir_fresh']}")

# ═══════════════════════════════════════════════════════════════
#  P1-1 —— 跨语种转载实体指纹去重
# ═══════════════════════════════════════════════════════════════

print("\n【P1-1】「交易所+动作+有序币种清单」实体：跨语种/跨截断长度合并为 1 条")
_CN = "火星财经消息，币安将移除 ENJ/USDC 等现货交易对并停止交易"
_EN = ("According to the announcement from Binance, the ENJ/USDC spot trading "
       "pair will be removed")
ent_cn, ent_en = sd._catalyst_entity(_CN), sd._catalyst_entity(_EN)
check(ent_cn is not None and ent_cn == ent_en,
      "中/英两版同一公告 → 同一实体", f"{ent_cn!r} vs {ent_en!r}")
check(sd._catalyst_entity("币安上线 ENJ/TRY") != ent_cn,
      "同交易所同币种但动作不同（上线 vs 移除）→ 实体不同（防误并）")
check(sd._catalyst_entity("ENJ 价格突破关键阻力位") is None,
      "无交易所 ⇒ 不生成实体（回退精确标题键，不做二次合并）")
check(sd._catalyst_entity("币安将移除若干现货交易对") is None,
      "有交易所+动作但无币种/交易对 ⇒ 不生成实体（防只凭动作误并）")
check("_catalyst_entity(" in _res_src and "seen_fp" in _res_src,
      "_get_resonance 去重循环内调用实体判据并维护 seen_fp（源码 AST）")
check("body_text" in _res_src,
      "实体取**正文** `body_text`（非 title/ai_summary），源码 AST",
      "用被源站截到 83 字的标题算实体 ⇒ 四版转载一条也合并不了")
check("_same_catalyst_batch(" in _res_src,
      "清单判等用 `_same_catalyst_batch`（同源截断的前缀式比较），源码 AST")

# 实测（只读 prod，asset 1396/ENJ 四条催化剂）：标题被源站截到 83 字，币种清单在
# 「…AIXBT/USDC、DOLO/US…」处断掉（英文版整段清单都没进标题）；`body_text` 是较完整体
# —— 但**也是截断的**：四版正文分别含 7/6/7/7 个交易对，PAnews 那版正好是长版的前缀。
_TITLE_TRUNC_A = ("火星财经消息，币安将于 9 月 25 日 11:00 下架以下现货交易对："
                  "AIXBT/USDC、DOLO/USDC、ENJ/USDC、HUMA/USDC、SXT...")
_TITLE_TRUNC_B = ("PANews 9月22日消息，据官方公告，币安将于 9 月 25 日 11:00（东八区时间）"
                  "移除以下现货交易对并停止交易：AIXBT/USDC、DOLO/US...")
_BODY_A = ("火星财经消息，币安将于 9 月 25 日 11:00 下架以下现货交易对：AIXBT/USDC、"
           "DOLO/USDC、ENJ/USDC、HUMA/USDC、SXT/USDC、TNSR/USDC 和 TURTLE/USDC。"
           "用户需在此之前关闭相关仓位。")
_BODY_B = ("PANews 9月22日消息，据官方公告，币安将于 9 月 25 日 11:00（东八区时间）移除"
           "以下现货交易对并停止交易：AIXBT/USDC、DOLO/USDC、ENJ/USDC、HUMA/USDC、"
           "SXT/USDC、TNSR/USDC")
_BODY_EN = ("According to the announcement from Binance, the exchange will remove and cease "
            "trading on seven spot trading pairs: AIXBT/USDC, DOLO/USDC, ENJ/USDC, "
            "HUMA/USDC, SXT/USDC, TNSR/USDC and TURTLE/USDC.")
check(sd._catalyst_entity(_TITLE_TRUNC_A) != sd._catalyst_entity(_TITLE_TRUNC_B),
      "⚠️ 用**截断标题**算实体两版不相等（首轮实现踩此坑，故改用 body_text）",
      f"{sd._catalyst_entity(_TITLE_TRUNC_A)!r} vs "
      f"{sd._catalyst_entity(_TITLE_TRUNC_B)!r}")
_ea, _eb = sd._catalyst_entity(_BODY_A), sd._catalyst_entity(_BODY_B)
check(_ea is not None and _eb is not None and _ea[0] == _eb[0]
      and sd._same_catalyst_batch(_ea[1], _eb[1]),
      "改用**正文**后：两版清单互为前缀（7 个 vs 6 个）⇒ 判为同一条转载",
      f"{_ea!r} vs {_eb!r}")
check(sd._same_catalyst_batch(_ea[1], sd._catalyst_entity(_BODY_EN)[1]),
      "中↔英两版正文清单一致 → 判为同一条（跨语种合并成立）")
# prod 实测 PANews 版正文：`…TNSR/USDC及 TURTLE/USDC`（中文紧贴，无空格）
_BODY_CJK_ADJ = ("PANews 9月22日消息，据官方公告，币安将于 9 月 25 日 11:00（东八区时间）"
                 "移除以下现货交易对并停止交易：AIXBT/USDC、DOLO/USDC、ENJ/USDC、"
                 "HUMA/USDC、SXT/USDC、TNSR/USDC及 TURTLE/USDC，并同步移除相关交易机器人服务。")
check(sd._catalyst_entity(_BODY_CJK_ADJ) == _ea,
      "`…USDC及` 中文紧贴不得截断清单（`\\b` 在中文处判不出边界 ⇒ 漏 TNSR、错位一位）",
      f"{sd._catalyst_entity(_BODY_CJK_ADJ)!r} vs {_ea!r}")
_swapped = ("币安将于 9 月 25 日 11:00 下架以下现货交易对：DOLO/USDC、SXT/USDC、BMT/USDC。")
check(not sd._same_catalyst_batch(_ea[1], sd._catalyst_entity(_swapped)[1]),
      "首项不同（两条不同下架批次）→ **不**判为转载（防误并）",
      f"{_ea!r} vs {sd._catalyst_entity(_swapped)!r}")
check(not sd._same_catalyst_batch(("AIXBT",), ("AIXBT", "DOLO")),
      "单元素清单无判别力 → 长度不同即不合并（防「同首项不同批次」误并）")

rows_three = [
    {"title": _TITLE_TRUNC_A, "ai_summary": _TITLE_TRUNC_A, "body_text": _BODY_A,
     "published_at": _now(4), "impact_direction": "bearish", "impact_strength": "strong"},
    {"title": _TITLE_TRUNC_B, "ai_summary": _TITLE_TRUNC_B, "body_text": _BODY_CJK_ADJ,
     "published_at": _now(4), "impact_direction": "bearish", "impact_strength": "strong"},
    {"title": "According to the announcement from Binance, the exchange will remove and "
              "cease trading on seven spot trading pairs as part of its routine review",
     "ai_summary": "Binance will remove seven spot trading pairs",
     "body_text": _BODY_EN,
     "published_at": _now(4), "impact_direction": "bearish", "impact_strength": "strong"},
]
res_three = sd._get_resonance(_FakeConn(rows_three), "ENJUSDT", 12345)
check(res_three["catalyst_dir"]["bearish"] == 1,
      "同一条币安公告的中/中/英三版（截断长度各异）→ 合并为 1 条"
      "（原「净空 3~4」虚高）",
      f"dir={res_three['catalyst_dir']} raw={res_three['catalyst_raw']}")
check(len(res_three["catalyst_all"]) == 1, "回放快照 catalyst_all 同为 1 条（口径同源）")

rows_two = [
    rows_three[0],
    {"title": "币安将上线 ENJ/TRY 现货交易对", "ai_summary": "币安上线 ENJ/TRY",
     "body_text": "币安将于 9 月 27 日 12:00 上线 ENJ/TRY 现货交易对。",
     "published_at": _now(1), "impact_direction": "bullish", "impact_strength": "strong"},
]
res_two = sd._get_resonance(_FakeConn(rows_two), "ENJUSDT", 12345)
check(res_two["catalyst_dir"]["bearish"] == 1 and res_two["catalyst_dir"]["bullish"] == 1,
      "同币种的两条**不同公告**（移除 vs 上线）不得互并",
      f"dir={res_two['catalyst_dir']}")

# ═══════════════════════════════════════════════════════════════
#  P0-4 —— 美股 ticker 串台门禁（linker.py）
# ═══════════════════════════════════════════════════════════════

print("\n【P0-4】美股 ticker 撞名（DoorDash/DASH）需加密语境才认")


class _RecConn:
    """记录「是否被查库」的最小 conn：用于断言门禁在查库**之前**拦下。"""

    def __init__(self):
        self.calls = 0

    def execute(self, *_a, **_kw):
        self.calls += 1

        class _R:
            def fetchone(self):
                return None
        return _R()


check("DASH" in lk._EQUITY_TICKER_COLLISIONS,
      "DASH（DoorDash, NASDAQ: DASH）在撞名清单内")
_DOORDASH = ("DoorDash 与纽约市达成 1.315 亿美元和解，涉及最低工资合规调查。"
             "纳斯达克：DASH")
check(lk.has_crypto_context(_DOORDASH) is False,
      "DoorDash 新闻正文无加密语境（现成正则即可拦下）")
check(lk.has_crypto_context("币安将上线 $DASH 永续合约，链上转账活跃") is True,
      "含 `$DASH` / 币安 / 链上 ⇒ 判为加密语境")
lk._symbol_asset_cache.clear()
_c1 = _RecConn()
out_blocked = lk.map_pairs_to_asset_ids(["DASHUSDT"], _c1, context_text=_DOORDASH)
check(out_blocked == [] and _c1.calls == 0,
      "DoorDash 新闻（symbol=DASHUSDT）在查库前被门禁拦下 → DASH 不入库",
      f"ids={out_blocked} calls={_c1.calls}")
lk._symbol_asset_cache.clear()
_c2 = _RecConn()
lk.map_pairs_to_asset_ids(["DASHUSDT"], _c2,
                          context_text="币安将上线 DASHUSDT 永续合约，链上转账活跃")
check(_c2.calls > 0,
      "带加密语境时门禁放行（继续走原映射路径，不误伤真加密新闻）",
      f"calls={_c2.calls}")
lk._symbol_asset_cache.clear()
_c3 = _RecConn()
lk.map_pairs_to_asset_ids(["ENJUSDT"], _c3, context_text="完全不含加密语境的普通句子")
check(_c3.calls > 0,
      "未撞名的 symbol（ENJ）不受该门禁影响", f"calls={_c3.calls}")

print("\n【P0-4·反例集】只读 prod 复算出的「必须拦下 / 必须放行」样本（改动前置）")
# 以下 ctx 全部取自 prod `biz.asset_catalyst` 的 `title + body_text`（逐字），改动正则前
# 先跑本段 —— 这三条约束我各踩过一次（见 linker.py 注释 ①②③④）。
_MUST_BLOCK = [   # （cid, ctx, 为什么是噪音）
    (10473, "DoorDash Inc. agreed to a $131.5 million settlement with New York City "
            "over a probe into its compliance with minimum-pay rules for couriers, "
            "according to Bloomberg. The company, the biggest food-delivery firm in "
            "the US by market share, said the deal resolves the investigation into "
            "missing wages.", "$131.5 曾被当成 cashtag"),
    (13616, "According to Wallstreetcn, BlackBerry reported second-quarter revenue of "
            "$163.3 million versus estimates of $145.8 million, adjusted basic "
            "earnings per share of $0.070 versus estimates of $0.04.", "$163 曾被当成 cashtag"),
    (14272, "According to CNBC, Costco Wholesale reported fiscal fourth-quarter revenue "
            "of $95.72 billion, up 11.1% from a year earlier and above Wall Street "
            "estimates of $94.86 billion.", "$95 曾被当成 cashtag"),
    (4164, "Blackstone Inc. is working through a backlog of investors seeking to "
           "withdraw capital from its flagship private credit fund.", ""),
    (12572, "Shareholders of LNG Canada, including Shell Plc, are poised to approve "
            "the final investment decision for the project.", ""),
    (13989, "Bob Chapek said he never had a chance at Disney and described his 33-month "
            "tenure as chief executive.", ""),
    (13722, "Brazil's central bank cut its 2026 economic growth forecast and said the "
            "slowdown will be sharper than previously expected.", "国家代码 BR"),
    (1642, "Walmart Inc. will begin delivering food orders from Papa John's "
           "International Inc. as it expands into restaurant delivery.", ""),
    (847, "Uber Technologies Inc. is shutting down its services in Nigeria effective "
          "Sept. 30, according to a company statement.", ""),
    # 商品侧（cid 10154 的正文）：若不慎把「上涨/下跌/涨幅/行情」等宏观高频词加进词表，
    # 这条会翻转为「放行」⇒ 商品 gate 被削弱（实测加了 6 个词即翻转 9 条）。
    (10154, "据彭博社 9 月 22 日报道，中国黄金进口今年处于创纪录水平，受国际金价下跌"
            "和人民币走强推动。海关数据显示，截至 8 月的采购量已超过 1000 吨。", "商品噪音"),
]
for _cid, _ctx, _why in _MUST_BLOCK:
    check(lk.has_crypto_context(_ctx) is False,
          f"必须拦下 cid={_cid}" + (f"（{_why}）" if _why else ""),
          "该 ctx 被判为「含加密语境」⇒ 美股/商品噪音会过闸")

# 已知残差（**不**断言为放行，属明知的代价）：纯行情快讯 `SOL 上涨突破 110 美元…`
# 这类「裸 ticker + 中文行情词」既无项目名也无加密专属词。若为救它把
# `上涨|下跌|涨幅|行情|突破` 加进词表，商品 gate 立即被削弱 9 条（见上 _MUST_BLOCK
# 的 cid 10154）。两害相权：宁漏几条行情快讯（复算 ≈6 条/30 天），不放商品噪音进来。
_MUST_PASS = [    # 真加密新闻，缺词/边界错就误杀
    ("PANews 9月24日消息，据 Lookonchain 监测，Tron 的总交易额已正式突破 30 万亿美元。", "项目名 + 信源"),
    ("PANews 9月18日消息，据 CoinDesk 报道，Solana于9月18日将目标出块时间由 300 毫秒降至 250 毫秒。",
     "`\\bsolana\\b` 在中文处判不出词尾 ⇒ 曾误杀 cid 6173"),
    ("Optimism approved Upgrade 20 to move its fault-proof system toward the architecture.",
     "项目名"),
    ("BlockBeats 消息，9 月 18 日，据 TradingBeats 监测，Chainlink 战略储备今晨再度增持价值 110 万美元 LINK。",
     "项目名 ⇒ 曾误杀 cid 5808"),
    ("Foresight News 消息，据 Gate 行情数据，SOL/USDT 现报 $110，24 小时涨幅 8.16%。",
     "`BASE/USDT` 斜杠口径"),
    ("Binance will list NEWUSDT on the spot market and enable trading.", ""),
    ("某巨鲸向交易所转入 1.2 万枚 ETH，链上出现大额转账。", ""),
]
for _ctx, _why in _MUST_PASS:
    check(lk.has_crypto_context(_ctx) is True,
          f"必须放行 {_ctx[:28]!r}…" + (f"（{_why}）" if _why else ""),
          "真加密新闻被误杀 ⇒ 该 symbol 的催化剂静默缺失")

print("\n【P0-4·防漂移】backfill_catalyst_links.py 内联 linker 副本必须同契约")


def _const_frozenset(tree, name):
    """取模块级 `NAME = frozenset({...})` 的字面量元素（取不到返回 None）。"""
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets):
            call = n.value
            if isinstance(call, ast.Call) and call.args:
                try:
                    return frozenset(ast.literal_eval(call.args[0]))
                except (ValueError, TypeError):
                    return None
    return None


# `backfill_catalyst_links.py` 内联了 linker 的映射逻辑（避免依赖 workbench 包），
# 历史上调用处传了 `context_text=ctx` 而函数签名**没有该参数** ⇒ 一跑即 TypeError，
# 门禁形同不存在、重跑会把脏关联重新写进 `biz.catalyst_asset_link`。
# 只锁**契约**（参数名 + 两套集合 + 正则内容），不比对注释措辞。
_BF_SRC = open(os.path.join(_SCRIPTS, "bin", "backfill_catalyst_links.py"),
               encoding="utf-8").read()
_BF_TREE = ast.parse(_BF_SRC)
_bf_fn = next((n for n in _BF_TREE.body if isinstance(n, ast.FunctionDef)
               and n.name == "map_pairs_to_asset_ids"), None)
check(_bf_fn is not None and "context_text" in [a.arg for a in _bf_fn.args.args],
      "内联副本 `map_pairs_to_asset_ids` 有 `context_text` 形参（否则调用即 TypeError）",
      f"args={[a.arg for a in _bf_fn.args.args] if _bf_fn else None}")
for _n in ("_COMMODITY_AMBIGUOUS_SYMBOLS", "_EQUITY_TICKER_COLLISIONS"):
    _a, _b = getattr(lk, _n), _const_frozenset(_BF_TREE, _n)
    check(_a == _b, f"两处 `{_n}` 内容一致（内联副本未漂移）",
          f"linker={_a} backfill={_b}")
# 正则逐字比对：两份必须同源，否则「补了 linker 忘了 backfill」会让回填重写脏关联
for _n in ("_CRYPTO_CONTEXT_RE",):
    # `_CRYPTO_CONTEXT_RE = re.compile(r"…" r"…")`：隐式拼接在 AST 里已被折成单个 Constant
    _lit = next((n.value.args[0].value for n in _BF_TREE.body
                 if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                 and n.value.args
                 and any(isinstance(t, ast.Name) and t.id == _n for t in n.targets)), None)
    check(_lit == lk._CRYPTO_CONTEXT_RE.pattern,
          f"两处 `{_n}` 逐字一致（含 cashtag 必须含字母 / 中文边界两处修复）",
          f"\n    linker  ={lk._CRYPTO_CONTEXT_RE.pattern}\n    backfill={_lit}")
check("has_crypto_context(" in _BF_SRC,
      "内联副本的映射循环内调用同一门禁 `has_crypto_context`")

print("\n【护栏】HTML 邮件不得含 markdown 强调符 `**`")
_html = sd._render_alert_email([_item(res=_res(bull=0, bear=1, stale=1, fresh=(0, 0, 0)))])
check("**" not in _html, "渲染结果无 `**`（应使用 <b>/<span style>）",
      "发现 markdown 强调符会原样进入邮件正文")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)