#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""候选否决体检（L0-B）—— 任何来源的标的，30 秒内判定「能不能碰」。

── 为什么是「否决」而不是「选币」────────────────────────────────────
2026-10-27 用 biz.asset_klines（1d，529 币，2022-01~2026-10，57 个调仓日、
10,974 条样本）回测了价格类横截面因子，结论是否定的：

  回看期  rs>0 组 30d 超额   基线      Q5−Q1 价差   分年度符号
  7d      −3.54%            −3.40%    +0.67pp      翻转（2024 −6.7pp）
  30d     −2.91%            −3.40%    +2.22pp      翻转（2024 −7.9pp）
  90d     −3.21%            −3.40%    +1.61pp      翻转（2024 −4.1pp）

  且最强五分位 Q5 的**中位数全部差于**最弱分位 Q1（如 90d：−9.81% vs −9.29%），
  分年度超额在 −7.9pp ~ +3.2pp 间乱跳 —— 这是噪音的形态，不是因子的形态。
  ⇒ 用涨幅榜/相对强度选币，等于用随机数选币。

唯一稳定的事实是**基线为负**：任一五分位、任一回看期，山寨 30 天都跑输 BTC
约 3~5%，胜率仅 33%。所以这套工具的价值不在「多抓到机会」，而在
**「少犯结构性错误」** —— 不做预测，只拦已知的亏钱结构。

── 拦截项与依据 ────────────────────────────────────────────────
V1 流动性     市值/排名缺失或过低          → 无法成交、易被操纵
V2 稀释       180d 已知稀释 ≥15% 或 FDV/MC ≥5x → 供给冲击（ARKM 实测 39.8%）
V3 内部抛压   180d 解锁中 team/investor ≥50%   → 抛压来源明确
V4 价值捕获   fee>0 且 revenue=0          → 协议干活但代币分不到钱
V5 估值       P/S > 池中位数 3 倍          → 显著偏贵（覆盖仅 18%，缺数据不拦）
V6 相对强度   rs_90d 落入池内最差五分位    → 接飞刀（Q1 均值 −5.27%，为各档最差）
V7 解锁体质   reaction_grade 属「解禁即崩」 → 历史解禁即崩

⚠️ 全部依据**历史已发生的数据**，不含任何预测假设；因此它只能否，不能选。
⚠️ V2/V4/V6 在数据缺失时一律 WARN（无法排除），**不得默认通过**。

用法：
  py -3.12 veto_check.py ARB ATH          # 体检指定标的
  py -3.12 veto_check.py --all            # 扫全池，列出「零 FAIL」名单
  py -3.12 veto_check.py ARB --json
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

POOL_WHERE = "m.market_cap_rank IS NOT NULL AND COALESCE(m.market_cap,0) >= 10000000"

L5_LATERAL = """
LEFT JOIN LATERAL (
    SELECT sum(e.unlock_value_usd) FILTER (WHERE e.unlock_date <= CURRENT_DATE + 180) AS u180,
           -- 🔴 HC-014（2026-10-27）：只认「事件级精确来源」。
           --    asset_level_only 是该资产**全生命周期**分配构成的主桶，与「本轮解锁
           --    流向谁」是两件事 —— 实测 157 个双全样本仅三分之一一致（三分之二判反），
           --    典型如 ARKM 资产级=community 但事件级 insider 占 63、ATH 更达 86。
           --    拿它算内部抛压会把风险标的洗成安全，属方向性错误，故显式排除。
           sum(e.unlock_value_usd) FILTER (WHERE e.unlock_date <= CURRENT_DATE + 180
                 AND e.beneficiary_source IN ('event_recipients','label','tokenomics_unlock_hero')
                 AND e.beneficiary_type IS NOT NULL)                                 AS u180_typed,
           count(*) FILTER (WHERE e.unlock_date <= CURRENT_DATE + 180)                AS n180,
           sum(e.unlock_value_usd) FILTER (WHERE e.unlock_date <= CURRENT_DATE + 180
                 AND e.beneficiary_source IN ('event_recipients','label','tokenomics_unlock_hero')
                 AND e.beneficiary_type IN ('team','investor'))                       AS insider180
    FROM biz.asset_unlock_event e
    WHERE e.asset_id = m.asset_id AND e.unlock_date >= CURRENT_DATE
) u ON TRUE
-- 下一期「有明细」的解锁：当 180d 全窗口覆盖率不足时，这一条往往才是决策相关的
-- （近端解锁决定短期抛压；远端没有明细不代表近端未知）。只取已有的精确 type，
-- 不做任何推断。
LEFT JOIN LATERAL (
    SELECT e2.unlock_date, e2.beneficiary_type,
           e2.beneficiary_source,
           NULLIF(e2.raw_ref->'beneficiary_parse'->>'insider_pct', '')::numeric AS ins_pct
    FROM biz.asset_unlock_event e2
    WHERE e2.asset_id = m.asset_id
      AND e2.unlock_date >= CURRENT_DATE
      AND e2.beneficiary_source IN ('event_recipients', 'label', 'tokenomics_unlock_hero')
      AND e2.beneficiary_type IS NOT NULL
    ORDER BY e2.unlock_date
    LIMIT 1
) nx ON TRUE
"""

