#!/usr/bin/env python3
"""
催化剂决策管道 - 历史数据回灌脚本（上线冷启动用）。

按依赖顺序 7 步回填，每步可独立重跑（幂等）：
  Step 1: G0 市场环境（最近 90 天）
  Step 2: L1 规则兜底分类 + G1 分级（全量 catalyst）
  Step 3: G2 共振打分（有资产链接的）
  Step 4: G6 信号生成（MVP 骨架，G4/G5 占位）
  Step 5: 过期巡检 + 持续性验证（简化版）
  Step 6: G3 二阶受益 + G4 基本面 + G5 技术面（P1 全量）
  Step 7: G6 信号重算（用完整 G3-G5 数据，全字段更新）

⚠️ 回灌期间不触发邮件（不写 notified_at）。

用法：
    python phase_catalyst_backfill.py --all            # 全量回填所有步骤
    python phase_catalyst_backfill.py --step 1         # 只跑单步
    python phase_catalyst_backfill.py --from-step 3    # 从第 3 步开始往后
    python phase_catalyst_backfill.py --dry-run        # 只打印会做什么，不执行
    python phase_catalyst_backfill.py --no-email       # 禁邮件（默认就是禁的）
"""
from __future__ import annotations

import argparse
import sys
import yaml
from pathlib import Path
from psycopg import sql as psql

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_dir_with(target_marker: str, candidates: list[Path]) -> Path | None:
    """在候选目录中找到包含 target_marker 的目录。"""
    for d in candidates:
        if (d / target_marker).exists():
            return d
    return None


def _setup_paths() -> Path:
    """
    探测并设置 sys.path，兼容两种部署结构：
      - 本地开发：  project/workbench/catalyst/  project/scripts/src/
      - 容器部署：  /app/catalyst/               /app/scripts/src/
    """
    project_root = SCRIPT_DIR.parent.parent

    base_candidates = [
        project_root / "workbench",
        project_root,
        Path("/app"),
    ]
    base_dir = _find_dir_with("catalyst/__init__.py", base_candidates)
    if base_dir is None:
        raise RuntimeError(
            f"找不到 catalyst 包，已探测: {[str(p) for p in base_candidates]}"
        )

    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))

    src_candidates = [project_root, Path("/app")]
    src_dir = _find_dir_with("scripts/src", src_candidates)
    if src_dir:
        scripts_src = src_dir / "scripts" / "src"
        if scripts_src.exists() and str(scripts_src) not in sys.path:
            sys.path.insert(0, str(scripts_src))

    return base_dir


BASE_DIR = _setup_paths()

from catalyst.db import get_conn
from catalyst.classify import RuleEventClassifier
from catalyst.grade import CatalystGrader, MarketRegime
from catalyst.resonance import ResonanceScorer
from catalyst.second_order import SecondOrderMapper, PersistenceScorer
from catalyst.fundamental import FundamentalChecker
from catalyst.technical import TechnicalAnalyzer
from catalyst.signal import CatalystSignalBuilder, expire_signals


STEPS = [
    ("step1", "G0 市场环境（最近 90 天）"),
    ("step2", "L1 规则兜底分类 + G1 分级（全量）"),
    ("step3", "G2 共振打分（有资产链接的）"),
    ("step4", "G6 信号生成（MVP 骨架）"),
    ("step5", "信号过期巡检 + 持续性验证"),
    ("step6", "G3 持续性 + G4 基本面 + G5 技术面（直连资产）"),
    ("step6b", "G3-1 二阶受益映射 + 二阶信号生成"),
    ("step7", "G6 信号重算（完整 G3-G5，全字段）"),
]


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
# Step 1: G0 市场环境
# =====================================================================

def step1_regime_backfill(conn, config: dict, dry_run: bool = False) -> dict:
    """回填最近 90 天的市场环境。

    P0 简化版：用 BTC 7日涨幅 近似计算每天的 regime。
    实际应该用历史日K，但 CMC 快照只有最新值，这里给一个近似方案。
    """
    print("\n[Step 1/5] G0 市场环境（最近 90 天）")
    print("-" * 50)

    regime_calc = MarketRegime(config)

    # P0 简化：用当前 BTC 7d 涨幅 + 线性回推（不准确，但能占位）
    # 更准确的做法应该用 historical quotes，这里先占位
    # 先查 BTC 的 24h/7d 数据
    from catalyst.resonance import get_btc_cmc_snapshot_ret
    btc_snap = get_btc_cmc_snapshot_ret(conn)
    btc_7d = btc_snap.get("percent_change_7d", 0) or 0

    if dry_run:
        print(f"  (dry-run) 将写入最近 90 天 regime，当前 BTC 7d ≈ {btc_7d:.2f}%")
        return {"step": "step1", "days": 90, "dry_run": True}

    # P0 简化：只写今天的（historical 后面 P1 补）
    result = regime_calc.determine(btc_7d=btc_7d, total_mcap_7d=0.0)
    regime_calc.upsert_to_db(conn, result)

    print(f"  已写入 {result.regime_date}: regime={result.regime}, btc_7d={result.btc_trend_7d:.2f}%")
    print(f"  ⚠️  P0 仅写当天，历史 90 天回填待 P1（需要 historical quotes）")
    return {"step": "step1", "days": 1, "regime": result.regime}


# =====================================================================
# Step 2: L1 分类 + G1 分级
# =====================================================================

