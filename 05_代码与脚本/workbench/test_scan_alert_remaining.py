#!/usr/bin/env python3
"""告警邮件「残留 4 项」复验探针（审计_盘面异动告警邮件_2026-09-22 §七 复测清单）。

运行：python workbench/test_scan_alert_remaining.py
      （离线部分恒可跑；能连生产库时自动追加**只读**库校验，连不上只跳过、不判失败）

为什么需要它（PROC-1）：审计判 P2-2 / P2-7 / P2-9「残留」，但三项实际已由
`8ca1258`（`--run-once` 移入单实例锁内 + 补写任务心跳）与 `44b407f`（BRK 同根
`bar=` 精确去重 + 补算 `stop_loss_pct`）修复并部署（部署判据见 AGENTS.md ⑤/⑥）。
「已修」这个结论此前只存在于对话里、外部无法独立复跑 —— 本探针把它变成可执行判据。

判定口径（关键，也是审计误判的成因）：
  * 源码层（AST，离线）：不依赖库，直接断言实现形状（顺序 / 调用存在性）。
  * 库层（只读）：以「`context_tags` 含 `bar=` 标签」作为**修复后产物**的自证标记
    （该标签由 `44b407f` 引入）。**无标记行 = 修复前历史数据，不参与不变量判定**
    —— 否则 09-21 那批 33 条无失效位旧行会让探针永久红。审计用**全表** `2/35`
    反推「生产者未落库」，但没注意那 2 条恰是**最新**两条，正是修复生效的证据。
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


def _called_names(node: ast.AST) -> set:
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


SRC = open(sd.__file__, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _func(name: str) -> ast.FunctionDef | None:
    return next((n for n in TREE.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


# ═══════════════════════════════════════════════════════════════
#  一、P2-2 —— `--run-once` 必须晚于单实例锁（否则绕锁 + 重复发信 + 不写心跳）
# ═══════════════════════════════════════════════════════════════

print("\n【P2-2】`--run-once` 在单实例锁之后执行且写心跳（源码 AST）")
main_fn = _func("main")
check(main_fn is not None, "找到 scan_daemon.main()")

lock_i = ro_i = None
if main_fn:
    for i, st in enumerate(main_fn.body):
        if not isinstance(st, ast.If):
            continue
        if "_acquire_singleton_lock" in _called_names(st.test) and lock_i is None:
            lock_i = i
        if (isinstance(st.test, ast.Attribute) and st.test.attr == "run_once"
                and ro_i is None):
            ro_i = i

check(lock_i is not None, "main() 存在 `_acquire_singleton_lock(...)` 闸门",
      "未找到取锁语句 ⇒ 单实例保护缺失")
check(ro_i is not None, "main() 存在 `if args.run_once:` 分支")
if lock_i is not None and ro_i is not None:
    check(lock_i < ro_i, "取锁语句在 `--run-once` 分支之前（不绕锁）",
          f"lock_stmt_idx={lock_i} run_once_stmt_idx={ro_i} ⇒ 单跑仍在锁外")
    hb = "_write_heartbeat" in _called_names(main_fn.body[ro_i])
    check(hb, "单跑分支成功后补写任务心跳（不留痕会误判「未部署」）",
          "run_once 分支内无 _write_heartbeat 调用")

# ═══════════════════════════════════════════════════════════════
#  二、P2-7 / P2-9 —— BRK 生产者形状（源码 AST）
# ═══════════════════════════════════════════════════════════════

print("\n【P2-7】BRK 生产者有「同根已收盘条」精确去重（源码 AST）")
acc_fn = _func("task_scan_accumulation")
check(acc_fn is not None, "找到 scan_daemon.task_scan_accumulation()")
if acc_fn:
    acc_src = ast.unparse(acc_fn)
    check("bar=" in acc_src and "brk_done" in acc_src,
          "生产者写入 `bar=<open_time>` 标签并按 brk_done 去重",
          "缺 bar= 标签或 brk_done 去重 ⇒ 同一根已收盘 1h 条会被判两次")
    check("closed" in acc_src and "INTERVAL_SECONDS" in acc_src,
          "BRK 只用**已收盘**条判定（未收盘条 vol_ratio 随采样分钟漂移、不可复现）")

print("\n【P2-9】BRK 生产者补算失效位（源码 AST + 夹带单测）")
if acc_fn:
    check("_atr_stop_pct" in _called_names(acc_fn),
          "BRK 分支调用 `_atr_stop_pct` 补算 stop_loss_pct（与主池同口径）",
          "未调用 ⇒ BRK 卡片无失效位（渲染层需 trigger_price AND stop_loss_pct 皆非空）")


def _bar(t, close, high=None, low=None):
    return {"open_time": t, "close_px": close, "quote_vol": 100.0,
            "high_px": high if high is not None else close * 1.002,
            "low_px": low if low is not None else close * 0.998}


from datetime import datetime, timedelta, timezone  # noqa: E402

_T0 = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
_narrow = [_bar(_T0 + timedelta(hours=i), 100.0, high=100.2, low=99.8) for i in range(20)]
_wide = [_bar(_T0 + timedelta(hours=i), 100.0, high=110.0, low=90.0) for i in range(20)]
check(sd._atr_stop_pct(_narrow, 100.0) == sd.STOP_PCT_MIN,
      f"窄幅 → 夹到下限 {sd.STOP_PCT_MIN}%",
      f"got={sd._atr_stop_pct(_narrow, 100.0)}")
check(sd._atr_stop_pct(_wide, 100.0) == sd.STOP_PCT_MAX,
      f"宽幅 → 夹到上限 {sd.STOP_PCT_MAX}%",
      f"got={sd._atr_stop_pct(_wide, 100.0)}")
check(sd._atr_stop_pct(_narrow[:5], 100.0) is None,
      "样本不足 → None（不知道就别说，不兜底）")

# ═══════════════════════════════════════════════════════════════
#  三、P2-7 / P2-9 —— 生产库不变量（只读；连不上则跳过）
# ═══════════════════════════════════════════════════════════════

print("\n【P2-7 / P2-9】生产库不变量（只读，以 `bar=` 标签为修复后产物标记）")


def _bar_tag(tags) -> str | None:
    for t in (tags or []):
        if t.startswith("bar="):
            return t
    return None


try:
    import psycopg.rows  # noqa: E402

    with sd._db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT id, symbol, signal_ts, stop_loss_pct, context_tags, "
                "       EXISTS (SELECT 1 FROM unnest(context_tags) AS t "
                "               WHERE left(t, 4) = 'bar=') AS tagged "
                "FROM biz.scan_signal WHERE scenario = 'BRK' ORDER BY signal_ts"
            )
            rows = cur.fetchall()
except Exception as exc:  # noqa: BLE001
    skip("生产库不变量（P2-7/P2-9）", f"无法连库：{type(exc).__name__}: {exc}")
    rows = None

if rows is not None:
    tagged = [r for r in rows if r["tagged"]]
    if not tagged:
        skip("生产库不变量（P2-7/P2-9）",
             f"库内 {len(rows)} 条 BRK 均无 `bar=` 标签 ⇒ 修复产物尚未出现，本项不可判定")
    else:
        cutoff = min(r["signal_ts"] for r in tagged)
        post = [r for r in rows if r["signal_ts"] >= cutoff]
        pre = [r for r in rows if r["signal_ts"] < cutoff]
        print(f"    （标记 {len(tagged)} 条，标记后 {len(post)} 条，标记前历史 {len(pre)} 条"
              f"，cutoff={cutoff:%Y-%m-%d %H:%M} UTC）")

        # P2-7：标记之后不得再出现无 bar= 标签的 BRK 行（去重机制在跑）
        untagged_post = [r for r in post if not r["tagged"]]
        check(not untagged_post,
              "P2-7 标记之后无「无 bar= 标签」的 BRK 行（去重机制生效）",
              f"越界行：{[ (r['id'], r['symbol']) for r in untagged_post ]}")

        # P2-7：(symbol, bar) 不得重复
        seen: dict[tuple, list] = {}
        for r in tagged:
            seen.setdefault((r["symbol"], _bar_tag(r["context_tags"])), []).append(r["id"])
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        check(not dups, "P2-7 标记后无 (symbol, 同根 bar) 重复判决",
              f"重复组：{dups}")

        # P2-9：标记之后 stop_loss_pct 必须非空且落在夹带内
        missing = [r["id"] for r in post if r["stop_loss_pct"] is None]
        check(not missing, "P2-9 标记后 BRK 行 stop_loss_pct 全非空",
              f"缺失行 id：{missing}")
        out_of_band = [(r["id"], str(r["stop_loss_pct"])) for r in post
                       if r["stop_loss_pct"] is not None
                       and not (sd.STOP_PCT_MIN <= float(r["stop_loss_pct"]) <= sd.STOP_PCT_MAX)]
        check(not out_of_band,
              f"P2-9 标记后 stop_loss_pct 全落在 [{sd.STOP_PCT_MIN}%, {sd.STOP_PCT_MAX}%]",
              f"越界行：{out_of_band}")

        # 历史欠账只报告、不判失败（已过/将过 24h TTL，非本轮缺陷）
        legacy_missing = sum(1 for r in pre if r["stop_loss_pct"] is None)
        if legacy_missing:
            print(f"    \u2139 历史欠账（不判失败）：标记前 {legacy_missing}/{len(pre)} 条"
                  f"无失效位，属修复前产物，随 24h TTL 自然出窗")

print(f"\n结果：{passed} 通过 / {failed} 失败 / {skipped} 跳过")
sys.exit(1 if failed else 0)
