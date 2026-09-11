"""
催化剂决策管道 — Flask 路由（API）

提供以下接口：
  GET  /api/catalyst/health           管道健康指标
  GET  /api/catalyst/signals          信号列表（分页+筛选）
  GET  /api/catalyst/signals/<id>     信号详情
  GET  /api/catalyst/stats            信号统计概览
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from flask import Blueprint, jsonify, request

# 路径兼容（Docker / 本地）
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent  # workbench/
    CODE_ROOT = WORKSPACE_ROOT.parent  # 05_代码与脚本/
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

from . import db  # noqa: E402

catalyst_bp = Blueprint("catalyst", __name__, url_prefix="/api/catalyst")


# ============================================================
# 工具函数
# ============================================================

def _parse_bool(v: str | None) -> bool | None:
    if v is None:
        return None
    v = v.lower().strip()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return None


def _row_to_signal(row: dict) -> dict:
    """把数据库行转为 API 返回结构（标准化日期 + 数值类型）。"""
    item = dict(row)
    # 日期字段转字符串
    for dt_field in (
        "published_at", "created_at", "updated_at", "expires_at",
        "pre_alert_sent_at", "notified_at", "backtest_hitted_at",
    ):
        if item.get(dt_field) and hasattr(item[dt_field], "isoformat"):
            item[dt_field] = item[dt_field].isoformat()
    # DECIMAL -> float
    for num_field in (
        "entry_price", "stop_loss", "take_profit", "rr_ratio",
        "confidence", "entry_trigger_price", "backtest_pnl",
    ):
        if item.get(num_field) is not None:
            item[num_field] = float(item[num_field])
    return item


# ============================================================
# 健康指标
# ============================================================

@catalyst_bp.route("/health")
def get_health():
    """催化剂管道健康指标。

    包含：总 catalyst 数、分类覆盖率、共振覆盖率、分级分布、
    信号总数/按 tier+status 分布、G3/G4/G5 覆盖率、技术面分布、
    持续性分布、二阶受益统计、快慢通道最近运行时间。
    """
    try:
        with db.get_conn() as conn:
            # 1. 基础统计
            total_catalysts = conn.execute(
                "SELECT COUNT(*) AS cnt FROM biz.asset_catalyst"
            ).fetchone()["cnt"]

            unclassified = conn.execute(
                "SELECT COUNT(*) AS cnt FROM biz.asset_catalyst WHERE rule_event_type IS NULL"
            ).fetchone()["cnt"]

            classify_coverage = round(
                (total_catalysts - unclassified) / total_catalysts * 100, 2
            ) if total_catalysts else 0

            # 2. G1 分级分布
            grade_rows = conn.execute("""
                SELECT kind, COUNT(*) AS cnt
                FROM biz.catalyst_grade
                GROUP BY kind
                ORDER BY cnt DESC
            """).fetchall()
            grade_distribution = {r["kind"]: r["cnt"] for r in grade_rows}

            ungraded = conn.execute("""
                SELECT COUNT(*) AS cnt FROM biz.asset_catalyst ac
                LEFT JOIN biz.catalyst_grade cg ON cg.catalyst_id = ac.catalyst_id
                WHERE cg.catalyst_id IS NULL
            """).fetchone()["cnt"]

            # 3. 共振覆盖率
            res_rows = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN resonance_state IS NOT NULL THEN 1 ELSE 0 END) AS has_res
                FROM biz.catalyst_resonance
            """).fetchone()
            resonance_coverage = round(
                (res_rows["has_res"] or 0) / (res_rows["total"] or 1) * 100, 2
            )

            # 4. 信号总数 + tier/status 分布
            total_signals = conn.execute(
                "SELECT COUNT(*) AS cnt FROM biz.catalyst_signal"
            ).fetchone()["cnt"]

            signals_by_tier = conn.execute("""
                SELECT tier, status, COUNT(*) AS cnt
                FROM biz.catalyst_signal
                GROUP BY tier, status
                ORDER BY tier, status
            """).fetchall()

            open_signals = conn.execute(
                "SELECT COUNT(*) AS cnt FROM biz.catalyst_signal WHERE status = 'open'"
            ).fetchone()["cnt"]

            # 5. G3/G4/G5 覆盖率（针对 open 信号）
            g3_coverage_row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN persistence IS NOT NULL THEN 1 ELSE 0 END) AS has_g3
                FROM biz.catalyst_signal
                WHERE status = 'open'
            """).fetchone()
            g3_coverage = round(
                g3_coverage_row["has_g3"] / g3_coverage_row["total"] * 100, 2
            ) if g3_coverage_row["total"] else 0

            g4_coverage_row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN fundamental_pass IS NOT NULL THEN 1 ELSE 0 END) AS has_g4
                FROM biz.catalyst_signal
                WHERE status = 'open'
            """).fetchone()
            g4_coverage = round(
                g4_coverage_row["has_g4"] / g4_coverage_row["total"] * 100, 2
            ) if g4_coverage_row["total"] else 0

            g5_coverage_row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN technical_state IS NOT NULL THEN 1 ELSE 0 END) AS has_g5
                FROM biz.catalyst_signal
                WHERE status = 'open'
            """).fetchone()
            g5_coverage = round(
                g5_coverage_row["has_g5"] / g5_coverage_row["total"] * 100, 2
            ) if g5_coverage_row["total"] else 0

            # 6. 技术面分布（open）
            tech_rows = conn.execute("""
                SELECT technical_state, COUNT(*) AS cnt
                FROM biz.catalyst_signal
                WHERE status = 'open' AND technical_state IS NOT NULL
                GROUP BY technical_state
                ORDER BY cnt DESC
            """).fetchall()
            technical_distribution = {r["technical_state"]: r["cnt"] for r in tech_rows}

            # 7. 持续性分布（open）
            pers_rows = conn.execute("""
                SELECT persistence, COUNT(*) AS cnt
                FROM biz.catalyst_signal
                WHERE status = 'open' AND persistence IS NOT NULL
                GROUP BY persistence
                ORDER BY cnt DESC
            """).fetchall()
            persistence_distribution = {r["persistence"]: r["cnt"] for r in pers_rows}

            # 8. 过期漏检
            expired_but_open = conn.execute("""
                SELECT COUNT(*) AS cnt FROM biz.catalyst_signal
                WHERE status = 'open' AND expires_at < NOW()
            """).fetchone()["cnt"]

            # 9. 二阶受益统计
            second_order_total = conn.execute(
                "SELECT COUNT(*) AS cnt FROM biz.catalyst_second_order"
            ).fetchone()["cnt"]

            second_order_by_level_rows = conn.execute("""
                SELECT order_level, COUNT(*) AS cnt
                FROM biz.catalyst_second_order
                GROUP BY order_level
                ORDER BY order_level
            """).fetchall()
            second_order_by_level = {
                str(r["order_level"]): r["cnt"] for r in second_order_by_level_rows
            }

            second_order_top_sectors = conn.execute("""
                SELECT sector_name AS sector, COUNT(*) AS cnt
                FROM biz.catalyst_second_order
                WHERE sector_name IS NOT NULL
                GROUP BY sector_name
                ORDER BY cnt DESC
                LIMIT 10
            """).fetchall()
            second_order_top_sectors_list = [
                {r["sector"]: r["cnt"]} for r in second_order_top_sectors
            ]

            # 10. 直连 vs 二阶信号数
            direct_signals = conn.execute("""
                SELECT COUNT(DISTINCT s.signal_id) AS cnt
                FROM biz.catalyst_signal s
                WHERE NOT EXISTS (
                    SELECT 1 FROM biz.catalyst_second_order so
                    WHERE so.catalyst_id = s.catalyst_id
                      AND so.asset_id = s.asset_id
                )
            """).fetchone()["cnt"]

            second_order_signals = conn.execute("""
                SELECT COUNT(DISTINCT s.signal_id) AS cnt
                FROM biz.catalyst_signal s
                JOIN biz.catalyst_second_order so
                  ON so.catalyst_id = s.catalyst_id
                 AND so.asset_id = s.asset_id
            """).fetchone()["cnt"]

            # 11. 慢通道最近运行（从最新信号 created_at 近似）
            slow_latest = conn.execute("""
                SELECT MAX(updated_at) AS ts FROM biz.catalyst_signal
                WHERE status = 'open'
            """).fetchone()["ts"]
            slow_latest_run = slow_latest.isoformat() if slow_latest else None

            fast_latest = conn.execute("""
                SELECT MAX(ac.created_at) AS ts
                FROM biz.asset_catalyst ac
                WHERE ac.source_code LIKE 'kol_catalyst_%'
            """).fetchone()["ts"]
            fast_latest_run = fast_latest.isoformat() if fast_latest else None

        return jsonify({
            "ok": True,
            "data": {
                "total_catalysts": total_catalysts,
                "unclassified_rule": unclassified,
                "classify_coverage": classify_coverage,
                "ungraded": ungraded,
                "grade_distribution": grade_distribution,
                "resonance_coverage": resonance_coverage,
                "total_signals": total_signals,
                "signals_by_tier": [dict(r) for r in signals_by_tier],
                "open_signals": open_signals,
                "g3_coverage": g3_coverage,
                "g4_coverage": g4_coverage,
                "g5_coverage": g5_coverage,
                "technical_distribution": technical_distribution,
                "persistence_distribution": persistence_distribution,
                "expired_but_open": expired_but_open,
                "fast_latest_run": fast_latest_run,
                "slow_latest_run": slow_latest_run,
                "second_order_total": second_order_total,
                "second_order_by_level": second_order_by_level,
                "second_order_top_sectors": second_order_top_sectors_list,
                "direct_signals": direct_signals,
                "second_order_signals": second_order_signals,
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 信号列表
# ============================================================

@catalyst_bp.route("/signals")
def list_signals():
    """催化剂决策信号列表。

    Query params:
        tier:     A/B/C，可逗号分隔多选，默认全部
        status:   open/expired/invalid/done，默认 open
        kind:     structural/event/sentiment/noise，可逗号分隔
        technical_state: up/range/down，可逗号分隔
        order:    2（仅二阶）/ direct（仅直连），默认全部
        hours:    时间窗口（小时），默认 168（7天）
        limit:    每页条数，默认 20，最大 100
        offset:   分页偏移，默认 0
        sort:     排序字段：composite_score/created_at/expires_at，默认 composite_score
        order_dir: asc/desc，默认 desc
    """
    try:
        tier = request.args.get("tier", "").strip()
        status = request.args.get("status", "open").strip()
        kind = request.args.get("kind", "").strip()
        tech_state = request.args.get("technical_state", "").strip()
        order_filter = request.args.get("order", "").strip().lower()
        hours = int(request.args.get("hours", 168))  # 7天
        limit = min(int(request.args.get("limit", 20)), 200)
        offset = int(request.args.get("offset", 0))
        sort = request.args.get("sort", "composite_score").strip()
        order_dir = request.args.get("order_dir", "desc").strip().lower()

        # 排序字段白名单
        sort_allowed = {"composite_score", "created_at", "expires_at", "base_strength"}
        if sort not in sort_allowed:
            sort = "composite_score"
        if order_dir not in ("asc", "desc"):
            order_dir = "desc"

        where = ["1=1"]
        params = []

        if tier:
            tiers = [t.strip() for t in tier.split(",") if t.strip()]
            placeholders = ",".join(["%s"] * len(tiers))
            where.append(f"s.tier IN ({placeholders})")
            params.extend(tiers)

        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            placeholders = ",".join(["%s"] * len(statuses))
            where.append(f"s.status IN ({placeholders})")
            params.extend(statuses)

        if kind:
            kinds = [k.strip() for k in kind.split(",") if k.strip()]
            placeholders = ",".join(["%s"] * len(kinds))
            where.append(f"s.kind IN ({placeholders})")
            params.extend(kinds)

        if tech_state:
            states = [s.strip() for s in tech_state.split(",") if s.strip()]
            placeholders = ",".join(["%s"] * len(states))
            where.append(f"s.technical_state IN ({placeholders})")
            params.extend(states)

        if hours > 0:
            where.append("s.created_at >= NOW() - (%s || ' hours')::interval")
            params.append(str(hours))

        # 直连/二阶过滤
        join_sql = ""
        if order_filter == "2":
            where.append("""
                EXISTS (SELECT 1 FROM biz.catalyst_second_order so
                        WHERE so.catalyst_id = s.catalyst_id
                          AND so.asset_id = s.asset_id)
            """)
        elif order_filter == "direct":
            where.append("""
                NOT EXISTS (SELECT 1 FROM biz.catalyst_second_order so
                            WHERE so.catalyst_id = s.catalyst_id
                              AND so.asset_id = s.asset_id)
            """)

        where_sql = " AND ".join(where)

        with db.get_conn() as conn:
            # 总数
            total_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM biz.catalyst_signal s {join_sql} WHERE {where_sql}",
                params,
            ).fetchone()
            total = total_row["cnt"]

            # 列表：关联 catalyst 标题 + asset 信息 + 二阶标记
            sql = f"""
                SELECT
                    s.signal_id,
                    s.catalyst_id,
                    s.asset_id,
                    a.canonical_symbol AS symbol,
                    a.canonical_name AS asset_name,
                    ac.title AS catalyst_title,
                    ac.source_code AS source,
                    ac.published_at,
                    s.kind,
                    s.base_strength,
                    s.resonance_score,
                    s.resonance_state,
                    s.persistence,
                    s.persistence_verified,
                    s.fundamental_pass,
                    s.technical_state,
                    s.entry_trigger,
                    s.entry_price,
                    s.stop_loss,
                    s.take_profit,
                    s.rr_ratio,
                    s.composite_score,
                    s.tier,
                    s.confidence,
                    s.regime,
                    s.status,
                    s.expires_at,
                    s.created_at,
                    s.updated_at,
                    -- 二阶标记
                    EXISTS (
                        SELECT 1 FROM biz.catalyst_second_order so
                        WHERE so.catalyst_id = s.catalyst_id
                          AND so.asset_id = s.asset_id
                    ) AS is_second_order
                FROM biz.catalyst_signal s
                JOIN core.asset a ON a.asset_id = s.asset_id
                JOIN biz.asset_catalyst ac ON ac.catalyst_id = s.catalyst_id
                WHERE {where_sql}
                ORDER BY s.{sort} {order_dir}
                LIMIT %s OFFSET %s
            """
            rows = conn.execute(sql, params + [limit, offset]).fetchall()

        signals = [_row_to_signal(dict(r)) for r in rows]

        return jsonify({
            "ok": True,
            "data": signals,
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "order_dir": order_dir,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 信号详情
# ============================================================

@catalyst_bp.route("/signals/<int:signal_id>")
def get_signal_detail(signal_id):
    """催化剂决策信号详情。

    返回信号完整信息 + 关联 catalyst 内容 + 资产基础信息。
    """
    try:
        with db.get_conn() as conn:
            row = conn.execute("""
                SELECT
                    s.*,
                    a.canonical_symbol AS symbol,
                    a.canonical_name AS asset_name,
                    ac.title AS catalyst_title,
                    ac.body_text AS content_body,
                    ac.source_code AS source,
                    ac.source_url,
                    ac.published_at,
                    ac.ai_event_type,
                    ac.ai_summary,
                    ac.ai_sentiment,
                    ac.ai_keywords,
                    -- 二阶层级
                    so.order_level,
                    so.sector_name AS second_order_sector,
                    so.derived_from AS second_order_source,
                    so.confidence AS second_order_confidence
                FROM biz.catalyst_signal s
                JOIN core.asset a ON a.asset_id = s.asset_id
                JOIN biz.asset_catalyst ac ON ac.catalyst_id = s.catalyst_id
                LEFT JOIN biz.catalyst_second_order so
                       ON so.catalyst_id = s.catalyst_id
                      AND so.asset_id = s.asset_id
                WHERE s.signal_id = %s
            """, (signal_id,)).fetchone()

        if not row:
            return jsonify({"ok": False, "error": "信号不存在"}), 404

        signal = _row_to_signal(dict(row))

        return jsonify({"ok": True, "data": signal})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 统计概览
# ============================================================

@catalyst_bp.route("/stats")
def get_stats():
    """催化剂信号统计概览（用于首屏卡片）。"""
    try:
        with db.get_conn() as conn:
            # 24h 新增信号
            new_24h = conn.execute("""
                SELECT COUNT(*) AS cnt FROM biz.catalyst_signal
                WHERE created_at >= NOW() - INTERVAL '24 hours'
            """).fetchone()["cnt"]

            # open 信号按 tier 分布
            tier_dist = conn.execute("""
                SELECT tier, COUNT(*) AS cnt
                FROM biz.catalyst_signal
                WHERE status = 'open'
                GROUP BY tier
                ORDER BY tier
            """).fetchall()

            # 今日 A 级信号
            today_a = conn.execute("""
                SELECT COUNT(*) AS cnt FROM biz.catalyst_signal
                WHERE tier = 'A' AND status = 'open'
                  AND created_at::date = CURRENT_DATE
            """).fetchone()["cnt"]

            # 各 kind 信号数（open）
            kind_dist = conn.execute("""
                SELECT kind, COUNT(*) AS cnt
                FROM biz.catalyst_signal
                WHERE status = 'open'
                GROUP BY kind
                ORDER BY cnt DESC
            """).fetchall()

            # 平均综合分
            avg_score_row = conn.execute("""
                SELECT AVG(composite_score) AS avg_score
                FROM biz.catalyst_signal
                WHERE status = 'open'
            """).fetchone()
            avg_score = round(float(avg_score_row["avg_score"]), 1) if avg_score_row["avg_score"] else 0

            # 催化剂源数
            source_count = conn.execute("""
                SELECT COUNT(DISTINCT source_code) AS cnt
                FROM biz.asset_catalyst
                WHERE created_at >= NOW() - INTERVAL '7 days'
            """).fetchone()["cnt"]

            # G5 覆盖率（open 信号中 technical_state 非空的比例）
            g5_cov_row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE technical_state IS NOT NULL) AS has_g5
                FROM biz.catalyst_signal
                WHERE status = 'open'
            """).fetchone()
            g5_coverage_pct = round(
                float(g5_cov_row["has_g5"]) / float(g5_cov_row["total"]) * 100, 1
            ) if g5_cov_row["total"] else 0

            # open 信号总数
            total_open = conn.execute("""
                SELECT COUNT(*) AS cnt FROM biz.catalyst_signal WHERE status = 'open'
            """).fetchone()["cnt"]

        return jsonify({
            "ok": True,
            "stats": {
                "new_24h": new_24h,
                "new_24h_tier_A": today_a,
                "total_open": total_open,
                "avg_score": avg_score,
                "tier_distribution": {r["tier"]: r["cnt"] for r in tier_dist},
                "kind_distribution": {r["kind"]: r["cnt"] for r in kind_dist},
                "source_count": source_count,
                "g5_coverage_pct": g5_coverage_pct,
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
