"""
DL 一键流水线：按正确依赖顺序自动执行 DefiLlama 的 3 个步骤。

顺序：
  ① 拉取协议列表           ingest_dl_protocols.py                    → src_dl.protocol_list
  ② 资产入库               bootstrap_dl_assets_batch.py（循环）       → core.asset
  ③ 补充文档入口           refresh_doc_source_entries_from_dl_auto.py → biz.doc_source_entry
  ④ 赛道标签全量刷新       run_refresh_sectors.py                     → biz.asset_sector + core.asset.primary_sector

任一步失败即停止，方便排查。每个子任务的 stdout/stderr 都实时流式输出。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

BOOTSTRAP_BATCH = 1000      # ② 每批入库的协议数
BOOTSTRAP_MAX_ROUNDS = 100  # ② 安全上限，防止异常情况下死循环

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"
# 清除代理变量：requests 会读取 HTTP(S)_PROXY，本地 socks5 代理不可用会导致 API 请求失败
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    env.pop(_proxy_var, None)


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


def _parse_json(out: str) -> dict | None:
    """从子任务输出里找最后一个可解析的 JSON 行，避免日志行干扰。"""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def main() -> int:
    # ① 拉取协议列表（源头，无前置）
    code, _ = _run(_python("ingest_dl_protocols.py"), "① 列表")
    if code != 0:
        return code

    # ② 资产入库（依赖①，循环直到无候选）
    for round_num in range(1, BOOTSTRAP_MAX_ROUNDS + 1):
        print(f"\n>>> ② 入库 第 {round_num} 轮 (batch={BOOTSTRAP_BATCH})", flush=True)
        code, out = _run(
            _python("bootstrap_dl_assets_batch.py", "--limit", str(BOOTSTRAP_BATCH)),
            "② 入库",
        )
        if code != 0:
            return code
        data = _parse_json(out)
        if data is None:
            print("[② 入库] 无法解析输出，视为已无候选，结束入库步骤。", flush=True)
            break
        if data.get("status") == "noop" or data.get("total", 0) == 0:
            print("[② 入库] 无更多候选，入库完成。", flush=True)
            break

    # ③ 补充文档入口（依赖②，内部自动循环）
    code, _ = _run(_python("refresh_doc_source_entries_from_dl_auto.py"), "③ 文档")
    if code != 0:
        return code

    # ④ 赛道标签全量刷新（依赖②，确保新入库资产分类正确）
    code, _ = _run(_python("run_refresh_sectors.py"), "④ 赛道刷新")
    if code != 0:
        return code

    print("\n" + "=" * 70)
    print("DL 一键流水线全部完成 ✅")
    print("=" * 70, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
