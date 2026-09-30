#!/usr/bin/env python3
"""盘口深度**口径隔离**单测（SCAN-LIQ-DEPTH-001 阶段 A 的护栏不变量）。

运行：python test_ob_depth_scope.py

钉住的不变量（工单 §4 / §6）：
  ① **不进实时判定链**：`biz.orderbook_depth_history` 不得出现在
     `scan_daemon.py` / `squeeze.py` / `squeeze_fuel.py` 的任何代码里（粒度错配 4h vs 5m/1h）；
  ② **多档/多口径不混桶**：存货表 PK 必须含 `interval` / `exchange_scope` / `range_pct`；
  ③ **缺失 ≠ 0**：`_num(None)` / `_num("")` / 非法值一律 None，不得用 0 冒充「无挂单」；
  ④ **scope 分流**：`parse_rows` 对 binance 用 `bids_usd`、对 all 用 `aggregated_bids_usd`，
     两套字段都解析，避免静默全 None；
  ⑤ **code 是字符串**：`probe` 判定用 `str(code) == "0"`（int 0 比较恒不等，2026-09-30 已踩坑）；
  ⑥ **游标只在整币成功后推进**：`CURSOR_SQL` 在 `executemany(UPSERT_SQL)` 之后、同一事务内。

⚠️ 本文件只钉**结构 / 口径**，不对任何阈值取值下结论。
"""
import io
import os
import re
import sys
import tokenize
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

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


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code_only(src: str) -> str:
    """只保留**非注释** token 后重建源码（词法剥离，字符串内的 `#` 不误伤）。"""
    spans: dict[int, list[tuple[int, int]]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            spans.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1]))
    if not spans:
        return src
    out = []
    for i, ln in enumerate(src.splitlines(keepends=True), start=1):
        for a, b in sorted(spans.get(i, []), reverse=True):
            ln = ln[:a] + ln[b:]
        out.append(ln)
    return "".join(out)


DAEMON = os.path.join(_SCRIPTS, "bin", "scan_daemon.py")
SQUEEZE = os.path.join(_SCRIPTS, "src", "crypto_research", "analysis", "squeeze.py")
FUEL = os.path.join(_SCRIPTS, "src", "crypto_research", "analysis", "squeeze_fuel.py")
BACKFILL = os.path.join(_SCRIPTS, "bin", "phase_backfill_ob_depth.py")
MIGRATION = os.path.join(_SCRIPTS, "migrations", "fix_081_orderbook_depth_history.sql")
CLIENT = os.path.join(_SCRIPTS, "src", "crypto_research", "clients", "coinglass_client.py")


