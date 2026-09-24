#!/usr/bin/env python3
"""
催化剂快通道守护进程（catalyst_fast_daemon）。

从 scheduler 剥离为独立常驻进程，每 15 分钟跑一次快通道：
  L1 规则分类 → G0 市场环境 → G1 分级 → G2 共振 → G6 信号骨架 → A级即时推送

设计：
  - 常驻进程，SkipIfRunning（上一轮未结束则跳过）
  - 共享 DB 连接，避免每次 subprocess 冷启动
  - 与 kol_daemon、scan_daemon 错峰（默认 offset 2min，即每小时 02/17/32/47 分）
  - 单轮异常不影响下一轮

用法：
    python catalyst_fast_daemon.py                  # 启动常驻
    python catalyst_fast_daemon.py --run-once        # 只跑一次（调试）
    python catalyst_fast_daemon.py --interval 900    # 自定义间隔（秒）
    python catalyst_fast_daemon.py --offset 120      # 自定义错峰（秒）
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# 让 catalyst 包可导入，兼容两种部署结构（与 phase_catalyst_pipeline._setup_paths 同口径）：
#   本地开发： 05_代码与脚本/workbench/catalyst/
#   容器部署： /app/catalyst/   ← Dockerfile 把 workbench/catalyst 拷到 /app/catalyst
# 容器内 SCRIPT_DIR=/app/scripts/bin，project_root=/app，硬编码 project_root/"workbench"
# 会指向不存在的 /app/workbench → ModuleNotFoundError: No module named 'catalyst'
# → supervisord 反复 "Exited too quickly" 直至 FATAL（快通道长期未运行，2026-09-21 定位）。
_project_root = SCRIPT_DIR.parent.parent
workbench_dir = next(
    (
        d
        for d in (_project_root / "workbench", _project_root, Path("/app"))
        if (d / "catalyst" / "__init__.py").exists()
    ),
    None,
)
if workbench_dir is None:
    raise RuntimeError(
        "找不到 catalyst 包，请检查部署结构（期望 workbench/catalyst 或 /app/catalyst）"
    )
if str(workbench_dir) not in sys.path:
    sys.path.insert(0, str(workbench_dir))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from catalyst.db import get_conn  # noqa: E402
from catalyst.classify import RuleEventClassifier  # noqa: E402
from catalyst.grade import CatalystGrader, MarketRegime  # noqa: E402
from catalyst.resonance import ResonanceScorer  # noqa: E402
from catalyst.signal import CatalystSignalBuilder  # noqa: E402
from catalyst.notifier import send_fast_alerts_for_new_signals, send_major_event_alerts  # noqa: E402
from catalyst.catalyst_trace import (  # noqa: E402
    reset as trace_reset,
    set_verbose as trace_set_verbose,
)


def load_config() -> dict:
    """加载 catalyst 配置（从 catalyst_rules.yaml）。"""
    import yaml
    config_path = workbench_dir / "catalyst" / "catalyst_rules.yaml"
    if not config_path.exists():
        # 兜底：尝试从包内读
        from importlib import resources
        try:
            with resources.path("catalyst", "catalyst_rules.yaml") as p:
                config_path = Path(p)
        except Exception:
            pass
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_calibration(conn) -> dict:
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


def run_fast_once(verbose: bool = False) -> dict:
    """执行一次快通道。返回统计 dict。"""
    config = load_config()
    trace_set_verbose(verbose)
    trace_reset()

    stats = {}
    with get_conn() as conn:
        # L1: 规则分类
        classifier = RuleEventClassifier(
            rules=config.get("rule_event_keywords", []),
            token_hint_pattern=config.get("token_hint_pattern", ""),
        )
        n_classify = run_classify_wrapper(conn, classifier)
        stats["classify"] = n_classify

        # G0: 市场环境
        regime_calc = MarketRegime(config)
        regime = run_regime_wrapper(conn, regime_calc)
        stats["regime"] = regime

        # G1: 分级（注入实测校准权重，无校准则走先验）
        calibration = _load_calibration(conn)
        if calibration:
            print(f"  [G1] 使用实测校准权重: "
                  f"event_type {len(calibration.get('event_type', {}))} 项, "
                  f"source {len(calibration.get('source', {}))} 项, "
                  f"scope {len(calibration.get('scope', {}))} 项")
        grader = CatalystGrader(config, calibration=calibration)
        n_grade = run_grade_wrapper(conn, grader)
        stats["grade"] = n_grade

        # G2: 共振
        scorer = ResonanceScorer(config)
        n_resonance = run_resonance_wrapper(conn, scorer)
        stats["resonance"] = n_resonance

        # G6: 信号
        builder = CatalystSignalBuilder(config)
        n_processed, n_inserted, new_sig_ids = run_signal_wrapper(conn, builder, regime)
        stats["signal_processed"] = n_processed
        stats["signal_inserted"] = n_inserted

        # A 级即时推送
        if new_sig_ids:
            alert_result = send_fast_alerts_for_new_signals(conn, new_sig_ids)
            stats["alert_sent"] = alert_result.get("sent", 0)
            stats["alert_suppressed"] = alert_result.get("suppressed", 0)
            stats["alert_failed"] = alert_result.get("failed", 0)
        else:
            stats["alert_sent"] = 0
            stats["alert_suppressed"] = 0
            stats["alert_failed"] = 0

        # 重大事件通道（重要性闸门，与上面的 A 级 Alert 独立去重/渲染）。
        # 不依赖 new_sig_ids：重大事件的判据是「tier A/B + 市场已确认」，
        # 与「本轮是否转为 open」无关，否则会漏掉本轮状态未变的老信号。
        major_result = send_major_event_alerts(conn)
        stats["major_event_sent"] = major_result.get("sent", 0)
        stats["major_event_failed"] = major_result.get("failed", 0)

        conn.commit()

    return stats


# ── 从 phase_catalyst_pipeline.py 复用的包装函数 ──
# 这些函数在原脚本的顶层，我们直接从原脚本导入
def _import_pipeline_functions():
    """动态导入原 pipeline 脚本的核心函数。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "catalyst_pipeline",
        SCRIPT_DIR / "phase_catalyst_pipeline.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # 防止 __main__ 级别执行 main()
    spec.loader.exec_module(mod)  # type: ignore
    return mod


