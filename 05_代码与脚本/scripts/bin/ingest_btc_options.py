#!/usr/bin/env python3
"""入库脚本：BTC 期权市场快照（Deribit 公开 API，免费无 key）。

Deribit 是全球 BTC 期权主市场。本脚本每轮拉取一次全量快照，写入
biz.btc_options_snapshot（按 metric_date 日频，同一自然日重复跑更新当日行）：
  - 现货指数（get_index_price btc_usd）
  - DVOL 30d 隐含波动率指数（get_volatility_index_data，取末点收盘）
  - 全期权合约 OI（get_book_summary_by_currency，753/934 合约有 OI）
  - 计算：Call/Put 总 OI、PCR（put/call）、最近到期日 max pain（OI 加权成本最小价位）

⚠️ 口径与局限：
  - OI 单位 = Deribit 合约张数（contract_size=1 BTC 名义），跨所汇总不可直接相加；
  - PCR / max pain 基于**名义张数**，未做 delta 加权（简单口径，趋势参考够用）；
  - 期权定位是**持仓/对冲结构**，不是方向信号：PCR 高≠马上跌，需配合现货结构；
  - max pain 是经验现象（临近交割价格向 OI 最大痛点收敛的概率倾向），非规律。

用法：
    python ingest_btc_options.py              # 增量（幂等 upsert 当日）
    python ingest_btc_options.py --dry-run    # 预览，不写入
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

DERIBIT = "https://www.deribit.com/api/v2/public"
TIMEOUT = 30


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.btc_options_snapshot (
                metric_date       DATE           NOT NULL PRIMARY KEY,
                snapshot_ts       TIMESTAMPTZ,
                spot_price        NUMERIC(12,2),
                dvol_30d          NUMERIC(8,4),
                call_oi           NUMERIC(20,2),
                put_oi            NUMERIC(20,2),
                pcr               NUMERIC(10,4),
                max_pain_strike   NUMERIC(12,2),
                nearest_expiry    TIMESTAMPTZ,
                n_instruments     INT,
                source_code       VARCHAR(20)    NOT NULL DEFAULT 'deribit',
                fetched_at        TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ    NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_btc_options_snapshot_date
                ON biz.btc_options_snapshot(metric_date DESC);
        """)
    conn.commit()