def main() -> int:
    import phase_backfill_ob_depth as bd  # noqa: E402

    # ── ① 不进实时判定链 ─────────────────────────────────────────
    print("① orderbook_depth_history 不进实时判定链")
    for path, tag in ((DAEMON, "scan_daemon"), (SQUEEZE, "squeeze"), (FUEL, "squeeze_fuel")):
        src = _code_only(_read(path))
        check("orderbook_depth_history" not in src and "ob_depth" not in src,
              f"{tag} 不引用深度表/游标表", path)

    # ── ② 迁移：PK 含 interval / exchange_scope / range_pct；幂等 ──
    print("② 迁移 PK 与幂等")
    mig = _read(MIGRATION)
    m = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", mig)
    check(m is not None, "存货表有 PRIMARY KEY")
    if m:
        pk = m.group(1).replace(" ", "")
        for col in ("symbol", "interval", "exchange_scope", "range_pct", "ts"):
            check(col in pk.split(","), f"PK 含 {col}", pk)
    check(mig.count("CREATE TABLE IF NOT EXISTS") >= 2, "两表均 CREATE IF NOT EXISTS（幂等）")
    check("CREATE INDEX IF NOT EXISTS" in mig, "索引幂等")
    check("orderbook_depth_history" in mig and "ob_depth_backfill_cursor" in mig,
          "存货表 + 游标表齐备")
    check("无 best bid/ask" in mig or "best bid/ask" in mig, "表注释声明「无价差字段」边界")

    # ── ③ 缺失 ≠ 0 ─────────────────────────────────────────────
    print("③ _num 缺失语义")
    check(bd._num(None) is None, "_num(None) → None")
    check(bd._num("") is None, "_num('') → None")
    check(bd._num("abc") is None, "_num('abc') → None")
    check(bd._num("123.45") == 123.45, "_num('123.45') → 123.45")
    check(bd._num(67.5) == 67.5, "_num(67.5) → 67.5")

    # ── ④ scope 分流 + start 裁剪 + 缺 time 跳过 ─────────────────
    print("④ parse_rows 口径分流")
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    t_ok = 1788883200000   # 2026-09-08 12:00 UTC（> start）
    t_old = 1756636800000  # 2025-08-31 12:00 UTC（< start）
    raw_binance = [
        {"time": t_ok, "bids_usd": 100.0, "asks_usd": 90.0,
         "bids_quantity": 1.0, "asks_quantity": 0.9},
        {"time": t_old, "bids_usd": 50.0, "asks_usd": 40.0},
        {"bids_usd": 1.0},  # 缺 time ⇒ 跳过
    ]
    rows = bd.parse_rows(raw_binance, "BTCUSDT", bd.SCOPE_BINANCE, "4h", 1.0, start)
    check(len(rows) == 1, "binance：start 裁剪 + 缺 time 跳过", f"n={len(rows)}")
    check(rows and rows[0][5] == 100.0 and rows[0][6] == 90.0,
          "binance 读 bids_usd/asks_usd（非 aggregated_）", str(rows[:1]))
    raw_all = [{"time": t_ok, "aggregated_bids_usd": 200.0, "aggregated_asks_usd": 180.0,
                "aggregated_bids_quantity": 2.0, "aggregated_asks_quantity": 1.8}]
    rows_all = bd.parse_rows(raw_all, "BTCUSDT", bd.SCOPE_ALL, "4h", 1.0, start)
    check(rows_all and rows_all[0][5] == 200.0 and rows_all[0][6] == 180.0,
          "all 读 aggregated_bids_usd/asks_usd", str(rows_all[:1]))
    check(rows and rows[0][1] == "4h" and rows[0][2] == "binance" and rows[0][3] == 1.0,
          "落库元组带 interval/exchange_scope/range_pct", str(rows[:1]))
    # ts 对齐：t_ok 是 12:00（4h 已对齐），对齐后不变
    check(rows and rows[0][4].hour % 4 == 0 and rows[0][4].minute == 0,
          "ts 按 4h 对齐", str(rows[0][4]) if rows else "")

    # ── ⑤ code 字符串护栏（probe 不得用 int 比较）─────────────────
    print("⑤ code 字符串判定护栏")
    bf = _code_only(_read(BACKFILL))
    check('str(body.get("code")) == "0"' in bf, "probe 用 str(code)=='0' 判定")
    check(re.search(r"\.get\(\"code\"\)\s*==\s*0\b", bf) is None,
          "源码无 code==0（int）比较残留")
    check(re.search(r"\.get\(\"code\"\)\s*!=\s*0\b", bf) is None,
          "源码无 code!=0（int）比较残留")

    # ── ⑥ 游标在整币成功后推进（结构序）─────────────────────────
    print("⑥ 游标推进顺序")
    i_upsert = bf.index("executemany(UPSERT_SQL")
    i_cursor = bf.index("cur.execute(CURSOR_SQL")
    i_commit = bf.index("conn.commit()", i_cursor)
    check(i_upsert < i_cursor < i_commit, "executemany → CURSOR → commit 顺序", )

    # ── ⑦ 客户端方法签名（range_ 不遮蔽内置）──────────────────────
    print("⑦ 客户端方法")
    cl = _read(CLIENT)
    check("def orderbook_ask_bids_history" in cl, "orderbook_ask_bids_history 存在")
    check("def orderbook_aggregated_ask_bids_history" in cl,
          "orderbook_aggregated_ask_bids_history 存在")
    check("range_: float" in cl, "参数名 range_（不遮蔽内置 range）")
    check('"range": self._norm_range(range_)' in cl, "请求参数经 _norm_range 规范化")
    check("start_time" in cl and "end_time" in cl, "支持 start_time/end_time（实测生效）")
    # range 格式敏感（2026-09-30 实测：1.0 ⇒ code=0 但静默 0 行）
    from crypto_research.clients.coinglass_client import CoinGlassClient  # noqa: E402
    check(CoinGlassClient._norm_range(1.0) == 1, "_norm_range(1.0) → int 1（防静默空集）")
    check(CoinGlassClient._norm_range(2.0) == 2, "_norm_range(2.0) → int 2")
    check(CoinGlassClient._norm_range(0.25) == 0.25, "_norm_range(0.25) → 0.25（非整数保留）")

    # ── ⑧ UPSERT 占位符与列数一致 ────────────────────────────────
    print("⑧ UPSERT 结构")
    vals = re.search(r"VALUES\s*\(([^)]+)\)", bd.UPSERT_SQL)
    cols = re.search(r"INSERT INTO biz\.orderbook_depth_history\s*\(([^)]+)\)", bd.UPSERT_SQL)
    check(vals and cols, "UPSERT 有列清单与 VALUES")
    if vals and cols:
        n_ph = vals.group(1).count("%s")
        n_col = len([c for c in cols.group(1).split(",") if c.strip()])
        # fetched_at 由 NOW() 填充、不占占位符 ⇒ 列数 - 1 == 占位符数
        # （注意：正则 [^)]+ 会在 NOW( 的括号前截断，group(1) 只含 "NOW(" 前缀）
        check("NOW(" in vals.group(1) and n_ph == n_col - 1,
              f"占位符({n_ph}) == 列数({n_col}) - fetched_at(NOW())")

    # ── ⑨ 深度档常量的语义注释（防把 range 当序号）─────────────────
    print("⑨ range 语义")
    check("深度百分比" in _read(BACKFILL) or "深度百分比" in mig,
          "文档/SQL 注释声明 range=深度百分比（非档位序号）")

    print(f"\n{'='*56}\n结果：{passed} 通过 / {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
