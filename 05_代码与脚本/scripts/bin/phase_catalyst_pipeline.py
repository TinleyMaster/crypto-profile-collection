#!/usr/bin/env python3
"""
催化剂决策管道主入口（P1 版本）。

快通道（--fast）：
    L1 规则兜底分类 + G0 市场环境 + G1 分级 + G2 共振 + G6 信号骨架
    目标：15 分钟级，零 LLM

慢通道（--slow）：
    G3-1 二阶受益展开 + G3 持续性预判 + G4 基本面 + G5 技术面 + G6 信号重算 + 过期巡检
    目标：小时级，补全快通道占位字段，扩展二阶机会

用法：
    python phase_catalyst_pipeline.py --fast          # 快通道增量
    python phase_catalyst_pipeline.py --slow          # 慢通道（G3-G5 补全 + 巡检）
    python phase_catalyst_pipeline.py --fast --slow   # 全链路
    python phase_catalyst_pipeline.py --catalyst-id 123  # 单条重算
    python phase_catalyst_pipeline.py --health        # 输出健康状态
"""
from __future__ import annotations

import argparse
import os
import sys
import yaml
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_dir_with(target_marker: str, candidates: list[Path]) -> Path | None:
    """在候选目录中找到包含 target_marker 的目录。"""
    for d in candidates:
        if (d / target_marker).exists():
            return d
    return None


def _setup_paths() -> tuple[Path, Path]:
    """
    探测并设置 sys.path，兼容两种部署结构：
      - 本地开发：  project/workbench/catalyst/  project/scripts/src/
      - 容器部署：  /app/catalyst/               /app/scripts/src/

    返回 (base_dir, src_dir)：
      base_dir: 含 catalyst 包的目录（即 workbench 或 /app）
      src_dir:  含 scripts/src 的父目录
    """
    project_root = SCRIPT_DIR.parent.parent

    # 候选的 base_dir（含 catalyst 包的目录）
    base_candidates = [
        project_root / "workbench",   # 本地结构
        project_root,                  # 容器结构（catalyst 直接在 /app 下）
        Path("/app"),                  # 兜底
    ]
    base_dir = _find_dir_with("catalyst/__init__.py", base_candidates)
    if base_dir is None:
        raise RuntimeError(
            f"找不到 catalyst 包，已探测: {[str(p) for p in base_candidates]}"
        )

    # 候选的 src_dir（含 scripts/src 的父目录）
    src_candidates = [
        project_root,                  # 本地 + 容器，scripts 都在 project 根
        Path("/app"),                  # 兜底
    ]
    # scripts/src 的标记文件：找一个确定存在的
    src_dir = _find_dir_with("scripts/src", src_candidates)
    if src_dir is None:
        # 退一步，scripts 直接在 base_dir 同级
        src_dir = base_dir.parent if (base_dir.parent / "scripts" / "src").exists() else base_dir

    # 将 base_dir 加入 sys.path（使 catalyst 包可导入）
    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))

    # 将 scripts/src 的父目录加入 sys.path
    scripts_src = src_dir / "scripts" / "src"
    if scripts_src.exists() and str(scripts_src) not in sys.path:
        sys.path.insert(0, str(scripts_src))

    return base_dir, src_dir


BASE_DIR, _SRC_ROOT = _setup_paths()

from catalyst.db import get_conn
from catalyst.classify import RuleEventClassifier
from catalyst.grade import CatalystGrader, MarketRegime
from catalyst.resonance import (
    ResonanceScorer,
    get_cmc_snapshot_ret,
    get_btc_cmc_snapshot_ret,
    calc_vol_zscore,
    get_peer_median_ret,
)
from catalyst.second_order import PersistenceScorer
from catalyst.fundamental import FundamentalChecker
from catalyst.technical import TechnicalAnalyzer
from catalyst.signal import CatalystSignalBuilder, expire_signals
from catalyst.notifier import send_fast_alerts_for_new_signals, send_slow_digest


def load_config() -> dict:
    """加载 catalyst_rules.yaml 配置，兼容本地与容器路径。"""
    candidates = [
        BASE_DIR / "catalyst_rules.yaml",
        Path(__file__).resolve().parent.parent.parent / "workbench" / "catalyst_rules.yaml",
        Path("/app/catalyst_rules.yaml"),
    ]
    for p in candidates:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    raise FileNotFoundError(
        f"找不到 catalyst_rules.yaml，已探测: {[str(p) for p in candidates]}"
    )


# =====================================================================
# L1: 规则兜底分类（补 rule_event_type）
# =====================================================================

def run_classify(conn, classifier: RuleEventClassifier,
                 catalyst_id: int | None = None,
                 limit: int | None = None) -> int:
    """给缺少 rule_event_type 的催化剂补规则分类。

    Returns:
        处理数量
    """
    # 找出未分类的 catalyst
    query = """
        SELECT catalyst_id, title, body_text, related_pairs
        FROM biz.asset_catalyst
        WHERE rule_event_type IS NULL
    """
    params = []
    if catalyst_id is not None:
        query += " AND catalyst_id = %s"
        params.append(catalyst_id)
    query += " ORDER BY catalyst_id DESC"
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return 0

    count = 0
    for row in rows:
        pairs = row["related_pairs"] or []
        event_type = classifier.classify(
            row["title"] or "",
            row["body_text"] or "",
            pairs,
        )
        conn.execute(
            "UPDATE biz.asset_catalyst SET rule_event_type = %s, updated_at = NOW() WHERE catalyst_id = %s",
            (event_type, row["catalyst_id"]),
        )
        count += 1

    return count


# =====================================================================
# G0: 市场环境快照
# =====================================================================