def step2_classify_grade(conn, config: dict, dry_run: bool = False) -> dict:
    """全量 catalyst 做规则分类 + G1 分级。"""
    print("\n[Step 2/5] L1 规则分类 + G1 分级（全量）")
    print("-" * 50)

    classifier = RuleEventClassifier(
        rules=config.get("rule_event_keywords", []),
        token_hint_pattern=config.get("token_hint_pattern", ""),
    )
    grader = CatalystGrader(config)

    # 一次性全量查询（只取需要的字段，避免大字段传输）
    rows = conn.execute(
        """
        SELECT ac.catalyst_id, ac.source_code, ac.title, ac.body_text,
               ac.related_pairs, ac.rule_event_type, ac.ai_event_type,
               ARRAY_AGG(cal.asset_id) FILTER (WHERE cal.asset_id IS NOT NULL) AS linked_asset_ids
        FROM biz.asset_catalyst ac
        LEFT JOIN biz.catalyst_asset_link cal ON ac.catalyst_id = cal.catalyst_id
        GROUP BY ac.catalyst_id, ac.source_code, ac.title, ac.body_text,
                 ac.related_pairs, ac.rule_event_type, ac.ai_event_type
        ORDER BY ac.catalyst_id
        """
    ).fetchall()
    total = len(rows)
    print(f"  总 catalyst 数: {total}")

    if dry_run:
        print(f"  (dry-run) 将处理 {total} 条 catalyst 的分类 + 分级")
        return {"step": "step2", "total": total, "dry_run": True}

    # --- 内存中批量计算 ---
    classify_updates = []   # (catalyst_id, rule_event_type)
    grade_rows = []         # CatalystGradeResult

    for row in rows:
        # 规则分类
        pairs = row.get("related_pairs") or []
        rule_event_type = row.get("rule_event_type")
        if not rule_event_type:
            event_type = classifier.classify(
                row["title"] or "",
                row.get("body_text") or "",
                pairs,
            )
            classify_updates.append((row["catalyst_id"], event_type))
            rule_event_type = event_type

        # G1 分级（需要 rule_event_type 字段）
        row_dict = dict(row)
        row_dict["rule_event_type"] = rule_event_type
        linked = [{"asset_id": aid} for aid in (row["linked_asset_ids"] or [])]
        grade_result = grader.grade(row_dict, linked_assets=linked if linked else None)
        grade_rows.append(grade_result)

    n_classified = len(classify_updates)
    n_graded = len(grade_rows)

    # --- 批量写入数据库 ---
    print(f"  分类计算完成，写入中...", end=" ", flush=True)

    # 1) 批量 UPDATE rule_event_type
    if classify_updates:
        values_sql = psql.SQL(",").join(
            psql.SQL("(%s, %s::VARCHAR)") for _ in classify_updates
        )
        params = [item for pair in classify_updates for item in pair]
        conn.execute(
            psql.SQL("""
                UPDATE biz.asset_catalyst ac
                SET rule_event_type = t.rule_event_type
                FROM (VALUES {}) AS t(catalyst_id, rule_event_type)
                WHERE ac.catalyst_id = t.catalyst_id::BIGINT
            """).format(values_sql),
            params,
        )

    # 2) 批量 INSERT grade (ON CONFLICT DO UPDATE)
    if grade_rows:
        grade_values_sql = psql.SQL(",").join(
            psql.SQL("(%s, %s, %s, %s, %s, %s, %s, %s)")
            for _ in grade_rows
        )
        grade_params = []
        for g in grade_rows:
            grade_params.extend([
                g.catalyst_id, g.authority_score, g.event_weight,
                g.scope_score, g.tradable, g.catalyst_kind,
                g.base_strength, g.event_type_src,
            ])
        conn.execute(
            psql.SQL("""
                INSERT INTO biz.catalyst_grade (
                    catalyst_id, authority_score, event_weight, scope_score,
                    tradable, catalyst_kind, base_strength, event_type_src
                ) VALUES {}
                ON CONFLICT (catalyst_id) DO UPDATE SET
                    authority_score = EXCLUDED.authority_score,
                    event_weight = EXCLUDED.event_weight,
                    scope_score = EXCLUDED.scope_score,
                    tradable = EXCLUDED.tradable,
                    catalyst_kind = EXCLUDED.catalyst_kind,
                    base_strength = EXCLUDED.base_strength,
                    event_type_src = EXCLUDED.event_type_src,
                    graded_by = 'rule',
                    updated_at = NOW()
            """).format(grade_values_sql),
            grade_params,
        )

    print("done")

    # 打印分布
    dist = conn.execute("""
        SELECT catalyst_kind, COUNT(*) as cnt
        FROM biz.catalyst_grade
        GROUP BY catalyst_kind
        ORDER BY cnt DESC
    """).fetchall()
    print("  kind 分布:")
    for d in dist:
        print(f"    {d['catalyst_kind']}: {d['cnt']}")

    return {"step": "step2", "classified": n_classified, "graded": n_graded}


# =====================================================================
# Step 3: G2 共振打分
# =====================================================================

