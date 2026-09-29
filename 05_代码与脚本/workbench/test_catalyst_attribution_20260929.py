#!/usr/bin/env python3
"""催化剂主币归因（P0-1）离线护栏单测（审计 2026-09-29）。

运行：python test_catalyst_attribution_20260929.py

背景：`primary_asset_id = asset_ids[0]`（正文首个命中 token）导致
「XRP 被盗」挂到 XLM、「Arbitrum 基金会安全计划」挂到 USDC（出资币）。
修复：`pipeline._order_pairs_by_salience` 按「事件主语显著度」重排交易对：
  ① 稳定币/计价代币降权；② 标题命中优先于正文；③ 位置靠前优先；不可判定则保持原序。

⚠️ 纯函数，不连库、不打网络。
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from catalyst import pipeline as P  # noqa: E402

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


ord_pairs = P._order_pairs_by_salience

print("== 1. _symbol_pos 词边界 ==")
check(P._symbol_pos("arbitrum security program", "ARB") >= 1e9,
      "ARB 不误命中 Arbitrum（词边界）")
check(P._symbol_pos("2500万 arb 与 176万 usdc", "ARB") < 1e9,
      "独立单词 ARB 正常命中")
check(P._symbol_pos("ethereum", "ETH") >= 1e9, "ETH 不误命中 Ethereum")

print("== 2. 稳定币降权（USDC 案：Arbitrum 事件不应挂 USDC） ==")
_usdc_title = "Arbitrum 基金会推出 Arbitrum Security Program（780万：176万 USDC + 2500万 ARB）"
_o = ord_pairs(["USDCUSDT", "ARBUSDT"], _usdc_title, "")
check(_o[0] == "ARBUSDT", "稳定币 USDC 降权 → ARB（事件主语）在前", str(_o))
check(_o == ["ARBUSDT", "USDCUSDT"], "顺序完整（不丢 pair）", str(_o))

print("== 3. 标题位置优先（XLM 案：XRP 被盗应归 XRP） ==")
_xlm_title = "D'CENT 钱包 12.4M XRP / 7000+ 钱包被盗；一名 Stellar 用户损失 XLM"
_o2 = ord_pairs(["XLMUSDT", "XRPUSDT"], _xlm_title, "")
check(_o2[0] == "XRPUSDT", "标题中 XRP 先于 XLM → XRP 在前", str(_o2))
# 反序输入也应得到同样主语
_o2b = ord_pairs(["XRPUSDT", "XLMUSDT"], _xlm_title, "")
check(_o2b[0] == "XRPUSDT", "输入顺序无关（按标题位置）", str(_o2b))

print("== 4. 标题未命中 → 用正文位置 ==")
_o3 = ord_pairs(["AAAUSDT", "BBBUSDT"], "某宏观标题", "正文先提 BBB 后提 AAA")
check(_o3[0] == "BBBUSDT", "标题未命中时按正文位置", str(_o3))

print("== 5. 不劣化：单元素 / 全未命中 / 全稳定 保持原序 ==")
check(ord_pairs(["BTCUSDT"], "x", "y") == ["BTCUSDT"], "单元素原样")
check(ord_pairs(["AAAUSDT", "BBBUSDT"], "无提及", "也无提及") == ["AAAUSDT", "BBBUSDT"],
      "全未命中 → 原序（稳定排序，不劣化）")
check(ord_pairs(["USDCUSDT", "USDTUSDT"], "两种稳定币", "") == ["USDCUSDT", "USDTUSDT"],
      "全为稳定币 → 原序（无更优候选时不乱动）")

print("== 6. 接线：_resolve_asset_ids 先重排再映射 ==")
import inspect  # noqa: E402
_src = inspect.getsource(P._resolve_asset_ids)
check("_order_pairs_by_salience(pairs, item.title, item.body_text)" in _src,
      "_resolve_asset_ids 调用了主语重排（非死代码）")
_merge_src = inspect.getsource(P._merge_catalyst)
check("_order_pairs_by_salience(all_pairs, ctx, None)" in _merge_src,
      "_merge_catalyst 合并路径同样重排")


print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