# 🔴 本 SQL 会被 psycopg 参数化（execute 带 params），因此**任何裸 `%` 都会报
#    `incomplete placeholder`** —— 注释里也不许出现百分号，必须写成文字或 `%%`。
#    （2026-10-27 已踩两次：HC-014 第一版与 insider_all 注释。）
SELECT_SQL = """
SELECT m.symbol, m.name, m.asset_id, m.market_cap, m.market_cap_rank,
       m.primary_sector, m.elasticity_grade, m.u_n_events, m.reaction_grade,
       m.rs_90d, m.flow_grade,
       round((a.total_supply / NULLIF(a.circulating_supply,0))::numeric, 2) AS fdv_mc,
       round(COALESCE(u.u180,0) / NULLIF(m.market_cap,0) * 100, 1)          AS dil_180,
       COALESCE(u.n180,0)                                                  AS n180,
       round(COALESCE(u.u180_typed,0) / NULLIF(COALESCE(u.u180,0),0) * 100, 0) AS insider_cov,
       round(COALESCE(u.insider180,0) / NULLIF(COALESCE(u.u180_typed,0),0) * 100, 1) AS insider_pct_180,
       -- insider 占**全部**解锁额（含未标注部分）的比例：量级参考。
       -- 与 insider_pct_180（占已标注部分）搭配看：
       --   覆盖率低 + 占已标注百分之百 ≠ 全部都是内部人，只是「已知的都是」。
       round(COALESCE(u.insider180,0) / NULLIF(COALESCE(u.u180,0),0) * 100, 1) AS insider_all,
       -- 存量参考：该资产全生命周期里团队+投资人的占比（asset_allocation_mix）。
       -- 与 insider_pct_180（未来 180d 解锁流向）含义不同，只作背景，不参与判定。
       am.insider_pct                                                      AS stock_insider_pct,
       -- 下一期有明细的解锁（见 L5_LATERAL 的 nx）；用于覆盖率不足时的近端参考
       nx.unlock_date                                                      AS next_known_date,
       nx.beneficiary_type                                                 AS next_known_type,
       nx.ins_pct                                                          AS next_known_insider_pct,
       COALESCE(m.unlock_fut_n,0)                                          AS fut_n,
       r.fees_30d, r.revenue_30d, r.holders_rev_30d,
       round((m.market_cap / NULLIF(r.revenue_30d * 12.1667, 0))::numeric, 1)       AS ps,
       round((m.market_cap / NULLIF(r.holders_rev_30d * 12.1667, 0))::numeric, 1)   AS ps_holders
FROM biz.mv_asset_screen m
JOIN core.asset a ON a.asset_id = m.asset_id
LEFT JOIN biz.asset_allocation_mix am ON am.asset_id = m.asset_id
LEFT JOIN biz.asset_revenue_snapshot r
       ON r.asset_id = m.asset_id AND r.source_code = 'dl'
{L5}
WHERE {where}
ORDER BY m.market_cap DESC NULLS LAST
"""


