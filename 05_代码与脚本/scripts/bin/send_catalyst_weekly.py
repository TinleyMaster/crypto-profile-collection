#!/usr/bin/env python3
"""催化剂周报邮件发送（薄脚本）。

周报内容：本周重要催化剂事件总结 + 影响解读（叙事由 LLM 生成，失败回退模板拼接）。
scheduler.py 注册：catalyst_weekly_report（每周一 09:00 Asia/Shanghai）。

用法：
    python send_catalyst_weekly.py              # 生成 + 发送（自然周去重）
    python send_catalyst_weekly.py --dry-run    # 仅生成并打印，不发信不占去重位
"""
from __future__ import annotations

import argparse
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
    parser.add_argument("--dry-run", action="store_true",
                        help="仅生成并打印周报，不发信、不占去重位")
    args = parser.parse_args()

    try:
        with get_conn() as conn:
            result = send_catalyst_weekly_report(conn, dry_run=args.dry_run)
    except Exception as e:
        print(f"[ERROR] 周报执行异常: {e}")
        return 1

    print(f"[INFO] 窗口 {result.get('window')} · 重要事件 {result.get('event_count')} 条 · "
          f"信号 {result.get('signals_total')} 条 · 叙事来源 {result.get('narrative_source')}")

    if result.get("sent"):
        print(f"[OK] 周报已发送: {result.get('reason')}")
        return 0
    if result.get("skipped"):
        print(f"[INFO] 跳过发送: {result.get('reason')}")
        return 0
    if result.get("body"):
        print(result["body"])
        print(f"[DRY-RUN] {result.get('reason')}")
        return 0
    print(f"[ERROR] 周报发送失败: {result.get('reason')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())