def run_regime(conn, regime_calc: MarketRegime) -> str:
    """计算并写入当日市场环境。

    Returns:
        当日 regime
    """
    # 获取 BTC 7日涨幅（从 CMC 快照的 percent_change_7d）
    btc_snap = get_btc_cmc_snapshot_ret(conn)
    btc_7d = btc_snap.get("percent_change_7d", 0) or 0

    # P0 简化：total_mcap_7d 暂时用 0（后续可以补全市场总市值）
    result = regime_calc.determine(btc_7d=btc_7d, total_mcap_7d=0.0)
    regime_calc.upsert_to_db(conn, result)
    return result.regime


# =====================================================================
# G1: 催化剂分级
# =====================================================================

def run_grade(conn, grader: CatalystGrader,
              catalyst_id: int | None = None,
              limit: int | None = None) -> int:
    """对未分级的催化剂做 G1 分级。

    Returns:
        处理数量
    """
    query = """
        SELECT ac.*,
               ARRAY_AGG(cal.asset_id) FILTER (WHERE cal.asset_id IS NOT NULL) AS linked_asset_ids
        FROM biz.asset_catalyst ac
        LEFT JOIN biz.catalyst_asset_link cal ON ac.catalyst_id = cal.catalyst_id
        WHERE ac.rule_event_type IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM biz.catalyst_grade cg WHERE cg.catalyst_id = ac.catalyst_id
          )
    """
    params = []
    if catalyst_id is not None:
        query += " AND ac.catalyst_id = %s"
        params.append(catalyst_id)
    query += " GROUP BY ac.catalyst_id"
    query += " ORDER BY ac.catalyst_id DESC"
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return 0

    count = 0
    for row in rows:
        linked = [{"asset_id": aid} for aid in (row["linked_asset_ids"] or [])]
        result = grader.grade(dict(row), linked_assets=linked if linked else None)
        grader.upsert_to_db(conn, result)
        count += 1

    return count


# =====================================================================
# G2: 共振打分
# =====================================================================

def run_resonance(conn, scorer: ResonanceScorer,
                  catalyst_id: int | None = None,
                  limit: int | None = None) -> int:
    """对有分级+有资产链接的催化剂做 G2 共振打分。

    P0 简化版：用 24h 窗口作为主窗口，数据来自 CMC 快照。

    Returns:
        处理数量
    """
    query = """
        SELECT DISTINCT ac.catalyst_id, ac.published_at,
                        cal.asset_id,
                        ci.impact_direction
        FROM biz.asset_catalyst ac
        JOIN biz.catalyst_asset_link cal ON ac.catalyst_id = cal.catalyst_id
        JOIN biz.catalyst_grade cg ON ac.catalyst_id = cg.catalyst_id
        LEFT JOIN biz.catalyst_impact ci ON ac.catalyst_id = ci.catalyst_id AND cal.asset_id = ci.asset_id
        WHERE cg.catalyst_kind != 'noise'
          AND NOT EXISTS (
              SELECT 1 FROM biz.catalyst_resonance cr
              WHERE cr.catalyst_id = ac.catalyst_id AND cr.asset_id = cal.asset_id
          )
    """
    params = []
    if catalyst_id is not None:
        query += " AND ac.catalyst_id = %s"
        params.append(catalyst_id)
    query += " ORDER BY ac.catalyst_id DESC"
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return 0

    # 获取 BTC 24h 收益
    btc_snap = get_btc_cmc_snapshot_ret(conn)
    btc_24h = btc_snap.get("percent_change_24h", 0) or 0

    count = 0
    for row in rows:
        asset_id = row["asset_id"]
        catalyst_id_val = row["catalyst_id"]
        impact_dir = row["impact_direction"] or "bullish"

        # 获取资产收益 + 量能
        asset_snap = get_cmc_snapshot_ret(conn, asset_id)
        asset_24h = asset_snap.get("percent_change_24h")
        vol_24h = asset_snap.get("volume_24h")

        if asset_24h is None:
            continue  # 无行情数据跳过

        # 量能 z-score
        vol_z = calc_vol_zscore(conn, asset_id, vol_24h or 0) if vol_24h else None

        # peer 中位数
        peer_ret = get_peer_median_ret(conn, asset_id, window_days=1)

        # 计算共振（24h 作为主窗口）
        result = scorer.compute(
            asset_ret_pct=asset_24h,
            btc_ret_pct=btc_24h,
            vol_zscore=vol_z or 0.0,
            impact_direction=impact_dir,
            peer_median_ret=peer_ret,
            window="24h",
        )
        result.catalyst_id = catalyst_id_val
        result.asset_id = asset_id

        # 也填一下 1h/7d 的（如果有数据）
        if asset_snap.get("percent_change_1h") is not None:
            btc_1h = btc_snap.get("percent_change_1h", 0) or 0
            result.excess_ret_1h = round(asset_snap["percent_change_1h"] - btc_1h, 4)
        if asset_snap.get("percent_change_7d") is not None:
            btc_7d = btc_snap.get("percent_change_7d", 0) or 0
            result.excess_ret_72h = round(asset_snap["percent_change_7d"] - btc_7d, 4)

        scorer.upsert_to_db(conn, result)
        count += 1

    return count


# =====================================================================
# G6: 信号构建
# =====================================================================

