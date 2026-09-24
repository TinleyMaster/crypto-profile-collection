#!/usr/bin/env python3
"""
催化剂决策管道主入口（P1 版本）。

快通道（--fast）：
    L1 规则兜底分类 + G0 市场环境 + G1 分级 + G2 共振 + G6 信号骨架
    目标：15 分钟级，零 LLM

慢通道（--slow）：
    G3-1 二阶受益展开 + G3 持续性预判 + G4 基本面 + G5 技术面 + G6 信号重算
    + 二阶共振刷新（既有二阶信号跟随 peer-median 重算）+ 过期巡检
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
import time
import yaml
from bisect import bisect_right
from datetime import datetime, timedelta
from pathlib import Path

import psycopg.errors

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
        project_root,                  # 本地+ 容器，scripts 都在 project 根
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
from catalyst.notifier import (
    send_fast_alerts_for_new_signals,
    send_major_event_alerts,
    send_slow_digest,
)
from catalyst.catalyst_trace import trace_step, reset as trace_reset, summary as trace_summary, set_verbose as trace_set_verbose


def load_config() -> dict:
    """加载 catalyst_rules.yaml 配置，兼容本地与容器路径。

    优先从 catalyst 包内读取（随包分发，Docker 自动包含），
    失败则回退到常见位置探测。
    """
    # 1. 从 catalyst 包内读（最可靠，随包分发）
    try:
        import catalyst
        pkg_dir = Path(catalyst.__file__).resolve().parent
        pkg_config = pkg_dir / "catalyst_rules.yaml"
        if pkg_config.exists():
            with open(pkg_config, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    except Exception:
        pass

    # 2. 多路径探测兜底
    candidates = [
        BASE_DIR / "catalyst_rules.yaml",
        BASE_DIR / "catalyst" / "catalyst_rules.yaml",
        Path(__file__).resolve().parent.parent.parent / "workbench" / "catalyst_rules.yaml",
        Path("/app/catalyst_rules.yaml"),
        Path("/app/catalyst/catalyst_rules.yaml"),
    ]
    for p in candidates:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    raise FileNotFoundError(
        f"找不到 catalyst_rules.yaml，已探测: {[str(p) for p in candidates]}"
    )


def load_calibration(conn) -> dict:
    """加载最新一期实测校准权重（P1）。

    取 catalyst_calibration 最新 window_end 且 weight_mode='calibrated' 的记录，
    组织为 {dim: {value: calibrated_score}} 供 CatalystGrader 注入。
    无校准记录时返回空 dict（完全走先验 yaml）。
    """
    try:
        rows = conn.execute(
            """
            SELECT dim, dim_value, calibrated_score
            FROM biz.catalyst_calibration
            WHERE weight_mode = 'calibrated'
              AND window_end = (SELECT MAX(window_end) FROM biz.catalyst_calibration)
            """
        ).fetchall()
        calib: dict = {}
        for r in rows:
            calib.setdefault(r["dim"], {})[r["dim_value"]] = r["calibrated_score"]
        return calib
    except Exception as e:
        print(f"  [warn] 加载校准权重失败，回退先验: {e}")
        return {}


class _LockRetryExhausted(Exception):
    """锁冲突重试耗尽（区别于 upsert 正常返回 None）。"""


def _rollback_quietly(conn) -> None:
    """psycopg 中语句失败后事务进入 aborted 状态，后续命令全部被拒
    （InFailedSqlTransaction），必须 rollback 才能继续。"""
    try:
        conn.rollback()
    except Exception:
        pass


def _exec_with_retry(conn, sql, params, *, max_retries: int = 3,
                     base_delay: float = 1.0, label: str = "write"):
    """执行一条 SQL，遇到 LockNotAvailable 退避重试。

    psycopg 中 LockNotAvailable 会把当前事务标记为 aborted，
    重试前必须 rollback，否则下一次命令直接抛 InFailedSqlTransaction。

    Returns:
        如果是 RETURNING/SELECT 语句返回 fetched row；否则返回 True 表示执行成功；
        重试耗尽返回 None 表示跳过
    """
    for attempt in range(1, max_retries + 1):
        try:
            cur = conn.execute(sql, params)
            try:
                return cur.fetchone()  # SELECT / RETURNING
            except psycopg.ProgrammingError:
                return True  # UPDATE / INSERT without RETURNING: 执行成功但无行
        except psycopg.errors.LockNotAvailable:
            delay = base_delay * (2 ** (attempt - 1))  # 指数退避：1s, 2s, 4s
            print(f"  [{label}] 锁冲突（第 {attempt}/{max_retries} 次），{delay:.0f}s 后重试...")
            _rollback_quietly(conn)  # 恢复 aborted 事务，否则重试必报 InFailedSqlTransaction
            time.sleep(delay)
    print(f"  [{label}] 锁冲突重试 {max_retries} 次仍失败，跳过这条")
    return None


def _call_with_retry(conn, fn, *, max_retries: int = 3, base_delay: float = 1.0,
                     label: str = "write"):
    """调用任意函数，遇到 LockNotAvailable 退避重试。

    注意：upsert_to_db 等函数成功时可能返回 None，因此不能靠返回值
    判断是否成功；重试耗尽时抛 _LockRetryExhausted 由调用方捕获。

    Returns:
        fn() 的返回值（成功时，可能是 None）
    Raises:
        _LockRetryExhausted: 锁冲突重试耗尽
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except psycopg.errors.LockNotAvailable:
            delay = base_delay * (2 ** (attempt - 1))
            print(f"  [{label}] 锁冲突（第 {attempt}/{max_retries} 次），{delay:.0f}s 后重试...")
            _rollback_quietly(conn)  # 恢复 aborted 事务
            time.sleep(delay)
    print(f"  [{label}] 锁冲突重试 {max_retries} 次仍失败，跳过这条")
    raise _LockRetryExhausted(label)


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
    skipped = 0
    for row in rows:
        pairs = row["related_pairs"] or []
        event_type = classifier.classify(
            row["title"] or "",
            row["body_text"] or "",
            pairs,
        )
        result = _exec_with_retry(
            conn,
            "UPDATE biz.asset_catalyst SET rule_event_type = %s, updated_at = NOW() WHERE catalyst_id = %s",
            (event_type, row["catalyst_id"]),
            label=f"L1_classify(cid={row['catalyst_id']})",
        )
        if result is None:
            skipped += 1
            trace_step("L1_classify", catalyst_id=row["catalyst_id"],
                       title=row["title"] or "",
                       passed=False, reason="锁冲突重试耗尽，跳过")
            continue
        count += 1
        # 每条立即提交：释放 asset_catalyst 行级锁（FOR NO KEY UPDATE），
        # 避免整个 pipeline 长事务持锁 → 与摄入进程的 UPDATE 行级锁冲突
        #（catalyst_grade/catalyst_resonance 的 FK 检查需要 FOR KEY SHARE，2026-09-15 P0）
        try:
            conn.commit()
        except Exception:
            pass
        trace_step("L1_classify", catalyst_id=row["catalyst_id"],
                   title=row["title"] or "",
                   passed=True, metrics={"event_type": event_type})

    if skipped:
        print(f"  L1 分类跳过 {skipped} 条（锁冲突），成功 {count} 条")
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
    try:
        _call_with_retry(
            conn,
            lambda: regime_calc.upsert_to_db(conn, result),
            label="G0_regime",
        )
        conn.commit()
    except _LockRetryExhausted:
        print("  [G0_regime] 锁冲突重试耗尽，跳过市场环境落库")
    return result.regime


