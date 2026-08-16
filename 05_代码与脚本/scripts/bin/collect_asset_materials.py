"""
单个代币的投研资料补齐流水线（防封号版）。

针对指定 asset_id，按依赖顺序串行执行各采集阶段，全程单 token、串行、
阶段间随机休眠，比批量并发更安全，可断点续跑。

用法：
    python collect_asset_materials.py --asset-id 12345
    python collect_asset_materials.py --asset-id 12345 --dry-run
    python collect_asset_materials.py --asset-id 12345 --stages deep,spa,ai_classify
    python collect_asset_materials.py --asset-id 12345 --sleep-min 2 --sleep-max 5

阶段说明：
    entry_refresh  刷新官网/文档/GitHub 入口（refresh_*_from_cmc/cg/dl 尚未支持 --asset-id，暂缓）
    deep           官网/文档站深爬，发现白皮书/审计/路线图等子文档
    spa            Playwright 渲染 JS 页面（SPA 兜底）
    third_party    第三方专项（审计/评级，来自 DefiLlama 协议详情）
    ai_classify    LLM 正文分类补 content_topics
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import time
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent

# key -> (脚本名, 是否已实现, 说明, 额外参数)。script=None 表示尚未实现。
STAGES: dict[str, dict] = {
    "entry_refresh": {
        "script": None,
        "implemented": False,
        "desc": "刷新官网/文档/GitHub 入口（refresh_*_from_cmc/cg/dl 尚未支持 --asset-id）",
    },
    "deep": {
        "script": "phase_b2_deep_doc_discovery.py",
        "implemented": True,
        "desc": "官网/文档站深爬，发现白皮书/审计/路线图等子文档",
        "args": ["--limit", "50", "--workers", "1", "--timeout", "15"],
    },
    "spa": {
        "script": "phase_b2_spa_browser_crawl.py",
        "implemented": True,
        "desc": "Playwright 渲染 JS 页面（SPA 兜底）",
        "args": ["--limit", "20", "--concurrency", "1"],
    },
    "third_party": {
        "script": "phase_b2_third_party.py",
        "implemented": True,
        "desc": "第三方专项（审计/评级，来自 DefiLlama 协议详情）",
        "args": ["--timeout", "20"],
    },
    "ai_classify": {
        "script": "backfill_ai_classify_links.py",
        "implemented": True,
        "desc": "LLM 正文分类补 content_topics",
        "args": ["--limit", "1000", "--workers", "2", "--method", "all"],
    },
}

DEFAULT_STAGE_ORDER = ["entry_refresh", "deep", "spa", "third_party", "ai_classify"]


def _run_stage(script: str, asset_id: int, extra_args: list[str], dry_run: bool, timeout: int) -> int:
    cmd = [sys.executable, str(BIN_DIR / script), "--asset-id", str(asset_id)]
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend(extra_args)
    print(f"\n    $ {' '.join(cmd)}\n")
    try:
        proc = subprocess.run(cmd, cwd=str(BIN_DIR), timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        print(f"    [超时] {script} 超过 {timeout}s，跳过。")
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description="单个代币投研资料补齐流水线（防封号版）")
    parser.add_argument("--asset-id", type=int, required=True, help="目标资产ID")
    parser.add_argument(
        "--stages", type=str, default=",".join(DEFAULT_STAGE_ORDER),
        help="逗号分隔的阶段 key，如 deep,spa,ai_classify",
    )
    parser.add_argument("--dry-run", action="store_true", help="各阶段预览，不写库")
    parser.add_argument("--sleep-min", type=float, default=2.0, help="阶段间随机休眠下限（秒）")
    parser.add_argument("--sleep-max", type=float, default=5.0, help="阶段间随机休眠上限（秒）")
    parser.add_argument("--timeout", type=int, default=900, help="单阶段超时（秒）")
    args = parser.parse_args()

    stage_keys = [s.strip() for s in args.stages.split(",") if s.strip()]
    sleep_min = min(args.sleep_min, args.sleep_max)
    sleep_max = max(args.sleep_min, args.sleep_max)

    print(f"目标 asset_id={args.asset_id} | 阶段: {stage_keys} | dry_run={args.dry_run}")

    results: dict[str, str] = {}
    for key in stage_keys:
        cfg = STAGES.get(key)
        if cfg is None:
            print(f"\n[跳过] 未知阶段: {key}")
            results[key] = "unknown"
            continue

        print(f"\n{'=' * 60}\n  阶段: {key} — {cfg['desc']}\n{'=' * 60}")

        if not cfg["implemented"]:
            print(f"  [未实现] 跳过：{cfg['desc']}")
            results[key] = "skipped_not_implemented"
            continue

        rc = _run_stage(cfg["script"], args.asset_id, cfg.get("args", []), args.dry_run, args.timeout)
        results[key] = "ok" if rc == 0 else f"exit_{rc}"
        print(f"  阶段 {key} 结束: {'OK' if rc == 0 else f'失败(code={rc})'}")

        # 防封号：阶段之间随机休眠（最后一个阶段后不再休眠）
        if key != stage_keys[-1]:
            delay = random.uniform(sleep_min, sleep_max)
            print(f"  [节流] 休眠 {delay:.1f}s ...")
            time.sleep(delay)

    failed = any(v.startswith("exit_") for v in results.values())
    print(f"\n{'=' * 60}\n流水线完成: {results}\n{'=' * 60}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
