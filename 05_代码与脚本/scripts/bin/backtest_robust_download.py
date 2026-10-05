#!/usr/bin/env python3
"""加固验证 · 按 symbol 分块下载事件数据到缓存（断点续传版）。

远程 DB 到本地的公网传输不稳定：一次性大 COPY 会在中途卡死（连接静默断开）。
改为按 symbol 分块 COPY —— 每批只有 KB 级，失败只重试该币，已完成的币自动跳过。

产物：DATA_DIR/robust_events_cache.csv（带表头，供 backtest_robust_checks.py 读取）

用法：
    python backtest_robust_download.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402

DATA_DIR = SCRIPT_DIR.parent / "data"
OUT = DATA_DIR / "robust_events_cache.csv"
PARTS = DATA_DIR / "robust_events_parts"


def connect(url: str) -> psycopg.Connection:
    """直连（不走连接池）：连接池 max_size=5 易被残留半开连接占满导致 PoolTimeout。
    keepalives 让半开连接 ~60s 内报错而非协议层永久挂起。"""
    return psycopg.connect(
        url,
        connect_timeout=15,
        options="-c lock_timeout=30000",
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )

# 单 symbol 的 SQL：窗口只在币内算，无跨币边界；结果集 KB 级。
SYM_SQL = """
WITH volbase AS (
    SELECT symbol, open_time, close_px,
           close_px / open_px - 1 AS chg1h,
           quote_vol,
           AVG(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
               ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg20,
           LEAD(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) AS px24h,
           close_px / LAG(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) - 1 AS chg24
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0
      AND symbol = %s
),
evt AS (
    SELECT symbol, open_time, close_px AS px0, chg1h,
           quote_vol / avg20 AS vr,
           px24h / close_px - 1 AS r24h,
           chg24
    FROM volbase
    WHERE avg20 > 0 AND quote_vol / avg20 >= 1.5 AND abs(chg1h) >= 0.015
      AND px24h IS NOT NULL AND chg24 IS NOT NULL
),
btc AS (
    SELECT open_time, close_px,
           LEAD(close_px, 24) OVER (ORDER BY open_time) AS px24
    FROM biz.asset_klines
    WHERE interval = '1h' AND symbol = 'BTCUSDT' AND open_px > 0
)
SELECT e.symbol, e.open_time AS eo, e.chg1h, e.vr, e.r24h, e.chg24,
       b.px24 / b.close_px - 1 AS btc_r24
FROM evt e
LEFT JOIN btc b ON b.open_time = e.open_time
"""


def main() -> int:
    PARTS.mkdir(exist_ok=True)
    settings = get_settings(require_database=True)

    # 断点续传外壳：连接/网络中断后重连继续；symbol 列表与下载都纳入重试
    syms: list[str] = []
    attempt = 0
    while True:
        try:
            attempt += 1
            if not syms:
                with connect(settings.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT DISTINCT symbol FROM biz.asset_klines WHERE interval='1h'")
                        syms = sorted(r[0] for r in cur.fetchall())
                print(f"[symbols] {len(syms)} 个合约")
            done = 0
            with connect(settings.database_url) as conn:
                for i, sym in enumerate(syms, 1):
                    part = PARTS / f"{sym}.csv"
                    mark = PARTS / f"{sym}.done"
                    if mark.exists():
                        done += 1
                        continue
                    try:
                        with conn.cursor() as cur:
                            with cur.copy(f"COPY ({SYM_SQL}) TO STDOUT WITH (FORMAT csv, HEADER false)") as cp, \
                                 part.open("wb") as f:
                                while (chunk := cp.read()) is not None:
                                    f.write(chunk)
                    except Exception as e:  # noqa: BLE001 —— 单币失败不阻塞，可重跑续传
                        print(f"  [skip] {sym}: {type(e).__name__}: {e}")
                        conn.rollback()
                        continue
                    mark.touch()   # 成功标记（即使 0 行事件也算完成）
                    done += 1
                    conn.rollback()
                    if i % 50 == 0 or i == len(syms):
                        print(f"[progress] {i}/{len(syms)} (已完成 {done})")
            if done >= len(syms):
                break
            print(f"[retry] 第 {attempt} 轮完成 {done}/{len(syms)}，还缺，重试")
        except Exception as e:  # noqa: BLE001
            print(f"[conn-err] 第 {attempt} 轮中断: {type(e).__name__}: {e}，重连重试")
            time.sleep(8)
        if attempt > 300:
            print("[abort] 超过 300 轮仍未完成，放弃（已有部分可再跑本脚本续传）")
            break

    # 合并（表头 + 各币 CSV 内容，跳过空文件）
    with OUT.open("w", newline="") as fo:
        fo.write("symbol,eo,chg1h,vr,r24h,chg24,btc_r24\n")
        n = 0
        for part in sorted(PARTS.glob("*.csv")):
            if part.stat().st_size == 0:
                continue
            with part.open() as fi:
                for line in fi:
                    fo.write(line)
                    n += 1
        print(f"[merged] {n:,} 行 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