# =====================================================================
# G1: 催化剂分级
# =====================================================================

def _load_prelaunch_klines(conn, symbols: list[str]) -> dict[str, list[tuple[float, float]]]:
    """预加载 1h K 线 → {symbol: [(open_time_epoch, close_px), ...]}，按 open_time 升序。

    用于计算「发布前 24h 启动程度」惩罚因子（prelaunch_ret_24h）。
    """
    if not symbols:
        return {}
    klines: dict[str, list[tuple[float, float]]] = {}
    rows = conn.execute(
        """
        SELECT symbol, open_time, close_px
        FROM biz.asset_klines
        WHERE interval = '1h' AND symbol = ANY(%s)
        ORDER BY symbol, open_time ASC
        """,
        (symbols,),
    ).fetchall()
    for r in rows:
        close_px = r["close_px"]
        if close_px is None:
            continue
        ts = r["open_time"].timestamp()
        klines.setdefault(r["symbol"], []).append((ts, float(close_px)))
    return klines


def _compute_prelaunch_ret(klines: list[tuple[float, float]], base_ts: float) -> float | None:
    """发布前 24h 价格变化 % = (base - base_24h_before) / base_24h_before * 100。

    base_price 取 open_time <= base_ts 的最近一根 close；基准取 base_ts - 86400 的最近一根。
    K 线不足（新资产 / 数据未覆盖）返回 None，由 grade 走 fallback 不惩罚。
    """
    if not klines:
        return None
    times = [k[0] for k in klines]
    closes = [k[1] for k in klines]
    i0 = bisect_right(times, base_ts) - 1
    if i0 < 0:
        return None
    i24 = bisect_right(times, base_ts - 86400) - 1
    if i24 < 0:
        return None
    p0 = closes[i0]
    p24 = closes[i24]
    if not p24:
        return None
    return (p0 - p24) / p24 * 100.0