def step3_resonance(conn, config: dict, dry_run: bool = False) -> dict:
    """有资产链接的 catalyst 做 G2 共振打分（批量优化版）。"""
    print("\n[Step 3/5] G2 共振打分（有资产链接的）")
    print("-" * 50)

    scorer = ResonanceScorer(config)

    # --- 1. 查出所有待处理的 catalyst + asset 对 ---
    rows = conn.execute("""
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
        ORDER BY ac.catalyst_id
    """).fetchall()

    if not rows:
        print("  没有待处理的共振数据")
        return {"step": "step3", "count": 0}

    total = len(rows)
    print(f"  待处理 catalyst-asset 对: {total}")

    if dry_run:
        print(f"  (dry-run) 将为 {total} 对计算共振")
        return {"step": "step3", "total": total, "dry_run": True}

    # 收集所有 asset_id
    asset_ids = list(set(r["asset_id"] for r in rows))
    print(f"  涉及资产数: {len(asset_ids)}")

    # --- 2. 批量查 BTC 快照 ---
    print("  [1/5] 获取 BTC 基准收益...", end=" ", flush=True)
    btc_snap = conn.execute("""
        SELECT percent_change_1h, percent_change_24h, percent_change_7d, volume_24h
        FROM src_cmc.cmc_asset_quote_snapshot
        WHERE cmc_id = (
            SELECT cmc_id FROM src_cmc.cmc_asset_map
            WHERE symbol = 'BTC'
            ORDER BY rank_num NULLS LAST
            LIMIT 1
        )
        ORDER BY quote_time DESC
        LIMIT 1
    """).fetchone()
    btc_1h = float(btc_snap.get("percent_change_1h") or 0) if btc_snap else 0.0
    btc_24h = float(btc_snap.get("percent_change_24h") or 0) if btc_snap else 0.0
    btc_7d = float(btc_snap.get("percent_change_7d") or 0) if btc_snap else 0.0
    print("done")

    # --- 3. 批量查 asset → cmc_id 映射 + 快照 ---
    print("  [2/5] 批量获取 CMC 快照...", end=" ", flush=True)
    # asset_id → cmc_id（优先从 asset_source_map）
    asset_to_cmc = {}
    # 先查 asset_source_map
    map_rows = conn.execute("""
        SELECT asset_id, source_asset_key
        FROM core.asset_source_map
        WHERE asset_id = ANY(%s::INT[])
          AND source_code = 'cmc'
          AND is_primary = true
    """, (asset_ids,)).fetchall()
    for r in map_rows:
        try:
            asset_to_cmc[r["asset_id"]] = int(r["source_asset_key"])
        except (ValueError, TypeError):
            pass

    # 剩余没查到的，用 canonical_symbol 兜底查
    remaining = [aid for aid in asset_ids if aid not in asset_to_cmc]
    if remaining:
        sym_rows = conn.execute("""
            SELECT a.asset_id, a.canonical_symbol
            FROM core.asset a
            WHERE a.asset_id = ANY(%s::INT[])
        """, (remaining,)).fetchall()
        symbols = [r["canonical_symbol"].upper() for r in sym_rows if r.get("canonical_symbol")]
        if symbols:
            cmc_map_rows = conn.execute("""
                SELECT symbol, cmc_id
                FROM src_cmc.cmc_asset_map
                WHERE symbol = ANY(%s::VARCHAR[])
            """, (symbols,)).fetchall()
            sym_to_cmc = {r["symbol"]: r["cmc_id"] for r in cmc_map_rows}
            for r in sym_rows:
                sym = (r.get("canonical_symbol") or "").upper()
                if sym in sym_to_cmc:
                    asset_to_cmc[r["asset_id"]] = sym_to_cmc[sym]

    # 批量查所有 cmc_id 的最新快照
    cmc_ids = list(set(v for v in asset_to_cmc.values()))
    snap_map = {}  # cmc_id → snap dict
    if cmc_ids:
        # 用 DISTINCT ON (cmc_id) 取每个 cmc_id 最新快照
        snap_rows = conn.execute("""
            SELECT DISTINCT ON (cmc_id)
                   cmc_id, percent_change_1h, percent_change_24h,
                   percent_change_7d, percent_change_30d, volume_24h
            FROM src_cmc.cmc_asset_quote_snapshot
            WHERE cmc_id = ANY(%s::BIGINT[])
            ORDER BY cmc_id, quote_time DESC
        """, (cmc_ids,)).fetchall()
        for r in snap_rows:
            snap_map[r["cmc_id"]] = dict(r)
    print("done")

    # --- 4. 批量算 20 日均量（vol z-score） ---
    print("  [3/5] 批量计算量能均值...", end=" ", flush=True)
    vol_avg_map = {}  # asset_id → avg_vol
    vol_rows = conn.execute("""
        SELECT asset_id, AVG(volume_24h) as avg_vol
        FROM (
            SELECT asset_id, volume_24h,
                   ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY market_date DESC) as rn
            FROM biz.asset_market_daily
            WHERE asset_id = ANY(%s::INT[])
              AND volume_24h > 0
        ) sub
        WHERE rn <= 20
        GROUP BY asset_id
    """, (asset_ids,)).fetchall()
    for r in vol_rows:
        vol_avg_map[r["asset_id"]] = float(r["avg_vol"])
    print("done")
    # --- 5. 批量查 sector + peer 中位数收益 ---
    print("  [4/5] 批量计算板块 peer 中位数...", end=" ", flush=True)
    # 先查所有资产的 sector
    sector_rows = conn.execute("""
        SELECT asset_id, primary_sector
        FROM core.asset
        WHERE asset_id = ANY(%s::INT[])
    """, (asset_ids,)).fetchall()
    asset_to_sector = {r["asset_id"]: r["primary_sector"] for r in sector_rows if r.get("primary_sector")}

    # 所有涉及的 sector
    sectors = list(set(asset_to_sector.values()))
    sector_median_map = {}  # sector → median_24h

    if sectors:
        # 每个 sector 取市值前 50 的 24h 收益中位数
        # 用一个 SQL 搞定
        median_rows = conn.execute("""
            WITH ranked AS (
                SELECT
                    a.primary_sector,
                    m.percent_change_24h,
                    ROW_NUMBER() OVER (
                        PARTITION BY a.primary_sector
                        ORDER BY COALESCE(m.market_cap, 0) DESC
                    ) as rn
                FROM core.asset a
                JOIN core.asset_source_map asm
                  ON a.asset_id = asm.asset_id AND asm.source_code = 'cmc' AND asm.is_primary = true
                JOIN src_cmc.cmc_asset_quote_snapshot m
                  ON m.cmc_id = asm.source_asset_key::BIGINT
                WHERE a.primary_sector = ANY(%s::VARCHAR[])
                  AND m.percent_change_24h IS NOT NULL
            )
            SELECT primary_sector,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY percent_change_24h) as median_24h
            FROM ranked
            WHERE rn <= 50
            GROUP BY primary_sector
        """, (sectors,)).fetchall()
        for r in median_rows:
            sector_median_map[r["primary_sector"]] = float(r["median_24h"]) if r["median_24h"] is not None else None
    print("done")

    # --- 6. 内存计算所有共振结果 ---
    print("  [5/5] 内存计算 + 批量写入...", end=" ", flush=True)
    results = []
    for row in rows:
        asset_id = row["asset_id"]
        catalyst_id_val = row["catalyst_id"]
        impact_dir = row["impact_direction"] or "bullish"

        cmc_id = asset_to_cmc.get(asset_id)
        if not cmc_id or cmc_id not in snap_map:
            continue  # 无行情数据跳过

        snap = snap_map[cmc_id]
        asset_24h = float(snap.get("percent_change_24h")) if snap.get("percent_change_24h") is not None else None
        vol_24h = float(snap.get("volume_24h")) if snap.get("volume_24h") is not None else None

        if asset_24h is None:
            continue

        # vol z-score
        avg_vol = vol_avg_map.get(asset_id)
        vol_z = None
        if vol_24h and vol_24h > 0 and avg_vol and avg_vol > 0:
            vol_z = float(vol_24h) / float(avg_vol)

        # peer 中位数
        sector = asset_to_sector.get(asset_id)
        peer_ret = float(sector_median_map[sector]) if (sector and sector in sector_median_map and sector_median_map[sector] is not None) else None

        # 计算共振（24h 作为主窗口）
        result = scorer.compute(
            asset_ret_pct=asset_24h,
            btc_ret_pct=float(btc_24h),
            vol_zscore=vol_z or 0.0,
            impact_direction=impact_dir,
            peer_median_ret=peer_ret,
            window="24h",
        )
        result.catalyst_id = catalyst_id_val
        result.asset_id = asset_id
        result.ret_source = "cmc_snapshot"

        # 填 1h / 7d
        if snap.get("percent_change_1h") is not None:
            result.excess_ret_1h = round(float(snap["percent_change_1h"]) - float(btc_1h), 4)
        if snap.get("percent_change_7d") is not None:
            result.excess_ret_72h = round(float(snap["percent_change_7d"]) - float(btc_7d), 4)

        results.append(result)

    # --- 7. 批量 upsert ---
    if results:
        # 构建批量 INSERT ... ON CONFLICT
        fields = [
            "catalyst_id", "asset_id", "excess_ret_1h", "excess_ret_4h",
            "excess_ret_24h", "excess_ret_72h", "vol_zscore_24h",
            "peer_median_ret_24h", "direction_match", "resonance_score",
            "resonance_state", "ret_source"
        ]

        # 用 VALUES 列表
        placeholders_per_row = "(" + ",".join(["%s"] * len(fields)) + ")"
        all_values_sql = psql.SQL(",").join(
            psql.SQL(placeholders_per_row) for _ in results
        )

        params = []
        for r in results:
            params.extend([
                r.catalyst_id, r.asset_id,
                r.excess_ret_1h, r.excess_ret_4h,
                r.excess_ret_24h, r.excess_ret_72h,
                r.vol_zscore_24h, r.peer_median_ret_24h,
                r.direction_match, r.resonance_score,
                r.resonance_state, r.ret_source,
            ])

        conn.execute(
            psql.SQL("""
                INSERT INTO biz.catalyst_resonance ({})
                VALUES {}
                ON CONFLICT (catalyst_id, asset_id) DO UPDATE SET
                    excess_ret_1h = COALESCE(EXCLUDED.excess_ret_1h, biz.catalyst_resonance.excess_ret_1h),
                    excess_ret_4h = COALESCE(EXCLUDED.excess_ret_4h, biz.catalyst_resonance.excess_ret_4h),
                    excess_ret_24h = COALESCE(EXCLUDED.excess_ret_24h, biz.catalyst_resonance.excess_ret_24h),
                    excess_ret_72h = COALESCE(EXCLUDED.excess_ret_72h, biz.catalyst_resonance.excess_ret_72h),
                    vol_zscore_24h = COALESCE(EXCLUDED.vol_zscore_24h, biz.catalyst_resonance.vol_zscore_24h),
                    peer_median_ret_24h = COALESCE(EXCLUDED.peer_median_ret_24h, biz.catalyst_resonance.peer_median_ret_24h),
                    direction_match = EXCLUDED.direction_match,
                    resonance_score = EXCLUDED.resonance_score,
                    resonance_state = EXCLUDED.resonance_state,
                    ret_source = EXCLUDED.ret_source,
                    updated_at = NOW()
            """).format(
                psql.SQL(", ").join(psql.Identifier(f) for f in fields),
                all_values_sql,
            ),
            params,
        )

    n = len(results)
    print(f"done ({n} 条)")

    # 打印分布
    dist = conn.execute("""
        SELECT resonance_state, COUNT(*) as cnt
        FROM biz.catalyst_resonance
        GROUP BY resonance_state
        ORDER BY cnt DESC
    """).fetchall()
    print("  state 分布:")
    for d in dist:
        print(f"    {d['resonance_state']}: {d['cnt']}")

    return {"step": "step3", "count": n}


