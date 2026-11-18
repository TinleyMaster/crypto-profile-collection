#!/usr/bin/env python3
"""id=1022（龙虾USDT）真实成交补录 —— 一次性数据修复工具。

背景（2026-11-18）
-----------------------------------------------------------------
  2026-11-17 07:05 信号 id=1022（龙虾USDT SHORT_LONG）挂出限价单 1829523293
  @ 0.022089，成交 1358 @ 0.02206（名义 30U，3x）。11-17 08:09 平仓
  @ 0.01953，真实 realizedPnl = -3.4357 USDT。
  但因 BinanceFuturesClient._query_string 未做 URL 编码，非 ASCII symbol 的
  GET 查单恒 -1022 → _process_limit_entries 每轮查单失败 → 永久卡
  exec_state='limit_pending' → 真实成交与亏损从未补录（v2_trade_log 0 行）。
  ⇒ 对账失真：outcome 表里躺的是【理想化结算】+1.94%（用 K 线价），
    而账户真实是 -3.4357 USDT（实测 outcome 与 trade_log 系统性偏差 1.6~3.0pp）。

  根因已修（commit 4e13594）：_query_string 统一 urlencode + 查单死循环熔断。
  本脚本负责把这条历史脏数据了结。

安全设计
-----------------------------------------------------------------
  ① 幂等：已补录则直接跳过（按 signal_id 查 v2_trade_log）
  ② 备份：先 INSERT 到 biz.v2_trade_log_repair_backup（表不存在则建）
  ③ 干跑默认：--apply 才真正写库
  ④ 校验：写入后回查 + 断言关键字段
  ⑤ 精确 WHERE：只影响 signal_id=1022 这一行（不会误伤其他信号）
  ⑥ 不动 outcome 表（它是理想化结算表，另有口径）

用法：
  python bin/repair_signal_1022.py            # 干跑（只打印将执行的变更）
  python bin/repair_signal_1022.py --apply    # 真写库
  python bin/repair_signal_1022.py --rollback # 回滚（按备份表还原）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

SIGNAL_ID = 1022
SYMBOL = "龙虾USDT"

# 真实成交数据（来源：币安交易所侧 userTrades / order 查询，2026-11-18 核实）
#   交叉验证：qty × avgPrice = 1358 × 0.02206 = 29.957 ≈ notional 30U ✅
TRADE = {
    "signal_id": SIGNAL_ID,
    "symbol": SYMBOL,
    "signal_type": "SHORT_LONG",
    "direction": "LONG",
    "open_ts": "2026-11-17 07:05:17.636093+00:00",
    "open_price": 0.02206,
    "open_qty": 1358.0,
    "notional_usdt": 30.0,
    "margin_usdt": 10.0,
    "leverage": 3,
    "close_ts": "2026-11-17 08:09:00.000000+00:00",
    "close_price": 0.01953,
    "realized_pnl_usdt": -3.4357,
    # commission：币安返佣/手续费未单独留存 → NULL（与既有 31 行中 16 行 NULL 一致）
    "commission_usdt": None,
    # exit_reason：既有取值为 trail / aborted_protective / manual_or_unknown / NULL。
    # 本笔是人工补录的真实平仓（非策略出场），用 manual_or_unknown 与既有惯例一致。
    "exit_reason": "manual_or_unknown",
    "win": False,
    "origin": "bot",
    "signal_window": "T24H",
    "scan_ts": "2026-11-17 07:05:04.607974+00:00",
    "event_bar_ts": "2026-11-17T06:00:00+00",
    "chg_24h_pct": 63.5962,
    "chg1h_pct": 29.3706,
    "vr_1h": 39.9139,
    "funding_rate": 0.00029435,
    "vol_24h_usd": 39301921.33,
    "planned_hold_h": 24,
    "sl_pct": 0.10,
    "tr_pct": 0.03,
    "tp_pct": None,
    "ref_price_usd": 0.0222,
    "order_id": "1829523293",
    "position_scale": 0.5,
    "dry_run": False,
}

# 信号行的终态与补录审计
SIGNAL_AUDIT = {
    "exec_reason": "limit_filled_recovered_20261118",
    "repair_note": ("2026-11-18 人工补录：-1022 签名缺陷导致查单永久失败，"
                    "真实成交(1358@0.02206)与平仓(-3.4357 USDT)未入 trade_log。"
                    "成交数据来源=币安交易所侧查询。"),
    "repair_at": "2026-11-18T00:00:00+00",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认干跑）")
    ap.add_argument("--rollback", action="store_true", help="按备份表回滚")
    args = ap.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # ── 幂等检查 ──
            cur.execute(
                "SELECT id, realized_pnl_usdt, close_ts FROM biz.v2_trade_log "
                "WHERE signal_id=%s", (SIGNAL_ID,))
            existing = cur.fetchall()
            if existing and not args.rollback:
                print(f"[skip] signal_id={SIGNAL_ID} 已在 v2_trade_log（{len(existing)} 行），"
                      f"无需补录（幂等）")
                for r in existing:
                    print(f"        id={r[0]} pnl={r[1]} close_ts={r[2]}")
                return 0

            if args.rollback:
                return _rollback(conn)

            # ── 干跑：打印将执行的变更 ──
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, symbol, exec_state, executed_at FROM biz.scan_gainer_signal "
                    "WHERE id=%s", (SIGNAL_ID,))
                sig = cur.fetchone()
            if not sig:
                print(f"[err] 找不到 signal_id={SIGNAL_ID}")
                return 1
            print("=" * 100)
            print(f"id={SIGNAL_ID} 补录 · {SYMBOL}（{'APPLY 真写库' if args.apply else 'DRY-RUN 干跑'}）")
            print("=" * 100)
            print(f"  当前信号行: id={sig[0]} symbol={sig[1]} exec_state={sig[2]} "
                  f"executed_at={sig[3]}")
            print(f"\n  ① INSERT biz.v2_trade_log（账户实况，1 行）")
            for k in ("open_ts", "open_price", "open_qty", "notional_usdt", "leverage",
                      "close_ts", "close_price", "realized_pnl_usdt", "exit_reason"):
                print(f"       {k:20s} = {TRADE[k]}")
            print(f"\n  ② UPDATE biz.scan_gainer_signal SET exec_state='ordered'")
            print(f"       params += {json.dumps(SIGNAL_AUDIT, ensure_ascii=False)[:110]}...")
            print(f"\n  ③ 幂等保护：已存在则跳过（当前不存在 → 执行）")

            if not args.apply:
                print("\n[dry-run] 未写库。加 --apply 执行。")
                return 0

            # ── 备份（信号行 + 已有 trade_log 行）──
            from psycopg.types.json import Jsonb
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS biz.v2_trade_log_repair_backup (
                        backup_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        repair_tag TEXT NOT NULL,
                        payload JSONB NOT NULL)
                """)
                cur.execute(
                    "SELECT to_jsonb(s) FROM biz.scan_gainer_signal s WHERE id=%s",
                    (SIGNAL_ID,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "INSERT INTO biz.v2_trade_log_repair_backup (repair_tag, payload) "
                        "VALUES (%s, %s)", (f"signal_{SIGNAL_ID}", Jsonb(row[0])))

            # ── 写入 trade_log ──
            cols = list(TRADE.keys())
            vals = [TRADE[c] for c in cols]
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO biz.v2_trade_log ({','.join(cols)}) "
                    f"VALUES ({','.join(['%s'] * len(cols))})", vals)

            # ── 更新信号行终态 ──
            from psycopg.types.json import Jsonb
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.scan_gainer_signal SET exec_state='ordered', "
                    "  params = COALESCE(params, '{}'::jsonb) || %s::jsonb WHERE id=%s",
                    (Jsonb(SIGNAL_AUDIT), SIGNAL_ID))

            # ── 校验回查 ──
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, symbol, exec_state, params->>'exec_reason' "
                    "FROM biz.scan_gainer_signal WHERE id=%s", (SIGNAL_ID,))
                chk_sig = cur.fetchone()
                cur.execute(
                    "SELECT id, realized_pnl_usdt, close_ts, notional_usdt "
                    "FROM biz.v2_trade_log WHERE signal_id=%s", (SIGNAL_ID,))
                chk_tr = cur.fetchall()
            print("\n" + "=" * 100)
            print("校验回查")
            print("=" * 100)
            print(f"  信号行: id={chk_sig[0]} symbol={chk_sig[1]} "
                  f"exec_state={chk_sig[2]} exec_reason={chk_sig[3]}")
            for r in chk_tr:
                print(f"  trade_log: id={r[0]} pnl={r[1]} close_ts={r[2]} notional={r[3]}")
            assert chk_sig[2] == "ordered", "信号行终态未更新"
            assert len(chk_tr) == 1, "trade_log 行数异常"
            assert abs(float(chk_tr[0][1]) - TRADE["realized_pnl_usdt"]) < 1e-6, "pnl 不符"
            print("\n  ✅ 补录完成且校验通过")
            print("  ⚠ 回滚方式：python bin/repair_signal_1022.py --rollback")
            return 0


def _rollback(conn) -> int:
    """按备份表还原：删掉补录的 trade_log 行 + 还原信号行。"""
    from psycopg.types.json import Jsonb
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM biz.v2_trade_log_repair_backup "
            "WHERE repair_tag=%s ORDER BY backup_at DESC LIMIT 1",
            (f"signal_{SIGNAL_ID}",))
        row = cur.fetchone()
        if not row:
            print("[err] 无备份，无法回滚")
            return 1
        payload = row[0]
        cur.execute("DELETE FROM biz.v2_trade_log WHERE signal_id=%s", (SIGNAL_ID,))
        n_del = cur.rowcount
        cur.execute(
            "UPDATE biz.scan_gainer_signal SET exec_state=%s, executed_at=%s, params=%s "
            "WHERE id=%s",
            (payload.get("exec_state"), payload.get("executed_at"),
             Jsonb(payload.get("params") or {}), SIGNAL_ID))
        print(f"[rollback] 删除 trade_log {n_del} 行；信号行已还原为 "
              f"exec_state={payload.get('exec_state')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