def run_grade(conn, grader: CatalystGrader,
              catalyst_id: int | None = None,
              limit: int | None = None) -> int:
    """对未分级的催化剂做 G1 分级。

    Returns:
        处理数量
    """
    query = """
        SELECT ac.*,
               ARRAY_AGG(cal.asset_id) FILTER (WHERE cal.asset_id IS NOT NULL) AS linked_asset_ids,
               (SELECT MIN(a.market_cap)
                FROM biz.catalyst_asset_link cal2
                JOIN core.asset a ON a.asset_id = cal2.asset_id
                WHERE cal2.catalyst_id = ac.catalyst_id AND a.market_cap IS NOT NULL) AS min_mcap,
               (SELECT a2.canonical_symbol
                FROM biz.catalyst_asset_link cal3
                JOIN core.asset a2 ON a2.asset_id = cal3.asset_id
                WHERE cal3.catalyst_id = ac.catalyst_id
                  AND a2.canonical_symbol IS NOT NULL
                ORDER BY (a2.market_cap IS NULL), a2.market_cap ASC
                LIMIT 1) AS anchor_symbol
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

    # 预加载发布前 K 线：计算「发布前 24h 启动程度」惩罚因子（prelaunch_ret_24h）
    anchor_symbols = sorted({
        (row.get("anchor_symbol") + "USDT")
        for row in rows
        if row.get("anchor_symbol")
    })
    prelaunch_klines = _load_prelaunch_klines(conn, anchor_symbols)

    count = 0
    skipped = 0
    for row in rows:
        linked = [{"asset_id": aid, "market_cap": row.get("min_mcap")}
                  for aid in (row["linked_asset_ids"] or [])]

        catalyst_row = dict(row)
        anchor_sym = row.get("anchor_symbol")
        published_at = row.get("published_at")
        if anchor_sym and published_at is not None:
            klines = prelaunch_klines.get(anchor_sym + "USDT")
            if klines:
                catalyst_row["prelaunch_ret_24h"] = _compute_prelaunch_ret(
                    klines, published_at.timestamp()
                )

        result = grader.grade(catalyst_row, linked_assets=linked if linked else None)
        try:
            _call_with_retry(
                conn,
                lambda: grader.upsert_to_db(conn, result),
                label=f"G1_grade(cid={result.catalyst_id})",
            )
        except _LockRetryExhausted:
            skipped += 1
            trace_step("G1_grade", catalyst_id=result.catalyst_id,
                       title=(row.get("title") or "") if hasattr(row, "get") else None,
                       passed=False, reason="锁冲突重试耗尽，跳过")
            continue
        count += 1
        # 每条立即提交：释放 catalyst_grade 行锁（INSERT 的 FK 检查 FOR KEY SHARE）
        # 及 asset_catalyst 父行锁，避免长事务与摄入进程互相阻塞（2026-09-15 P0）
        try:
            conn.commit()
        except Exception:
            pass

        is_noise = result.catalyst_kind == "noise"
        trace_step(
            "G1_grade",
            catalyst_id=result.catalyst_id,
            title=(row.get("title") or "") if hasattr(row, "get") else None,
            passed=not is_noise,
            reason=("noise：无关联资产/弱事件，G2 前置过滤" if is_noise else None),
            metrics={
                "kind": result.catalyst_kind,
                "base_strength": result.base_strength,
                "authority": result.authority_score,
                "event_weight": result.event_weight,
                "scope": result.scope_score,
                "tradable": result.tradable,
            },
        )

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
            trace_step("G2_resonance", catalyst_id=catalyst_id_val, asset_id=asset_id,
                       passed=False, reason="无行情数据(percent_change_24h=None)，跳过")
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

        try:
            _call_with_retry(
                conn,
                lambda: scorer.upsert_to_db(conn, result),
                label=f"G2_resonance(cid={catalyst_id_val},aid={asset_id})",
            )
        except _LockRetryExhausted:
            trace_step("G2_resonance", catalyst_id=catalyst_id_val, asset_id=asset_id,
                       passed=False, reason="锁冲突重试耗尽，跳过")
            continue
        count += 1
        # 每条立即提交：释放 catalyst_resonance 行锁及 FK 检查的父行锁（2026-09-15 P0）
        try:
            conn.commit()
        except Exception:
            pass

        is_pending = result.resonance_state == "pending"
        trace_step(
            "G2_resonance",
            catalyst_id=catalyst_id_val,
            asset_id=asset_id,
            passed=not is_pending,
            reason=("共振pending（低于 weak 阈值），G6 不会入选" if is_pending else None),
            metrics={
                "resonance_score": result.resonance_score,
                "resonance_state": result.resonance_state,
                "excess_24h": result.excess_ret_24h,
                "vol_z": result.vol_zscore_24h,
                "direction": result.direction_match,
            },
        )

    return count


# =====================================================================
# G6: 信号构建
# =====================================================================

def run_signal(conn, builder: CatalystSignalBuilder,
               regime: str,
               catalyst_id: int | None = None,
               limit: int | None = None) -> tuple[int, int, list[int]]:
    """对有 grade + resonance 的资产构建/更新信号。

    每次全量重算（快通道增量刷新），确保评分、tier、updated_at 随市场变化更新。
    快提醒有 notification 去重表，不会重复推送。

    Returns:
        (处理数, 新插入数, 本轮转为可动作(open)的信号 ID 列表)
            第三个元素用于快提醒：d3 分层后，price 已定价(confirmed) 的信号入观察池
            watch 不推送；只有 status 变 open（新插入，或 watch→open 晋升）才推送。
    """
    # 价格档位必须带入：build() 的「价格完整性闸门」（2026-09-22 P0，COPPER 全 0 档位
    # 仍进 A 级）依赖 entry/stop/tp 判断可交易性。本函数是每轮全量重算的刷新入口，
    # 若传 None，闸门会对**每一行**触发 → 所有 A/B 被封顶 C、所有 open 被降为 watch，
    # 覆盖掉慢通道（run_slow_g3g5）刚算出的正确档位，导致 tier='A' AND status='open'
    # 恒为 0 行、A 级 Alert 邮件被静默跳过。
    # upsert_to_db 对三档用 COALESCE 保留库内旧值，故库内档位即该行有效档位，读回即可。
    query = """
        SELECT cr.catalyst_id, cr.asset_id, cr.resonance_score, cr.resonance_state,
               cg.catalyst_kind, cg.base_strength,
               ac.published_at,
               COALESCE(ci.impact_direction, ac.ai_sentiment) AS impact_direction,
               cs.entry_price, cs.stop_loss, cs.take_profit,
               cs.technical_state, cs.fundamental_pass
        FROM biz.catalyst_resonance cr
        JOIN biz.catalyst_grade cg ON cr.catalyst_id = cg.catalyst_id
        JOIN biz.asset_catalyst ac ON cr.catalyst_id = ac.catalyst_id
        LEFT JOIN biz.catalyst_impact ci
          ON cr.catalyst_id = ci.catalyst_id AND cr.asset_id = ci.asset_id
        LEFT JOIN biz.catalyst_signal cs
          ON cs.catalyst_id = cr.catalyst_id AND cs.asset_id = cr.asset_id
        WHERE cg.catalyst_kind != 'noise'
          AND cr.resonance_state != 'pending'
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
            # G4/G5 不在快通道计算范围（慢通道 run_slow_g3g5 负责），但必须读回库内
            # 已落库的结果参与加权——传 None 会让 fundamental/technical 永久按占位分 50
            # 计分，且 upsert 会把慢通道结果抹掉（见 signal.upsert_to_db 的 COALESCE）。
            fundamental_pass=row["fundamental_pass"],
            technical_state=row["technical_state"],
            regime=regime,
            impact_direction=row["impact_direction"],
            # 读回库内档位参与「价格完整性闸门」判定（详见上方 query 注释）
            entry_price=_to_num(row["entry_price"]),
            stop_loss=_to_num(row["stop_loss"]),
            take_profit=_to_num(row["take_profit"]),
        )
        processed += 1
        try:
            sig_result = _call_with_retry(
                conn,
                lambda: builder.upsert_to_db(conn, signal),
                label=f"G6_signal(cid={signal.catalyst_id})",
            )
        except _LockRetryExhausted:
            trace_step("G6_signal", catalyst_id=signal.catalyst_id,
                       passed=False, reason="锁冲突重试耗尽，跳过")
            continue
        # sig_result 为 None 是正常情况（tier 太低不写入），不是锁冲突
        # sig_result 是 (signal_id, is_new_insert, became_open) 元组
        if sig_result:
            sig_id, is_new, became_open = sig_result
            if is_new:
                inserted += 1
            # d3：只有「本轮由非 open 变为 open」的信号才进推送集合
            # （新插入即 open，或 watch→open 晋升）；已定价的 watch 不推送
            if became_open:
                new_signal_ids.append(sig_id)
        # 每条立即提交：释放 catalyst_signal 行锁及 FK 检查的父行锁（2026-09-15 P0）
        try:
            conn.commit()
        except Exception:
            pass

        # 追溯：G6 被拦原因（tier=None），区分「分不够」与「RR 不达标」
        dropped = signal.tier is None
        reason = None
        if dropped:
            if signal.composite_score < builder.tier_c:
                reason = f"composite={signal.composite_score} < C阈值{builder.tier_c}"
            elif signal.rr_ratio is not None and signal.rr_ratio < builder.min_rr:
                reason = f"rr={signal.rr_ratio} < min_rr={builder.min_rr}"
            else:
                reason = "tier=None（分数不足或RR不达标）"
        trace_step("G6_signal", catalyst_id=row["catalyst_id"], asset_id=row["asset_id"],
                   passed=not dropped, reason=reason,
                   metrics={"tier": signal.tier, "composite": signal.composite_score,
                            "rr": signal.rr_ratio, "kind": signal.kind,
                            "res_state": signal.resonance_state,
                            "status": signal.status,
                            "base_strength": signal.base_strength})

    return processed, inserted, new_signal_ids


# =====================================================================
# 慢通道：二阶受益增量展开
# =====================================================================

