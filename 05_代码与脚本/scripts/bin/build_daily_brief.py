#!/usr/bin/env python3
"""每日早报快照落库 + 趋势 diff（P1-4 第二刀）。

scheduler.py 注册：daily_brief_snapshot（Asia/Shanghai，早于邮件发送）。
流程：数据源新鲜度自检 → 实时拉 overview（force_refresh=1 绕过缓存）→
读昨日快照 → 生成早报 → 落库今日供明日 diff。
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

# scripts/src 加入 path（crypto_research 包）。
# 快照生成会调用 macro_market._build_mvrv_universe / fetch_*，它们内部
# `from crypto_research.config import get_settings`；缺此路径会以
# "No module named 'crypto_research'" 失败，导致 mvrv_universe 落库为 error
#（2026-09-16/17/18 连续出现，早报 MVRV 维度持续缺失）。
_scripts_src = os.path.join(_code_root, "scripts", "src")
if os.path.isdir(_scripts_src) and _scripts_src not in sys.path:
    sys.path.insert(0, _scripts_src)

from macro_market import (  # noqa: E402
    generate_morning_brief,
    get_market_overview,
    load_snapshot,
    save_snapshot,
)


def _check_data_freshness() -> list[str]:
    """早报依赖数据源新鲜度自检：返回滞后（>2 天）或无数据的数据源列表。

    在生成早报前调用，滞后项打印告警并附加到 brief.degraded（邮件降级标注）。
    ETF 数据 T+1 更新 + 周末休市，>2 天视为滞后。
    """
    stale: list[str] = []
    checks = [
        ("恐贪指数", "biz.fear_greed_daily", "metric_date"),
        ("BTC OI", "biz.btc_oi_daily", "metric_date"),
        ("CEFI 指数", "biz.cefi_index_daily", "metric_date"),
        ("赛道TVL", "biz.category_tvl_daily", "snapshot_date"),
        ("ETF资金流", "biz.etf_flow_daily", "flow_date"),
        ("赛道市值", "biz.sector_flow_daily", "metric_date"),
        ("大盘快照", "biz.market_snapshot_daily", "snapshot_date"),
    ]
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                for name, tbl, col in checks:
                    try:
                        cur.execute(f"SELECT MAX({col}) FROM {tbl}")
                        row = cur.fetchone()
                        latest = row[0] if row else None
                        if latest is not None:
                            days = (date.today() - latest).days
                            if days > 2:
                                print(f"[freshness] ⚠️ {name}: {latest}（滞后{days}天）")
                                stale.append(f"{name}({latest})")
                            else:
                                print(f"[freshness] ✓ {name}: {latest}")
                        else:
                            print(f"[freshness] ⚠️ {name}: 无数据")
                            stale.append(name)
                    except Exception as e:
                        print(f"[freshness] ⚠️ {name}: 查询失败 {e}")
                        stale.append(name)
    except Exception as e:
        print(f"[freshness] ⚠️ 自检失败: {e}")
    return stale


def main() -> dict:
    stale = _check_data_freshness()
    today = get_market_overview(force_refresh="1")  # 快照必须最新，绕过 CACHE_TTL
    y_date = (date.today() - timedelta(days=1)).isoformat()
    yesterday = load_snapshot(y_date)
    brief = generate_morning_brief(today, yesterday)
    if stale:
        brief.setdefault("M9_degraded", []).extend(stale)
    save_snapshot(date.today().isoformat(), today)  # 落库供明日 diff

    print("M0:", brief.get("M0_tldr"))
    print("DIFF:", brief.get("DIFF"))
    return brief


if __name__ == "__main__":
    main()
