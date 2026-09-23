#!/usr/bin/env python3
"""场景标签口径错位（审计 v0.8 B4）修复探针。

运行：python workbench/test_scan_scenario_label.py
      （离线部分恒可跑；能连生产库时自动追加**只读**库校验，连不上只跳过、不判失败）

缺陷：`biz.scan_signal.scenario` 混有两套**互相冲突**的编码，共用 S1..S8 编号空间：
  · 生产口径（`scan_daemon._compute_l2`）：只用 (p_dir, oi_dir) 两维 → S1..S4
  · 设计口径（`phase_scan_main_pool.py`，未被 daemon 调度）：含 cvd_dir 三维 → S1..S8
生产 S2 = 价↓+OI↑（真实空头），而设计 S2 = 价↑+OI↑+CVD↓（诱多）——同编号语义相反。
渲染层原先统一取设计口径文案表 ⇒ 生产 S2 被标成「诱多」，方向判读完全反了。

判别难点：生产行**也落 `cvd_dir`**（供渲染），故不能靠「有无 CVD 维」区分来源；
本探针断言的判据是 `PROD_SCENARIO_BY_DIMS[(p_dir, oi_dir)]` 是否等于行内 `scenario`。
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
import send_scan_signal_brief as brief  # noqa: E402

passed = 0
failed = 0
skipped = 0


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


def skip(name, reason):
    global skipped
    skipped += 1
    print(f"  \u2013 跳过 {name}：{reason}")


SRC = open(sd.__file__, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _func(name: str):
    return next((n for n in TREE.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


def _sig(scenario, p_dir, oi_dir, cvd_dir):
    return {"scenario": scenario, "p_dir": p_dir, "oi_dir": oi_dir, "cvd_dir": cvd_dir}


# ═══════════════════════════════════════════════════════════════
#  一、生产口径行：只用 (p_dir, oi_dir)，必须走生产文案表
# ═══════════════════════════════════════════════════════════════
print("\n【B4】生产口径行（daemon _compute_l2，两维）")
for p, o, sc, want in (("up", "up", "S1", "S1 多头进攻"),
                       ("down", "up", "S2", "S2 空头扎实"),
                       ("up", "down", "S3", "S3 多头减仓"),
                       ("down", "down", "S4", "S4 空头兑现")):
    # CVD 维在生产行是**渲染用**附带信息，不得影响编号归属（两向都试）
    for cvd in ("up", "down"):
        got = sd._scenario_label(_sig(sc, p, o, cvd))
        check(got == want, f"生产 {p}/{o}（cvd={cvd}）→ {want}", f"实得 {got}")

check(sd.PROD_SCENARIO_BY_DIMS[("down", "up")] == "S2"
      and sd.PROD_SCENARIO_DESC["S2"] == "空头扎实",
      "生产 S2 语义为「价↓+OI↑ = 空头扎实」（非设计口径的「诱多」）")

# ═══════════════════════════════════════════════════════════════
#  二、设计口径行：含 CVD 维，按三维重算编号
# ═══════════════════════════════════════════════════════════════
print("\n【B4】设计口径行（phase_scan_main_pool，三维）")
for p, o, c, sc, want in (("up", "up", "up", "S1", "S1 多头进攻"),
                          ("up", "up", "down", "S2", "S2 诱多"),
                          ("down", "up", "down", "S3", "S3 空头扎实"),
                          ("down", "up", "up", "S4", "S4 诱空"),
                          ("up", "down", "up", "S5", "S5 多头兑现"),
                          ("up", "down", "down", "S6", "S6 修复反弹"),
                          ("down", "down", "down", "S7", "S7 跌势衰竭"),
                          ("down", "down", "up", "S8", "S8 见底反弹")):
    got = sd._scenario_label(_sig(sc, p, o, c))
    check(got == want, f"设计 {p}/{o}/{c} → {want}", f"实得 {got}")

# ═══════════════════════════════════════════════════════════════
#  三、判别有效性：同一 scenario 值在两套编码下必须渲染出不同文案
# ═══════════════════════════════════════════════════════════════
print("\n【B4】判别有效性（同编号、两套编码 → 文案必须不同）")
for sc, prod_dims, design_dims in (("S2", ("down", "up", "down"), ("up", "up", "down")),
                                   ("S3", ("up", "down", "up"), ("down", "up", "down")),
                                   ("S4", ("down", "down", "up"), ("down", "up", "up"))):
    a = sd._scenario_label(_sig(sc, *prod_dims))
    b = sd._scenario_label(_sig(sc, *design_dims))
    check(a != b, f"同 scenario={sc} 的两套行渲染出不同文案", f"a={a} b={b}")
    check(a.split()[0] == b.split()[0] == sc, f"{sc} 编号保持不变（只纠正文案）",
          f"a={a} b={b}")

# ═══════════════════════════════════════════════════════════════
#  四、非主池编号与缺值兜底
# ═══════════════════════════════════════════════════════════════
print("\n【B4】池内编号与缺值兜底")
check(sd._scenario_label({"scenario": "BRK"}) == "BRK 蓄势突破", "BRK 走池内文案")
check(sd._scenario_label({"scenario": "ACC"}) == "ACC 蓄势(吸筹?)", "ACC 走池内文案")
check(sd._scenario_label(_sig("SQZ_CHURN", "up", "up", "up")) == "SQZ_CHURN",
      "轧空池 SQZ_* 原样返回（不参与 S 编号映射）")
check(sd._scenario_label({"scenario": None}) == "-", "无场景 → '-'（不抛异常）")
check(sd._scenario_label({"scenario": "S5", "p_dir": None, "oi_dir": None}) == "S5",
      "维度缺失时不臆造文案（原样返回编号）")

# ═══════════════════════════════════════════════════════════════
#  五、渲染层接线：卡片与图例
# ═══════════════════════════════════════════════════════════════
print("\n【B4】渲染层接线（卡片文案 + 图例口径）")


def _res():
    return {"event": [], "catalyst": [], "kol": [],
            "catalyst_dir": {"bullish": 0, "bearish": 0, "neutral": 0},
            "catalyst_dir_fresh": {}, "catalyst_raw": 0, "catalyst_latest": None,
            "catalyst_stale": 0, "catalyst_all": [], "asset_linked": True}


def _main_sig(scenario, p_dir, oi_dir, cvd_dir):
    return {"id": 1, "symbol": "XUSDT", "pool": "main", "scenario": scenario,
            "timeframe": "15m", "p_dir": p_dir, "price_chg_pct": 3.20,
            "vol_ratio": 2.01, "oi_dir": oi_dir, "oi_chg_pct": 5.0,
            "cvd_dir": cvd_dir, "cvd_usd": -31684.0, "cvd_ratio": -0.144,
            "funding_rate": 0.00005, "confidence": "high", "status": "active",
            "context_tags": ["lv2_15m"], "trigger_price": 0.02384,
            "stop_loss_pct": 8.0, "breakout_px": 0.024}


_html_prod = sd._render_alert_email(
    [{"signal": _main_sig("S2", "down", "up", "down"), "resonance": _res()}],
    ["btc_1h=up(+0.06%)"])
_html_design = sd._render_alert_email(
    [{"signal": _main_sig("S2", "up", "up", "down"), "resonance": _res()}],
    ["btc_1h=up(+0.06%)"])
_body_prod = _html_prod.split("图例：")[0]
_body_design = _html_design.split("图例：")[0]
check("S2 空头扎实" in _body_prod, "生产 S2 卡片标「空头扎实」")
check("S2 诱多" not in _body_prod, "生产 S2 卡片不得出现「诱多」（原缺陷）")

check("S2 诱多" in _body_design, "设计 S2 卡片标「诱多」")
check("S2 空头扎实" not in _body_design, "设计 S2 卡片不得出现「空头扎实」")

check("S1 多头进攻" in _html_prod and "S4 空头兑现" in _html_prod,
      "图例列生产口径 S1..S4 文案")
check("S5..S8" in _html_prod, "图例声明回测口径另含 CVD 维（S5..S8）")
check("S2 诱多" not in _html_prod.split("图例：")[1]
      or "S2 空头扎实" in _html_prod.split("图例：")[1],
      "图例不再把生产 S2 说成「诱多」")

# ═══════════════════════════════════════════════════════════════
#  六、AST：渲染层与先验分组必须按维度，不得按 scenario 编号
# ═══════════════════════════════════════════════════════════════
print("\n【B4】AST —— 渲染/先验按维度分组，不按 scenario 编号")
_render = _func("_render_alert_email")
_r_src = ast.unparse(_render) if _render else ""
check("_scenario_label(sig)" in _r_src, "渲染循环调 _scenario_label(sig) 重算文案")
check("SCENARIO_DESC.get(sc" not in _r_src,
      "渲染循环不再直接查设计口径文案表（原缺陷点）")

_priors = _func("_scenario_priors")
_p_src = ast.unparse(_priors) if _priors else ""
check("quadrants" in _p_src, "_scenario_priors 按象限（quadrants）取参")
check("(r['p_dir'], r['oi_dir'])" in _p_src, "分桶键为 (p_dir, oi_dir) 而非 scenario")
check("sg.scenario = ANY" not in _p_src, "SQL 不再按 scenario 过滤（两套编码同编号会串桶）")
check("sg.oi_dir IS NOT NULL" in _p_src, "SQL 要求 oi_dir 非空（分桶前提）")

_alert = _func("task_scan_alert")
_a_src = ast.unparse(_alert) if _alert else ""
check("_scenario_priors(conn, quadrants)" in _a_src, "task_scan_alert 传象限给先验查询")

# ═══════════════════════════════════════════════════════════════
#  七、日报脚本（send_scan_signal_brief）同口径
# ═══════════════════════════════════════════════════════════════
print("\n【B4】日报脚本同口径")
check(brief._scenario_label({"scenario": "S2", "p_dir": "down", "oi_dir": "up",
                             "cvd_dir": "down"}) == ("S2", "空头扎实"),
      "日报：生产 S2 → 「空头扎实」")
check(brief._scenario_label({"scenario": "S2", "p_dir": "up", "oi_dir": "up",
                             "cvd_dir": "down"}) == ("S2", "诱多"),
      "日报：设计 S2 → 「诱多」")
check(brief._scenario_label({"scenario": "BRK"}) == ("BRK", "蓄势突破"),
      "日报：BRK 走池内文案")
check(brief.PROD_SCENARIO_BY_DIMS == sd.PROD_SCENARIO_BY_DIMS
      and brief.PROD_SCENARIO_DESC == sd.PROD_SCENARIO_DESC
      and brief.DESIGN_SCENARIO_BY_DIMS == sd.DESIGN_SCENARIO_BY_DIMS
      and brief.DESIGN_SCENARIO_DESC == sd.DESIGN_SCENARIO_DESC,
      "日报与 daemon 的两套口径表逐字一致（防单侧漂移）")

# ═══════════════════════════════════════════════════════════════
#  八、生产库不变量（只读）：每行标签编号必须与自身维度自洽
# ═══════════════════════════════════════════════════════════════
try:
    import psycopg.rows  # noqa: E402

    with sd._db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT id, scenario, p_dir, oi_dir, cvd_dir "
                "FROM biz.scan_signal WHERE pool = 'main' "
                "AND created_at > NOW() - INTERVAL '30 days'"
            )
            db_rows = cur.fetchall()
except Exception as exc:  # noqa: BLE001
    skip("生产库不变量（B4）", f"无法连库：{type(exc).__name__}: {exc}")
    db_rows = None

if db_rows is not None:
    if not db_rows:
        skip("生产库不变量（B4）", "近 30 天无主池行，本项不可判定")
    else:
        bad = []
        fixed = 0
        for r in db_rows:
            p, o, c = r["p_dir"], r["oi_dir"], r["cvd_dir"]
            if p not in ("up", "down") or o not in ("up", "down"):
                continue
            lab = sd._scenario_label(r)
            num = lab.split()[0]
            allowed = {sd.PROD_SCENARIO_BY_DIMS[(p, o)]}
            if c in ("up", "down"):
                allowed.add(sd.DESIGN_SCENARIO_BY_DIMS[(p, o, c)])
            if num not in allowed:
                bad.append((r["id"], r["scenario"], p, o, c, lab))
            # 原实现（统一设计口径文案）与本实现的差异行数
            if r["scenario"] in sd.DESIGN_SCENARIO_DESC:
                legacy = sd.DESIGN_SCENARIO_DESC[r["scenario"]]
                if lab.split(" ", 1)[-1] != legacy:
                    fixed += 1
        print(f"    （近 30 天主池行 {len(db_rows)} 条，其中 {fixed} 条原被标错文案）")
        check(not bad, "每行标签编号与自身 (p_dir, oi_dir[, cvd_dir]) 自洽",
              f"不自洽行：{bad[:10]}")
        check(fixed > 0, "库内确有两套编码并存（否则本探针无覆盖意义）",
              f"差异行数={fixed}")

print(f"\n结果：{passed} 通过 / {failed} 失败 / {skipped} 跳过")
sys.exit(1 if failed else 0)