def _peer_median_by_catalyst(conn, catalyst_ids: list[int]) -> dict[int, int]:
    """计算各 catalyst 的直连资产共振（peer）中位数。

    口径：取 `biz.catalyst_resonance` 该 catalyst 的全部 resonance_score，
    升序后取下标 len//2（偶数个取偏上中位），与二阶展开创建时的口径一致。
    仅返回「有共振行」的 catalyst——无数据的键不在返回值中，由调用方决定
    兜底策略（创建时按 0，刷新时跳过不降级）。
    """
    from collections import defaultdict

    if not catalyst_ids:
        return {}
    rows = conn.execute("""
        SELECT cr.catalyst_id, cr.resonance_score
        FROM biz.catalyst_resonance cr
        WHERE cr.catalyst_id = ANY(%s::BIGINT[])
    """, (catalyst_ids,)).fetchall()
    scores: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        scores[r["catalyst_id"]].append(int(r["resonance_score"] or 0))
    return {cid: sorted(v)[len(v) // 2] for cid, v in scores.items()}


def _write_second_order_signals(conn, signals: list[dict]) -> None:
    """写入二阶受益信号骨架（一次完整重放）。

    整体封装为幂等函数：锁冲突重试时事务会 rollback，temp 表随之消失，
    因此必须由本函数从头重建 temp 表再写，不能拆成两个独立语句重试。
    """
    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS tmp_slow_so_signals (
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
    conn.execute("TRUNCATE tmp_slow_so_signals")
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
               ac.rule_event_type, ac.ai_sentiment, ac.published_at
        FROM biz.catalyst_grade cg
        JOIN biz.asset_catalyst ac ON cg.catalyst_id = ac.catalyst_id
        WHERE cg.catalyst_kind IN ('structural', 'event')
          AND cg.base_strength >= 50
          AND cg.created_at >= NOW() - (%s::int * INTERVAL '1 hour')
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
            trace_step("SO_second_order", catalyst_id=catalyst_id,
                       passed=False,
                       reason=f"should_do_second_order=False（kind={kind}, base={base_strength}）")
            continue

        direct_assets = [l["asset_id"] for l in cat_links.get(catalyst_id, [])]
        direct_sectors = list(cat_sectors.get(catalyst_id, set()))
        if not direct_sectors:
            trace_step("SO_second_order", catalyst_id=catalyst_id,
                       passed=False, reason="无直连板块(primary_sector)可展开")
            continue

        cat_count = 0
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
            cat_count += count

        trace_step("SO_second_order", catalyst_id=catalyst_id, passed=True,
                   metrics={"kind": kind, "base_strength": base_strength,
                            "mappings": cat_count})

    if not all_so_results:
        return {"second_order_count": 0, "new_signals": 0}

    # 5. 写入二阶映射 + 生成信号骨架
    # 锁冲突重试：慢通道与回填/定时双跑并发写同一 (catalyst_id, asset_id, order_level)
    # 唯一键时会撞 lock_timeout，重试耗尽则本轮跳过（增量查询 NOT EXISTS 保证下次重跑）
    try:
        _call_with_retry(
            conn,
            lambda: SecondOrderMapper.batch_upsert(conn, all_so_results),
            label="SO_second_order_upsert",
        )
    except _LockRetryExhausted:
        print(f"  [SO_second_order] 二阶映射写入锁冲突重试耗尽，本轮跳过 "
              f"{len(all_so_results)} 条（下次增量重跑）")
        trace_step("SO_second_order", passed=False,
                   reason=f"batch_upsert 锁冲突重试耗尽，跳过 {len(all_so_results)} 条映射")
        return {"second_order_count": 0, "new_signals": 0,
                "catalyst_count": len(cat_rows), "lock_skipped": True}

    cat_info = {}
    for r in cat_rows:
        cat_info[r["catalyst_id"]] = {
            "kind": r["catalyst_kind"],
            "base_strength": int(r["base_strength"]),
            "rule_event_type": r["rule_event_type"] or "other",
            "ai_sentiment": r["ai_sentiment"],
            "published_at": r["published_at"],
        }

    # 各 catalyst 的直连资产共振中位数（口径见 _peer_median_by_catalyst）；
    # 创建路径沿用「无共振数据按 0」的兜底，与历史行为一致
    cat_peer_median = _peer_median_by_catalyst(conn, catalyst_ids)

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
            impact_direction=info["ai_sentiment"],
        )
        if not sig.tier:
            continue

        # 二阶传导信号 tier 上限设为 C —— 弱传导不占 A/B 邮件位
        # 二阶受益属间接关联，置信度天然低于直连资产，不应进入高级别推送
        if sig.tier in ("A", "B"):
            sig.tier = "C"

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
            "status": sig.status,
        })

    if signals:
        try:
            _call_with_retry(
                conn,
                lambda: _write_second_order_signals(conn, signals),
                label="SO_second_order_signal",
            )
        except _LockRetryExhausted:
            print(f"  [SO_second_order] 二阶信号写入锁冲突重试耗尽，本轮跳过 "
                  f"{len(signals)} 条（下次增量重跑）")

    return {
        "second_order_count": len(all_so_results),
        "new_signals": len(signals),
        "catalyst_count": len(cat_rows),
    }


def _write_so_resonance_refresh(conn, updates: list[dict]) -> None:
    """回写二阶共振刷新结果（整体幂等：锁冲突重试时重建 temp 表再写）。"""
    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS tmp_so_res_refresh (
            catalyst_id BIGINT,
            asset_id BIGINT,
            resonance_score SMALLINT,
            resonance_state TEXT,
            composite_score SMALLINT,
            tier TEXT,
            confidence NUMERIC(4,3),
            invalidation TEXT,
            status TEXT
        ) ON COMMIT DROP
    """)
    conn.execute("TRUNCATE tmp_so_res_refresh")
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO tmp_so_res_refresh VALUES (
                %(catalyst_id)s, %(asset_id)s,
                %(resonance_score)s, %(resonance_state)s,
                %(composite_score)s, %(tier)s,
                %(confidence)s, %(invalidation)s, %(status)s
            )
        """, updates)

    conn.execute("""
        UPDATE biz.catalyst_signal cs
        SET resonance_score = t.resonance_score,
            resonance_state = t.resonance_state,
            composite_score = t.composite_score,
            tier = t.tier,
            confidence = t.confidence,
            invalidation = t.invalidation,
            status = t.status,
            updated_at = NOW()
        FROM tmp_so_res_refresh t
        WHERE cs.catalyst_id = t.catalyst_id
          AND cs.asset_id = t.asset_id
          -- 选中后可能被过期巡检置为终态，终态冻结不覆盖
          AND cs.status IN ('open', 'watch', 'invalid')
    """)


