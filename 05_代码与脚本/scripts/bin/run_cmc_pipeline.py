"""
CMC 一键流水线：按正确依赖顺序自动执行 CMC 的 4 个步骤。

顺序：
  ① CMC 拉取全量币种列表   ingest_cmc_map.py                     → src_cmc.cmc_asset_map
  ② CMC 拉取币种详情       ingest_cmc_info.py（循环直到无缺失）   → src_cmc.cmc_asset_info
  ③ CMC 资产全量入库       backfill_core_assets_from_cmc_auto.py  → core.asset
  ④ CMC 补充文档入口       refresh_doc_source_entries_from_cmc_auto.py → biz.doc_source_entry

任一步失败即停止，方便排查。每个子任务的 stdout/stderr 都实时流式输出。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

INFO_BATCH = 200          # ② 每批拉取的详情数，与工作台单任务默认值一致
INFO_MAX_ROUNDS = 500     # ② 安全上限，防止异常情况下死循环

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"


def _python(script: str, *args: str) -> list[str]:
    return [sys.executable, "-u", str(SCRIPT_DIR / script), *args]


def _run(cmd: list[str], label: str) -> tuple[int, str]:
    """运行一个子任务，实时流式输出其日志，返回 (退出码, 原始 stdout)。"""
    print(f"\n{'=' * 70}")
    print(f"[{label}] 执行: {' '.join(cmd)}")
    print("=" * 70, flush=True)

    proc = subprocess.Popen(
        cmd,
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None

    collected: list[str] = []
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        collected.append(line)
        print(f"[{label}] {line}", flush=True)

    proc.wait()
    code = proc.returncode
    if code != 0:
        print(f"[{label}] 退出码 {code}，流水线中止。", flush=True)
    return code, "\n".join(collected)


def main() -> int:
    # ① 全量币种列表（源头，无前置）
    code, _ = _run(_python("ingest_cmc_map.py"), "① 列表")
    if code != 0:
        return code

    # ② 币种详情（依赖①，循环直到无缺失）
    for round_num in range(1, INFO_MAX_ROUNDS + 1):
        print(f"\n>>> ② 详情 第 {round_num} 轮 (batch={INFO_BATCH})", flush=True)
        code, out = _run(
            _python("ingest_cmc_info.py", "--from-map-missing", "--limit", str(INFO_BATCH)),
            "② 详情",
        )
        if code != 0:
            return code
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            print("[② 详情] 无法解析输出，视为已无缺失，结束详情步骤。", flush=True)
            break
        if data.get("status") == "noop" or data.get("row_count", 0) == 0:
            print("[② 详情] 无更多缺失 id，详情拉取完成。", flush=True)
            break

    # ③ 资产全量入库（依赖①，内部自动循环）
    code, _ = _run(_python("backfill_core_assets_from_cmc_auto.py"), "③ 入库")
    if code != 0:
        return code

    # ④ 补充文档入口（依赖②+③，内部自动循环）
    code, _ = _run(_python("refresh_doc_source_entries_from_cmc_auto.py"), "④ 文档")
    if code != 0:
        return code

    print("\n" + "=" * 70)
    print("CMC 一键流水线全部完成 ✅")
    print("=" * 70, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