# =====================================================================
# Step 4: G6 信号生成
# =====================================================================

def step4_signal(conn, config: dict, dry_run: bool = False) -> dict:
    """生成 G6 信号（composite_score + tier + expires_at）— 批量优化版。"""
    print("\n[Step 4/5] G6 信号生成")
    print("-" * 50)

    builder = CatalystSignalBuilder(config)

    # 先拿当前 regime
    regime_row = conn.execute(
        "SELECT regime FROM biz.market_regime_daily ORDER BY regime_date DESC LIMIT 1"
    ).fetchone()
    regime = regime_row["regime"] if regime_row else "neutral"

    # 一次性查出所有待生成信号的数据
    rows = conn.execute("""
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
        ORDER BY cr.catalyst_id
    """).fetchall()

    total = len(rows)
    print(f"  待生成信号数: {total}")
    print(f"  当前 regime: {regime}")

    if dry_run:
        print(f"  (dry-run) 将生成 {total} 条信号")
        return {"step": "step4", "total": total, "dry_run": True}

    # 内存计算
    signals = []
    processed = 0
    for row in rows:
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
            fundamental_pass=None,
            technical_state=None,
            regime=regime,
        )
        processed += 1
        if signal.tier is not None:
            signals.append(signal)

    inserted = len(signals)

    # 批量写入
    if signals:
        signal_fields = [
            "catalyst_id", "asset_id",
            "kind", "base_strength",
            "resonance_score", "resonance_state",
            "persistence", "persistence_verified",
            "fundamental_pass",
            "technical_state",
            "entry_price", "stop_loss", "take_profit", "rr_ratio",
            "composite_score", "tier", "confidence",
            "regime", "invalidation",
            "expires_at", "status",
        ]

        placeholders = "(" + ",".join(["%s"] * len(signal_fields)) + ")"
        all_values_sql = psql.SQL(",").join(
            psql.SQL(placeholders) for _ in signals
        )

        params = []
        for s in signals:
            params.extend([
                s.catalyst_id, s.asset_id,
                s.kind, s.base_strength,
                s.resonance_score, s.resonance_state,
                s.persistence, s.persistence_verified,
                s.fundamental_pass,
                s.technical_state,
                s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
                s.composite_score, s.tier, s.confidence,
                s.regime, s.invalidation,
                s.expires_at, s.status,
            ])

        conn.execute(
            psql.SQL("""
                INSERT INTO biz.catalyst_signal ({})
                VALUES {}
                ON CONFLICT (catalyst_id, asset_id) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    base_strength = EXCLUDED.base_strength,
                    resonance_score = EXCLUDED.resonance_score,
                    resonance_state = EXCLUDED.resonance_state,
                    persistence = EXCLUDED.persistence,
                    persistence_verified = EXCLUDED.persistence_verified,
                    fundamental_pass = EXCLUDED.fundamental_pass,
                    technical_state = EXCLUDED.technical_state,
                    entry_price = COALESCE(EXCLUDED.entry_price, biz.catalyst_signal.entry_price),
                    stop_loss = COALESCE(EXCLUDED.stop_loss, biz.catalyst_signal.stop_loss),
                    take_profit = COALESCE(EXCLUDED.take_profit, biz.catalyst_signal.take_profit),
                    rr_ratio = COALESCE(EXCLUDED.rr_ratio, biz.catalyst_signal.rr_ratio),
                    composite_score = EXCLUDED.composite_score,
                    tier = EXCLUDED.tier,
                    confidence = EXCLUDED.confidence,
                    regime = COALESCE(EXCLUDED.regime, biz.catalyst_signal.regime),
                    invalidation = COALESCE(EXCLUDED.invalidation, biz.catalyst_signal.invalidation),
                    expires_at = EXCLUDED.expires_at,
                    status = CASE
                        WHEN biz.catalyst_signal.status = 'open' THEN EXCLUDED.status
                        ELSE biz.catalyst_signal.status
                    END,
                    updated_at = NOW()
            """).format(
                psql.SQL(", ").join(psql.Identifier(f) for f in signal_fields),
                all_values_sql,
            ),
            params,
        )

    print(f"  处理: {processed} 条")
    print(f"  入库: {inserted} 条（composite_score ≥ 40）")

    # tier 分布
    dist = conn.execute("""
        SELECT tier, status, COUNT(*) as cnt
        FROM biz.catalyst_signal
        GROUP BY tier, status
        ORDER BY tier, status
    """).fetchall()
    print("  tier 分布:")
    for d in dist:
        print(f"    {d['tier']} / {d['status']}: {d['cnt']}")

    return {"step": "step4", "processed": processed, "inserted": inserted}


