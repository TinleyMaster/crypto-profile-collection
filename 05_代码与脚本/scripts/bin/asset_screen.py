#!/usr/bin/env python3
"""资产多维筛选：弹性 × 解锁四象限 + 弹性 × 资金流双确认（投研 SOP P1.9）。

把三条独立指标合到一个选币视图：
    事件弹性（P1.6）—— 消息值不值钱
    解锁表现（P1.7）—— 解锁砸不砸盘
    资金流因子（P1.8）—— 资金有没有真进来

输出
----
1. **四象限（弹性 × 解锁）**
   ★理想（高弹性+抗解禁）｜ ⚠利好+解锁风险（高弹性+解禁即崩）
   防守（低弹性+抗解禁） ｜ ✗规避（低弹性+解禁即崩）
2. **双确认（弹性 × 资金流）**
   ★双确认（高弹性+资金流入）｜ ⚠背离（高弹性+资金流出）
3. **综合榜**：同时满足 高弹性 & 资金流≥中性 & 解锁非崩 的标的
4. **★ 高确定性预筛**（SKILL-HC-001，与 MV fix_110 `hc_prefilter` 同口径）：
   统计有效（弹性 n≥10 / 解锁 n≥5）+ 高弹性 + 资金流入 + 解锁非崩 + 无 CMC 大额解锁。
   ⚠️ **仅预筛**：报告层仍须叠加「无高危负向标签 + 因子冲突矩阵无强冲突」终判（铁律 #14）。

用法
----
    python bin/asset_screen.py                 # 控制台（全部）
    python bin/asset_screen.py --md            # markdown 片段
    python bin/asset_screen.py --top 300       # 只看市值前 300
    python bin/asset_screen.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

SCREEN_SQL = """
SELECT a.asset_id, a.canonical_symbol AS sym, a.canonical_name AS name, a.market_cap_rank AS rk,
       a.market_cap,
       e.n_events AS e_n, e.ar_72h_mean, e.hit_rate, e.elasticity_grade AS e_grade, e.elasticity_score AS e_score,
       r.n_events AS u_n, r.excess7_mean, r.reaction_grade AS u_grade, r.reaction_score AS u_score,
       r.dominant_beneficiary, r.next_unlock_beneficiary, r.next_unlock_source, r.insider_pct,
       c.next_unlock_date AS cmc_date, c.next_unlock_usd AS cmc_usd,
       c.next_unlock_pct_supply AS cmc_pct, c.dominant_bucket AS cmc_bene,
       c.insider_pct AS cmc_insider_pct,
       f.flow_score, f.flow_grade, f.obi_7d, f.taker_imb_7d, f.netflow_7d_pct_mcap
FROM core.asset a
JOIN LATERAL (SELECT * FROM biz.asset_price_elasticity x WHERE x.asset_id=a.asset_id
              ORDER BY x.as_of DESC LIMIT 1) e ON TRUE
LEFT JOIN LATERAL (SELECT * FROM biz.asset_unlock_reaction x WHERE x.asset_id=a.asset_id
              ORDER BY x.as_of DESC LIMIT 1) r ON TRUE
LEFT JOIN LATERAL (SELECT * FROM biz.asset_flow_factor x WHERE x.asset_id=a.asset_id
              ORDER BY x.as_of DESC LIMIT 1) f ON TRUE