def refresh_second_order_resonance(conn, config: dict,
                                   limit: int | None = None) -> dict:
    """二阶信号共振刷新：既有二阶信号每轮跟随当前 peer-median 共振重算。

    为什么需要：run_slow_second_order 的候选集是 one-shot（NOT EXISTS second_order）
    且写入用 ON CONFLICT DO NOTHING，二阶信号的 resonance（composite 权重 0.30，
    最大项）只在创建时算一次。实测 3,670 条二阶信号中 2,352 条 resonance_score
    与当前 peer-median 期望值不符（521 条连 resonance_state 都已失真）。

    刷新口径（与创建时一致，口径单点真源）：
        peer_median     = 该 catalyst 直连资产 resonance_score 升序取 len//2
        resonance_score = max(0, int(peer_median * 0.7))
        resonance_state = confirmed(>=70) / weak(>=35) / pending
        composite/tier  = G6 公式（CatalystSignalBuilder，勿在他处复算）
        status          = d3 动作闸门（confirmed/pending→watch、weak→open、
                          divergent→invalid；tier=None → invalid）
    不覆盖：entry/stop_loss/take_profit/rr_ratio（G5）、persistence/
        fundamental_detail/technical_state（G3-G4）、ai_reason/investment_cycle
        （G7）、expires_at（过期语义）。
    无 peer 共振数据的 catalyst 跳过（不拿缺失数据反向降级），单独计数；
    值无变化的行不写（updated_at 被「信号滞后」口径引用，避免无谓抖动）。

    Returns:
        dict with scanned / refreshed / skipped_no_peer / status_promoted / status_demoted
    """
    from catalyst.signal import CatalystSignalBuilder

    builder = CatalystSignalBuilder(config)

    query = """
        SELECT cs.catalyst_id, cs.asset_id, cs.status,
               cs.kind, cs.base_strength,
               cs.resonance_score, cs.resonance_state,
               cs.composite_score, cs.tier,
               cs.persistence, cs.fundamental_pass, cs.technical_state,
               cs.regime, cs.entry_price, cs.stop_loss, cs.take_profit,
               ac.ai_sentiment, ac.published_at
        FROM biz.catalyst_signal cs
        JOIN biz.catalyst_second_order cso
          ON cso.catalyst_id = cs.catalyst_id AND cso.asset_id = cs.asset_id
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = cs.catalyst_id
        WHERE cs.status IN ('open', 'watch', 'invalid')
        ORDER BY cs.catalyst_id DESC
    """
    params: list = []
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return {"scanned": 0, "refreshed": 0, "skipped_no_peer": 0,
                "status_promoted": 0, "status_demoted": 0}

    catalyst_ids = sorted({r["catalyst_id"] for r in rows})
    peer_median = _peer_median_by_catalyst(conn, catalyst_ids)

    updates: list[dict] = []
    transitions: list[tuple] = []
    skipped_no_peer = 0
    promoted = 0
    demoted = 0

    for row in rows:
        median = peer_median.get(row["catalyst_id"])
        if median is None:
            skipped_no_peer += 1
            continue

        res_score = max(0, int(median * 0.7))
        res_state = ("confirmed" if res_score >= 70
                     else "weak" if res_score >= 35 else "pending")

        signal = builder.build(
            catalyst_id=row["catalyst_id"],
            asset_id=row["asset_id"],
            kind=row["kind"] or "event",
            base_strength=int(row["base_strength"] or 0),
            resonance_score=res_score,
            resonance_state=res_state,
            published_at=row["published_at"],
            persistence=row["persistence"],
            fundamental_pass=row["fundamental_pass"],
            technical_state=row["technical_state"],
            regime=row["regime"],
            entry_price=_to_num(row["entry_price"]),
            stop_loss=_to_num(row["stop_loss"]),
            take_profit=_to_num(row["take_profit"]),
            impact_direction=row["ai_sentiment"],
        )

        # 二阶传导信号 tier 上限 C（P2-2 对齐创建路径 run_slow_second_order）：
        # 二阶受益属间接关联，弱传导不占 A/B 推送位。此前 refresh 路径漏了这一步，
        # 导致 2,558 条二阶信号滞留 B 级。
        if signal.tier in ("A", "B"):
            signal.tier = "C"

        new_status = signal.status if signal.tier is not None else "invalid"

        if (res_score == int(row["resonance_score"] or 0)
                and res_state == (row["resonance_state"] or "")
                and signal.composite_score == int(row["composite_score"] or 0)
                and signal.tier == row["tier"]
                and new_status == row["status"]):
            continue  # 无变化：不写，避免 updated_at 抖动

        if new_status != row["status"]:
            transitions.append((row["catalyst_id"], row["asset_id"],
                                row["status"], new_status, signal.composite_score))
            if new_status == "open":
                promoted += 1
            elif row["status"] == "open":
                demoted += 1

        updates.append({
            "catalyst_id": row["catalyst_id"],
            "asset_id": row["asset_id"],
            "resonance_score": res_score,
            "resonance_state": res_state,
            "composite_score": signal.composite_score,
            "tier": signal.tier,
            "confidence": signal.confidence,
            "invalidation": signal.invalidation,
            "status": new_status,
        })

    if updates:
        try:
            _call_with_retry(
                conn,
                lambda: _write_so_resonance_refresh(conn, updates),
                label="SO_resonance_refresh",
            )
        except _LockRetryExhausted:
            print(f"  [SO_resonance_refresh] 回写锁冲突重试耗尽，本轮跳过 "
                  f"{len(updates)} 条（下轮重算）")
            trace_step("SO_resonance_refresh", passed=False,
                       reason=f"回写锁冲突重试耗尽，跳过 {len(updates)} 条")
            return {"scanned": len(rows), "refreshed": 0,
                    "skipped_no_peer": skipped_no_peer,
                    "status_promoted": 0, "status_demoted": 0,
                    "lock_skipped": True}

    # 只追溯状态迁移（每轮量级小，且是下游「可动作集合」的真实变化），
    # 纯分数修正不逐条追溯，避免每轮数百行噪声
    for cat_id, asset_id, prev_status, new_status, composite in transitions:
        trace_step("SO_resonance_refresh", catalyst_id=cat_id, asset_id=asset_id,
                   passed=True,
                   reason=f"status {prev_status} → {new_status}",
                   metrics={"prev_status": prev_status, "status": new_status,
                            "composite": composite})

    return {
        "scanned": len(rows),
        "refreshed": len(updates),
        "skipped_no_peer": skipped_no_peer,
        "status_promoted": promoted,
        "status_demoted": demoted,
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
               ac.rule_event_type, ac.ai_sentiment, ac.published_at,
               a.asset_type, a.primary_sector,
               ci.impact_direction,
               (cso.catalyst_id IS NOT NULL) AS is_second_order
        FROM biz.catalyst_signal cs
        JOIN biz.asset_catalyst ac ON cs.catalyst_id = ac.catalyst_id
        JOIN core.asset a ON cs.asset_id = a.asset_id
        LEFT JOIN biz.catalyst_impact ci
          ON cs.catalyst_id = ci.catalyst_id AND cs.asset_id = ci.asset_id
        LEFT JOIN biz.catalyst_second_order cso
          ON cs.catalyst_id = cso.catalyst_id AND cs.asset_id = cso.asset_id
        -- d3：观察池(watch)同样要补全 G3-G5。否则晋升为 open 后仍无 entry/stop/tp，
        -- 慢通道 Alert 的「交易档位齐全」闸门永远过不了，只能空转。
        -- 注：technical_detail（fix_054）不在此条件内。它由 backfill_technical_detail()
        --    单独追加式回填，避免把「明细缺失」当成「G3-G5 缺失」而触发全量重算
        --    （会一次性改写全部 open/watch 信号的档位/档级，属非预期副作用）。
        WHERE cs.status IN ('open', 'watch')
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
        FROM biz.v_asset_market_daily_primary
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
        # 方向：优先 (催化剂,资产) 级 impact，二阶资产无 impact 行时回退催化剂级 ai_sentiment
        impact_direction = row["impact_direction"] or row["ai_sentiment"]

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

        # 止损止盈（基于技术分析的支撑阻力位，RR 动态而非固定 2.2）
        entry_price = tech_result.entry_trigger_price
        stop_loss = tech_result.stop_loss_price
        take_profit = tech_result.take_profit_price

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
            impact_direction=impact_direction,
        )

        # 二阶传导信号 tier 上限 C（P2-2 对齐创建路径 run_slow_second_order）：
        # 重算 G3-G5 时同样不能把二阶信号抬回 A/B。
        if row["is_second_order"] and signal.tier in ("A", "B"):
            signal.tier = "C"

        tier_dist[signal.tier or "none"] += 1

        # d3 动作闸门：status 由本轮 resonance_state 推出（tier=None → invalid），
        # 不能硬编码 'open'，否则已定价(confirmed)/未反应(pending)的信号会被
        # 误放进可动作集合（破坏「open 全 weak」不变量）。
        new_status = signal.status if signal.tier is not None else "invalid"

        # 追溯：G3-G5 补全后的信号重算结果（tier=None → 会被置为 invalid）
        trace_step(
            "G3G5_recalc",
            catalyst_id=catalyst_id,
            asset_id=asset_id,
            passed=signal.tier is not None,
            reason=(f"重算后 tier=None → 置 invalid（composite={signal.composite_score}）"
                    if signal.tier is None else None),
            metrics={
                "tier": signal.tier,
                "composite": signal.composite_score,
                "rr": signal.rr_ratio,
                "persistence": persistence,
                "fundamental": fund_result.pass_,
                "technical": tech_result.technical_state,
                "status": new_status,
            },
        )

        updates.append({
            "catalyst_id": catalyst_id,
            "asset_id": asset_id,
            "persistence": persistence,
            "persistence_verified": False,
            "fundamental_pass": fund_result.pass_,
            "fundamental_detail": json.dumps(fund_result.detail, ensure_ascii=False),
            "technical_state": tech_result.technical_state,
            "technical_detail": json.dumps(tech_result.detail, ensure_ascii=False),
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
            "status": new_status,
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
                technical_detail JSONB,
                entry_trigger TEXT,
                entry_trigger_price NUMERIC,
                entry_price NUMERIC,
                stop_loss NUMERIC,
                take_profit NUMERIC,
                rr_ratio NUMERIC(6,2),
                composite_score SMALLINT,
                tier TEXT,
                confidence NUMERIC(4,3),
                invalidation TEXT,
                status TEXT
            ) ON COMMIT DROP
        """)
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO tmp_slow_g3g5 VALUES (
                    %(catalyst_id)s, %(asset_id)s, %(persistence)s,
                    %(persistence_verified)s, %(fundamental_pass)s,
                    %(fundamental_detail)s::jsonb, %(technical_state)s,
                    %(technical_detail)s::jsonb, %(entry_trigger)s, %(entry_trigger_price)s,
                    %(entry_price)s, %(stop_loss)s, %(take_profit)s,
                    %(rr_ratio)s, %(composite_score)s, %(tier)s,
                    %(confidence)s, %(invalidation)s, %(status)s
                )
            """, updates)

        conn.execute("""
            UPDATE biz.catalyst_signal cs
            SET persistence = t.persistence,
                persistence_verified = t.persistence_verified,
                fundamental_pass = t.fundamental_pass,
                fundamental_detail = t.fundamental_detail,
                technical_state = t.technical_state,
                technical_detail = t.technical_detail,
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
                -- d3 分层：status 跟随本轮 resonance_state 推出的动作闸门，
                -- 不硬编码 'open'（口径见 CatalystSignalBuilder._initial_status）
                status = t.status,
                updated_at = NOW()
            FROM tmp_slow_g3g5 t
            WHERE cs.catalyst_id = t.catalyst_id
              AND cs.asset_id = t.asset_id
              -- 选中后可能被过期巡检置为终态，终态冻结不覆盖
              AND cs.status IN ('open', 'watch')
        """)

    return {
        "processed": len(updates),
        "tier_distribution": dict(tier_dist),
    }