# =====================================================================
# Step 5: 过期巡检 + 持续性验证（简化版）
# =====================================================================

def step5_expire_verify(conn, config: dict, dry_run: bool = False) -> dict:
    """过期巡检 + 持续性验证（P0 简化）。"""
    print("\n[Step 5/5] 过期巡检 + 持续性验证")
    print("-" * 50)

    if dry_run:
        print("  (dry-run) 将巡检过期信号 + 验证持续性")
        return {"step": "step5", "dry_run": True}

    # 过期巡检
    n_expired = expire_signals(conn)
    print(f"  过期巡检: {n_expired} 条置为 expired")

    # P0 持续性验证简化：只更新 published_at > 72h 的 structural 事件
    # 验证条件：后续 72h 内是否有同类 catalyst
    rows = conn.execute("""
        UPDATE biz.catalyst_signal cs
        SET persistence_verified = true,
            updated_at = NOW()
        FROM biz.asset_catalyst ac
        WHERE cs.catalyst_id = ac.catalyst_id
          AND cs.persistence = 'structural'
          AND cs.persistence_verified = false
          AND ac.published_at < NOW() - INTERVAL '72 hours'
          AND EXISTS (
              -- 72h 内有同资产同类型的后续 catalyst
              SELECT 1
              FROM biz.asset_catalyst ac2
              JOIN biz.catalyst_asset_link cal2 ON ac2.catalyst_id = cal2.catalyst_id
              JOIN biz.catalyst_grade cg2 ON ac2.catalyst_id = cg2.catalyst_id
              WHERE cal2.asset_id = cs.asset_id
                AND cg2.catalyst_kind = 'structural'
                AND ac2.published_at > ac.published_at
                AND ac2.published_at < ac.published_at + INTERVAL '72 hours'
          )
    """)
    n_verified = rows.rowcount if hasattr(rows, 'rowcount') else 0
    print(f"  持续性验证通过: {n_verified} 条")

    return {"step": "step5", "expired": n_expired, "verified": n_verified}


# =====================================================================
# Step 6: G3 二阶受益 + G4 基本面 + G5 技术面（P1 全量）
# =====================================================================