_pipeline_mod = None


def _get_pipeline_mod():
    global _pipeline_mod
    if _pipeline_mod is None:
        _pipeline_mod = _import_pipeline_functions()
    return _pipeline_mod


def run_classify_wrapper(conn, classifier):
    mod = _get_pipeline_mod()
    return mod.run_classify(conn, classifier)


def run_regime_wrapper(conn, regime_calc):
    mod = _get_pipeline_mod()
    return mod.run_regime(conn, regime_calc)


def run_grade_wrapper(conn, grader):
    mod = _get_pipeline_mod()
    return mod.run_grade(conn, grader)


def run_resonance_wrapper(conn, scorer):
    mod = _get_pipeline_mod()
    return mod.run_resonance(conn, scorer)


def run_signal_wrapper(conn, builder, regime):
    mod = _get_pipeline_mod()
    return mod.run_signal(conn, builder, regime)


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂快通道常驻守护进程")
    parser.add_argument("--run-once", action="store_true", help="只跑一次（调试用）")
    parser.add_argument("--interval", type=int, default=900,
                        help="轮询间隔秒数（默认 900 = 15 分钟）")
    parser.add_argument("--offset", type=int, default=120,
                        help="启动后错峰等待秒数（默认 120 = 2 分钟，对齐 cron 2,17,32,47）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    print(f"[catalyst_fast_daemon] 启动，间隔 {args.interval}s，错峰 {args.offset}s")

    if args.run_once:
        start = time.time()
        try:
            stats = run_fast_once(verbose=args.verbose)
            elapsed = time.time() - start
            print(f"[catalyst_fast_daemon] 单次完成，耗时 {elapsed:.1f}s，结果: {stats}")
        except Exception as e:
            print(f"[catalyst_fast_daemon] 单次异常: {e}", file=sys.stderr)
            traceback.print_exc()
            return 1
        return 0

    # 初始错峰
    if args.offset > 0:
        time.sleep(args.offset)

    round_count = 0
    running = False
    while True:
        round_count += 1
        start_ts = time.time()

        if running:
            print(f"[catalyst_fast_daemon] 跳过第 {round_count} 轮（上一轮仍在运行）")
            time.sleep(args.interval)
            continue

        running = True
        try:
            print(f"\n[catalyst_fast_daemon] === 第 {round_count} 轮开始 ===")
            stats = run_fast_once(verbose=args.verbose)
            elapsed = time.time() - start_ts
            print(f"[catalyst_fast_daemon] 第 {round_count} 轮完成，耗时 {elapsed:.1f}s")
            print(f"[catalyst_fast_daemon]   分类:{stats.get('classify',0)} "
                  f"分级:{stats.get('grade',0)} 共振:{stats.get('resonance',0)} "
                  f"信号:{stats.get('signal_inserted',0)} "
                  f"告警:{stats.get('alert_sent',0)} "
                  f"抑制:{stats.get('alert_suppressed',0)} "
                  f"重大事件:{stats.get('major_event_sent',0)}")
        except Exception as e:
            elapsed = time.time() - start_ts
            print(f"[catalyst_fast_daemon] 第 {round_count} 轮异常 ({elapsed:.1f}s): {e}",
                  file=sys.stderr)
            traceback.print_exc()
        finally:
            running = False

        # 固定节奏（扣除本轮耗时）
        elapsed = time.time() - start_ts
        sleep_time = max(1.0, args.interval - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    sys.exit(main())
