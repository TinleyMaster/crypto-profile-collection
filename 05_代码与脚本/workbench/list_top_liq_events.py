#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""提取代表性极端爆仓事件（按桶+爆仓额排序）供新闻归因。"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "scripts" / "data"


def load(contract: str) -> list[dict]:
    p = DATA / f"backtest_liq_cascade_events_{contract}.csv"
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def bucket(r: dict) -> str:
    ls = float(r["long_share"])
    if ls >= 0.70:
        return "EXT-LONG"
    if ls <= 0.30:
        return "EXT-SHORT"
    return "EXT-MIX"


def main() -> None:
    for contract in ("BTCUSDT", "ETHUSDT"):
        rows = load(contract)
        print(f"\n===== {contract} 代表性事件 =====")
        for b in ("EXT-LONG", "EXT-SHORT", "EXT-MIX"):
            sub = sorted([r for r in rows if bucket(r) == b],
                         key=lambda r: float(r["liq_total"]), reverse=True)
            print(f"\n--- {b} (爆仓额 Top 8) ---")
            for r in sub[:8]:
                d1 = float(r["d1"]) * 100
                f7 = float(r["fwd7"]) * 100 if r["fwd7"] else float("nan")
                f14 = float(r["fwd14"]) * 100 if r["fwd14"] else float("nan")
                print(f"  {r['date']}  爆仓={float(r['liq_total'])/1e6:,.0f}M "
                      f"比值={float(r['liq_ratio'])*100:.2f}% pct={float(r['pct']):.2f} "
                      f"多单占比={float(r['long_share'])*100:.0f}% "
                      f"当日={d1:+.1f}% H7={f7:+.1f}% H14={f14:+.1f}%")


if __name__ == "__main__":
    main()