def step6_g3_g4_g5(conn, config: dict, dry_run: bool = False) -> dict:
    """批量计算 G3（二阶+持续性）、G4（基本面）、G5（技术面）。

    批量策略：
      1. 一次性查出所有有共振记录的 catalyst-asset 对 + 资产基础信息
      2. 批量查：生命周期 / 风险标签 / 流动性 / 解锁 / 合约安全 / TVL
      3. 批量查：asset_market_daily 日线（最近 60 天，算 MA5/20/60）
      4. 内存中：G3 持续性预判 + G4 基本面 + G5 技术面
      5. 批量写：更新 catalyst_signal 的 G3/G4/G5 字段
    """
    from decimal import Decimal
    import json

    print("  [1/8] 初始化计分器...", end=" ", flush=True)
    pers_scorer = PersistenceScorer(config)
    fund_checker = FundamentalChecker(config)
    tech_analyzer = TechnicalAnalyzer(config)
    print("done")

    # --- 2. 查出所有 signal 对应的 catalyst + asset 基础信息 ---
    print("  [2/8] 批量获取 signal + catalyst + asset 基础信息...", end=" ", flush=True)
    signal_rows = conn.execute("""
        SELECT cs.signal_id, cs.catalyst_id, cs.asset_id,
               cs.kind, cs.base_strength,
               ac.rule_event_type, ac.published_at,
               a.asset_type, a.primary_sector, a.canonical_symbol,
               ci.impact_direction
        FROM biz.catalyst_signal cs
        JOIN biz.asset_catalyst ac ON cs.catalyst_id = ac.catalyst_id
        JOIN core.asset a ON cs.asset_id = a.asset_id
        LEFT JOIN biz.catalyst_impact ci
          ON cs.catalyst_id = ci.catalyst_id AND cs.asset_id = ci.asset_id
        WHERE cs.status != 'expired'
        ORDER BY cs.catalyst_id
    """).fetchall()
    print(f"done ({len(signal_rows)} 条)")

    if not signal_rows:
        return {"step": "step6", "processed": 0}

    asset_ids = list(set(r["asset_id"] for r in signal_rows))

    # --- 3. 批量查基本面数据 ---
    print("  [3/8] 批量查生命周期...", end=" ", flush=True)
    life_rows = conn.execute("""
        SELECT asset_id, stage
        FROM biz.asset_lifecycle
        WHERE asset_id = ANY(%s::INT[])
    """, (asset_ids,)).fetchall()
    life_map = {r["asset_id"]: r["stage"] for r in life_rows}
    print("done")

    print("  [4/8] 批量查风险标签...", end=" ", flush=True)
    risk_rows = conn.execute("""
        SELECT asset_id, total_score
        FROM biz.asset_risk_labels
        WHERE asset_id = ANY(%s::INT[])
    """, (asset_ids,)).fetchall()
    risk_map = {r["asset_id"]: int(r["total_score"]) if r.get("total_score") is not None else None
                for r in risk_rows}
    print("done")

    print("  [5/8] 批量查流动性...", end=" ", flush=True)
    liq_rows = conn.execute("""
        SELECT DISTINCT ON (asset_id) asset_id, total_liquidity_usd
        FROM biz.asset_liquidity
        WHERE asset_id = ANY(%s::INT[])
        ORDER BY asset_id, scanned_at DESC
    """, (asset_ids,)).fetchall()
    liq_map = {r["asset_id"]: float(r["total_liquidity_usd"]) if r.get("total_liquidity_usd") else None
               for r in liq_rows}
    print("done")

    print("  [6/8] 批量查解锁压力...", end=" ", flush=True)
    unlock_rows = conn.execute("""
        SELECT asset_id, risk_level
        FROM biz.asset_unlock_pressure
        WHERE asset_id = ANY(%s::INT[])
    """, (asset_ids,)).fetchall()
    unlock_map = {r["asset_id"]: r["risk_level"] for r in unlock_rows}
    print("done")

    # --- 7. 批量查日线数据（最近 60 天，算 MA 和 ATR） ---
    print("  [7/8] 批量查日线（60天）...", end=" ", flush=True)
    daily_rows = conn.execute("""
        SELECT asset_id, market_date, price_usd, volume_24h
        FROM biz.asset_market_daily
        WHERE asset_id = ANY(%s::INT[])
          AND market_date >= NOW() - INTERVAL '60 days'
        ORDER BY asset_id, market_date ASC
    """, (asset_ids,)).fetchall()
    # 转成 {asset_id: [list of price dicts]}
    from collections import defaultdict
    daily_map = defaultdict(list)
    for r in daily_rows:
        daily_map[r["asset_id"]].append({
            "market_date": r["market_date"],
            "price_usd": float(r["price_usd"]) if r.get("price_usd") is not None else None,
            "volume_24h": float(r["volume_24h"]) if r.get("volume_24h") is not None else None,
        })
    print(f"done ({len(daily_rows)} 行)")

    # --- 8. 内存计算 + 批量更新 ---
    print("  [8/8] 内存计算 + 批量更新 signal 表...", end=" ", flush=True)
    updates = []
    for row in signal_rows:
        asset_id = row["asset_id"]
        catalyst_id = row["catalyst_id"]
        kind = row["kind"] or "event"
        base_strength = int(row["base_strength"] or 0)
        event_type = row["rule_event_type"] or "other"
        published_at = row["published_at"]
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

        # 收集更新数据
        updates.append({
            "catalyst_id": catalyst_id,
            "asset_id": asset_id,
            "persistence": persistence,
            "fundamental_pass": fund_result.pass_,
            "fundamental_detail": json.dumps(fund_result.detail, ensure_ascii=False),
            "technical_state": tech_result.technical_state,
            "entry_trigger": tech_result.entry_trigger,
            "entry_trigger_price": tech_result.entry_trigger_price,
            "entry_price": tech_result.entry_trigger_price,
        })

    if updates and not dry_run:
        # 批量 UPDATE（用 CASE WHEN 或 UPDATE ... FROM VALUES）
        # 用临时表方式更清晰
        conn.execute("""
            CREATE TEMP TABLE tmp_g3g4g5 (
                catalyst_id BIGINT,
                asset_id BIGINT,
                persistence TEXT,
                fundamental_pass BOOLEAN,
                fundamental_detail JSONB,
                technical_state TEXT,
                entry_trigger TEXT,
                entry_trigger_price NUMERIC(24,10),
                entry_price NUMERIC(24,10)
            ) ON COMMIT DROP
        """)

        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO tmp_g3g4g5 VALUES (
                    %(catalyst_id)s, %(asset_id)s, %(persistence)s,
                    %(fundamental_pass)s, %(fundamental_detail)s::jsonb,
                    %(technical_state)s, %(entry_trigger)s,
                    %(entry_trigger_price)s, %(entry_price)s
                )
            """, updates)

        conn.execute("""
            UPDATE biz.catalyst_signal cs
            SET persistence = t.persistence,
                fundamental_pass = t.fundamental_pass,
                fundamental_detail = t.fundamental_detail,
                technical_state = t.technical_state,
                entry_trigger = t.entry_trigger,
                entry_trigger_price = t.entry_trigger_price,
                entry_price = t.entry_price,
                updated_at = NOW()
            FROM tmp_g3g4g5 t
            WHERE cs.catalyst_id = t.catalyst_id
              AND cs.asset_id = t.asset_id
        """)

    print(f"done ({len(updates)} 条)")

    # 打印分布
    if updates:
        tech_states = defaultdict(int)
        fund_pass_cnt = 0
        pers_dist = defaultdict(int)
        for u in updates:
            tech_states[u["technical_state"]] += 1
            if u["fundamental_pass"]:
                fund_pass_cnt += 1
            pers_dist[u["persistence"]] += 1
        print("  技术面分布:")
        for state, cnt in sorted(tech_states.items()):
            print(f"    {state}: {cnt}")
        print(f"  基本面通过: {fund_pass_cnt} / {len(updates)}")
        print("  持续性分布:")
        for p, cnt in sorted(pers_dist.items()):
            print(f"    {p}: {cnt}")

    return {"step": "step6", "processed": len(updates)}


# =====================================================================
# Step 6b: G3-1 二阶受益映射 + 二阶信号生成
# =====================================================================

def step6b_second_order(conn, config: dict, dry_run: bool = False) -> dict:
    """展开二阶受益资产，写入 catalyst_second_order + 生成二阶信号。

    策略：
    1. 找出 structural/event 级且 base_strength 合格的 catalyst
    2. 对每个 catalyst，找出直连资产的 primary_sector
    3. 从同板块其他资产中挑选二阶受益标的（按 CMC 市值/流动性排序）
    4. 写入 catalyst_second_order
    5. 为二阶资产生成信号（base_strength 打折，resonance 用 peer 近似）
    """
    from catalyst.second_order import SecondOrderMapper
    from catalyst.signal import CatalystSignalBuilder

    mapper = SecondOrderMapper(config)
    builder = CatalystSignalBuilder(config)

    # 1. 找出需要展开二阶的 catalyst（structural/event + 有 grade）
    print("  [1/5] 找出候选 catalyst...", end=" ", flush=True)
    cat_rows = conn.execute("""
        SELECT cg.catalyst_id, cg.catalyst_kind,
               cg.base_strength,
               ac.rule_event_type, ac.published_at
        FROM biz.catalyst_grade cg
        JOIN biz.asset_catalyst ac ON cg.catalyst_id = ac.catalyst_id
        WHERE cg.catalyst_kind IN ('structural', 'event')
          AND cg.base_strength >= 30
        GROUP BY cg.catalyst_id, cg.catalyst_kind, cg.base_strength,
                 ac.rule_event_type, ac.published_at
        ORDER BY cg.base_strength DESC
    """).fetchall()
    print(f"done ({len(cat_rows)} 条)")

    if not cat_rows:
        return {"step": "step6b", "second_order_count": 0, "new_signals": 0}

    catalyst_ids = [r["catalyst_id"] for r in cat_rows]

    # 2. 批量查每个 catalyst 的直连资产
    print("  [2/5] 批量查直连资产 + 行业...", end=" ", flush=True)
    link_rows = conn.execute("""
        SELECT cal.catalyst_id, cal.asset_id, a.primary_sector
        FROM biz.catalyst_asset_link cal
        JOIN core.asset a ON cal.asset_id = a.asset_id
        WHERE cal.catalyst_id = ANY(%s::BIGINT[])
          AND a.primary_sector IS NOT NULL
    """, (catalyst_ids,)).fetchall()
    print(f"done ({len(link_rows)} 条链接)")

    # 按 catalyst 分组
    from collections import defaultdict
    cat_links = defaultdict(list)
    cat_sectors = defaultdict(set)
    for r in link_rows:
        cat_links[r["catalyst_id"]].append({
            "asset_id": r["asset_id"],
            "sector": r["primary_sector"],
        })
        if r["primary_sector"]:
            cat_sectors[r["catalyst_id"]].add(r["primary_sector"])

    # 3. 批量收集每个 sector 的候选二阶资产（有 CMC 映射 + 按市值排序）
    # 一次查全所有 sector 的 top N 资产，后面按需取
    print("  [3/5] 预加载各板块候选资产池...", end=" ", flush=True)
    all_sectors = set()
    for s_set in cat_sectors.values():
        all_sectors.update(s_set)
    all_sectors = list(all_sectors)

    sector_pool = defaultdict(list)
    if all_sectors:
        # 取每个 sector 市值前 50 的资产（有 CMC 主映射的）
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
            sector_pool[r["primary_sector"]].append({
                "asset_id": r["asset_id"],
                "market_cap": mc,
            })
        # 按市值降序
        for sector in sector_pool:
            sector_pool[sector].sort(key=lambda x: x["market_cap"], reverse=True)
    print(f"done ({len(all_sectors)} 个板块)")

    # 4. 生成二阶受益映射
    print("  [4/5] 生成二阶受益映射...", end=" ", flush=True)
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

        # 按板块取 top N 资产，排除直连的
        for sector in direct_sectors:
            pool = sector_pool.get(sector, [])
            direct_set = set(direct_assets)
            count = 0
            for cand in pool:
                if cand["asset_id"] in direct_set:
                    continue
                # 信心度：二阶基础信心 + 强度加成
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
    print(f"done ({len(all_so_results)} 条二阶映射)")

    # 5. 写入二阶映射 + 生成二阶信号
    if not dry_run and all_so_results:
        print("  [5/5] 写入二阶映射 + 生成信号...", end=" ", flush=True)
        SecondOrderMapper.batch_upsert(conn, all_so_results)

        # 为二阶资产生成初始信号
        # 需要：每个 (catalyst_id, asset_id) 对生成一条信号
        # base_strength 打折（二阶 = 一阶 * 0.6），resonance 用 peer 中位数近似
        # 先查每个 catalyst 的直连资产 resonance 中位数（作为 peer 共振）
        res_rows = conn.execute("""
            SELECT cr.catalyst_id, cr.resonance_score, cr.asset_id
            FROM biz.catalyst_resonance cr
            WHERE cr.catalyst_id = ANY(%s::BIGINT[])
        """, (catalyst_ids,)).fetchall()
        cat_res = defaultdict(list)
        for r in res_rows:
            cat_res[r["catalyst_id"]].append(int(r["resonance_score"] or 0))

        cat_peer_ret = {}
        for cat_id in catalyst_ids:
            scores = cat_res.get(cat_id, [])
            if scores:
                cat_peer_ret[cat_id] = sorted(scores)[len(scores) // 2]  # 中位数
            else:
                cat_peer_ret[cat_id] = 0

        # 准备信号数据
        # 需要 published_at, rule_event_type, horizon 等
        cat_info = {}
        for r in cat_rows:
            cat_info[r["catalyst_id"]] = {
                "kind": r["catalyst_kind"],
                "base_strength": int(r["base_strength"]),
                "rule_event_type": r["rule_event_type"] or "other",
                "published_at": r["published_at"],
            }

        # 批量生成信号（用 CatalystSignalBuilder）
        # 二阶信号：base_strength 打 6 折，resonance 用 peer 中位数
        signals = []
        from datetime import timedelta
        for so in all_so_results:
            cat_id = so.catalyst_id
            info = cat_info.get(cat_id)
            if not info:
                continue

            # 二阶 base_strength 打折
            order2_strength = int(info["base_strength"] * 0.6)
            if order2_strength < 40:
                continue  # 太弱的不生成信号

            peer_median = cat_peer_ret.get(cat_id, 0)
            # 二阶共振稍弱一些（打折）
            res_score = max(0, int(peer_median * 0.7))
            res_state = "confirmed" if res_score >= 70 else ("weak" if res_score >= 35 else "pending")

            # 过期时间同 kind
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
                persistence=None,  # 留空，慢通道补全
                fundamental_pass=None,
                technical_state=None,
                regime=None,
                entry_price=None,
                stop_loss=None,
                take_profit=None,
            )

            if not sig.tier:
                continue  # <40 分不入信号表

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

        # 批量写入（幂等：ON CONFLICT 跳过）
        if signals:
            conn.execute("""
                CREATE TEMP TABLE tmp_so_signals (
                    catalyst_id BIGINT,
                    asset_id BIGINT,
                    kind TEXT,
                    base_strength SMALLINT,
                    resonance_score SMALLINT,
                    resonance_state TEXT,
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
                    regime TEXT,
                    invalidation TEXT,
                    expires_at TIMESTAMPTZ,
                    status TEXT
                ) ON COMMIT DROP
            """)
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO tmp_so_signals (
                        catalyst_id, asset_id, kind, base_strength,
                        resonance_score, resonance_state, persistence,
                        persistence_verified, fundamental_pass, fundamental_detail,
                        technical_state, entry_trigger, entry_trigger_price,
                        entry_price, stop_loss, take_profit, rr_ratio,
                        composite_score, tier, confidence, regime,
                        invalidation, expires_at, status
                    ) VALUES (
                        %(catalyst_id)s, %(asset_id)s, %(kind)s, %(base_strength)s,
                        %(resonance_score)s, %(resonance_state)s, %(persistence)s,
                        %(persistence_verified)s, %(fundamental_pass)s, NULL::jsonb,
                        %(technical_state)s, %(entry_trigger)s, %(entry_trigger_price)s,
                        %(entry_price)s, %(stop_loss)s, %(take_profit)s, %(rr_ratio)s,
                        %(composite_score)s, %(tier)s, %(confidence)s, %(regime)s,
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
                FROM tmp_so_signals t
                ON CONFLICT (catalyst_id, asset_id) DO NOTHING
            """)
        print(f"done (写入 {len(all_so_results)} 条映射, {len(signals)} 条信号)")
    else:
        print("  [5/5] 写入二阶映射 + 生成信号... skipped (dry-run 或无数据)")

    return {
        "step": "step6b",
        "second_order_count": len(all_so_results),
        "catalyst_count": len(cat_rows),
    }