def _get(method: str, params: dict) -> dict:
    r = requests.get(f"{DERIBIT}/{method}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise RuntimeError(f"Deribit {method} error: {d['error']}")
    return d


def _f(x):
    return float(x) if x is not None else None


def fetch_options_snapshot() -> dict:
    """拉取并计算期权快照，返回 dict（字段与表列一致）。"""
    # 1) 现货指数
    spot = _f(_get("get_index_price", {"index_name": "btc_usd"})
              .get("result", {}).get("index_price"))

    # 2) DVOL 30d IV（取最近 7 天数据最后一根收盘）
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 7 * 86400 * 1000
    dvol = None
    try:
        data = _get("get_volatility_index_data", {
            "currency": "BTC", "start_timestamp": start_ms,
            "end_timestamp": now_ms, "resolution": "1D"}).get("result", {}).get("data") or []
        if data:
            dvol = _f(data[-1][4])  # [ts, open, high, low, close]
    except Exception as e:
        print(f"[options] DVOL 拉取失败（降级为 None）: {e}", file=sys.stderr)

    # 3) 合约列表（strike / option_type / expiry） + 每合约 OI
    ins = _get("get_instruments", {"currency": "BTC", "kind": "option",
                                   "expired": "false"}).get("result") or []
    by_name = {
        i.get("instrument_name"): i
        for i in ins if i.get("instrument_name")
    }
    bs = _get("get_book_summary_by_currency", {"currency": "BTC",
                                               "kind": "option"}).get("result") or []

    call_oi = put_oi = 0.0
    oi_by_expiry: dict[int, dict[int, float]] = {}  # expiry_ms -> {strike -> OI}
    for b in bs:
        name = b.get("instrument_name")
        info = by_name.get(name)
        oi = _f(b.get("open_interest")) or 0.0
        if not info or oi <= 0:
            continue
        otype = info.get("option_type")
        strike = _f(info.get("strike"))
        exp_ms = info.get("expiration_timestamp")
        if otype == "call":
            call_oi += oi
        elif otype == "put":
            put_oi += oi
        if strike and exp_ms:
            oi_by_expiry.setdefault(exp_ms, {}).setdefault(strike, 0.0)
            oi_by_expiry[exp_ms][strike] += oi

    pcr = (put_oi / call_oi) if call_oi > 0 else None

    # 4) 最近未到期日 的 max pain（OI 加权总成本最小价位）
    now_ts = datetime.now(timezone.utc).timestamp() * 1000
    future = sorted(k for k in oi_by_expiry if k > now_ts)
    nearest_expiry = future[0] if future else None
    max_pain = None
    if nearest_expiry is not None:
        strikes = sorted(oi_by_expiry[nearest_expiry])
        best_cost, best_k = None, None
        for k in strikes:
            cost = sum(oi * abs(ks - k) for ks, oi in oi_by_expiry[nearest_expiry].items())
            if best_cost is None or cost < best_cost:
                best_cost, best_k = cost, k
        max_pain = best_k

    nearest_dt = (datetime.fromtimestamp(nearest_expiry / 1000, tz=timezone.utc)
                  if nearest_expiry else None)
    print(f"[options] spot={spot:.2f} dvol={dvol} call_oi={call_oi:,.0f} "
          f"put_oi={put_oi:,.0f} pcr={pcr if pcr is not None else float('nan'):.3f} "
          f"max_pain={max_pain} nearest={nearest_dt:%Y-%m-%d} "
          f"instruments={len(ins)} with_oi={sum(1 for x in bs if x.get('open_interest'))}")
    return {
        "spot_price": spot, "dvol_30d": dvol,
        "call_oi": call_oi, "put_oi": put_oi, "pcr": pcr,
        "max_pain_strike": max_pain, "nearest_expiry": nearest_dt,
        "n_instruments": len(ins),
    }


def upsert(conn, snap: dict, metric_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO biz.btc_options_snapshot
                (metric_date, snapshot_ts, spot_price, dvol_30d, call_oi, put_oi,
                 pcr, max_pain_strike, nearest_expiry, n_instruments,
                 source_code, fetched_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'deribit', NOW(), NOW())
            ON CONFLICT (metric_date) DO UPDATE SET
                snapshot_ts = EXCLUDED.snapshot_ts,
                spot_price = EXCLUDED.spot_price,
                dvol_30d = EXCLUDED.dvol_30d,
                call_oi = EXCLUDED.call_oi,
                put_oi = EXCLUDED.put_oi,
                pcr = EXCLUDED.pcr,
                max_pain_strike = EXCLUDED.max_pain_strike,
                nearest_expiry = EXCLUDED.nearest_expiry,
                n_instruments = EXCLUDED.n_instruments,
                updated_at = NOW()
        """, (
            metric_date, datetime.now(timezone.utc), snap["spot_price"], snap["dvol_30d"],
            snap["call_oi"], snap["put_oi"], snap["pcr"], snap["max_pain_strike"],
            snap["nearest_expiry"], snap["n_instruments"],
        ))
    conn.commit()
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 期权市场快照采集（Deribit 公开 API）")
    ap.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = ap.parse_args()

    snap = fetch_options_snapshot()
    if args.dry_run:
        print("[dry-run] 不写库：", snap)
        return 0

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)
        n = upsert(conn, snap, datetime.now(timezone.utc).date())
        print(f"[options] upserted {n} 行（当日覆盖）✓")


if __name__ == "__main__":
    sys.exit(main())
