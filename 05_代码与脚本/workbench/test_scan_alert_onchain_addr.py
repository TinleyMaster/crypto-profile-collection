#!/usr/bin/env python3
"""告警邮件「链上大额转账」最大笔取数与地址显示 —— 离线护栏测试。

来源：`审计_告警邮件LSK-CELR_链上转账地址_2026-09-24.md`
  - N-A56-1（P2）：生产者用三个独立 `MAX()` 取 chain/from_label/to_label，与
    `MAX(value_usd)` 无关（类别列取 MAX 是字典序）⇒ 14% 行「最大单笔」元组与真实
    最大笔不符（含**链说错**）。
  - N-A56-2（P2）：邮件读陈旧标量列，1,029 行 names 非空但标量仍 unknown。
  - N-A56-3（P3）：`is_to_exchange` 未披露（本批 4 笔中 2 笔流入交易所）。
  - 地址显示：完整地址另起一行、豁免 90 字截断（塞进摘要会被砍成残缺串）。

运行: python test_scan_alert_onchain_addr.py
"""
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_scripts_bin = os.path.join(os.path.dirname(_here), "scripts", "bin")
for _p in (_here, _scripts_bin):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scan_daemon as sd  # noqa: E402
import phase_build_event_watchlist as pbw  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"    {detail}")


FROM_A = "0x2658723bf70c7667de6b25f99fcce13a16d25d08"
TO_A = "0xd2d7535e099f26ebfba26d96bd1a661d3531d0e9"

# ════════════════════════════════════════════════════════════
print("\n【N-A56-1/2】生产者标签优先级 _pick_label")
# ════════════════════════════════════════════════════════════

pl = pbw._pick_label
check(pl(["Binance"], ["exchange"], "unknown") == "Binance", "names[0] 优先（Binance 胜过 unknown）")
check(pl(None, ["exchange"], "unknown") == "exchange", "names 缺失 → labels[0]")
check(pl([], [], "exchange") == "exchange", "数组皆空 → 标量")
check(pl([], [], "unknown") == "?", "全 unknown → '?'")
check(pl(None, None, None) == "?", "None → '?'")
check(pl(["unknown"], ["exchange"], "unknown") == "exchange", "names[0]=unknown → 退到 labels[0]")
check(pl(["Binance", "Binance (propagated from BSC)"], None, None) == "Binance",
      "多元素 names 取第一个")

print("\n【N-A56-1】生产者 SQL 取真实最大笔（源码守卫）")
_src = open(pbw.__file__, encoding="utf-8").read()
check("JOIN LATERAL" in _src, "用 LATERAL 取单笔明细")
check("ORDER BY t2.value_usd DESC" in _src, "按 value_usd 降序 ⇒ 真实最大笔")
check("LIMIT 1" in _src, "只取一笔")
check("MAX(t.chain)" not in _src and "MAX(t.from_label)" not in _src
      and "MAX(t.to_label)" not in _src, "旧的三个独立 MAX() 已移除")
check("n_to_exchange" in _src, "N-A56-3：统计流向交易所笔数")
check('"max_tx"' in _src, "source_ref 增 max_tx（地址明细）")
check("from_address" in _src and "to_address" in _src and "tx_hash" in _src,
      "max_tx 含收发地址与 tx_hash")

# ════════════════════════════════════════════════════════════
print("\n【地址显示】_render_resonance_msgs 另起一行完整地址")
# ════════════════════════════════════════════════════════════


def _res(addr=None, text="近7天大额转账 4 笔 / 合计 $315.3M / 最大单笔 $39.3M（eth，unknown→unknown）"):
    ev = {"dir": "neutral", "kind": "🔄 链上转账", "text": text, "date": None}
    if addr is not None:
        ev["addr"] = addr
    return {"event": [ev], "catalyst": [], "kol": [], "kol_total": 0,
            "catalyst_dir": {"bullish": 0, "bearish": 0, "neutral": 0}}


h = sd._render_resonance_msgs(_res({"chain": "eth", "from": FROM_A, "to": TO_A}))
check(FROM_A in h, "完整 from 地址出现（未被截断）")
check(TO_A in h, "完整 to 地址出现（未被截断）")
check("最大一笔 · eth · " in h, "地址行含「最大一笔 · 链 ·」前缀")
check("ui-monospace" in h, "地址行用等宽字体")
check("word-break:break-all" in h, "地址行自动换行（防手机端溢出）")
check("→" in h, "from → to 箭头")

# 地址行不得走 90 字截断：文本超长 + 地址仍完整
h_long = sd._render_resonance_msgs(_res(
    {"chain": "eth", "from": FROM_A, "to": TO_A}, text="长" * 300))
check(FROM_A in h_long and TO_A in h_long, "摘要超长时地址仍完整（豁免 90 字截断）")
check("长" * sd.RESONANCE_MSG_CHARS not in h_long, "摘要本身仍按 90 字截断")

# 无 addr → 不渲染地址行（无回归）
h_no = sd._render_resonance_msgs(_res())
check("最大一笔" not in h_no and "ui-monospace" not in h_no, "无 addr 时不渲染地址行")

# 无地址字段（解锁/旧数据）→ 不报错
h_unlock = sd._render_resonance_msgs({"event": [
    {"dir": "bearish", "kind": "🔓 解锁", "text": "解锁 2026-09-25（1 天后）", "date": None}],
    "catalyst": [], "kol": [], "kol_total": 0,
    "catalyst_dir": {"bullish": 0, "bearish": 0, "neutral": 0}})
check("解锁 2026-09-25" in h_unlock and "最大一笔" not in h_unlock, "解锁事件不渲染地址行（不误伤）")

# 旧快照字符串元素兼容
h_old = sd._render_resonance_msgs({"event": ["🔄链上转账: 旧字符串形态"],
                                   "catalyst": [], "kol": [], "kol_total": 0,
                                   "catalyst_dir": {"bullish": 0, "bearish": 0, "neutral": 0}})
check("旧字符串形态" in h_old, "兼容旧快照纯字符串元素")

print("\n【地址显示】_get_resonance 透传 source_ref.max_tx（源码守卫）")
_sd_src = open(sd.__file__, encoding="utf-8").read()
_fm = re.search(r"def _get_resonance\(.*?\n(.*?)\ndef ", _sd_src, re.S)
_fsrc = _fm.group(1) if _fm else ""
check("source_ref" in _fsrc, "事件段 SELECT 增 source_ref")
check('"max_tx"' in _fsrc, "从 source_ref.max_tx 取地址")
check('ev["addr"]' in _fsrc, "置入 addr 键（向后兼容）")
check("from_address" in _fsrc, "只在有地址时才置 addr")

print("\n【图例】地址行与「流向交易所」声明")
_leg = sd._render_alert_email([], [])
_leg = _leg.split("图例：")[1] if "图例：" in _leg else _leg
check("最大一笔" in _leg, "图例声明「最大一笔」")
check("豁免单条 90 字截断" in _leg, "图例声明地址豁免截断")
check("流向交易所" in _leg, "图例声明「其中 N/M 笔流向交易所」")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
