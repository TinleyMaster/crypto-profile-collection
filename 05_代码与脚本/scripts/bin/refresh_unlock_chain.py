#!/usr/bin/env python3
"""解锁数据链一键刷新（SKILL-HC-010）—— 抓取 → 事件同步 → 反应重算 → MV 刷新。

背景（SKILL-HC-009 审计结论，2026-10-26）
----------------------------------------
「刷了 MV 但 hc_n 一个不变」的真因不是 REFRESH 失败，而是链路上游没动：
  · biz.asset_unlock_reaction 是**每日快照表**（as_of / n_events / n_seen），
    不主动重算就不会更新；
  · biz.mv_asset_screen 的 u_n_events 直接 LEFT JOIN LATERAL 取
    `ORDER BY as_of DESC LIMIT 1` 的最新快照；
  · 于是「补了 new 事件 → sync 进 asset_unlock_event → REFRESH MV」，
    快照还是旧的，u_n_events 还是 4，hc_prefilter 的 `u_n_events>=5` 照旧不过。
四步缺一，漏斗就空转。本脚本把四步固化成一条链，避免再次「少跑一步以为没修好」。

四步（严格串行，缺一不可）
--------------------------
  ① crawl     phase_chain_token_unlocks.py --symbol/--asset-id --save
              抓 tokenomics.com 双表（Upcoming + Past）写 biz.asset_token_unlocks
  ② event     sync_unlock_events_from_json.py --asset-id
              JSON → biz.asset_unlock_event 结构化表
  ③ reaction  asset_unlock_reaction.py --asset-id --persist
              重算并落库 biz.asset_unlock_reaction 快照（**这一步最常被漏**）
  ④ mv        refresh_asset_screen_mv.py
              REFRESH MATERIALIZED VIEW CONCURRENTLY（只重读，不重算）

用法
----
    # 单/多标的
    python bin/refresh_unlock_chain.py --symbol ARB --symbol UNI
    python bin/refresh_unlock_chain.py --asset-id 11841,3890

    # 目标清单（每行 symbol 或纯数字 asset_id，制表符后内容当备注忽略）
    python bin/refresh_unlock_chain.py --from-file ../../hc009_targets.tsv

    # 只重算（不重新抓网页，最快；已确认数据在库、只想刷快照+MV）
    python bin/refresh_unlock_chain.py --from-file t.tsv --skip-crawl

    # 只刷 MV
    python bin/refresh_unlock_chain.py --refresh-only

    # 干跑：只打印将要执行的命令
    python bin/refresh_unlock_chain.py --symbol ARB --dry-run

失败隔离
--------
单个资产某一步失败**不中止**后续资产（沿用 run_data_sync_daily 的隔离原则），
但会把失败记进汇总表并让退出码非 0；第 ④ 步「只要前面有任何一步成功」就照常执行，
避免白抓了数据却不刷视图。

退出码
------
    0  全绿
    1  有资产任一步失败（明细见汇总表）
    2  前置检查失败（DB 不可达 / 目标为空 / 符号无法解析为 asset_id）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
PROJECT_SRC = SCRIPTS_DIR / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

try:
    from crypto_research.config import get_settings  # noqa: E402
    from crypto_research.db.conn import get_connection  # noqa: E402
except ImportError as _e:  # pragma: no cover
    # 本机 `python` 默认是 3.13（无 psycopg），必须用 py -3.12。
    # 不接住会甩一段 psycopg 的原始 traceback，看不出是解释器选错了。
    print(f"[FATAL] 依赖缺失（{_e}）。本脚本需 Python 3.12 + psycopg3：")
    print(f"        py -3.12 bin/refresh_unlock_chain.py ...   （当前解释器 {sys.version.split()[0]}）")
    raise SystemExit(2) from None

PY = sys.executable or "python"
DEFAULT_TIMEOUT = 300  # 单步墙钟上限（秒）；抓取含浏览器启动，给宽一点


# ── 小工具 ──────────────────────────────────────────────────

def _log(msg: str = "") -> None:
    print(msg, flush=True)


def _rule(title: str = "") -> None:
    _log("=" * 78)
    if title:
        _log(title)
        _log("=" * 78)


def _run_step(name: str, argv: list[str], timeout: int, dry_run: bool) -> dict:
    """跑一个子步骤。返回 {step, ok, sec, tail, err}。"""
    cmd = [PY, str(SCRIPT_DIR / argv[0])] + argv[1:]
    shown = " ".join([Path(argv[0]).name] + argv[1:])
    if dry_run:
        _log(f"    [dry-run] {shown}")
        return {"step": name, "ok": True, "sec": 0.0, "tail": "(dry-run)", "err": ""}

    t0 = time.time()
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(SCRIPTS_DIR),
        )
        sec = time.time() - t0
        ok = p.returncode == 0
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        tail_lines = [ln for ln in out.strip().splitlines() if ln.strip()][-4:]
        if not ok and not tail_lines:
            tail_lines = [f"exit={p.returncode}（无输出）"]
        return {
            "step": name, "ok": ok, "sec": sec,
            "tail": " | ".join(tail_lines)[:400],
            "err": "" if ok else f"exit={p.returncode}",
        }
    except subprocess.TimeoutExpired:
        return {"step": name, "ok": False, "sec": time.time() - t0,
                "tail": "", "err": f"超时 >{timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"step": name, "ok": False, "sec": time.time() - t0,
                "tail": "", "err": f"{type(e).__name__}: {e}"}


HC_SQL = """
SELECT count(*)                                        AS total_rows,
       count(*) FILTER (WHERE hc_prefilter)            AS hc_n,
       count(*) FILTER (WHERE u_stat_valid)            AS u_valid_n,
       count(*) FILTER (WHERE e_stat_valid)            AS e_valid_n,
       count(*) FILTER (WHERE u_stat_valid AND e_stat_valid) AS both_valid_n
