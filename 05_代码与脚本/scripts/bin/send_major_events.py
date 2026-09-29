#!/usr/bin/env python3
"""重大事件通道 · 低时延兜底发送（薄脚本）。

背景（审计 2026-09-29 P2-3）：重大事件此前只随 `phase_catalyst_pipeline --slow`（每 4h）
或 `catalyst_run_all`（每 12h）/ 快通道 daemon 发送；快 daemon 间歇失效时，事件可能排队
数小时才发出（实测 XLM 8.6h）。本脚本提供独立、高频（scheduler 每 30 分钟）的兜底发送：
只读候选 + 原子去重（同资产 24h + 发送锁），**不会重复发信**；把时延上限压到 ~30min，
并借高频重试覆盖单次查询/发送的瞬时失败。

用法：
    python send_major_events.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_dir_with(target_marker: str, candidates: list[Path]) -> Path | None:
    for d in candidates:
        if (d / target_marker).exists():
            return d
    return None


def _setup_paths() -> Path:
    """探测 catalyst 包位置并加入 sys.path（本地 workbench/ 与容器 /app 双兼容）。"""
    project_root = SCRIPT_DIR.parent.parent
    base_candidates = [project_root / "workbench", project_root, Path("/app")]
    base_dir = _find_dir_with("catalyst/__init__.py", base_candidates)
    if base_dir is None:
        raise RuntimeError(
            f"找不到 catalyst 包，已探测: {[str(p) for p in base_candidates]}"
        )
    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))
    scripts_src = project_root / "scripts" / "src"
    if scripts_src.exists() and str(scripts_src) not in sys.path:
        sys.path.insert(0, str(scripts_src))
    return base_dir


_setup_paths()

from catalyst.db import get_conn  # noqa: E402
from catalyst.notifier import send_major_event_alerts  # noqa: E402


def main() -> int:
    try:
        with get_conn() as conn:
            result = send_major_event_alerts(conn)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 重大事件发送异常: {e}")
        return 1
    print(f"[INFO] 重大事件：sent={result.get('sent')} skipped={result.get('skipped')} "
          f"failed={result.get('failed')}")
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