def run_signal(conn, builder: CatalystSignalBuilder,
               regime: str,
               catalyst_id: int | None = None,
               limit: int | None = None) -> tuple[int, int, list[int]]:
    """对有 grade + resonance 的资产构建信号。

    Returns:
        (处理数, 入信号表数, 新/更新信号 ID 列表)
    """
    query = """
        SELECT cr.catalyst_id, cr.asset_id, cr.resonance_score, cr.resonance_state,
               cg.catalyst_kind, cg.base_strength,
               ac.published_at
        FROM biz.catalyst_resonance cr
        JOIN biz.catalyst_grade cg ON cr.catalyst_id = cg.catalyst_id
        JOIN biz.asset_catalyst ac ON cr.catalyst_id = ac.catalyst_id
        WHERE cg.catalyst_kind != 'noise'
          AND cr.resonance_state != 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM biz.catalyst_signal cs
              WHERE cs.catalyst_id = cr.catalyst_id AND cs.asset_id = cr.asset_id
          )
    """
    params = []
    if catalyst_id is not None:
        query += " AND cr.catalyst_id = %s"
        params.append(catalyst_id)
    query += " ORDER BY cr.catalyst_id DESC"
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return 0, 0, []

    processed = 0
    inserted = 0
    new_signal_ids: list[int] = []
    for row in rows:
        # P0: persistence 预判值（根据 kind）
        persistence = "structural" if row["catalyst_kind"] == "structural" else (
            "one_off" if row["catalyst_kind"] == "event" else "decaying"
        )

        signal = builder.build(
            catalyst_id=row["catalyst_id"],
            asset_id=row["asset_id"],
            kind=row["catalyst_kind"],
            base_strength=row["base_strength"],
            resonance_score=row["resonance_score"],
            resonance_state=row["resonance_state"],
            published_at=row["published_at"],
            persistence=persistence,
            fundamental_pass=None,     # P0 占位
            technical_state=None,      # P0 占位
            regime=regime,
        )
        processed += 1
        sig_id = builder.upsert_to_db(conn, signal)
        if sig_id:
            inserted += 1
            new_signal_ids.append(sig_id)

    return processed, inserted, new_signal_ids


# =====================================================================
# 慢通道：二阶受益增量展开
# =====================================================================

