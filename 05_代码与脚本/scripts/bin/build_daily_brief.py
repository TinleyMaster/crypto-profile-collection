#!/usr/bin/env python3
"""每日早报快照落库 + 趋势 diff（P1-4 第二刀）。

scheduler.py 注册：daily_brief_snapshot（Asia/Shanghai，早于邮件发送）。
流程：实时拉 overview（force_refresh=1 绕过缓存）→ 读昨日快照 → 生成早报 → 落库今日供明日 diff。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

# 将 workbench（macro_market.py）所在目录加入 sys.path：
#   prod: /app/scripts/bin → /app（macro_market.py 在 /app）
#   local: scripts/bin → 05_代码与脚本（macro_market.py 在 05_代码与脚本/workbench）
_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
for cand in (os.path.join(_code_root, "workbench"), "/app", _code_root):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

from macro_market import (  # noqa: E402
    generate_morning_brief,
    get_market_overview,
    load_snapshot,
    save_snapshot,
)


def main() -> dict:
    today = get_market_overview(force_refresh="1")  # 快照必须最新，绕过 CACHE_TTL
    y_date = (date.today() - timedelta(days=1)).isoformat()
    yesterday = load_snapshot(y_date)
    brief = generate_morning_brief(today, yesterday)
    save_snapshot(date.today().isoformat(), today)  # 落库供明日 diff

    print("M0:", brief.get("M0_tldr"))
    print("DIFF:", brief.get("DIFF"))
    return brief


if __name__ == "__main__":
    main()