def verdict(row: dict, ctx: dict) -> tuple[list[str], list[str], list[str]]:
    """返回 (fail_reasons, warn_reasons, gap_reasons)

    为什么要三档而不是两档
        「缺数据」和「有风险」是两件事。若混成一档，实测 684 只里 0 只能过
        （82% 的标的没有收入数据，永远带警示）—— 那就等于永远无法通过，
        失去区分度。故拆出 GAP：**无法判断 ≠ 有风险 ≠ 没风险**。
    """
    fails, warns, gaps = [], [], []

    # V1 流动性
    if row["market_cap"] is None or float(row["market_cap"]) < 10e6 or row["market_cap_rank"] is None:
        fails.append("V1 市值<10M 或无排名 → 流动性不足/易操纵")

    # V2 稀释
    dil, fdv, n180 = row["dil_180"], row["fdv_mc"], row["n180"] or 0
    v2_failed = False
    if dil is not None and float(dil) >= 15:
        fails.append(f"V2 已知 180d 稀释 {dil}% ≥ 15% 红线")
        v2_failed = True
    if fdv is not None and float(fdv) >= 5:
        fails.append(f"V2 FDV/流通 {fdv}x ≥ 5x")
        v2_failed = True
    if n180 == 0:
        gaps.append("V2 无未来解锁记录 → 无法排除高稀释（不得当作低稀释）")
    elif not v2_failed and (float(dil or 0) >= 8 or float(fdv or 0) >= 3):
        warns.append(f"V2 中高稀释（180d {dil}% / FDV {fdv}x）")

    # V3 内部抛压
    #   HC-014（2026-10-27）修复后口径：只认事件级精确来源（event_recipients /
    #   label / tokenomics_unlock_hero），资产级生命周期推断一律不计入（实测 67% 判反）。
    #   修复前：180d 窗口 1199 事件仅 6.6% 有 type、362 只资产仅 40 只（11%）可算。
    #   修复后覆盖率见运行时读数；覆盖率不足时必须报 GAP，不得默认通过。
    ins, cov = row["insider_pct_180"], row["insider_cov"]
    stock = row.get("stock_insider_pct")
    if not row["n180"]:
        pass  # 无未来解锁，本项不适用（已由 V2 的 gap 提示）
    elif cov is None or float(cov) < 50:
        msg = (f"V3 内部抛压无法判断：未来 180d 仅 {cov or 0}% 解锁额有事件级受益人明细"
               f"（已标注部分 insider 占 {ins if ins is not None else '—'}%）")
        # 全窗口覆盖率不足时，把「最近一期已知明细」单列出来 —— 远端无明细
        # 不代表近端未知，而近端才是短期抛压的来源。
        if row.get("next_known_date"):
            nd = row["next_known_date"]
            nt = row["next_known_type"]
            ni = row.get("next_known_insider_pct")
            seg = f"｜下一期 {nd} 已知主导={nt}"
            if ni is not None:
                seg += f"、insider {float(ni):.1f}%"
            msg += seg + "（仅该期，不外推整窗）"
        if stock is not None:
            msg += f"｜存量参考：全生命周期 insider 占 {stock}%（非本次流向）"
        gaps.append(msg)
    elif float(ins or 0) >= 50:
        # 文案必须区分「占已标注」与「占全部」：覆盖 50% 时说成「100% 流向内部人」
        # 是把半句真话当结论。故同时给出覆盖率与占全额的量级。
        allp = row.get("insider_all")
        fails.append(
            f"V3 内部抛压主导：已标注的解锁额中 insider 占 {ins}%"
            f"（明细覆盖 {cov:.0f}%），折算占全部未来解锁 {allp if allp is not None else '—'}%"
        )
    elif float(ins or 0) >= 30:
        warns.append(f"V3 insider 占未来解锁 {ins}%（覆盖率 {cov}%）偏高")

    # V4 价值捕获
    fee, rev, hold = row["fees_30d"], row["revenue_30d"], row["holders_rev_30d"]
    if fee is None or rev is None:
        gaps.append("V4 无收入数据 → 贵不贵无法判断（不得当作便宜）")
    elif float(fee) > 0 and float(rev) <= 0:
        warns.append("V4 收费但不留存收入 → 价值不捕获（或 DL 未配置口径，须复核）")
    elif float(rev) > 0 and (hold is None or float(hold) <= 0):
        warns.append("V4 有协议收入但未分给持币者")

    # V5 估值
    ps, med = row["ps"], ctx["ps_median"]
    if ps is not None and med and float(ps) > float(med) * 3:
        warns.append(f"V5 P/S {ps}x > 池中位 {med}x 的 3 倍 → 显著偏贵")

    # V6 相对强度最差分位
    rs, cut = row["rs_90d"], ctx["rs_p20"]
    if rs is None:
        gaps.append("V6 无 90d 相对强度数据")
    elif cut is not None and float(rs) <= float(cut):
        warns.append(f"V6 rs_90d {rs} 落入池内最差 20%（≤{cut}）→ 接飞刀")

    # V7 解锁体质
    rg = row["reaction_grade"]
    if rg in ("偏解禁即崩", "解禁即崩"):
        warns.append(f"V7 解锁反应体质「{rg}」")

    return fails, warns, gaps


