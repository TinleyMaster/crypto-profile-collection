"""催化剂管道逐步追溯（trace）——每一步筛选留痕，便于定位问题与调参。

OPT-CATALYST-ALERT-001 衍生需求（2026-09-15）：
    每个 catalyst/asset 在每一阶段（L1 分类 → G1 分级 → G2 共振 → G6 信号
    → 慢通道 二阶/G3-G5/G7）是「通过」还是「被拦」，都要落痕，含阈值与原因，
    便于定位"哪一步出了问题"、据此调参。

输出两路（任何异常都不影响主流程）：
    1) JSONL 文件  workbench/output/catalyst_trace/YYYY-MM-DD.jsonl（完整审计，可 grep）
    2) stdout：被拦行始终打印；通过行仅在 verbose 时打印（避免全量刷屏）

用法（管道内，模块级即可，无需传参）：
    from catalyst.catalyst_trace import trace_step, reset, summary, set_verbose
    set_verbose(args.verbose)          # 管道入口
    reset()                             # 每轮运行开始前清零统计
    trace_step("G6_signal", catalyst_id=..., asset_id=...,
               passed=False, reason="composite=35 < C阈值40",
               metrics={"composite": 35, "tier": None})
    summary()                           # 结束时打印每步 通过/被拦 统计
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

_lock = threading.Lock()

# 默认 trace 目录：workbench/output/catalyst_trace/（与 ai_trace 同级）
_TRACE_BASE = os.environ.get(
    "CATALYST_TRACE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "output", "catalyst_trace"),
)

# 每步「通过/被拦」计数（跨 run_* 函数汇总，main() 里 reset / summary）
_STATS: dict[str, dict] = {}
_VERBOSE = False


def set_verbose(v: bool) -> None:
    """控制 stdout：True 时通过行也打印（默认只打印被拦行）。"""
    global _VERBOSE
    _VERBOSE = bool(v)


def reset() -> None:
    """清零分步统计（每轮管道运行开始时调用）。"""
    _STATS.clear()


def _trace_path() -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    try:
        os.makedirs(_TRACE_BASE, exist_ok=True)
    except Exception:
        pass
    return os.path.join(_TRACE_BASE, f"{day}.jsonl")


def _safe_json(v):
    """非基础类型统一转 str，保证 json.dumps 不炸。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def trace_step(stage: str, catalyst_id=None, asset_id=None, title=None,
               passed: bool = True, reason: str | None = None,
               metrics: dict | None = None) -> None:
    """写一条逐步追溯记录。

    Args:
        stage:      步骤标识（L1_classify / G1_grade / G2_resonance / G6_signal
                    / SO_second_order / G3G5_recalc）
        catalyst_id: 催化剂 ID
        asset_id:    资产 ID（G2/G6 等资产级步骤）
        title:       催化剂标题（前 24 字展示用）
        passed:      True=通过进入下一步；False=在本步被拦下
        reason:      被拦原因（含阈值），如 "composite=35 < C阈值40"
        metrics:     该步关键数值 dict（便于调参），如 {"kind":"noise","base_strength":20}
    """
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "catalyst_id": catalyst_id,
        "asset_id": asset_id,
        "title": (title or "")[:80],
        "passed": bool(passed),
        "reason": reason,
        "metrics": {k: _safe_json(v) for k, v in (metrics or {}).items()},
    }
    line = json.dumps(rec, ensure_ascii=False)

    # 1) 文件：始终写（完整审计）
    try:
        with _lock:
            with open(_trace_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

    # 2) 分步计数
    try:
        s = _STATS.setdefault(stage, {"passed": 0, "dropped": 0})
        if rec["passed"]:
            s["passed"] += 1
        else:
            s["dropped"] += 1
    except Exception:
        pass

    # 3) stdout：被拦行始终打印；通过行仅 verbose
    try:
        flag = "✓" if rec["passed"] else "✗"
        head = f"[trace:{stage}] {flag} cat={catalyst_id}"
        if asset_id is not None:
            head += f" asset={asset_id}"
        if rec["title"]:
            head += f" {rec['title']}"
        tail = f" | {reason}" if reason else ""
        m = " ".join(f"{k}={v}" for k, v in rec["metrics"].items())
        if m:
            tail += f" | {m}"
        if not rec["passed"]:
            print(f"  {head}{tail}")
        elif _VERBOSE:
            print(f"  {head}{tail}")
    except Exception:
        pass


def summary() -> None:
    """打印本进程内累计的每步 通过/被拦 统计。"""
    if not _STATS:
        return
    try:
        print("\n── 催化剂逐步追溯统计（trace summary）──")
        print(f"  {'步骤':<20} {'通过':>6} {'被拦':>6}")
        for stage, s in sorted(_STATS.items()):
            print(f"  {stage:<20} {s['passed']:>6} {s['dropped']:>6}")
        print(f"  完整明细见 {_TRACE_BASE}")
    except Exception:
        pass
