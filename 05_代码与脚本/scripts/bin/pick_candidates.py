#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选币漏斗（L0）—— 从全市场筛出「值得深研」的候选清单。

定位：这是《代币研究 SOP》的第 0 层。它只做**筛选**，不做判断，
      不下买卖结论。输出的是「进入五层漏斗深研的名单」。

为什么要这个脚本（2026-10-27 实测暴露的问题）：
  `bin/asset_screen.py` 按 `--top 400` 市值切片，会**静默丢掉小市值高确定性标的**。
  实测 ARKM 市值排名 574、但 hc 评分全池第 2 → 被切掉，导致脚本给 3 只、MV 给 4 只。
  本脚本改为「全量扫 + 市值下限做流动性闸」，不再按排名切片。

用法：
  py -3.12 pick_candidates.py                    # 默认市值下限 10M，输出 markdown
  py -3.12 pick_candidates.py --min-mcap 0       # 不设流动性下限
  py -3.12 pick_candidates.py --json             # 机读
  py -3.12 pick_candidates.py --explain          # 逐刀漏斗读数（诊断用）

判定七刀（与仓库 asset_screen.py / MV hc_prefilter 同口径，仅多「市值下限」一刀）：
  ① 弹性样本 n≥10      —— 事件弹性可统计
  ② 高/中高弹性        —— 消息对它有明显正向弹性
  ③ 资金流入（双确认）  —— 消息值钱 + 资金真进
  ④ 解锁样本 n≥5       —— 解锁反应可统计（天花板 n=7，n≥8 结构性不可达）
  ⑤ 解锁非崩           —— 不是「解禁即崩」体质
  ⑥ 无 CMC 大额解锁    —— 下次解锁占流通 <5%
  ⑦ 有未来解锁事件     —— 还有「解锁前布局」窗口
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 既能从仓库 bin/ 运行，也能从工作区副本运行（找不到 src 包时给出明确指引，
# 而不是抛 ModuleNotFoundError 让人误判成「数据/环境坏了」）
SCRIPT_DIR = Path(__file__).resolve().parent
_CANDS = (
    SCRIPT_DIR,
    SCRIPT_DIR / "05_代码与脚本" / "scripts",
    Path(r"E:\瞎搞乱搞\web3\加密货币研究报告\05_代码与脚本\scripts"),
)
for _c in _CANDS:
    if (_c / "src" / "crypto_research").is_dir():
        if str(_c) not in sys.path:
            sys.path.insert(0, str(_c))
        break
else:
    sys.exit("找不到 src/crypto_research —— 请在仓库 05_代码与脚本/scripts 下运行本脚本")

from src.crypto_research.config import get_settings  # noqa: E402
from src.crypto_research.db.conn import get_connection  # noqa: E402

# 七刀定义：(标签, 条件 SQL)
GATES = [
    ("①弹性n10", "COALESCE(e_n_events,0) >= 10"),
    ("②高弹性", "elasticity_grade IN ('高弹性','中高弹性')"),
    ("③资金流入", "is_double_confirm"),
    ("④解锁n5", "COALESCE(u_n_events,0) >= 5"),
    ("⑤解锁非崩", "(reaction_grade IS NULL OR reaction_grade NOT IN ('偏解禁即崩','解禁即崩'))"),
    ("⑥无CMC大额", "COALESCE(cmc_next_unlock_pct_supply,0) < 5"),
    ("⑦有未来解锁", "COALESCE(unlock_fut_n,0) >= 1"),
]