def main() -> int:
    ap = argparse.ArgumentParser(description="候选否决体检（L0-B）")
    ap.add_argument("symbols", nargs="*", help="标的代码，如 ARB ATH")
    ap.add_argument("--all", action="store_true", help="扫全池，列出零 FAIL 名单")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.symbols and not args.all:
        ap.error("需指定标的，或使用 --all")

    url = get_settings(require_database=True).database_url
    with get_connection(url) as conn, conn.cursor() as cur:
        # 参照系：池内 P/S 中位数 + rs_90d 的 20% 分位
        cur.execute(f"""
            SELECT round(percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY s.market_cap / (r.revenue_30d * 12.1667))::numeric, 1)
            FROM biz.mv_asset_screen s
            JOIN biz.asset_revenue_snapshot r
              ON r.asset_id = s.asset_id AND r.source_code = 'dl'
            WHERE r.revenue_30d > 0 AND s.market_cap_rank IS NOT NULL AND s.market_cap >= 10000000
        """)
        ps_median = cur.fetchone()[0]
        cur.execute(f"""
            SELECT round(percentile_cont(0.2) WITHIN GROUP (ORDER BY m.rs_90d)::numeric, 2)
            FROM biz.mv_asset_screen m
            WHERE {POOL_WHERE} AND m.rs_90d IS NOT NULL
        """)
        rs_p20 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM biz.mv_asset_screen m WHERE " + POOL_WHERE)
        n_pool = cur.fetchone()[0]
        cur.execute("SELECT max(latest_as_of) FROM biz.mv_asset_screen")
        asof = cur.fetchone()[0]

        ctx = {"ps_median": ps_median, "rs_p20": rs_p20}

        if args.all:
            cur.execute(SELECT_SQL.format(where=POOL_WHERE, L5=L5_LATERAL))
        else:
            syms = [s.strip().upper() for s in args.symbols if s.strip()]
            ph = ",".join(["%s"] * len(syms))
            cur.execute(SELECT_SQL.format(where=f"upper(m.symbol) IN ({ph})", L5=L5_LATERAL), syms)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    data = [dict(zip(cols, r)) for r in rows]
    if not data:
        print("未匹配到任何标的（检查代码拼写；同名多资产时请看 symbol 全称）")
        return 1

    for d in data:
        d["fails"], d["warns"], d["gaps"] = verdict(d, ctx)
        if d["fails"]:
            d["level"] = "FAIL"
        elif d["warns"]:
            d["level"] = "WARN"
        elif d["gaps"]:
            d["level"] = "GAP"
        else:
            d["level"] = "PASS"

    if args.json:
        print(json.dumps({"as_of": str(asof), "pool": n_pool, "ctx": ctx,
                          "items": data}, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.all:
        groups = {k: [d for d in data if d["level"] == k]
                  for k in ("PASS", "WARN", "GAP", "FAIL")}
        print(f"# 全池否决体检（{len(data)} 只 ｜ MV 数据日 {asof}）\n")
        print(f"> 参照系：P/S 中位数 **{ps_median or '—'}x** ｜ rs_90d 最差 20% 门槛 **{rs_p20}**")
        print("> ⚠️ PASS = **无已知红旗**，不是买入信号。仍需走 L1–L6。")
        print("> ⚠️ GAP = **数据不足，无法判断**——不等于没风险，也不等于有风险。\n")
        print("| 结果 | 数量 | 占比 | 含义 |")
        print("|---|---:|---:|---|")
        MEAN = {
            "PASS": ("✅ 零红旗", "已查项目全通过，且数据完整"),
            "WARN": ("⚠️ 有警示", "存在已知风险项，须逐条判断"),
            "GAP": ("❔ 数据不足", "关键项缺失，无法下结论"),
            "FAIL": ("🚫 硬否决", "命中结构性亏钱特征，直接弃"),
        }
        for k in ("PASS", "WARN", "GAP", "FAIL"):
            n = len(groups[k])
            lab, desc = MEAN[k]
            print(f"| {lab} | {n} | {n/len(data)*100:.1f}% | {desc} |")

        # 「无硬否决」才是可深研的候选面 —— PASS 因数据缺口几乎不可能全绿
        ok = groups["PASS"] + groups["WARN"] + groups["GAP"]
        print(f"\n**无硬否决 = {len(ok)} 只（{len(ok)/len(data)*100:.1f}%）**，其中数据齐全无警示的只有 "
              f"{len(groups['PASS'])} 只。")

        if groups["PASS"]:
            print(f"\n## ✅ 零红旗清单（{len(groups['PASS'])} 只）\n")
            print("| 标的 | 名称 | 市值 | FDV/MC | 180d稀释 | P/S | 板块 |")
            print("|------|------|-----:|-------:|---------:|----:|------|")
            for d in groups["PASS"][:30]:
                print(f"| **{d['symbol']}** | {(d['name'] or '')[:18]} | "
                      f"{float(d['market_cap'] or 0)/1e6:.0f}M | {d['fdv_mc'] or '—'}x | "
                      f"{d['dil_180'] if d['dil_180'] is not None else '—'}% | "
                      f"{d['ps'] or '—'} | {d['primary_sector'] or '-'} |")

        from collections import Counter
        print("\n## 🚫 硬否决原因分布\n")
        cnt = Counter(r.split(" ")[0] for d in groups["FAIL"] for r in d["fails"])
        for k, v in cnt.most_common(8):
            print(f"- **{k}**：{v} 只")
        print("\n## ❔ 数据缺口原因分布（前 6）\n")
        cnt2 = Counter(r.split(" ")[0] for d in data for r in d["gaps"])
        for k, v in cnt2.most_common(6):
            print(f"- **{k}**：{v} 只")
        return 0

    # 单标的模式
    def dil_disp(d):
        """n180=0 时 u180=0 会被算成 0.0% —— 那是「没有记录」，不是「零稀释」。
        实测 ETH/UNI 均命中此坑：显示 0.0% 会被读成「很安全」，必须显示 —。"""
        if not (d["n180"] or 0):
            return "—（无未来解锁记录）"
        return f"{d['dil_180']}%"

    for d in data:
        icon = {"PASS": "✅ 零红旗", "WARN": "⚠️ 有警示",
                "GAP": "❔ 数据不足", "FAIL": "🚫 硬否决"}[d["level"]]
        print(f"## {d['symbol']} — {icon}\n")
        print(f"- 名称：{d['name']}｜市值 {float(d['market_cap'] or 0)/1e6:.0f}M"
              f"（排名 {d['market_cap_rank']}）｜板块 {d['primary_sector'] or '-'}")
        # HC-014：覆盖率不足时不许把「仅 11% 样本里的 insider 占比」当结论摆在首行，
        #   否则读者只会看到 100.0% 而忽略 11% 的覆盖率 —— 与 HC-012 的假零同源。
        if not (d["n180"] or 0):
            ins_line = ""
        elif d["insider_pct_180"] is None:
            ins_line = "｜insider 占比 —（无事件级明细）"
        elif (d["insider_cov"] or 0) < 50:
            ins_line = f"｜insider 占比 ❔无法判断（明细仅覆盖 {d['insider_cov'] or 0:.0f}%）"
        else:
            ins_line = (f"｜insider 占比 {d['insider_pct_180']}%"
                        f"（覆盖 {d['insider_cov']:.0f}%）")
        fut_line = f"（未来解锁 {d['fut_n']} 个）" if (d["n180"] or 0) else ""
        print(f"- FDV/流通 {d['fdv_mc'] or '—'}x｜180d 稀释 {dil_disp(d)}{fut_line}{ins_line}")
        print(f"- 收入：fee30d={d['fees_30d']}｜rev30d={d['revenue_30d']}"
              f"｜hold30d={d['holders_rev_30d']}｜P/S={d['ps'] or '—'}")
        print(f"- 90d 相对强度 {d['rs_90d']}（池内最差门槛 {rs_p20}）｜资金 {d['flow_grade'] or '—'}")
        if d["fails"]:
            print("\n**硬否决：**")
            for r in d["fails"]:
                print(f"- 🚫 {r}")
        if d["warns"]:
            print("\n**警示：**")
            for r in d["warns"]:
                print(f"- ⚠️ {r}")
        if d["gaps"]:
            print("\n**数据缺口（无法判断，不得当作通过）：**")
            for r in d["gaps"]:
                print(f"- ❔ {r}")
        if d["level"] == "PASS":
            print("\n**未触发任何否决项，且数据完整** —— 仍只是「无已知红旗」，不等于值得买。")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