def run_slow_second_order(conn, config: dict,
                          lookback_hours: int = 24,
                          limit: int | None = None) -> dict:
    """慢通道增量展开二阶受益资产。

    处理最近 lookback_hours 内新分级的 structural/event 级 catalyst，
    生成二阶受益映射 + 初始信号骨架（G3-G5 留空，后续 run_slow_g3g5 补全）。

    Returns:
        dict with second_order_count, new_signals
    """
    from catalyst.second_order import SecondOrderMapper
    from catalyst.signal import CatalystSignalBuilder
    from collections import defaultdict
    from datetime import timedelta

    mapper = SecondOrderMapper(config)
    builder = CatalystSignalBuilder(config)

    # 1. 找出最近 N 小时内新分级的 structural/event catalyst
    #    且还没有任何二阶映射记录的
    query = """
        SELECT DISTINCT ON (cg.catalyst_id)
               cg.catalyst_id, cg.catalyst_kind, cg.base_strength,
               ac.rule_event_type, ac.published_at
        FROM biz.catalyst_grade cg
        JOIN biz.asset_catalyst ac ON cg.catalyst_id = ac.catalyst_id
        WHERE cg.catalyst_kind IN ('structural', 'event')
          AND cg.base_strength >= 30
          AND cg.created_at >= NOW() - INTERVAL '%s hours'
          AND NOT EXISTS (
            SELECT 1 FROM biz.catalyst_second_order cso
            WHERE cso.catalyst_id = cg.catalyst_id
          )
        ORDER BY cg.catalyst_id, cg.created_at DESC
    """
    params = [lookback_hours]
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    cat_rows = conn.execute(query, params).fetchall()
    if not cat_rows:
        return {"second_order_count": 0, "new_signals": 0}

    catalyst_ids = [r["catalyst_id"] for r in cat_rows]

    # 2. 批量查直连资产 + sector
    link_rows = conn.execute("""
        SELECT cal.catalyst_id, cal.asset_id, a.primary_sector
        FROM biz.catalyst_asset_link cal
        JOIN core.asset a ON cal.asset_id = a.asset_id
        WHERE cal.catalyst_id = ANY(%s::BIGINT[])
          AND a.primary_sector IS NOT NULL
    """, (catalyst_ids,)).fetchall()

    cat_links = defaultdict(list)
    cat_sectors = defaultdict(set)
    for r in link_rows:
        cat_links[r["catalyst_id"]].append({"asset_id": r["asset_id"], "sector": r["primary_sector"]})
        if r["primary_sector"]:
            cat_sectors[r["catalyst_id"]].add(r["primary_sector"])

    # 3. 预加载各板块资产池
    all_sectors = set()
    for s_set in cat_sectors.values():
        all_sectors.update(s_set)
    all_sectors = list(all_sectors)

    sector_pool = defaultdict(list)
    if all_sectors:
        pool_rows = conn.execute("""
            SELECT DISTINCT ON (a.asset_id) a.asset_id, a.primary_sector,
                   COALESCE(q.market_cap, 0) as market_cap
            FROM core.asset a
            JOIN core.asset_source_map asm
              ON a.asset_id = asm.asset_id AND asm.source_code = 'cmc' AND asm.is_primary = true
            LEFT JOIN src_cmc.cmc_asset_quote_snapshot q
              ON q.cmc_id = asm.source_asset_key::bigint
            WHERE a.primary_sector = ANY(%s::TEXT[])
              AND a.asset_type IN ('coin', 'token')
            ORDER BY a.asset_id, q.quote_time DESC
        """, (all_sectors,)).fetchall()
        for r in pool_rows:
            mc = float(r["market_cap"]) if r.get("market_cap") else 0.0
            sector_pool[r["primary_sector"]].append({"asset_id": r["asset_id"], "market_cap": mc})
        for sector in sector_pool:
            sector_pool[sector].sort(key=lambda x: x["market_cap"], reverse=True)

    # 4. 生成二阶映射
    from catalyst.second_order import SecondOrderResult
    all_so_results = []
    for cat_row in cat_rows:
        catalyst_id = cat_row["catalyst_id"]
        kind = cat_row["catalyst_kind"]
        base_strength = int(cat_row["base_strength"])

        if not mapper.should_do_second_order(kind, base_strength):
            continue

        direct_assets = [l["asset_id"] for l in cat_links.get(catalyst_id, [])]
        direct_sectors = list(cat_sectors.get(catalyst_id, set()))
        if not direct_sectors:
            continue

        for sector in direct_sectors:
            pool = sector_pool.get(sector, [])
            direct_set = set(direct_assets)
            count = 0
            for cand in pool:
                if cand["asset_id"] in direct_set:
                    continue
                conf = mapper.order2_confidence
                if base_strength >= 70:
                    conf = min(conf + 0.1, 0.8)
                elif base_strength >= 50:
                    conf = min(conf + 0.05, 0.7)
                all_so_results.append(SecondOrderResult(
                    catalyst_id=catalyst_id,
                    asset_id=cand["asset_id"],
                    order_level=2,
                    confidence=conf,
                    sector_name=sector,
                    link_basis="sector",
                ))
                count += 1
                if count >= mapper.max_second_order:
                    break

    if not all_so_results:
        return {"second_order_count": 0, "new_signals": 0}

    # 5. 写入二阶映射 + 生成信号骨架
    SecondOrderMapper.batch_upsert(conn, all_so_results)

    cat_info = {}
    for r in cat_rows:
        cat_info[r["catalyst_id"]] = {
            "kind": r["catalyst_kind"],
            "base_strength": int(r["base_strength"]),
            "rule_event_type": r["rule_event_type"] or "other",
            "published_at": r["published_at"],
        }

    # 查每个 catalyst 的直连资产 resonance 中位数
    res_rows = conn.execute("""
        SELECT cr.catalyst_id, cr.resonance_score
        FROM biz.catalyst_resonance cr
        WHERE cr.catalyst_id = ANY(%s::BIGINT[])
    """, (catalyst_ids,)).fetchall()
    cat_res = defaultdict(list)
    for r in res_rows:
        cat_res[r["catalyst_id"]].append(int(r["resonance_score"] or 0))

    cat_peer_median = {}
    for cat_id in catalyst_ids:
        scores = cat_res.get(cat_id, [])
        cat_peer_median[cat_id] = sorted(scores)[len(scores) // 2] if scores else 0

    signals = []
    for so in all_so_results:
        cat_id = so.catalyst_id
        info = cat_info.get(cat_id)
        if not info:
            continue

        order2_strength = int(info["base_strength"] * 0.6)
        peer_median = cat_peer_median.get(cat_id, 0)
        res_score = max(0, int(peer_median * 0.7))
        res_state = "confirmed" if res_score >= 70 else ("weak" if res_score >= 35 else "pending")

        kind = info["kind"]
        if kind == "structural":
            expires_at = info["published_at"] + timedelta(days=7)
        elif kind == "event":
            expires_at = info["published_at"] + timedelta(days=3)
        else:
            expires_at = info["published_at"] + timedelta(days=1)

        sig = builder.build(
            catalyst_id=cat_id,
            asset_id=so.asset_id,
            kind=kind,
            base_strength=order2_strength,
            resonance_score=res_score,
            resonance_state=res_state,
            published_at=info["published_at"],
            persistence=None,
            fundamental_pass=None,
            technical_state=None,
            regime=None,
            entry_price=None,
            stop_loss=None,
            take_profit=None,
        )
        if not sig.tier:
            continue

        signals.append({
            "catalyst_id": cat_id,
            "asset_id": so.asset_id,
            "kind": sig.kind,
            "base_strength": sig.base_strength,
            "resonance_score": sig.resonance_score,
            "resonance_state": sig.resonance_state,
            "persistence": sig.persistence,
            "persistence_verified": False,
            "fundamental_pass": sig.fundamental_pass,
            "technical_state": sig.technical_state,
            "entry_trigger": None,
            "entry_trigger_price": None,
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "rr_ratio": None,
            "composite_score": sig.composite_score,
            "tier": sig.tier,
            "confidence": sig.confidence,
            "regime": sig.regime,
            "invalidation": sig.invalidation,
            "expires_at": expires_at,
            "status": "open",
        })

    if signals:
        conn.execute("""
            CREATE TEMP TABLE tmp_slow_so_signals (
                catalyst_id BIGINT, asset_id BIGINT, kind TEXT,
                base_strength SMALLINT, resonance_score SMALLINT,
                resonance_state TEXT, persistence TEXT,
                persistence_verified BOOLEAN, fundamental_pass BOOLEAN,
                fundamental_detail JSONB, technical_state TEXT,
                entry_trigger TEXT, entry_trigger_price NUMERIC,
                entry_price NUMERIC, stop_loss NUMERIC,
                take_profit NUMERIC, rr_ratio NUMERIC(6,2),
                composite_score SMALLINT, tier TEXT,
                confidence NUMERIC(4,3), regime TEXT,
                invalidation TEXT, expires_at TIMESTAMPTZ, status TEXT
            ) ON COMMIT DROP
        """)
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO tmp_slow_so_signals VALUES (
                    %(catalyst_id)s, %(asset_id)s, %(kind)s,
                    %(base_strength)s, %(resonance_score)s,
                    %(resonance_state)s, %(persistence)s,
                    %(persistence_verified)s, %(fundamental_pass)s,
                    NULL::jsonb, %(technical_state)s,
                    %(entry_trigger)s, %(entry_trigger_price)s,
                    %(entry_price)s, %(stop_loss)s,
                    %(take_profit)s, %(rr_ratio)s,
                    %(composite_score)s, %(tier)s,
                    %(confidence)s, %(regime)s,
                    %(invalidation)s, %(expires_at)s, %(status)s
                )
            """, signals)

        conn.execute("""
            INSERT INTO biz.catalyst_signal (
                catalyst_id, asset_id, kind, base_strength,
                resonance_score, resonance_state, persistence,
                persistence_verified, fundamental_pass, fundamental_detail,
                technical_state, entry_trigger, entry_trigger_price,
                entry_price, stop_loss, take_profit, rr_ratio,
                composite_score, tier, confidence, regime,
                invalidation, expires_at, status
            )
            SELECT t.catalyst_id, t.asset_id, t.kind, t.base_strength,
                   t.resonance_score, t.resonance_state, t.persistence,
                   t.persistence_verified, t.fundamental_pass, t.fundamental_detail,
                   t.technical_state, t.entry_trigger, t.entry_trigger_price,
                   t.entry_price, t.stop_loss, t.take_profit, t.rr_ratio,
                   t.composite_score, t.tier, t.confidence, t.regime,
                   t.invalidation, t.expires_at, t.status
            FROM tmp_slow_so_signals t
            ON CONFLICT (catalyst_id, asset_id) DO NOTHING
        """)

    return {
        "second_order_count": len(all_so_results),
        "new_signals": len(signals),
        "catalyst_count": len(cat_rows),
    }


# =====================================================================
# 慢通道：G3-G5 增量计算 + 信号重算
# =====================================================================

def run_slow_g3g5(conn, config: dict,
                  limit: int | None = None) -> dict:
    """慢通道增量计算 G3（持续性预判）、G4（基本面）、G5（技术面），并重算信号。

    处理对象：open 状态且 persistence/technical_state 仍为占位值的信号
    （快通道生成时 G3-G5 给占位，慢通道补全）。

    Returns:
        dict with processed, recalculated counts
    """
    import json
    from collections import defaultdict

    pers_scorer = PersistenceScorer(config)
    fund_checker = FundamentalChecker(config)
    tech_analyzer = TechnicalAnalyzer(config)
    builder = CatalystSignalBuilder(config)

    # 1. 找出需要补 G3-G5 的 open 信号
    query = """
        SELECT cs.signal_id, cs.catalyst_id, cs.asset_id,
               cs.kind, cs.base_strength,
               cs.resonance_score, cs.resonance_state,
               cs.regime, cs.expires_at,
               ac.rule_event_type, ac.published_at,
               a.asset_type, a.primary_sector,
               ci.impact_direction
        FROM biz.catalyst_signal cs
        JOIN biz.asset_catalyst ac ON cs.catalyst_id = ac.catalyst_id
        JOIN core.asset a ON cs.asset_id = a.asset_id
        LEFT JOIN biz.catalyst_impact ci
          ON cs.catalyst_id = ci.catalyst_id AND cs.asset_id = ci.asset_id
        WHERE cs.status = 'open'
          AND (cs.persistence IS NULL
               OR cs.technical_state IS NULL
               OR cs.fundamental_pass IS NULL)
        ORDER BY cs.catalyst_id DESC
    """
    params = []
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return {"processed": 0, "recalculated": 0}

    asset_ids = list(set(r["asset_id"] for r in rows))

    # 2. 批量查基本面数据
    # 生命周期
    life_rows = conn.execute("""
        SELECT asset_id, stage
        FROM biz.asset_lifecycle
        WHERE asset_id = ANY(%s::INT[])
    """, (asset_ids,)).fetchall()
    life_map = {r["asset_id"]: r["stage"] for r in life_rows}

    # 风险标签
    risk_rows = conn.execute("""
        SELECT asset_id, total_score
        FROM biz.asset_risk_labels
        WHERE asset_id = ANY(%s::INT[])
    """, (asset_ids,)).fetchall()
    risk_map = {r["asset_id"]: int(r["total_score"]) if r.get("total_score") is not None else None
                for r in risk_rows}

    # 流动性
    liq_rows = conn.execute("""
        SELECT DISTINCT ON (asset_id) asset_id, total_liquidity_usd
        FROM biz.asset_liquidity
        WHERE asset_id = ANY(%s::INT[])
        ORDER BY asset_id, scanned_at DESC
    """, (asset_ids,)).fetchall()
    liq_map = {r["asset_id"]: float(r["total_liquidity_usd"]) if r.get("total_liquidity_usd") else None
               for r in liq_rows}

    # 解锁压力
    unlock_rows = conn.execute("""
        SELECT asset_id, risk_level
        FROM biz.asset_unlock_pressure
        WHERE asset_id = ANY(%s::INT[])
    """, (asset_ids,)).fetchall()
    unlock_map = {r["asset_id"]: r["risk_level"] for r in unlock_rows}

    # 3. 批量查日线（60 天）
    daily_rows = conn.execute("""
        SELECT asset_id, market_date, price_usd, volume_24h
        FROM biz.asset_market_daily
        WHERE asset_id = ANY(%s::INT[])
          AND market_date >= NOW() - INTERVAL '60 days'
        ORDER BY asset_id, market_date ASC
    """, (asset_ids,)).fetchall()
    daily_map = defaultdict(list)
    for r in daily_rows:
        daily_map[r["asset_id"]].append({
            "market_date": r["market_date"],
            "price_usd": float(r["price_usd"]) if r.get("price_usd") is not None else None,
            "volume_24h": float(r["volume_24h"]) if r.get("volume_24h") is not None else None,
        })

    # 4. 内存计算 + 批量更新
    updates = []
    tier_dist = defaultdict(int)
    for row in rows:
        asset_id = row["asset_id"]
        catalyst_id = row["catalyst_id"]
        kind = row["kind"] or "event"
        base_strength = int(row["base_strength"] or 0)
        event_type = row["rule_event_type"] or "other"
        asset_type = row["asset_type"]
        sector = row["primary_sector"]
        impact_direction = row["impact_direction"]

        # G3 持续性预判
        persistence = pers_scorer.predict(event_type, kind, base_strength)

        # G4 基本面
        fund_result = fund_checker.compute(
            asset_id=asset_id,
            asset_type=asset_type,
            lifecycle_phase=life_map.get(asset_id),
            risk_score=risk_map.get(asset_id),
            liquidity_usd=liq_map.get(asset_id),
            unlock_pressure=unlock_map.get(asset_id),
            sector=sector,
        )

        # G5 技术面
        daily_data = daily_map.get(asset_id, [])
        tech_result = tech_analyzer.analyze(
            asset_id=asset_id,
            daily_data=daily_data,
            impact_direction=impact_direction,
        )

        # 止损止盈（简化：用 regime + 技术面调整 ATR 比例）
        entry_price = tech_result.entry_trigger_price
        stop_loss = None
        take_profit = None
        if entry_price and entry_price > 0:
            regime = row["regime"] or "neutral"
            atr_pct = 0.08 if regime == "risk_off" else (0.12 if regime == "risk_on" else 0.10)
            if tech_result.technical_state == "down":
                atr_pct *= 0.7
            stop_loss = round(entry_price * (1 - atr_pct), 6)
            take_profit = round(entry_price * (1 + 2.2 * atr_pct), 6)

        # 重算信号
        signal = builder.build(
            catalyst_id=catalyst_id,
            asset_id=asset_id,
            kind=kind,
            base_strength=base_strength,
            resonance_score=int(row["resonance_score"] or 0),
            resonance_state=row["resonance_state"] or "pending",
            published_at=row["published_at"],
            persistence=persistence,
            fundamental_pass=fund_result.pass_,
            technical_state=tech_result.technical_state,
            regime=row["regime"],
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        tier_dist[signal.tier or "none"] += 1
        updates.append({
            "catalyst_id": catalyst_id,
            "asset_id": asset_id,
            "persistence": persistence,
            "persistence_verified": False,
            "fundamental_pass": fund_result.pass_,
            "fundamental_detail": json.dumps(fund_result.detail, ensure_ascii=False),
            "technical_state": tech_result.technical_state,
            "entry_trigger": tech_result.entry_trigger,
            "entry_trigger_price": tech_result.entry_trigger_price,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "rr_ratio": signal.rr_ratio,
            "composite_score": signal.composite_score,
            "tier": signal.tier,
            "confidence": signal.confidence,
            "invalidation": signal.invalidation,
        })

    if updates:
        # 用临时表批量 UPDATE
        conn.execute("""
            CREATE TEMP TABLE tmp_slow_g3g5 (
                catalyst_id BIGINT,
                asset_id BIGINT,
                persistence TEXT,
                persistence_verified BOOLEAN,
                fundamental_pass BOOLEAN,
                fundamental_detail JSONB,
                technical_state TEXT,
                entry_trigger TEXT,
                entry_trigger_price NUMERIC,
                entry_price NUMERIC,
                stop_loss NUMERIC,
                take_profit NUMERIC,
                rr_ratio NUMERIC(6,2),
                composite_score SMALLINT,
                tier TEXT,
                confidence NUMERIC(4,3),
                invalidation TEXT
            ) ON COMMIT DROP
        """)
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO tmp_slow_g3g5 VALUES (
                    %(catalyst_id)s, %(asset_id)s, %(persistence)s,
                    %(persistence_verified)s, %(fundamental_pass)s,
                    %(fundamental_detail)s::jsonb, %(technical_state)s,
                    %(entry_trigger)s, %(entry_trigger_price)s,
                    %(entry_price)s, %(stop_loss)s, %(take_profit)s,
                    %(rr_ratio)s, %(composite_score)s, %(tier)s,
                    %(confidence)s, %(invalidation)s
                )
            """, updates)

        conn.execute("""
            UPDATE biz.catalyst_signal cs
            SET persistence = t.persistence,
                persistence_verified = t.persistence_verified,
                fundamental_pass = t.fundamental_pass,
                fundamental_detail = t.fundamental_detail,
                technical_state = t.technical_state,
                entry_trigger = t.entry_trigger,
                entry_trigger_price = t.entry_trigger_price,
                entry_price = t.entry_price,
                stop_loss = t.stop_loss,
                take_profit = t.take_profit,
                rr_ratio = t.rr_ratio,
                composite_score = t.composite_score,
                tier = t.tier,
                confidence = t.confidence,
                invalidation = t.invalidation,
                status = CASE
                    WHEN t.tier IS NULL THEN 'invalid'
                    ELSE 'open'
                END,
                updated_at = NOW()
            FROM tmp_slow_g3g5 t
            WHERE cs.catalyst_id = t.catalyst_id
              AND cs.asset_id = t.asset_id
        """)

    return {
        "processed": len(updates),
        "tier_distribution": dict(tier_dist),
    }