def backfill_technical_detail(conn, config: dict,
                              limit: int | None = None) -> dict:
    """追加式回填 G5 技术面明细（fix_054），只写 technical_detail 一列。

    与 run_slow_g3g5 的区别：本函数不重算 persistence/fundamental/档位/tier/status，
    因此不会因为「明细缺失」而改写历史决策结果。用途是让 A 级 Alert 邮件能展开
    「MA/ATR/30d 高低点 → 档位」的推导过程。

    处理对象：status IN ('open','watch') AND technical_detail IS NULL
      - expired/done 终态不回填（避免用当前技术位冒充决策时点数据）
    幂等：已填充的行不再入选；无 NULL 行时只做一次查询即返回。
    """
    import json
    from collections import defaultdict

    tech_analyzer = TechnicalAnalyzer(config)

    query = """
        SELECT cs.signal_id, cs.asset_id, ci.impact_direction
        FROM biz.catalyst_signal cs
        LEFT JOIN biz.catalyst_impact ci
          ON cs.catalyst_id = ci.catalyst_id AND cs.asset_id = ci.asset_id
        WHERE cs.status IN ('open', 'watch')
          AND cs.technical_detail IS NULL
        ORDER BY cs.asset_id
    """
    params = []
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return {"scanned": 0, "backfilled": 0}

    asset_ids = list({r["asset_id"] for r in rows})
    daily_rows = conn.execute("""
        SELECT asset_id, market_date, price_usd, volume_24h
        FROM biz.v_asset_market_daily_primary
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

    updates = []
    for row in rows:
        asset_id = row["asset_id"]
        tech_result = tech_analyzer.analyze(
            asset_id=asset_id,
            daily_data=daily_map.get(asset_id, []),
            impact_direction=row["impact_direction"],
        )
        if not tech_result.detail:
            continue
        updates.append({
            "signal_id": row["signal_id"],
            "technical_detail": json.dumps(tech_result.detail, ensure_ascii=False),
        })

    if updates:
        conn.execute("""
            CREATE TEMP TABLE tmp_backfill_td (
                signal_id BIGINT,
                technical_detail JSONB
            ) ON COMMIT DROP
        """)
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO tmp_backfill_td VALUES (
                    %(signal_id)s, %(technical_detail)s::jsonb
                )
            """, updates)
        conn.execute("""
            UPDATE biz.catalyst_signal cs
            SET technical_detail = t.technical_detail
            FROM tmp_backfill_td t
            WHERE cs.signal_id = t.signal_id
              AND cs.technical_detail IS NULL
        """)

    return {"scanned": len(rows), "backfilled": len(updates)}


