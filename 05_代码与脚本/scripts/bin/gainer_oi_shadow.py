#!/usr/bin/env python3
"""涨幅榜信号 · OI 影子闸门（shadow gate）——只记录，不拦截。

背景（2026-11-16 回测结论）
-----------------------------------------------------------------
  A/B 事件 × 信号时刻的 OI 变化率分层，n=1,378（多所聚合 OI，361 天）：
    Q1 胜 9.9% 均 −6.64% PF 0.13  →  Q4 胜 52.2% 均 +7.53% PF 3.20
    Cliff's d = +0.437（MW p=4.2e-37）；与 chg24 相关性 r=+0.018（独立信息）
  限价口径复跑（n=1,326）：成交率 95.5%，d=+0.436，四项单调不变。

生产侧口径约束（重要）
-----------------------------------------------------------------
  生产只有 Binance **单所** OI 可实时取到（biz.oi_cvd_snapshot，5m 桶，530/530 覆盖）；
  多所聚合 OI（biz.cg_oi_hist）无实时快照表 → 无法用于实盘。
  故本模块用单所口径，阈值按单所口径单独标定：
    oi_chg_12h ≥ 1.0（OI 12h 翻倍以上）：n=37，胜 54.1%，均 +8.15%，PF 3.51，t=+2.19
    ⚠ 单所样本仅 170 条（2026-08-27 起 68 天），证据强度弱于多所口径。

设计原则（严格遵守）
-----------------------------------------------------------------
  ① **只观察不拦截**：本模块绝不改变执行器的任何决策，不产生 skip/放行。
  ② **不新增表**：OI 特征写入既有 biz.scan_gainer_signal.params（JSONB）附加字段。
  ③ **失败不阻塞**：OI 查询失败 → 特征留空 → 信号照常产生（不因附属指标拖垮主流程）。
  ④ **单一事实来源**：阈值常量在此定义，执行器/扫描器从这里 import。

用法（由 scan_gainers_v2.py 自动调用）：
  from gainer_oi_shadow import attach_oi_shadow
  feats = attach_oi_shadow(conn, symbol, t_signal_epoch)

校验（每日）：
  python bin/gainer_oi_shadow_report.py     # 放行组 vs 拦截组对比（影子，不影响实盘）
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# ── 单一事实来源：阈值常量 ──────────────────────────────────────────
# 单所口径标定（回测 bin/backtest_prescreen_dims.py --oi-source binance）：
#   oi_chg_12h 切点扫描（n=170，2026-08-27~2026-11-05）：
#     切点 0.30 → 保留 88.2%  胜 32.7%  均 +2.08%  PF 1.42  t=+1.04
#     切点 0.50 → 保留 68.8%  胜 35.9%  均 +3.69%  PF 1.79  t=+1.48
#     切点 0.80 → 保留 37.6%  胜 40.6%  均 +3.89%  PF 1.90  t=+1.54
#     切点 1.00 → 保留 21.8%  胜 54.1%  均 +8.15%  PF 3.51  t=+2.19  ← 选此
#   选 1.00 的理由：PF 3.51 / 胜率 54.1% 是单所口径下表现最好的切点，
#   且「OI 12h 翻倍以上」有明确经济含义（新增杠杆资金进场）。
#   ⚠ 单所样本仅 170 条（68 天），证据强度弱于多所口径（n=1,378，d=+0.437）。
#   ⚠ 多所口径（cg_oi_hist）无实时快照表 → 生产无法使用，故阈值不可直接搬用。
OI_SHADOW_THRESHOLD = 1.0
OI_SHADOW_WINDOW_H = 12          # 变化率窗口（小时）
OI_SHADOW_MIN_BUCKETS = 6        # 窗口内最少 5m 桶数（不足则特征留空）
# 窗口覆盖率下限：12h 满格 = 144 个 5m 桶；低于 40% 说明数据刚开始或币很新，
# 此时 oi_chg 的分母是「部分窗口」的值 → 数值不可比（实测有 buckets=30 的案例，
# 其 oi_chg 被算成 +104% 但实际只覆盖 2.5h）→ 覆盖率不足时特征留空（fail-open）。
OI_SHADOW_MIN_COVERAGE = 0.40

# 特征字段名（写入 params JSONB）
OI_KEY = "oi_chg_12h"
OI_BUCKET_KEY = "oi_bucket"
OI_COVERAGE_KEY = "oi_cov"
OI_SHADOW_PASS_KEY = "oi_shadow_pass"
OI_VERSION_KEY = "oi_shadow_ver"

OI_SHADOW_VERSION = "v1"
# 分档定义（基于阈值）：0=拦截侧(<阈值) / 1=放行侧(>=阈值)；-1=无数据
OI_BUCKET_BLOCK = 0
OI_BUCKET_PASS = 1
OI_BUCKET_NA = -1


def _fetch_oi_series(conn, symbol: str, t_signal: int, lookback_h: int) -> list[tuple]:
    """取该币 [t_signal−lookback, t_signal] 的 OI 桶（升序）。只读。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM ts) AS t, oi_usd
            FROM biz.oi_cvd_snapshot
            WHERE symbol = %s
              AND ts >= to_timestamp(%s)
              AND ts <= to_timestamp(%s)
            ORDER BY ts
        """, (symbol, float(t_signal - lookback_h * 3600), float(t_signal)))
        return cur.fetchall()


def compute_oi_features(conn, symbol: str, t_signal: int,
                        window_h: int = OI_SHADOW_WINDOW_H) -> dict:
    """算信号时刻的 OI 变化率（无前视：只取 ≤ t_signal 的桶）。

    口径：oi_chg = oi(最新桶) / oi(窗口内最早桶) − 1
      —— 与回测 SQL 侧聚合一致（array_agg 取最接近 t−W 的桶作分母）。

    覆盖率守门：窗口满格 = window_h × 12 个 5m 桶。实际桶数低于
    OI_SHADOW_MIN_COVERAGE × 满格 → 特征留空（fail-open）。
      原因：实测有币在 12h 窗口内只有 30 个桶（数据刚接入/新币），
      此时分母只代表部分窗口，算出的 oi_chg 与回测口径不可比。

    返回 dict：
      {"oi_chg_12h": float|None, "oi_bucket": int, "oi_shadow_pass": bool|None,
       "n_buckets": int, "coverage": float}
    失败一律返回空特征（不抛异常），由调用方决定是否落库。
    """
    out = {"oi_chg_12h": None, "oi_bucket": OI_BUCKET_NA,
           "oi_shadow_pass": None, "n_buckets": 0, "coverage": 0.0}
    try:
        rows = _fetch_oi_series(conn, symbol, t_signal, window_h)
    except Exception as e:  # noqa: BLE001 —— 附属指标失败不阻塞主流程
        print(f"[oi-shadow] {symbol} OI 查询失败：{type(e).__name__}: {str(e)[:60]}")
        return out
    if not rows:
        return out

    ts = [float(r[0]) for r in rows]
    oi = [float(r[1]) for r in rows if r[1] is not None]
    if len(oi) < OI_SHADOW_MIN_BUCKETS or len(oi) != len(ts):
        # 桶数不足或 OI 有 NULL → 特征留空（fail-open）
        return out

    full = window_h * 12                       # 12h × 12 桶/h = 144
    coverage = len(oi) / full if full else 0.0
    out["n_buckets"] = len(oi)
    out["coverage"] = coverage
    if coverage < OI_SHADOW_MIN_COVERAGE:
        # 覆盖不足：数值不可比 → 留空（记在 n_buckets/coverage 便于排查）
        return out

    first, last = oi[0], oi[-1]
    if first <= 0:
        return out
    chg = last / first - 1.0
    out["oi_chg_12h"] = chg
    passed = chg >= OI_SHADOW_THRESHOLD
    out["oi_shadow_pass"] = passed
    out["oi_bucket"] = OI_BUCKET_PASS if passed else OI_BUCKET_BLOCK
    return out


def attach_oi_shadow(conn, symbol: str, t_signal: int) -> dict:
    """算 OI 影子特征（供扫描器写入 params）。失败返回 {}。"""
    try:
        f = compute_oi_features(conn, symbol, t_signal)
    except Exception as e:  # noqa: BLE001
        print(f"[oi-shadow] {symbol} 影子特征异常：{type(e).__name__}: {str(e)[:60]}")
        return {}
    if f.get("oi_chg_12h") is None:
        return {}
    return {
        OI_KEY: round(f["oi_chg_12h"], 6),
        OI_BUCKET_KEY: f["oi_bucket"],
        OI_SHADOW_PASS_KEY: f["oi_shadow_pass"],
        OI_COVERAGE_KEY: round(f["coverage"], 4),
        OI_VERSION_KEY: OI_SHADOW_VERSION,
    }


def main() -> int:
    """自检：打印常量与一个样例币的计算结果。"""
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    print(f"[oi-shadow] 阈值：{OI_KEY} ≥ {OI_SHADOW_THRESHOLD}"
          f"（窗口 {OI_SHADOW_WINDOW_H}h，最少 {OI_SHADOW_MIN_BUCKETS} 桶）")
    print(f"[oi-shadow] 版本：{OI_SHADOW_VERSION}　模式：只记录不拦截")

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, EXTRACT(EPOCH FROM max(ts))
                FROM biz.oi_cvd_snapshot
                GROUP BY symbol ORDER BY count(*) DESC LIMIT 3
            """)
            rows = cur.fetchall()
        for sym, ts in rows:
            f = compute_oi_features(conn, sym, int(float(ts)))
            print(f"  {sym:14s} {OI_KEY}={f['oi_chg_12h']}  bucket={f['oi_bucket']}  "
                  f"pass={f['oi_shadow_pass']}  buckets={f['n_buckets']}  "
                  f"cov={f['coverage']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