FROM biz.mv_asset_screen
"""


def _hc_snapshot() -> dict | None:
    """读 MV 的 hc 漏斗四指标。

    HC-010 的核心诉求：**脚本自己把闭环读数打出来**。否则每次改完数据都要手工
    开 psql 数一遍 hc_n，还会重演「以为刷新没生效」的误判（HC-009）。
    """
    try:
        with get_connection(get_settings(require_database=True).database_url) as conn, conn.cursor() as cur:
            cur.execute(HC_SQL)
            cols = [c.name for c in cur.description]
            return dict(zip(cols, cur.fetchone()))
    except Exception as e:  # noqa: BLE001
        _log(f"  [WARN] 读取 hc 漏斗失败：{type(e).__name__}: {str(e)[:120]}")
        return None


def _preflight() -> bool:
    """DB 连通性前置检查。失败即退出码 2，不给下游制造假象。"""
    try:
        with get_connection(get_settings(require_database=True).database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        _log("[前置] DB 连通 ✅")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"[前置] DB 不可达 ❌ {type(e).__name__}: {str(e)[:200]}")
        _log("       先跑链路自检；常见原因：tailscale 隧道断 / 代理劫持 fake-ip")
        return False


def _resolve_targets(rows: list[tuple[int | None, str | None]]) -> list[dict]:
    """(asset_id, symbol) → 规范化目标列表；symbol 统一查库补 asset_id。"""
    url = get_settings(require_database=True).database_url
    syms = sorted({s.upper() for aid, s in rows if s and aid is None})
    resolved: dict[str, tuple[int, str, int | None]] = {}
    if syms:
        with get_connection(url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_symbol, asset_id, market_cap FROM core.asset "
                "WHERE UPPER(canonical_symbol) = ANY(%s) ORDER BY market_cap DESC NULLS LAST",
                (syms,),
            )
            for sym, aid, mcap in cur.fetchall():
                if sym.upper() in resolved:
                    _log(f"  [WARN] 符号 {sym} 存在多个 asset_id，取市值最大的 "
                         f"{resolved[sym.upper()][0]}，忽略 {aid}；如需精确请改用 --asset-id")
                    continue
                resolved[sym.upper()] = (aid, sym, mcap)

    out: list[dict] = []
    for aid, sym in rows:
        if aid is not None:
            out.append({"asset_id": aid, "symbol": (sym or "").upper() or None,
                        "from": "asset_id"})
            continue
        key = (sym or "").upper()
        if key in resolved:
            rid, rsym, _mcap = resolved[key]
            out.append({"asset_id": rid, "symbol": rsym, "from": "symbol"})
        else:
            out.append({"asset_id": None, "symbol": key, "from": "symbol",
                        "unresolved": True})

    # --asset-id 进来的目标没带符号 → 回查一次库补上（只为日志可读 + 判定不歧义）
    need_sym = [t["asset_id"] for t in out
                if t.get("asset_id") is not None and not t.get("symbol")]
    if need_sym:
        with get_connection(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT asset_id, canonical_symbol FROM core.asset "
                        "WHERE asset_id = ANY(%s)", (need_sym,))
            sym_map = {r[0]: r[1] for r in cur.fetchall()}
        for t in out:
            if t.get("asset_id") in sym_map and not t.get("symbol"):
                t["symbol"] = sym_map[t["asset_id"]]

    # 去重（保持顺序）
    seen: set[int] = set()
    deduped: list[dict] = []
    for t in out:
        aid = t.get("asset_id")
        if aid is not None and aid in seen:
            continue
        if aid is not None:
            seen.add(aid)
        deduped.append(t)
    return deduped


def _parse_from_file(path: str) -> list[tuple[int | None, str | None]]:
    rows: list[tuple[int | None, str | None]] = []
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = re.split(r"[\t,;\s]+", line)
        for tok in parts:
            if not tok:
                continue
            if tok.isdigit():
                rows.append((int(tok), None))
            else:
                rows.append((None, tok))
            break  # 一行只取第一个 token，后面当备注
    return rows


# ── 主流程 ──────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="解锁数据链一键刷新：抓取 → 事件同步 → 反应重算 → MV 刷新")
    ap.add_argument("--asset-id", action="append", default=[],
                    help="asset_id，可逗号分隔或重复传")
    ap.add_argument("--symbol", action="append", default=[],
                    help="币种符号，可重复传")
    ap.add_argument("--from-file", help="目标清单文件（每行 symbol 或 asset_id）")
    ap.add_argument("--skip-crawl", action="store_true", help="跳过第①步抓取")
    ap.add_argument("--skip-event", action="store_true", help="跳过第②步事件同步")
    ap.add_argument("--skip-reaction", action="store_true", help="跳过第③步反应重算")
    ap.add_argument("--no-refresh", action="store_true", help="跳过第④步 MV 刷新")
    ap.add_argument("--refresh-only", action="store_true",
                    help="只跑第④步（等价于三个 --skip-* + 不需要目标）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单步超时秒数")
    ap.add_argument("--json", action="store_true", help="末尾输出机器可读汇总")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    args = ap.parse_args()

    if args.refresh_only:
        args.skip_crawl = args.skip_event = args.skip_reaction = True

    rows: list[tuple[int | None, str | None]] = []
    for chunk in args.asset_id:
        for tok in chunk.split(","):
            tok = tok.strip()
            if tok.isdigit():
                rows.append((int(tok), None))
    for s in args.symbol:
        for tok in s.split(","):
            if tok.strip():
                rows.append((None, tok.strip()))
    if args.from_file:
        rows.extend(_parse_from_file(args.from_file))

    if not args.refresh_only and not rows:
        _log("需指定 --symbol / --asset-id / --from-file（或 --refresh-only）")
        return 2

    _rule(f"解锁数据链刷新  SKILL-HC-010   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if not _preflight():
        return 2

    targets: list[dict] = []
    if not args.refresh_only:
        targets = _resolve_targets(rows)
        bad = [t for t in targets if t.get("unresolved")]
        if bad:
            _log(f"[前置] {len(bad)} 个符号解析不到 asset_id："
                 f"{[t['symbol'] for t in bad]}")
            _log("       请改用 --asset-id，或先确认 core.asset 是否收录该币")
            return 2
        _log(f"[前置] 目标 {len(targets)} 个：" +
             ", ".join(f"{t['symbol'] or '?'}({t['asset_id']})" for t in targets))

    _log(f"[链路] ① crawl={'skip' if args.skip_crawl else 'run'}"
         f" ② event={'skip' if args.skip_event else 'run'}"
         f" ③ reaction={'skip' if args.skip_reaction else 'run'}"
         f" ④ mv={'skip' if args.no_refresh else 'run'}")
    _log()

    results: list[dict] = []
    for i, t in enumerate(targets, 1):
        aid, sym = t["asset_id"], (t["symbol"] or f"id{t['asset_id']}")
        _log(f"[{i}/{len(targets)}] {sym} (asset_id={aid})")
        rec: dict = {"asset_id": aid, "symbol": sym, "steps": []}

        if not args.skip_crawl:
            # 只用 --asset-id：phase_chain 的 --symbol/--asset-id 是**互斥组**，
            # 两个一起传直接 argparse 报错退出；asset_id 已在上游解析好，
            # 走 --asset-id 还能绕开 slug 猜测（slug 由库内符号推导）。
            rec["steps"].append(_run_step(
                "crawl",
                ["phase_chain_token_unlocks.py", "--asset-id", str(aid), "--save"],
                args.timeout, args.dry_run))

        if not args.skip_event:
            rec["steps"].append(_run_step(
                "event",
                ["sync_unlock_events_from_json.py", "--asset-id", str(aid)],
                args.timeout, args.dry_run))

        if not args.skip_reaction:
            rec["steps"].append(_run_step(
                "reaction",
                ["asset_unlock_reaction.py", "--asset-id", str(aid), "--persist"],
                args.timeout, args.dry_run))

        for st in rec["steps"]:
            mark = "✅" if st["ok"] else "❌"
            if st["ok"] and st["step"] == "crawl" and "not_found" in st["tail"]:
                mark = "⚠️"   # 源未收录（写了 not_found 墓碑），不是链路故障
            extra = f"  {st['err']}" if st["err"] else ""
            _log(f"    {mark} {st['step']:9} {st['sec']:6.1f}s  {st['tail']}{extra}")
        rec["ok"] = all(s["ok"] for s in rec["steps"])
        results.append(rec)
        _log()

    # ④ MV 刷新：只要前面有成功动作就照常刷（白抓不刷视图才是真浪费）
    mv_ok = True
    mv_sec = 0.0
    hc_before = hc_after = None
    if not args.no_refresh:
        _log("[④] 刷新 MV biz.mv_asset_screen")
        hc_before = _hc_snapshot()
        st = _run_step("mv", ["refresh_asset_screen_mv.py"], max(args.timeout, 300),
                       args.dry_run)
        mv_ok, mv_sec = st["ok"], st["sec"]
        _log(f"    {'✅' if st['ok'] else '❌'} mv        {st['sec']:6.1f}s  {st['tail']}"
             f"{('  ' + st['err']) if st['err'] else ''}")
        hc_after = _hc_snapshot()
        if hc_before and hc_after and mv_ok:
            _log("    ┄ hc 漏斗（刷新前 → 后）")
            for k in ("hc_n", "u_valid_n", "e_valid_n", "both_valid_n", "total_rows"):
                b, a = hc_before.get(k), hc_after.get(k)
                if b is None or a is None:
                    continue
                flag = "" if a == b else "  ← 变化"
                _log(f"      {k:14} {b:>6} → {a:<6}{flag}")
        _log()

    # 汇总
    _rule("汇总")
    if results:
        _log(f"{'SYM':10} {'crawl':>7} {'event':>7} {'reaction':>9}  判定")
        for r in results:
            cells = {s["step"]: s for s in r["steps"]}
            def _c(k: str) -> str:
                s = cells.get(k)
                if s is None:
                    return "  -"
                return " ✅" if s["ok"] else " ❌"
            _log(f"{r['symbol']:10} {_c('crawl'):>7} {_c('event'):>7} "
                 f"{_c('reaction'):>9}  {'✅' if r['ok'] else '❌'}")
    _log(f"{'MV':10} {'':>7} {'':>7} {'':>9}  {'✅' if mv_ok else '❌'}"
         f"  ({mv_sec:.1f}s)")

    failed = [r for r in results if not r["ok"]]
    rc = 0 if (not failed and mv_ok) else 1

    _log()
    if rc == 0:
        _log("✅ 全链路通过。hc 漏斗可直接用 biz.mv_asset_screen 读数。")
    else:
        if failed:
            names = [r["symbol"] for r in failed]
            _log(f"❌ {len(failed)} 个资产有步骤失败：{names}")
        if not mv_ok:
            _log("❌ MV 刷新失败 —— 即便上游数据已更新，视图仍是旧值，不要下结论。")

    if args.json:
        _log("")
        _log(json.dumps({
            "targets": results,
            "mv": {"ok": mv_ok, "sec": round(mv_sec, 2)},
            "hc_before": hc_before, "hc_after": hc_after,
            "rc": rc,
        }, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