# =====================================================================
# G7: AI 决策增强（慢通道）
# =====================================================================

def run_ai_decision(conn, config: dict, limit: int | None = None) -> dict:
    """为 A/B 级活跃信号(open+watch)补全 AI 推荐原因 + 投资周期（G7）。

    - 处理对象：status IN ('open','watch') AND tier IN ('A','B') AND ai_reason IS NULL
      （d3：观察池同样需要 ai_reason 供早报观察区展示）
    - 用 LLM 逐条生成 reasoning + investment_cycle，
      target_price/stop_loss 为对规则值的评审，AI 有建议则覆盖
    - LLM 不可用或失败：静默跳过，不影响慢通道其他步骤

    Returns:
        dict: {"enhanced": n, "failed": n, "skipped": n}
    """
    from crypto_research.config import get_settings
    try:
        from crypto_research.clients.llm_client import LLMClient
    except Exception:  # noqa: BLE001
        return {"enhanced": 0, "failed": 0, "skipped": 0}

    from catalyst.decision import AIDecisionGenerator

    settings = get_settings(require_database=False)
    llm = LLMClient(settings, rpm=10, timeout=90)
    if not llm.is_available():
        return {"enhanced": 0, "failed": 0, "skipped": 0}

    ai_gen = AIDecisionGenerator(llm)

    # 待补全信号（含催化剂标题、摘要、评分、技术面、盈亏）
    query = """
        SELECT s.signal_id,
               a.canonical_symbol AS symbol,
               a.canonical_name,
               s.kind, s.composite_score, s.tier,
               s.technical_state, s.persistence,
               s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
               s.regime,
               ac.title AS catalyst_title,
               ac.ai_summary
        FROM biz.catalyst_signal s
        JOIN core.asset a ON s.asset_id = a.asset_id
        JOIN biz.asset_catalyst ac ON s.catalyst_id = ac.catalyst_id
        WHERE s.status IN ('open', 'watch')
          AND s.tier IN ('A', 'B')
          AND s.ai_reason IS NULL
        ORDER BY s.composite_score DESC
    """
    params: list = []
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return {"enhanced": 0, "failed": 0, "skipped": 0}

    enhanced = 0
    failed = 0
    for r in rows:
        try:
            dec = ai_gen.generate(
                symbol=r["symbol"],
                asset_name=r["canonical_name"],
                kind=r["kind"],
                catalyst_title=r["catalyst_title"],
                catalyst_summary=r.get("ai_summary"),
                regime=r["regime"],
                composite_score=int(r["composite_score"] or 0),
                tier=r["tier"],
                technical_state=r["technical_state"],
                persistence=r["persistence"],
                take_profit=_to_num(r.get("take_profit")),
                stop_loss=_to_num(r.get("stop_loss")),
                entry_price=_to_num(r.get("entry_price")),
                rr_ratio=_to_num(r.get("rr_ratio")),
            )
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠️  signal {r['signal_id']} AI 决策异常: {e}")
            failed += 1
            continue

        if dec is None or (not dec.get("reasoning") and not dec.get("investment_cycle")):
            failed += 1
            continue

        # 组合目标价/止损（AI 有建议则覆盖，否则保留规则值）
        new_tp = dec.get("target_price") if dec.get("target_price") is not None else _to_num(r.get("take_profit"))
        new_sl = dec.get("stop_loss") if dec.get("stop_loss") is not None else _to_num(r.get("stop_loss"))

        conn.execute(
            """
            UPDATE biz.catalyst_signal
            SET ai_reason = %s,
                investment_cycle = %s,
                take_profit = %s,
                stop_loss = %s,
                updated_at = NOW()
            WHERE signal_id = %s
            """,
            (dec.get("reasoning"), dec.get("investment_cycle"), new_tp, new_sl, r["signal_id"]),
        )
        enhanced += 1

    return {"enhanced": enhanced, "failed": failed, "skipped": len(rows) - enhanced - failed}


