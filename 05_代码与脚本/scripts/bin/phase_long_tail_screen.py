#!/usr/bin/env python3
"""P2-① 长尾轻量初筛：全市场 7000+ 资产三轴低保真评分。

三轴：
  - holder_score：top10 集中度（低=健康）+ holder_change_7d（正=吸筹）+ whale 回补
  - social_score：asset_social_heat.score + dex_boost_score（DEX 热搜）
  - momentum_score：asset_market_daily 24h 动量（change_24h 归一）

composite_lowfi = 三轴加权（默认各 1/3）；缺失轴计 0 不崩。
结果落 biz.long_tail_screen。

用法：
    python phase_long_tail_screen.py               # 全量扫 7000+ 资产
    python phase_long_tail_screen.py --dry-run     # 预览不写库
    python phase_long_tail_screen.py --limit 100   # 限制扫描数量（调试用）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

import psycopg
import psycopg.rows

# ── 评分权重（yaml 可调，此处默认） ──
HOLDER_WEIGHT = 0.34
SOCIAL_WEIGHT = 0.33
MOMENTUM_WEIGHT = 0.33


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _clamp01(v: float) -> float:
    return max(0.0, min(100.0, v))


def _score_holder(row: dict) -> float:
    """holder 轴：集中度低 + 持有者增长 + 鲸鱼回补 → 高分。"""
    t10 = _safe_float(row.get("top10_concentration"))
    h7d = row.get("holder_change_7d")
    h7d_pct = _safe_float(row.get("holder_change_7d_pct"))
    whale7d = _safe_float(row.get("whale_balance_change_7d_pct"))
    total = _safe_float(row.get("total_holders"))

    parts = []
    # 集中度：t10 越低越好（0%=完美分散，100%=全部一人）
    if t10 is not None:
        parts.append(max(0, (100 - t10) * 0.4))
    # 持有者增长：正增长 = 吸筹
    if h7d is not None and total > 0:
        parts.append(min(30, max(0, h7d_pct * 3)))
    # 鲸鱼回补：whale 7d 转负暂停或正增长
    if whale7d is not None:
        parts.append(min(30, max(0, -whale7d * 2 + 15)))

    return _clamp01(sum(parts) if parts else 0)


def _score_social(row: dict) -> float:
    """social 轴：热度分 + DEX 热搜。"""
    score = _safe_float(row.get("score"))
    dex_boost = _safe_float(row.get("dex_boost_score"))
    # score 0-100 直接用；dex_boost 额外加成
    return _clamp01(score * 0.7 + min(30, dex_boost * 0.3))


def _score_momentum(row: dict) -> float:
    """momentum 轴：24h change_24h 归一化到 0-100。"""
    chg = _safe_float(row.get("change_24h"))
    # -50% → 0, 0% → 50, +50% → 100（截断到 [-50, +50]）
    return _clamp01((max(-50, min(50, chg)) + 50) / 100 * 100)


def screen_all(dry_run: bool = False, limit: int | None = None) -> dict:
    settings = get_settings(require_database=True)
    today = date.today()

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # ── 1. 拉全量有 holder 数据的资产（主集） ──
            cur.execute("""
                SELECT DISTINCT hs.asset_id
                FROM biz.onchain_holder_snapshot hs
                WHERE hs.snapshot_date = (
                    SELECT MAX(snapshot_date) FROM biz.onchain_holder_snapshot
                )
            """)
            asset_ids = [r["asset_id"] for r in cur.fetchall()]

            if limit:
                asset_ids = asset_ids[:limit]

            if not asset_ids:
                return {"ok": True, "count": 0, "dry_run": dry_run}

            # ── 2. 批量拉取三源数据 ──
            placeholders = ",".join(["%s"] * len(asset_ids))

            # holder 数据
            cur.execute(f"""
                SELECT hs.asset_id, a.canonical_symbol AS symbol, ac.chain,
                       hs.top10_concentration, hs.total_holders,
                       hs.holder_change_7d, hs.holder_change_30d,
                       hs.whale_balance_change_7d_pct, hs.whale_balance_change_30d_pct,
                       hs.exchange_wallet_pct
                FROM biz.onchain_holder_snapshot hs
                JOIN core.asset a ON a.asset_id = hs.asset_id
                LEFT JOIN core.asset_contract ac
                  ON ac.asset_id = a.asset_id AND ac.is_primary = true
                WHERE hs.asset_id IN ({placeholders})
                  AND hs.snapshot_date = (
                      SELECT MAX(snapshot_date) FROM biz.onchain_holder_snapshot
                  )
            """, tuple(asset_ids))
            holder_rows = {r["asset_id"]: r for r in cur.fetchall()}

            # social 数据
            cur.execute(f"""
                SELECT asset_id, score, dex_boost_score
                FROM biz.asset_social_heat
                WHERE asset_id IN ({placeholders})
            """, tuple(asset_ids))
            social_rows = {r["asset_id"]: r for r in cur.fetchall()}

            # momentum 数据（最新日 change_24h）
            cur.execute(f"""
                SELECT asset_id, change_24h
                FROM biz.asset_market_daily
                WHERE asset_id IN ({placeholders})
                  AND market_date = (
                      SELECT MAX(market_date) FROM biz.asset_market_daily
                      WHERE asset_id IN ({placeholders})
                  )
            """, tuple(asset_ids) + tuple(asset_ids))
            momentum_rows = {r["asset_id"]: r for r in cur.fetchall()}

            # MVRV bonus（仅 15 币）
            cur.execute("""
                SELECT DISTINCT ON (asset_id) asset_id, mvrv_pct_full
                FROM biz.cm_onchain_percentile_full
                WHERE mvrv_pct_full IS NOT NULL
                ORDER BY asset_id, metric_date DESC
            """)
            mvrv_rows = {r["asset_id"]: r for r in cur.fetchall()}

            # ── 3. 逐资产评分 ──
            results = []
            for aid in asset_ids:
                holder = holder_rows.get(aid) or {}
                social = social_rows.get(aid) or {}
                momentum = momentum_rows.get(aid) or {}
                mvrv = mvrv_rows.get(aid)

                symbol = holder.get("symbol") or ""
                chain = holder.get("chain") or ""

                hs = _score_holder(holder)
                ss = _score_social(social)
                ms = _score_momentum(momentum)

                composite = hs * HOLDER_WEIGHT + ss * SOCIAL_WEIGHT + ms * MOMENTUM_WEIGHT

                # MVRV bonus：低估值加分
                mvrv_bonus = 0
                if mvrv and mvrv.get("mvrv_pct_full") is not None:
                    pct = float(mvrv["mvrv_pct_full"])
                    if pct <= 20:
                        mvrv_bonus = 20
                    elif pct <= 40:
                        mvrv_bonus = 10
                composite = _clamp01(composite + mvrv_bonus)

                signals = {
                    "holder": {"t10": holder.get("top10_concentration"), "h7d": holder.get("holder_change_7d"), "whale7d": holder.get("whale_balance_change_7d_pct")},
                    "social": {"score": social.get("score"), "dex_boost": social.get("dex_boost_score")},
                    "momentum": {"change_24h": momentum.get("change_24h")},
                }

                results.append({
                    "asset_id": aid,
                    "symbol": symbol,
                    "chain": chain,
                    "holder_score": round(hs, 2),
                    "social_score": round(ss, 2),
                    "momentum_score": round(ms, 2),
                    "composite_lowfi": round(composite, 2),
                    "mvrv_bonus": mvrv_bonus,
                    "signals_json": signals,
                })

            # ── 4. 写入 long_tail_screen ──
            if not dry_run and results:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS biz.long_tail_screen (
                        screen_id SERIAL PRIMARY KEY,
                        asset_id INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        chain TEXT,
                        screen_date DATE NOT NULL DEFAULT CURRENT_DATE,
                        holder_score NUMERIC(5,2) DEFAULT 0,
                        social_score NUMERIC(5,2) DEFAULT 0,
                        momentum_score NUMERIC(5,2) DEFAULT 0,
                        composite_lowfi NUMERIC(5,2) DEFAULT 0,
                        mvrv_bonus NUMERIC(5,2) DEFAULT 0,
                        signals_json JSONB,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (asset_id, screen_date)
                    )
                """)
                cur.execute("DELETE FROM biz.long_tail_screen WHERE screen_date = %s", (today,))
                inserted = 0
                for r in results:
                    cur.execute("""
                        INSERT INTO biz.long_tail_screen
                            (asset_id, symbol, chain, screen_date,
                             holder_score, social_score, momentum_score,
                             composite_lowfi, mvrv_bonus, signals_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """, (
                        r["asset_id"], r["symbol"], r["chain"], today,
                        r["holder_score"], r["social_score"], r["momentum_score"],
                        r["composite_lowfi"], r["mvrv_bonus"],
                        json.dumps(r["signals_json"], default=str),
                    ))
                    inserted += 1
                conn.commit()
                print(f"[OK] 写入 {inserted} 条到 biz.long_tail_screen")
            else:
                print(f"[DRY-RUN] 预览 {len(results)} 条，未写库")

            # 统计
            scores = [r["composite_lowfi"] for r in results]
            print(f"  资产数: {len(results)}")
            print(f"  composite_lowfi: min={min(scores) if scores else 0:.1f}, max={max(scores) if scores else 0:.1f}, avg={sum(scores)/len(scores) if scores else 0:.1f}")

            return {"ok": True, "count": len(results), "dry_run": dry_run}


def main():
    parser = argparse.ArgumentParser(description="P2-① 长尾轻量初筛")
    parser.add_argument("--dry-run", action="store_true", help="预览不写库")
    parser.add_argument("--limit", type=int, default=None, help="限制扫描数量")
    args = parser.parse_args()

    t0 = time.time()
    result = screen_all(dry_run=args.dry_run, limit=args.limit)
    elapsed = time.time() - t0
    print(f"耗时: {elapsed:.1f}s")
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