# ── L5-A 预检：稀释与抛压（纯本地数据，不依赖任何新数据源）────────────────
#   为什么在 L0 就查：ARKM 案例证明「L0 胜率说可试、L2 稀释说别碰」的矛盾
#   只有在选币阶段就暴露才有用，等到深研才发现是浪费。
#   三个口径的诚实边界：
#     · fdv_mc  = total_supply/circulating_supply，即「全稀释是流通的几倍」，100% 可算
#     · dil_180 = 已知未来 180 天解锁额 / 流通市值（**下限**，非完整时间表）
#                 实测 334/367 只资产的解锁事件最远只到 180 天内，故无年度口径，不得外推为「年化稀释」
#     · dd_ath  = 距 ATH 跌幅。⚠️ 这不是估值指标，跌 99% 不等于便宜（GRT 实测 −99.2%），
#                 仅作「市场预期已重置」的参考，不得单独当买入理由
L5_LATERAL = """
LEFT JOIN LATERAL (
    SELECT sum(e.unlock_value_usd) FILTER (WHERE e.unlock_date <= CURRENT_DATE + 180) AS u180,
           count(*) FILTER (WHERE e.unlock_date <= CURRENT_DATE + 180)                AS n180,
           sum(e.unlock_value_usd) FILTER (WHERE e.unlock_date <= CURRENT_DATE + 180
                 AND e.beneficiary_type IN ('team','investor'))                       AS insider180
    FROM biz.asset_unlock_event e
    WHERE e.asset_id = m.asset_id AND e.unlock_date >= CURRENT_DATE
) u ON TRUE
"""

SELECT_SQL = """
SELECT m.symbol, m.name, m.asset_id,
       round((m.market_cap/1e6)::numeric, 1)                     AS mcap_m,
       m.elasticity_grade, m.e_n_events,
       round(m.ar_72h_mean::numeric, 4)                          AS ar72,
       round(m.hit_rate::numeric, 3)                             AS hit,
       round(m.hit_z::numeric, 2)                                AS hit_z,
       m.u_n_events, m.reaction_grade,
       round(m.excess7_mean::numeric, 4)                         AS excess7,
       m.flow_grade, round(m.flow_score::numeric, 1)             AS flow_score,
       m.unlock_fut_n,
       to_char(m.unlock_next_date,'YYYY-MM-DD')                  AS next_date,
       round(COALESCE(m.cmc_next_unlock_pct_supply,0)::numeric,2) AS cmc_pct,
       COALESCE(m.next_unlock_beneficiary::text,'-')             AS next_bene,
       round(COALESCE(m.screen_score,0)::numeric, 2)             AS score,
       COALESCE(m.quad_key,'-')                                  AS quad,
       -- L5-A 预检三口径
       round((a.total_supply / NULLIF(a.circulating_supply,0))::numeric, 2)  AS fdv_mc,
       round(COALESCE(u.u180,0) / NULLIF(m.market_cap,0) * 100, 1)           AS dil_180,
       round(CASE WHEN a.ath_usd > 0 AND a.circulating_supply > 0
                  THEN (1 - (m.market_cap / a.circulating_supply) / a.ath_usd) * 100 END, 1) AS dd_ath,
       COALESCE(u.n180,0)                                        AS n180,
       round(COALESCE(u.insider180,0) / NULLIF(COALESCE(u.u180,0),0) * 100, 1) AS insider_pct_180,
       -- L5-B 收入估值（DeFiLlama，fix_117）。NULL = 无该口径，不是 0
       round(r.fees_30d, 0)                                      AS fee30,
       round(r.revenue_30d, 0)                                   AS rev30,
       round(r.holders_rev_30d, 0)                               AS hold30,
       round((m.market_cap / NULLIF(r.revenue_30d * 12.1667, 0))::numeric, 1) AS ps,
       round((m.market_cap / NULLIF(r.holders_rev_30d * 12.1667, 0))::numeric, 1) AS ps_holders
FROM biz.mv_asset_screen m
JOIN core.asset a ON a.asset_id = m.asset_id
LEFT JOIN biz.asset_revenue_snapshot r
       ON r.asset_id = m.asset_id AND r.source_code = 'dl'
{L5}
WHERE {where}
ORDER BY m.screen_score DESC NULLS LAST
"""