def _to_num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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

    # 活跃信号(open+watch)各维度覆盖率（G3/G4/G5）
    # d3：覆盖率的分母必须含观察池，否则 watch 未补全会污染指标并触发误告警
    g3_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status IN ('open','watch') AND persistence IS NOT NULL
    """).fetchone()
    g4_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status IN ('open','watch') AND fundamental_pass IS NOT NULL
    """).fetchone()
    g5_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status IN ('open','watch') AND technical_state IS NOT NULL
    """).fetchone()
    live_row = conn.execute("""
        SELECT COUNT(*) FILTER (WHERE status = 'open')  AS open_cnt,
               COUNT(*) FILTER (WHERE status = 'watch') AS watch_cnt
        FROM biz.catalyst_signal
        WHERE status IN ('open','watch')
    """).fetchone()
    open_cnt = live_row["open_cnt"] if live_row else 0
    watch_cnt = live_row["watch_cnt"] if live_row else 0
    live_cnt = open_cnt + watch_cnt
    stats["open_signals"] = open_cnt
    stats["watch_signals"] = watch_cnt
    stats["g3_coverage"] = round(
        (g3_row["cnt"] if g3_row else 0) / live_cnt * 100, 2
    ) if live_cnt else 0.0
    stats["g4_coverage"] = round(
        (g4_row["cnt"] if g4_row else 0) / live_cnt * 100, 2
    ) if live_cnt else 0.0
    stats["g5_coverage"] = round(
        (g5_row["cnt"] if g5_row else 0) / live_cnt * 100, 2
    ) if live_cnt else 0.0

    # 技术面分布（活跃信号）
    rows = conn.execute("""
        SELECT technical_state, COUNT(*) as cnt
        FROM biz.catalyst_signal
        WHERE status IN ('open','watch') AND technical_state IS NOT NULL
        GROUP BY technical_state
        ORDER BY cnt DESC
    """).fetchall()
    stats["technical_distribution"] = {r["technical_state"]: r["cnt"] for r in rows}

    # 持续性分布（活跃信号）
    rows = conn.execute("""
        SELECT persistence, COUNT(*) as cnt
        FROM biz.catalyst_signal
        WHERE status IN ('open','watch') AND persistence IS NOT NULL
        GROUP BY persistence
        ORDER BY cnt DESC
    """).fetchall()
    stats["persistence_distribution"] = {r["persistence"]: r["cnt"] for r in rows}

    # 活跃信号中过期的（巡检应该为 0）
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM biz.catalyst_signal
        WHERE status IN ('open','watch') AND expires_at < NOW()
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
    parser.add_argument("--no-alert", action="store_true", help="跳过所有邮件/推送通知（全量重跑时用）")
    parser.add_argument("--backfill-technical-detail", action="store_true",
                        help="一次性回填 G5 技术面明细 technical_detail（fix_054，只写该列，幂等）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    if (not args.fast and not args.slow and not args.health
            and not args.backfill_technical_detail):
        parser.print_help()
        return 1

    config = load_config()

    # 逐步追溯：开启 verbose 时 stdout 也打印通过行；统计每轮清零
    trace_set_verbose(args.verbose)
    trace_reset()

    with get_conn() as conn:
        # ---- 健康检查 ----
        if args.health:
            stats = run_health(conn)
            import json
            print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
            return 0

        # ---- G5 技术面明细回填（fix_054；只写 technical_detail，不重算档位/档级）----
        if args.backfill_technical_detail:
            print("=" * 60)
            print("G5 技术面明细回填（technical_detail）")
            print("=" * 60)
            td_result = backfill_technical_detail(conn, config, limit=args.limit)
            print(f"  扫描 {td_result['scanned']} 条（open/watch 且明细为空），"
                  f"回填 {td_result['backfilled']} 条")

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

            # G1: 分级（注入实测校准权重，无校准则走先验）
            calibration = load_calibration(conn)
            if calibration:
                print(f"  G1 使用实测校准权重: "
                      f"event_type {len(calibration.get('event_type', {}))} 项, "
                      f"source {len(calibration.get('source', {}))} 项, "
                      f"scope {len(calibration.get('scope', {}))} 项")
            grader = CatalystGrader(config, calibration=calibration)
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
            if new_sig_ids and not args.no_alert:
                alert_result = send_fast_alerts_for_new_signals(conn, new_sig_ids)
                if alert_result["sent"] > 0:
                    print(f"  ⚡ 快提醒: 发送 {alert_result['sent']} 条 A 级信号提醒")
                if alert_result.get("suppressed", 0) > 0:
                    print(f"  🚫 快提醒: AI 否决抑制 {alert_result['suppressed']} 条（不发快讯，已留痕）")
                if alert_result["failed"] > 0:
                    print(f"  ⚠️  快提醒失败: {alert_result['failed']} 条")
            elif new_sig_ids and args.no_alert:
                print(f"  ⚡ 快提醒: 跳过（--no-alert），共 {len(new_sig_ids)} 条新信号")

            # 重大事件通道（重要性闸门，与 A 级快提醒独立去重/渲染）
            if args.no_alert:
                print("  📢 重大事件: 跳过（--no-alert）")
            else:
                major_result = send_major_event_alerts(conn)
                if major_result["sent"] > 0:
                    print(f"  📢 重大事件: 发送 {major_result['sent']} 条通报")
                if major_result["failed"] > 0:
                    print(f"  ⚠️  重大事件发送失败: {major_result['failed']} 条")

            print()
            print("快通道完成 ✓")
            trace_summary()

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

            # 2.5 二阶共振刷新：既有二阶信号跟随当前 peer-median 重算
            #     （创建路径是 one-shot + ON CONFLICT DO NOTHING，不刷新则
            #       resonance（权重 0.30）永久冻结在创建时值）
            so_refresh = refresh_second_order_resonance(conn, config, limit=args.limit)
            print(f"  二阶共振刷新: 扫描 {so_refresh.get('scanned', 0)} 条, "
                  f"回写 {so_refresh.get('refreshed', 0)} 条"
                  f"（转 open {so_refresh.get('status_promoted', 0)} / "
                  f"退出 open {so_refresh.get('status_demoted', 0)} / "
                  f"无 peer 数据跳过 {so_refresh.get('skipped_no_peer', 0)}）")

            # 3. 过期巡检
            n_expired = expire_signals(conn)
            print(f"  过期巡检: {n_expired} 条信号置为 expired")

            # 3.5 G7 AI 决策增强（补全推荐原因 + 投资周期；LLM 不可用时跳过）
            ai_result = run_ai_decision(conn, config, limit=args.limit)
            n_ai = ai_result.get("enhanced", 0)
            n_ai_fail = ai_result.get("failed", 0)
            if n_ai or n_ai_fail:
                print(f"  G7 AI 决策: 补全 {n_ai} 条, 失败 {n_ai_fail} 条")
            else:
                print(f"   G7 AI 决策: 无可补全信号（ai_reason 已填充）")

            # 4. 慢通道汇总邮件
            if args.no_alert:
                print(f"  📧  汇总邮件: 跳过（--no-alert）")
            else:
                digest_stats = {
                    "second_order_count": n_so,
                    "g3g5_processed": n_g3g5,
                    "tier_distribution": tier_dist,
                    "expired_count": n_expired,
                }
                digest_result = send_slow_digest(conn, digest_stats)
                if digest_result["sent"] > 0:
                    print(f"  📧  汇总邮件: 已发送（24h 新信号 {digest_result.get('new_signals_24h', 0)} 条）")
                elif digest_result.get("skipped"):
                    print(f"  📧  汇总邮件: 跳过（{digest_result.get('reason', '无新信号')}）")
                else:
                    print(f"  ⚠️  汇总邮件失败: {digest_result.get('reason', 'unknown')}")

            # 4.5 重大事件通道（重要性闸门；慢通道每 4h 兜底一次，
            #     快通道常驻进程恢复后由它提供分钟级时延）
            if args.no_alert:
                print("  📢 重大事件: 跳过（--no-alert）")
            else:
                major_result = send_major_event_alerts(conn)
                if major_result["sent"] > 0:
                    print(f"  📢 重大事件: 发送 {major_result['sent']} 条通报")
                if major_result["failed"] > 0:
                    print(f"  ⚠️  重大事件发送失败: {major_result['failed']} 条")

            print()
            print("慢通道完成 ✓")
            trace_summary()

        conn.commit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
