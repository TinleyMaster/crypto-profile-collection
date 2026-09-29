#!/usr/bin/env python3
"""催化剂周报邮件发送（薄脚本）。

流程：get_conn → send_catalyst_weekly_report → 打印结果。
scheduler.py 注册：catalyst_weekly_report（每周一 09:00 Asia/Shanghai）。

用法：
    python send_catalyst_weekly.py              # 生成 + 发送（自然周去重）
    python send_catalyst_weekly.py --dry-run    # 仅打印统计/清单，不发送
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_dir_with(target_marker: str, candidates: list[Path]) -> Path | None:
    for d in candidates:
        if (d / target_marker).exists():
            return d
    return None


def _setup_paths() -> Path:
    """探测 catalyst 包位置并加入 sys.path，兼容本地与容器两种结构。

    复用 phase_catalyst_pipeline.py 的探测逻辑：
      - 本地开发：project/workbench/catalyst/
      - 容器部署：/app/catalyst/
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

    scripts_src = project_root / "scripts" / "src"
    if scripts_src.exists() and str(scripts_src) not in sys.path:
        sys.path.insert(0, str(scripts_src))
    return base_dir


_setup_paths()

from catalyst.db import get_conn  # noqa: E402
from catalyst.notifier import send_catalyst_weekly_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂周报邮件发送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印统计/清单，不发送")
    args = parser.parse_args()

    try:
        with get_conn() as conn:
            if args.dry_run:
                # 复用内部窗口/统计函数做只读预览（不触发去重/发送）
                from catalyst.notifier import (
                    _weekly_window,
                    _weekly_overview_stats,
                    _weekly_a_signals,
                )
                start_utc, end_utc, window_label = _weekly_window()
                stats = _weekly_overview_stats(conn, start_utc, end_utc)
                a_rows = _weekly_a_signals(conn, start_utc, end_utc)
                print(f"[DRY-RUN] 窗口 {window_label}")
                print(f"[DRY-RUN] 信号总数 {stats['signals_total']} · "
                      f"新入库催化剂 {stats['catalysts_new']} · A 级清单 {len(a_rows)} 条")
                print(f"[DRY-RUN] tier 分布 {stats['tier_dist']}")
                print(f"[DRY-RUN] 事件类型 {stats['event_type_dist']}")
                print(f"[DRY-RUN] 情感 {stats['sentiment_dist']}")
                for r in a_rows:
                    print(f"[DRY-RUN] A级 {r.get('symbol')} "
                          f"(score={float(r.get('composite_score') or 0):.1f}) "
                          f"{r.get('title_cn') or r.get('title') or ''}")
                return 0

            result = send_catalyst_weekly_report(conn)
    except Exception as e:
        print(f"[ERROR] 周报发送异常: {e}")
        return 1

    if result.get("sent"):
        print(f"[OK] 周报已发送: {result.get('reason')}")
        return 0
    if result.get("skipped"):
        print(f"[INFO] 跳过发送: {result.get('reason')}")
        return 0
    print(f"[ERROR] 周报发送失败: {result.get('reason')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