# =====================================================================
# 健康检查
# =====================================================================

def run_health(conn) -> dict:
    """输出管道健康状态。"""
    stats = {}

    # 总 catalyst 数
    row = conn.execute("SELECT COUNT(*) as cnt FROM biz.asset_catalyst").fetchone()
    total_catalysts = row["cnt"] if row else 0
    stats["total_catalysts"] = total_catalysts

    # 未规则分类数
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM biz.asset_catalyst WHERE rule_event_type IS NULL"
    ).fetchone()
    unclassified = row["cnt"] if row else 0
    stats["unclassified_rule"] = unclassified
    stats["classify_coverage"] = round(
        (total_catalysts - unclassified) / total_catalysts * 100, 2
    ) if total_catalysts else 0.0

    # 未分级数（有 rule_event_type 但无 grade）
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.asset_catalyst ac
        WHERE ac.rule_event_type IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM biz.catalyst_grade cg WHERE cg.catalyst_id = ac.catalyst_id)
    """).fetchone()
    stats["ungraded"] = row["cnt"] if row else 0

    # grade 分布
    rows = conn.execute("""
        SELECT catalyst_kind, COUNT(*) as cnt
        FROM biz.catalyst_grade
        GROUP BY catalyst_kind
        ORDER BY cnt DESC
    """).fetchall()
    stats["grade_distribution"] = {r["catalyst_kind"]: r["cnt"] for r in rows}

    # 共振覆盖率（有 grade 的 catalyst 中有多少有 resonance）
    row = conn.execute("""
        SELECT COUNT(DISTINCT cg.catalyst_id) as cnt
        FROM biz.catalyst_grade cg
        JOIN biz.catalyst_asset_link cal ON cg.catalyst_id = cal.catalyst_id
        WHERE cg.catalyst_kind != 'noise'
    """).fetchone()
    grade_with_asset = row["cnt"] if row else 0
    row = conn.execute("""
        SELECT COUNT(DISTINCT cr.catalyst_id) as cnt
        FROM biz.catalyst_resonance cr
    """).fetchone()
    resonance_cnt = row["cnt"] if row else 0
    stats["resonance_coverage"] = round(
        resonance_cnt / grade_with_asset * 100, 2
    ) if grade_with_asset else 0.0

    # 信号总数 + 各状态
    row = conn.execute("SELECT COUNT(*) as cnt FROM biz.catalyst_signal").fetchone()
    stats["total_signals"] = row["cnt"] if row else 0

    # 信号数（按 tier 分布）
    rows = conn.execute("""
        SELECT tier, status, COUNT(*) as cnt
        FROM biz.catalyst_signal
        GROUP BY tier, status
        ORDER BY tier, status
    """).fetchall()
    stats["signals_by_tier"] = [dict(r) for r in rows]

    # open 信号各维度覆盖率（G3/G4/G5）
    g3_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status = 'open' AND persistence IS NOT NULL
    """).fetchone()
    g4_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status = 'open' AND fundamental_pass IS NOT NULL
    """).fetchone()
    g5_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status = 'open' AND technical_state IS NOT NULL
    """).fetchone()
    open_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal WHERE status = 'open'
    """).fetchone()
    open_cnt = open_row["cnt"] if open_row else 0
    stats["open_signals"] = open_cnt
    stats["g3_coverage"] = round(
        (g3_row["cnt"] if g3_row else 0) / open_cnt * 100, 2
    ) if open_cnt else 0.0
    stats["g4_coverage"] = round(
        (g4_row["cnt"] if g4_row else 0) / open_cnt * 100, 2
    ) if open_cnt else 0.0
    stats["g5_coverage"] = round(
        (g5_row["cnt"] if g5_row else 0) / open_cnt * 100, 2
    ) if open_cnt else 0.0

    # 技术面分布（open 信号）
    rows = conn.execute("""
        SELECT technical_state, COUNT(*) as cnt
        FROM biz.catalyst_signal
        WHERE status = 'open' AND technical_state IS NOT NULL
        GROUP BY technical_state
        ORDER BY cnt DESC
    """).fetchall()
    stats["technical_distribution"] = {r["technical_state"]: r["cnt"] for r in rows}

    # 持续性分布（open 信号）
    rows = conn.execute("""
        SELECT persistence, COUNT(*) as cnt
        FROM biz.catalyst_signal
        WHERE status = 'open' AND persistence IS NOT NULL
        GROUP BY persistence
        ORDER BY cnt DESC
    """).fetchall()
    stats["persistence_distribution"] = {r["persistence"]: r["cnt"] for r in rows}

    # open 信号中过期的（巡检应该为 0）
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status = 'open' AND expires_at < NOW()
    """).fetchone()
    stats["expired_but_open"] = row["cnt"] if row else 0

    # 最新快通道处理时间
    row = conn.execute("""
        SELECT MAX(updated_at) as latest FROM biz.catalyst_grade
    """).fetchone()
    stats["fast_latest_run"] = str(row["latest"]) if row and row["latest"] else None

    # 最新慢通道处理时间
    row = conn.execute("""
        SELECT MAX(updated_at) as latest FROM biz.catalyst_signal
        WHERE persistence IS NOT NULL
    """).fetchone()
    stats["slow_latest_run"] = str(row["latest"]) if row and row["latest"] else None

    # 二阶受益统计
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_second_order
    """).fetchone()
    stats["second_order_total"] = row["cnt"] if row else 0

    rows = conn.execute("""
        SELECT order_level, COUNT(*) as cnt
        FROM biz.catalyst_second_order
        GROUP BY order_level
        ORDER BY order_level
    """).fetchall()
    stats["second_order_by_level"] = {int(r["order_level"]): r["cnt"] for r in rows}

    rows = conn.execute("""
        SELECT sector_name, COUNT(*) as cnt
        FROM biz.catalyst_second_order
        WHERE sector_name IS NOT NULL
        GROUP BY sector_name
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    stats["second_order_top_sectors"] = [{r["sector_name"]: r["cnt"]} for r in rows]

    # 直连 vs 二阶信号比
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal cs
        WHERE NOT EXISTS (
            SELECT 1 FROM biz.catalyst_second_order cso
            WHERE cso.catalyst_id = cs.catalyst_id
              AND cso.asset_id = cs.asset_id
        )
    """).fetchone()
    stats["direct_signals"] = row["cnt"] if row else 0
    stats["second_order_signals"] = stats.get("total_signals", 0) - stats["direct_signals"]

    return stats


# =====================================================================
# 主入口
# =====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂决策管道（P0 MVP）")
    parser.add_argument("--fast", action="store_true", help="运行快通道（L1+G0+G1+G2+G6）")
    parser.add_argument("--slow", action="store_true", help="运行慢通道（P0 仅过期巡检）")
    parser.add_argument("--catalyst-id", type=int, help="单条 catalyst 重算")
    parser.add_argument("--limit", type=int, help="限制处理数量（用于调试）")
    parser.add_argument("--health", action="store_true", help="输出健康状态")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    if not args.fast and not args.slow and not args.health:
        parser.print_help()
        return 1

    config = load_config()

    with get_conn() as conn:
        # ---- 健康检查 ----
        if args.health:
            stats = run_health(conn)
            import json
            print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
            return 0

        # ---- 快通道 ----
        if args.fast:
            print("=" * 60)
            print("催化剂快通道（L1 + G0 + G1 + G2 + G6）")
            print("=" * 60)

            # L1: 规则分类
            classifier = RuleEventClassifier(
                rules=config.get("rule_event_keywords", []),
                token_hint_pattern=config.get("token_hint_pattern", ""),
            )
            n_classify = run_classify(conn, classifier,
                                      catalyst_id=args.catalyst_id,
                                      limit=args.limit)
            print(f"  L1 规则分类: {n_classify} 条")

            # G0: 市场环境
            regime_calc = MarketRegime(config)
            regime = run_regime(conn, regime_calc)
            print(f"  G0 市场环境: {regime}")

            # G1: 分级
            grader = CatalystGrader(config)
            n_grade = run_grade(conn, grader,
                                catalyst_id=args.catalyst_id,
                                limit=args.limit)
            print(f"  G1 分级: {n_grade} 条")

            # G2: 共振
            scorer = ResonanceScorer(config)
            n_resonance = run_resonance(conn, scorer,
                                        catalyst_id=args.catalyst_id,
                                        limit=args.limit)
            print(f"  G2 共振: {n_resonance} 条")

            # G6: 信号
            builder = CatalystSignalBuilder(config)
            n_processed, n_inserted, new_sig_ids = run_signal(conn, builder, regime,
                                                               catalyst_id=args.catalyst_id,
                                                               limit=args.limit)
            print(f"  G6 信号: 处理 {n_processed} 条, 入库 {n_inserted} 条")

            # 快提醒：A 级信号即时推送
            if new_sig_ids:
                alert_result = send_fast_alerts_for_new_signals(conn, new_sig_ids)
                if alert_result["sent"] > 0:
                    print(f"  ⚡ 快提醒: 发送 {alert_result['sent']} 条 A 级信号提醒")
                if alert_result["failed"] > 0:
                    print(f"  ⚠️  快提醒失败: {alert_result['failed']} 条")

            print()
            print("快通道完成 ✓")

        # ---- 慢通道 ----
        if args.slow:
            print("=" * 60)
            print("催化剂慢通道（二阶展开 + G3-G5 补全 + 信号重算 + 过期巡检）")
            print("=" * 60)

            # 1. 二阶受益增量展开
            so_result = run_slow_second_order(conn, config, limit=args.limit)
            n_so = so_result.get("second_order_count", 0)
            n_so_sig = so_result.get("new_signals", 0)
            print(f"  二阶受益展开: {n_so} 条映射, {n_so_sig} 条新信号")

            # 2. G3-G5 增量计算 + 信号重算（包括新生成的二阶信号）
            g3g5_result = run_slow_g3g5(conn, config, limit=args.limit)
            n_g3g5 = g3g5_result.get("processed", 0)
            tier_dist = g3g5_result.get("tier_distribution", {})
            print(f"  G3-G5 补全: {n_g3g5} 条信号")
            if tier_dist:
                for tier, cnt in sorted(tier_dist.items()):
                    print(f"    tier {tier}: {cnt}")

            # 3. 过期巡检
            n_expired = expire_signals(conn)
            print(f"  过期巡检: {n_expired} 条信号置为 expired")

            # 4. 慢通道汇总邮件
            digest_stats = {
                "second_order_count": n_so,
                "g3g5_processed": n_g3g5,
                "tier_distribution": tier_dist,
                "expired_count": n_expired,
            }
            digest_result = send_slow_digest(conn, digest_stats)
            if digest_result["sent"] > 0:
                print(f"  📧 汇总邮件: 已发送（24h 新信号 {digest_result.get('new_signals_24h', 0)} 条）")
            elif digest_result.get("skipped"):
                print(f"  📧 汇总邮件: 跳过（{digest_result.get('reason', '无新信号')}）")
            else:
                print(f"  ⚠️  汇总邮件失败: {digest_result.get('reason', 'unknown')}")

            print()
            print("慢通道完成 ✓")

        conn.commit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