# =====================================================================
# Step 7: G6 信号重算（用完整 G3-G5 数据，全字段更新）
# =====================================================================

def step7_recalc_signals(conn, config: dict, dry_run: bool = False) -> dict:
    """用完整的 G3-G5 数据重算 composite_score + tier + rr_ratio + 价格位。

    P0 的 signal 是用占位值（persistence=50, fundamental=50, technical=50）算的，
    P1 有了真实 G3-G5 数据后需要重算 composite_score 和 tier。
    """
    print("  [1/3] 加载配置...", end=" ", flush=True)
    builder = CatalystSignalBuilder(config)
    print("done")

    # 查出所有已有 signal + 完整 G1-G5 数据
    print("  [2/3] 批量获取完整 G1-G5 数据...", end=" ", flush=True)
    rows = conn.execute("""
        SELECT cs.catalyst_id, cs.asset_id,
               cs.kind, cs.base_strength,
               cs.resonance_score, cs.resonance_state,
               cs.persistence, cs.fundamental_pass, cs.technical_state,
               cs.entry_trigger_price, cs.regime,
               ac.published_at
        FROM biz.catalyst_signal cs
        JOIN biz.asset_catalyst ac ON cs.catalyst_id = ac.catalyst_id
        ORDER BY cs.catalyst_id
    """).fetchall()
    print(f"done ({len(rows)} 条)")

    if not rows:
        return {"step": "step7", "recalculated": 0}

    # 算 stop_loss / take_profit（简化：用 30d 波动率估算 ATR）
    # 这里直接用 entry_trigger_price 的比例近似（简单但够用）
    print("  [3/3] 重算 composite_score + tier + 批量更新...", end=" ", flush=True)

    updates = []
    tier_dist = {}
    for row in rows:
        kind = row["kind"] or "event"
        entry_price = row["entry_trigger_price"]
        if entry_price is not None:
            entry_price = float(entry_price)

        # 简化版 stop/tp：
        #   stop_loss = entry * (1 - atr_factor)，atr_factor 用 regime 调整
        #   take_profit = entry * (1 + 2 * atr_factor)（R:R ≈ 2）
        stop_loss = None
        take_profit = None
        if entry_price and entry_price > 0:
            regime = row["regime"] or "neutral"
            atr_pct = 0.08 if regime == "risk_off" else (0.12 if regime == "risk_on" else 0.10)
            # 技术面 down 态的话止损收紧
            if row["technical_state"] == "down":
                atr_pct *= 0.7
            stop_loss = round(entry_price * (1 - atr_pct), 6)
            take_profit = round(entry_price * (1 + 2.2 * atr_pct), 6)

        # 用完整数据重算信号
        signal = builder.build(
            catalyst_id=row["catalyst_id"],
            asset_id=row["asset_id"],
            kind=kind,
            base_strength=int(row["base_strength"] or 0),
            resonance_score=int(row["resonance_score"] or 0),
            resonance_state=row["resonance_state"] or "pending",
            published_at=row["published_at"],
            persistence=row["persistence"],
            fundamental_pass=row["fundamental_pass"],
            technical_state=row["technical_state"],
            regime=row["regime"],
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        tier_dist[signal.tier or "none"] = tier_dist.get(signal.tier or "none", 0) + 1
        updates.append({
            "catalyst_id": row["catalyst_id"],
            "asset_id": row["asset_id"],
            "composite_score": signal.composite_score,
            "tier": signal.tier,
            "confidence": signal.confidence,
            "invalidation": signal.invalidation,
            "expires_at": signal.expires_at,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "rr_ratio": signal.rr_ratio,
        })

    if updates and not dry_run:
        conn.execute("""
            CREATE TEMP TABLE tmp_signal_recalc (
                catalyst_id BIGINT,
                asset_id BIGINT,
                composite_score SMALLINT,
                tier TEXT,
                confidence NUMERIC(4,3),
                invalidation TEXT,
                expires_at TIMESTAMPTZ,
                stop_loss NUMERIC(24,10),
                take_profit NUMERIC(24,10),
                rr_ratio NUMERIC(6,2)
            ) ON COMMIT DROP
        """)
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO tmp_signal_recalc VALUES (
                    %(catalyst_id)s, %(asset_id)s, %(composite_score)s, %(tier)s,
                    %(confidence)s, %(invalidation)s, %(expires_at)s,
                    %(stop_loss)s, %(take_profit)s, %(rr_ratio)s
                )
            """, updates)

        conn.execute("""
            UPDATE biz.catalyst_signal cs
            SET composite_score = t.composite_score,
                tier = t.tier,
                confidence = t.confidence,
                invalidation = t.invalidation,
                expires_at = t.expires_at,
                stop_loss = t.stop_loss,
                take_profit = t.take_profit,
                rr_ratio = t.rr_ratio,
                status = CASE
                    WHEN t.tier IS NULL THEN 'invalid'
                    WHEN cs.status = 'expired' THEN 'expired'
                    ELSE 'open'
                END,
                updated_at = NOW()
            FROM tmp_signal_recalc t
            WHERE cs.catalyst_id = t.catalyst_id
              AND cs.asset_id = t.asset_id
        """)

    print(f"done ({len(updates)} 条)")
    print("  tier 分布（重算后）:")
    for tier, cnt in sorted(tier_dist.items()):
        print(f"    {tier}: {cnt}")

    return {"step": "step7", "recalculated": len(updates), "tier_distribution": tier_dist}


# =====================================================================
# 主入口
# =====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂决策管道 - 历史回灌")
    parser.add_argument("--all", action="store_true", help="执行全部步骤")
    parser.add_argument("--step", type=int, help="只执行单步 (1-7)")
    parser.add_argument("--from-step", type=int, help="从第几步开始")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--no-email", action="store_true", help="禁邮件（默认已禁）")
    args = parser.parse_args()

    if not args.all and not args.step and not args.from_step:
        parser.print_help()
        print("\n示例:")
        print("  python phase_catalyst_backfill.py --all          # 全量回填")
        print("  python phase_catalyst_backfill.py --step 6       # 只跑第 6 步")
        print("  python phase_catalyst_backfill.py --from-step 6  # 从第 6 步开始")
        print("  python phase_catalyst_backfill.py --all --dry-run # 预览")
        return 1

    config = load_config()

    # 确定要跑的步骤
    step_funcs = [
        step1_regime_backfill,
        step2_classify_grade,
        step3_resonance,
        step4_signal,
        step5_expire_verify,
        step6_g3_g4_g5,
        step6b_second_order,
        step7_recalc_signals,
    ]

    if args.step:
        if args.step < 1 or args.step > len(step_funcs):
            print(f"错误: step 必须在 1-{len(step_funcs)} 之间")
            return 1
        start = args.step - 1
        end = args.step
    elif args.from_step:
        start = args.from_step - 1
        end = len(step_funcs)
    else:
        start = 0
        end = len(step_funcs)

    print("=" * 60)
    print("催化剂决策管道 - 历史回灌")
    print(f"执行步骤: {start+1}-{end}")
    if args.dry_run:
        print("模式: DRY-RUN（只打印，不写入）")
    print("=" * 60)

    with get_conn() as conn:
        results = []
        for i in range(start, end):
            func = step_funcs[i]
            try:
                result = func(conn, config, dry_run=args.dry_run)
                results.append(result)
            except Exception as e:
                print(f"\n❌ Step {i+1} 失败: {e}")
                import traceback
                traceback.print_exc()
                if not args.dry_run:
                    conn.rollback()
                return 1

        if not args.dry_run:
            conn.commit()

    print("\n" + "=" * 60)
    print("回灌完成 ✓")
    print("=" * 60)
    for r in results:
        print(f"  {r.get('step', '?')}: {r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