LEFT JOIN biz.asset_unlock_next_cmc c ON c.asset_id = a.asset_id
WHERE a.market_cap_rank IS NOT NULL AND a.market_cap_rank <= %s
"""

E_HI = {"高弹性", "中高弹性"}
E_LO = {"低弹性", "负弹性"}
U_UP = {"抗解禁型", "偏抗解禁"}
U_DN = {"偏解禁即崩", "解禁即崩"}


def f2(v, s="%", sign=True):
    if not isinstance(v, (int, float)):
        return "-"
    return f"{v:+.2f}{s}" if sign else f"{v:.2f}{s}"


def main() -> int:
    ap = argparse.ArgumentParser(description="资产多维筛选（弹性×解锁 / 弹性×资金流）")
    ap.add_argument("--top", type=int, default=400, help="市值前 N（默认 400）")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    with get_connection(get_settings(require_database=True).database_url) as conn:
        cur = conn.cursor()
        cur.execute(SCREEN_SQL, (args.top,))
        cols = [c[0] for c in cur.description]
        rows = [{k: (float(v) if isinstance(v, Decimal) else v)
                 for k, v in zip(cols, r)} for r in cur.fetchall()]

    # 分桶
    quad = {"ideal": [], "elastic_dump": [], "defensive": [], "avoid": []}
    for x in rows:
        eb = "hi" if x["e_grade"] in E_HI else ("lo" if x["e_grade"] in E_LO else None)
        ub = "up" if x["u_grade"] in U_UP else ("dn" if x["u_grade"] in U_DN else None)
        if not eb or not ub:
            continue
        key = {("hi", "up"): "ideal", ("hi", "dn"): "elastic_dump",
               ("lo", "up"): "defensive", ("lo", "dn"): "avoid"}[(eb, ub)]
        quad[key].append(x)
    for k in quad:
        quad[k].sort(key=lambda x: -(x["e_score"] or 0))

    double_confirm, divergence = [], []
    for x in rows:
        if x["e_grade"] not in E_HI or x["flow_grade"] is None:
            continue
        if x["flow_grade"] == "资金流入":
            double_confirm.append(x)
        elif x["flow_grade"] == "资金流出":
            divergence.append(x)
    double_confirm.sort(key=lambda x: -(x["flow_score"] or 0))
    divergence.sort(key=lambda x: (x["flow_score"] or 100))

    # ── ★ 高确定性预筛（SKILL-HC-001；与 MV fix_110 hc_prefilter 严格同口径）──────
    #   统计有效（弹性 n≥10 且 解锁 n≥5）+ 高弹性 + 资金流入 + 解锁非崩 + 无 CMC 大额解锁
    #   ⚠️ 仅预筛：报告层仍须终判「无高危风险标签 + 因子冲突矩阵无强冲突」（铁律 #14）
    def _hc(x):
        if (x["e_n"] or 0) < 10 or (x["u_n"] or 0) < 5:
            return False
        if x["e_grade"] not in E_HI:
            return False
        if x["flow_grade"] != "资金流入":
            return False
        if x["u_grade"] in U_DN:
            return False
        if x["cmc_pct"] is not None and x["cmc_pct"] >= 5:
            return False
        return True

    hc_pool = sorted([x for x in rows if _hc(x)],
                     key=lambda x: -(x["e_score"] or 0))
    # 因样本不足被剔除的"名义高弹性"标的（供人工判断是否降级为观察）
    hc_small = sorted([x for x in rows
                       if x["e_grade"] in E_HI and not _hc(x)
                       and ((x["e_n"] or 0) < 10 or (x["u_n"] or 0) < 5)],
                      key=lambda x: -(x["e_score"] or 0))

    if args.json:
        print(json.dumps({"hc_prefilter": hc_pool, "hc_small_sample": hc_small,
                          "quadrant": {k: quad[k] for k in quad},
                          "double_confirm": double_confirm, "divergence": divergence},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    L = []
    L.append("# 资产多维筛选：弹性 × 解锁 × 资金流")
    L.append("")
    L.append(f"> 口径：市值前 {args.top} ｜ 弹性=P1.6 事件弹性 ｜ 解锁=P1.7 解锁反应 ｜ 资金流=P1.8")
    L.append("")

    def tbl(title, items, note=""):
        L.append(f"## {title}")
        if note:
            L.append(f"> {note}")
        L.append("")
        if not items:
            L.append("（无）")
            L.append("")
            return
        L.append("| 币 | 市值# | 弹性AR72 | 弹性级 | 解锁ex7 | 解锁级 | 主导类别 | 下次解锁 | CMC解锁(占供应/主导/美元) | 资金流分 | 资金级 |")
        L.append("|---|---:|---:|---|---:|---|---|---|---|---:|---|")
        for x in items[:20]:
            cmc_pct = x.get("cmc_pct")
            cmc_cell = "-"
            if cmc_pct is not None:
                flag = "⚠" if cmc_pct >= 5 else ""
                usd = x.get("cmc_usd")
                usd_s = (f"${usd / 1e6:.0f}M" if usd and usd >= 1e6
                         else (f"${usd / 1e3:.0f}K" if usd else "-"))
                cmc_cell = f"{flag}{cmc_pct:.2f}% / {x.get('cmc_bene') or '-'} / {usd_s}"
            L.append(f"| {x['sym']} | {x['rk'] or '-'} | {f2(x['ar_72h_mean'])} | {x['e_grade']} "
                     f"| {f2(x['excess7_mean'])} | {x['u_grade'] or '-'} | {x['dominant_beneficiary'] or '-'} "
                     f"| {x['next_unlock_beneficiary'] or '-'} "
                     f"| {cmc_cell} "
                     f"| {f2(x['flow_score'], '', sign=False)} | {x['flow_grade'] or '-'} |")
        L.append("")

    tbl("★ 高确定性预筛（统计有效 + 双确认 + 解锁非崩 + 无大额解锁）", hc_pool,
        "SKILL-HC-001 预筛：弹性 n≥10 且 解锁 n≥5 统计有效 + 高弹性 + 资金流入 + 解锁非崩 "
        "+ 无 CMC 大额解锁（≥5%）；⚠️ 仅预筛，报告层仍须终判风险标签与因子冲突（铁律 #14）")
    if hc_small:
        L.append(f"> ⚠ 另有 {len(hc_small)} 个高弹性标的因**样本不足**（弹性 n<10 或 解锁 n<5）"
                 f"未入预筛：{', '.join(str(x['sym']) for x in hc_small[:15])}"
                 f"{' …' if len(hc_small) > 15 else ''}")
        L.append("")

    tbl("★ 象限一：理想（高弹性 + 抗解禁）", quad["ideal"],
        "利好能涨，且解锁不砸盘 —— 事件驱动的首选池")
    tbl("⚠ 象限二：利好可涨但解锁有风险（高弹性 + 解禁即崩）", quad["elastic_dump"],
        "利好兑现能力强，但临近解锁日需规避")
    tbl("防守（低弹性 + 抗解禁）", quad["defensive"], "利好反应钝，但解锁不构成抛压")
    tbl("✗ 象限四：规避（低弹性 + 解禁即崩）", quad["avoid"], "既接不住利好，解锁还要跌")
    tbl("★ 双确认（高弹性 + 资金流入）", double_confirm,
        "消息值钱 + 资金真进 —— 最高置信")
    tbl("⚠ 背离（高弹性 + 资金流出）", divergence,
        "消息面强但资金在撤 —— 谨防利好出尽")

    out = "\n".join(L)
    if args.md:
        print(out)
    else:
        # 控制台精简版
        print(out.replace("# ", "◆ ").replace("## ", "▸ "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
