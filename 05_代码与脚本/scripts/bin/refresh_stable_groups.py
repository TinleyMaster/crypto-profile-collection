#!/usr/bin/env python3
"""每周刷新「稳定联动组」名单（扫描归因「同稳定组可能联动」的数据源）。

流程：
  1. 子进程跑 workbench/backtest_group_linkage.py（组内相关稳健性回测）
  2. 解析 JSON，取「首/后半段组内相关均超全市场基线」的稳定组
     （stability.stable == true）
  3. 全量替换 biz.stable_linkage_group（单事务：DELETE 全部 + INSERT 新名单）
  4. catalyst_attribution.linkage_peers 实时读该表；表空/缺表时回退内置默认

运行：
    python refresh_stable_groups.py              # 全流程
    python refresh_stable_groups.py --dry-run    # 只打印将写入的名单，不落库
    python refresh_stable_groups.py --top-n 400 --min-days 40
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PROJECT_SRC = _HERE.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def _find_backtest_script() -> Path:
    """定位 workbench/backtest_group_linkage.py（本地与容器两种布局）。"""
    cands = [
        _HERE.parents[1] / "workbench" / "backtest_group_linkage.py",
        Path("/app/workbench/backtest_group_linkage.py"),
        Path("/app/backtest_group_linkage.py"),  # 容器平铺布局（Dockerfile 拷 workbench/*.py 到 /app/）
    ]
    for c in cands:
        if c.exists():
            return c
    raise FileNotFoundError(
        "找不到 workbench/backtest_group_linkage.py，候选: "
        + ", ".join(str(c) for c in cands))


def _run_backtest(top_n: int, min_days: int, min_members: int) -> dict:
    """子进程跑回测 → 解析 JSON 报告。"""
    script = _find_backtest_script()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out_path = tf.name
    cmd = [sys.executable, str(script), "--output", out_path,
           "--top-n", str(top_n), "--min-days", str(min_days),
           "--min-members", str(min_members)]
    print(f"[refresh_stable_groups] 运行回测: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"回测失败 (exit={proc.returncode}): {proc.stderr[-2000:]}")
    try:
        report = json.loads(Path(out_path).read_text(encoding="utf-8"))
    finally:
        Path(out_path).unlink(missing_ok=True)
    return report


def extract_stable_groups(report: dict) -> dict[str, list[str]]:
    """从回测报告提取稳定组：{dim: [group_name, ...]}（stability.stable 为真）。"""
    out: dict[str, list[str]] = {}
    for dim, blk in (report.get("dims") or {}).items():
        groups = [g["group"] for g in blk.get("groups", [])
                  if g.get("stability") and g["stability"].get("stable")]
        if groups:
            out[dim] = sorted(groups)
    return out


def _get_db():
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    return get_connection(get_settings(require_database=True).database_url)


def _replace_groups(conn, groups: dict[str, list[str]]) -> int:
    """全量替换 biz.stable_linkage_group（单事务）。"""
    n = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM biz.stable_linkage_group")
        for dim, gs in groups.items():
            for g in gs:
                cur.execute(
                    "INSERT INTO biz.stable_linkage_group (dim, group_name, verified_at) "
                    "VALUES (%s, %s, NOW())", (dim, g))
                n += 1
    conn.commit()
    return n


def run_once(top_n: int = 400, min_days: int = 90, min_members: int = 3,
             dry_run: bool = False) -> dict:
    """刷新入口（daemon 周任务与独立运行共用）。"""
    report = _run_backtest(top_n, min_days, min_members)
    groups = extract_stable_groups(report)
    summary = {dim: len(gs) for dim, gs in groups.items()}
    print(f"[refresh_stable_groups] 稳定组: {summary}")
    if dry_run:
        for dim, gs in groups.items():
            print(f"  {dim}: {gs}")
        return {"status": "dry_run", "groups": groups}
    with _get_db() as conn:
        n = _replace_groups(conn, groups)
    print(f"[refresh_stable_groups] 已写入 biz.stable_linkage_group {n} 条"
          f"（窗口 {report.get('window')}）")
    return {"status": "ok", "n": n, "groups": groups}


def _notify_failure(exc: Exception) -> None:
    """失败维护告警（P0 审计 C3）：刷新失败发一封运维邮件；SMTP 未配置只打日志。"""
    import html as _html

    try:
        from crypto_research.clients.notifier import EmailNotifier
        from crypto_research.config import get_settings

        settings = get_settings(require_database=True)
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[refresh_stable_groups] SMTP 未配置，跳过失败告警", file=sys.stderr)
            return
        body = (f"<p>稳定联动组名单刷新失败：<pre>{_html.escape(str(exc))}</pre></p>"
                "<p>名单保持上次刷新结果；扫描归因继续用旧名单（兜底内置默认），"
                "不会中断。</p><p>下次自动重试：7 天后。</p>")
        ok, msg = notifier.send(
            "【稳定联动组刷新失败】需人工查看",
            body, from_name="稳定联动组刷新",
            to=settings.admin_email or settings.smtp_to)
        print(f"[refresh_stable_groups] 失败告警发送 {'成功' if ok else '失败: ' + msg}")
    except Exception as notify_exc:  # noqa: BLE001 - 告警失败不影响主流程
        print(f"[refresh_stable_groups] 失败告警发送异常: {notify_exc}", file=sys.stderr)


def run(top_n: int = 400, min_days: int = 90, min_members: int = 3,
        dry_run: bool = False) -> dict:
    """带失败告警的刷新入口（main 与 daemon 周任务共用）。"""
    try:
        return run_once(top_n, min_days, min_members, dry_run)
    except Exception as exc:  # noqa: BLE001
        _notify_failure(exc)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description="每周刷新稳定联动组名单")
    ap.add_argument("--top-n", type=int, default=400)
    ap.add_argument("--min-days", type=int, default=90,
                    help="日频回溯天数（A1：90 天窗口比 40 天样本充足得多，稳定组判定更可信）")
    ap.add_argument("--min-members", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        result = run(args.top_n, args.min_days, args.min_members, args.dry_run)
        return 0 if result["status"] == "ok" else 0
    except Exception as exc:  # noqa: BLE001 - 周任务失败要有可读输出
        print(f"[refresh_stable_groups] 失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