def build_where(min_mcap_m: float) -> str:
    conds = list(g for _, g in GATES)
    # 流动性闸：不按排名切，按绝对市值下限（避免静默丢小市值高分标的）
    conds.append(f"COALESCE(m.market_cap,0) >= {min_mcap_m * 1e6}")
    return " AND ".join(conds)


def main() -> int:
    ap = argparse.ArgumentParser(description="选币漏斗 L0：全市场 → 候选清单")
    ap.add_argument("--min-mcap", type=float, default=10.0, help="市值下限（百万美元），默认 10")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--explain", action="store_true", help="打印逐刀漏斗读数")
    ap.add_argument("--strict", action="store_true", help="只输出命中率显著>50%（铁律14 第①条）的标的")
    args = ap.parse_args()

    url = get_settings(require_database=True).database_url

    with get_connection(url) as conn, conn.cursor() as cur:
        if args.explain:
            print("## 逐刀漏斗（每刀在上一刀基础上加严）\n")
            print(f"{'刀':<14}{'剩余':>8}")
            prev = None
            for i in range(len(GATES) + 1):
                conds = [g for _, g in GATES[:i]] or ["TRUE"]
                cur.execute(f"SELECT count(*) FROM biz.mv_asset_screen m WHERE {' AND '.join(conds)}")
                n = cur.fetchone()[0]
                label = "全量" if i == 0 else GATES[i - 1][0]
                delta = "" if prev is None else f"  (−{prev - n})"
                print(f"{label:<14}{n:>8}{delta}")
                prev = n
            cur.execute(
                f"SELECT count(*) FROM biz.mv_asset_screen m WHERE {build_where(args.min_mcap)}")
            print(f"{'⑧市值≥'+str(args.min_mcap)+'M':<14}{cur.fetchone()[0]:>8}")
            return 0

        cur.execute(SELECT_SQL.format(where=build_where(args.min_mcap), L5=L5_LATERAL))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        # 全池 P/S 中位数（作参照系，避免用拍脑袋的绝对阈值判贵贱）
        cur.execute("""
            SELECT round(percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY s.market_cap / (r.revenue_30d * 12.1667))::numeric, 1)
            FROM biz.mv_asset_screen s
            JOIN biz.asset_revenue_snapshot r
              ON r.asset_id = s.asset_id AND r.source_code = 'dl'
            WHERE r.revenue_30d > 0 AND s.market_cap_rank IS NOT NULL
              AND s.market_cap >= 10000000
        """)
        ps_median = cur.fetchone()[0]

        # MV 的数据日期与构建时间（供表头使用；不能拿候选字段凑，会误导）
        cur.execute("SELECT max(latest_as_of), max(built_at) FROM biz.mv_asset_screen")
        mv_asof, mv_built = cur.fetchone()

    data = [dict(zip(cols, r)) for r in rows]

    # ── 命中率分层（铁律 14 第①条：命中率显著 >50%，|z|≥1.96）─────────────
    #   hit_rate = 解锁后 72h **跑赢大盘**的比例；hit_z = 二项检验 z 值。
    #   ⚠️ 这一步 hc_prefilter 不做 —— 不补这刀，会把「均值被少数暴涨拉高、
    #      多数事件在跌」的标的当成机会放进来（实测 ATH: 均值 +3.58% 但胜率 25%）。
    #   ⚠️ 为何要中间档：解锁事件天然小样本（有方向样本多为 8–50 个），
    #      z≥1.96 极难达成（实测全池 4 只无一达标）。若只留「显著/不显著」二分会
    #      把所有候选一刀切成垃圾，失去区分度。故设 secondary：弱证据但方向明确，
    #      只能小仓位试错/重点观察，**仍不得称「高确定性」**。
    def tier(d):
        hz, hr = d.get("hit_z"), d.get("hit")
        if hz is None or hr is None:
            return "unknown"
        if hz >= 1.96 and hr > 0.5:
            return "core"        # 命中率显著为正 → 真机会
        if hz <= -1.96:
            return "avoid"       # 显著为负 → 反向信号，明确规避
        if hz >= 1.0 and hr >= 0.6:
            return "secondary"   # 方向明确但未达显著 → 观察偏多，小仓试错
        return "watch"           # 无方向证据 → 纯观察

    for d in data:
        d["tier"] = tier(d)

    # ── L5-A 估值分级（稀释维度）──────────────────────────────────────────
    #   判据来源：SOP v0.2「未来 12 个月解锁 / 当前流通 > 15% 要警惕」。
    #   因解锁数据只有 180 天视野，只能拿 180 天已知事件比，故 15% 红线在此更严格
    #   ——不是放宽，是数据只有这么长；缺数据一律 unknown，不许默认「低稀释」。
    def val_grade(d):
        dil, fdv = d.get("dil_180"), d.get("fdv_mc")
        n = d.get("n180") or 0
        if n == 0 and fdv is None:
            return "unknown"
        # 红线优先：已知 180 天稀释已超 15%，无论 FDV 多低都是高危
        if dil is not None and dil >= 15:
            return "red"
        if fdv is not None and fdv >= 5:
            return "red"
        if n == 0:
            return "unknown"        # 无解锁事件记录 → 不能判「低稀释」
        if (dil or 0) >= 8 or (fdv or 0) >= 3:
            return "amber"
        return "green"

    for d in data:
        d["val_grade"] = val_grade(d)

    # ── L5-B 价值捕获状态 ─────────────────────────────────────────────────
    #   `fee30 / rev30 / hold30` 三个口径不可互换：
    #     fee=用户付的总手续费；rev=协议留存；hold=分给持币者
    #   「收费但不留存」(fee>0 & rev=0) 是**负面信号**：协议在干活，但代币分不到钱。
    #   实测 ZRO fee 32.7 万/30d 而 rev=0、GRT fee 1.5 万而 rev=0。
    #   ⚠️ 也可能是 DeFiLlama 未配置该协议的收入口径（数据缺口）——须人工复核，
    #      不得直接下「无价值捕获」的定论。
    def capture(d):
        fee, rev, hold = d.get("fee30"), d.get("rev30"), d.get("hold30")
        if fee is None and rev is None and hold is None:
            return "none"
        if (rev or 0) <= 0 and (fee or 0) > 0:
            return "no_capture"
        if (hold or 0) > 0:
            return "shareholders"
        if (rev or 0) > 0:
            return "protocol_only"
        return "none"

    for d in data:
        d["capture"] = capture(d)

    if args.strict:
        n_before = len(data)
        data = [d for d in data if d["tier"] == "core"]
        if not data and n_before:
            print(f"# 主候选：空\n")
            print(f"七刀全过有 **{n_before}** 只，但**无一达到胜率显著**（`hit_z ≥ 1.96` 且 `hit_rate > 50%`）。")
            print("\n这不是脚本坏了，是统计现实：解锁事件天然小样本，严格显著极难达成。")
            print("→ 去掉 `--strict` 看次级/观察/规避分档；**今日无高确定性机会本身就是结论**。")
            return 0

    if args.json:
        print(json.dumps({"min_mcap_m": args.min_mcap, "n": len(data),
                          "candidates": data}, ensure_ascii=False, indent=2, default=str))
        return 0

    if not data:
        print("# 候选清单：空\n")
        print(f"当前无标的同时满足七刀 + 市值≥{args.min_mcap}M。")
        print("排查顺序：①`--explain` 看卡在哪一刀 ②`--min-mcap 0` 放掉流动性闸 ③确认 MV 已刷新")
        return 0

    print(f"# 选币漏斗 L0 候选清单（{len(data)} 只）\n")
    print(f"> 口径：七刀全过 + 市值 ≥ {args.min_mcap}M ｜ MV 数据日 {mv_asof} ｜ MV 构建 {mv_built:%Y-%m-%d %H:%M}")
    print("> ⚠️ 这是**深研名单**，不是买入信号。每只仍须走 L1–L5 五层 + L6 决策。")

    HEAD = ("| # | 标的 | 市值 | 弹性 | 弹性n | AR72 | 胜率 | z | 解锁n | 反应 | 资金 | 未来解锁 | 下次解锁 | CMC占比 | 主导受益人 | 评分 |\n"
            "|---|------|-----:|------|------:|-----:|-----:|--:|------:|------|------|---------:|---------|--------:|-----------|-----:|")

    def row(i, d):
        return (f"| {i} | **{d['symbol']}** | {d['mcap_m']}M | {d['elasticity_grade']} | {d['e_n_events']} | "
                f"{d['ar72']} | {(d['hit'] or 0)*100:.0f}% | {d['hit_z']} | {d['u_n_events']} | "
                f"{d['reaction_grade'] or '-'} | {d['flow_grade']} | {d['unlock_fut_n']} | "
                f"{d['next_date'] or '-'} | {d['cmc_pct']}% | {d['next_bene']} | {d['score']} |")

    groups = [
        ("core", "🎯 主候选（命中率显著 >50%，z≥1.96）—— 可进 L1–L5 深研"),
        ("secondary", "🔸 次级候选（z≥1.0 且胜率≥60%，未达显著）—— 方向明确但样本薄，只可小仓试错/重点观察"),
        ("watch", "👀 观察池（无方向证据）—— 仅跟踪，**不得标高确定性**"),
        ("avoid", "🚫 规避池（命中率显著为负，z≤−1.96）—— 解锁后大概率跑输大盘"),
        ("unknown", "❔ 无方向样本 —— 无法判定"),
    ]
    for key, title in groups:
        sub = [d for d in data if d["tier"] == key]
        if not sub:
            continue
        print(f"\n## {title}｜{len(sub)} 只\n")
        print(HEAD)
        for i, d in enumerate(sub, 1):
            print(row(i, d))

    print("\n## L5-A 预检：稀释与抛压（本地口径，非收入估值）\n")
    print("> `FDV/MC` = 全稀释市值 ÷ 流通市值（越大稀释空间越大）｜`180d稀释` = **已知**未来 180 天解锁额 ÷ 流通市值"
          "（下限，非年度）｜`距ATH` **不是估值指标**，仅供参考\n")
    print("| 标的 | 市值 | FDV/MC | 180d稀释 | 已知解锁数 | insider占比 | 距ATH | 分级 |")
    print("|------|-----:|-------:|---------:|-----------:|------------:|------:|------|")
    VG = {"red": "🔴 高稀释", "amber": "🟡 中", "green": "🟢 低", "unknown": "❔ 无解锁数据"}
    for d in data:
        print(f"| **{d['symbol']}** | {d['mcap_m']}M | {d['fdv_mc'] or '—'}x | "
              f"{d['dil_180'] if d['dil_180'] is not None else '—'}% | {d['n180']} | "
              f"{d['insider_pct_180'] if d['insider_pct_180'] is not None else '—'}% | "
              f"{('−' + str(d['dd_ath']) + '%') if d['dd_ath'] is not None else '—'} | {VG[d['val_grade']]} |")

    print("\n## L5-B 收入估值（DeFiLlama 口径，覆盖仅 18%）\n")
    print(f"> `fee30d` 用户总手续费 ｜ `rev30d` 协议留存 ｜ `hold30d` 分给持币者 ｜ "
          f"`P/S` = 流通市值 ÷ 年化收入 ｜ 全池 P/S 中位数 **{ps_median if ps_median is not None else '—'}x**")
    print("> ⚠️ 覆盖只有 17.8%（684 只中 122 只）。`—` = **无该口径或上游未收录，不是收入为 0**，")
    print("> 此时「贵不贵」**无法判断**，不得当作便宜。\n")
    print("| 标的 | fee30d | rev30d | hold30d | P/S | 价值捕获 |")
    print("|------|-------:|-------:|--------:|----:|----------|")
    CAP = {
        "shareholders": "✅ 有收入且分给持币者",
        "protocol_only": "🟡 有收入但未分给持币者",
        "no_capture": "🔴 收费但不留存（**或 DL 未配置口径，须复核**）",
        "none": "❔ 无数据，无法判断",
    }

    def money(v):
        if v is None:
            return "—"
        v = float(v)
        if v == 0:
            return "$0"          # 0 是上游明确给出的值，与 —（无数据）语义不同
        return f"${v/1e6:.1f}M" if abs(v) >= 1e6 else f"${v/1e3:.0f}K"

    for d in data:
        ps = d.get("ps")
        ps_s = f"{ps}x" if ps is not None else "—"
        mark = ""
        if ps is not None and ps_median and float(ps) > float(ps_median) * 3:
            mark = " ⚠️偏贵"
        print(f"| **{d['symbol']}** | {money(d['fee30'])} | {money(d['rev30'])} | {money(d['hold30'])} | "
              f"{ps_s}{mark} | {CAP[d['capture']]} |")

    print("\n## 解读要点（AI 必做，不得跳过）\n")
    for d in data:
        notes = []
        t = {"core": "✅ 命中率显著", "secondary": "🔸 胜率高但未达显著（样本薄）",
             "watch": "⚠️ 无方向证据", "avoid": "❌ 命中率显著为负", "unknown": "❔ 无方向样本"}[d["tier"]]
        notes.append(t)
        if (d["u_n_events"] or 0) < 7:
            notes.append(f"解锁样本 n={d['u_n_events']} <7，推断力弱、不外推")
        if d["next_bene"] in ("team", "investor"):
            notes.append("下次解锁由 team/investor 主导 → **内部抛压风险**")
        if (d["cmc_pct"] or 0) >= 3:
            notes.append(f"CMC 口径下次解锁占流通 {d['cmc_pct']}%，接近 5% 预警线")
        if (d["mcap_m"] or 0) < 50:
            notes.append("小市值，流动性与操纵风险偏高")
        # L5-A 结论必须进解读，不能只躺在表里
        if d["val_grade"] == "red":
            why = []
            if (d["dil_180"] or 0) >= 15:
                why.append(f"已知 180 天稀释 {d['dil_180']}% ≥ 15% 红线")
            if (d["fdv_mc"] or 0) >= 5:
                why.append(f"FDV/流通 {d['fdv_mc']}x ≥ 5x")
            notes.append("🔴 **L5 否决**：" + "、".join(why))
        elif d["val_grade"] == "unknown":
            notes.append("❔ L5 无法判断（无解锁事件记录），**不得默认低稀释**")
        if (d["insider_pct_180"] or 0) >= 50:
            notes.append(f"未来 180 天解锁 **{d['insider_pct_180']}% 流向 team/investor** → 内部抛压主导")
        # L5-B 结论
        if d["capture"] == "no_capture":
            notes.append("🔴 收了手续费但协议不留存收入 → **价值不捕获**（或 DL 未配置口径，须人工复核）")
        elif d["capture"] == "protocol_only":
            notes.append("🟡 有协议收入但未分配给持币者 → 币价缺乏现金流支撑")
        elif d["capture"] == "none":
            notes.append("❔ L5-B 无数据（上游未收录），**贵不贵无法判断**")
        if d.get("ps") is not None and ps_median and float(d["ps"]) > float(ps_median) * 3:
            notes.append(f"⚠️ P/S {d['ps']}x 超全池中位数（{ps_median}x）3 倍，**显著偏贵**")
        print(f"- **{d['symbol']}**：" + "；".join(notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
