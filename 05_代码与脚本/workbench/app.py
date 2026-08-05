"""
加密货币投研资料采集系统 — Web 工作台
提供仪表盘、任务管理、日志查看等可视化操作界面。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from flask import Flask, render_template, jsonify, request

# Docker 环境：/app/scripts/... ；本地：05_代码与脚本/scripts/...
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
    SCRIPTS_BIN = Path("/app/scripts/bin")
    SQL_DIR = Path("/app/scripts/sql")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent  # workbench/
    CODE_ROOT = WORKSPACE_ROOT.parent  # 05_代码与脚本/
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"
    SCRIPTS_BIN = CODE_ROOT / "scripts" / "bin"
    SQL_DIR = CODE_ROOT / "scripts" / "sql"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

app = Flask(__name__)

from task_manager import TaskManager  # noqa: E402

task_mgr = TaskManager(max_concurrent=3)

# db_stats 延迟导入（启动时不立即连数据库，避免启动即崩溃）
_db_stats_module = None


def _get_db_stats():
    global _db_stats_module
    if _db_stats_module is None:
        import db_stats  # noqa: E402
        _db_stats_module = db_stats
    return _db_stats_module

# ── 任务定义 ──
TASK_DEFS = {
    "b2_deep_discovery": {
        "name": "B2 深度文档发现",
        "description": "从 doc_source_entry 深度爬取 HTML，发现嵌入的文档链接",
        "script": "phase_b2_deep_doc_discovery.py",
        "default_args": ["--limit", "1000", "--workers", "15", "--timeout", "8"],
        "category": "文档采集",
    },
    "b2_auto_loop": {
        "name": "B2 深度文档发现（自动循环）",
        "description": "持续运行 B2，直到 docs 类型全部爬完",
        "script": "phase_b2_auto_loop.py",
        "default_args": [],
        "category": "文档采集",
    },
    "b3_download": {
        "name": "B3 文档下载",
        "description": "下载 doc_asset 中的 PDF 到本地存储",
        "script": "phase_b3_doc_download.py",
        "default_args": ["--limit", "200"],
        "category": "文档采集",
    },
    "b5_health_ai": {
        "name": "B5 链接健康检查 + AI 筛选",
        "description": "检测链接健康状态，AI 评估投研相关性",
        "script": "phase_b5_link_health_ai_filter.py",
        "default_args": ["--limit", "200", "--skip-ai"],
        "category": "投研筛选",
    },
    "b6_generate": {
        "name": "B6 生成投研资料文件",
        "description": "为每个币生成投研网址链接.txt + 基础数据.md",
        "script": "phase_b6_generate_research_files.py",
        "default_args": ["--limit", "100"],
        "category": "投研筛选",
    },
    "b7_fallback": {
        "name": "B7 防屏蔽链接下载",
        "description": "针对 Cloudflare/WAF 链接的兜底下载",
        "script": "phase_b7_fallback_download.py",
        "default_args": ["--limit", "100"],
        "category": "文档采集",
    },
    "cg_coin_info": {
        "name": "CG 拉取币种详情",
        "description": "从 CoinGecko 拉取 coin_info，补充官网/文档/GitHub 链接（Demo 月配额 10k）",
        "script": "ingest_cg_coin_info.py",
        "default_args": ["--from-list-missing", "--limit", "200", "--max-calls", "5000", "--calls-per-minute", "90"],
        "category": "数据源采集",
    },
    "cg_coin_info_auto": {
        "name": "CG 拉取币种详情（自动循环）",
        "description": "自动循环拉取 coin_info，直到全部完成或月配额用完（上限 8000 次）",
        "script": "ingest_cg_coin_info_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "cg_bootstrap_assets": {
        "name": "CG 新增币种入库",
        "description": "将 CG 独有的币种补充到 core.asset（按 symbol 匹配）",
        "script": "bootstrap_cg_assets_from_list.py",
        "default_args": ["--limit", "500"],
        "category": "数据源采集",
    },
    "cg_refresh_docs": {
        "name": "CG 补充文档入口",
        "description": "从 coin_info 的 links 中提取官网/文档/GitHub，写入 doc_source_entry",
        "script": "refresh_doc_source_entries_from_cg.py",
        "default_args": ["--limit", "200"],
        "category": "数据源采集",
    },
    "github_activity": {
        "name": "GitHub 开发活跃度采集",
        "description": "从 doc_source_entry 提取 GitHub 仓库，拉取开发活跃度数据",
        "script": "collect_github_activity.py",
        "default_args": ["--limit", "50"],
        "category": "数据源采集",
    },
    "b2_ai_noise_clean": {
        "name": "B2 AI 噪声清理",
        "description": "规则直删(paperdigest等)+AI精筛(GitHub blob/tree)，RPM=300 高速版",
        "script": "phase_b2_ai_noise_clean.py",
        "default_args": ["--limit", "500", "--batch-size", "100", "--rpm", "300", "--source", "all"],
        "category": "AI 筛选",
    },
    "b2_ai_noise_clean_auto": {
        "name": "B2 AI 噪声清理（自动循环）",
        "description": "自动循环，每轮2000条，规则秒删+AI高速筛，总上限10万条",
        "script": "phase_b2_ai_noise_clean_auto.py",
        "default_args": [],
        "category": "AI 筛选",
    },
}


# ── 页面路由 ──

@app.route("/healthz")
def healthz():
    """健康检查：Zeabur 用来判断服务是否就绪"""
    return jsonify({"ok": True, "status": "alive"})

@app.route("/")
def index():
    return render_template("index.html", task_defs=TASK_DEFS)


# ── API 路由 ──


@app.route("/api/dashboard")
def api_dashboard():
    try:
        stats = _get_db_stats().get_dashboard_stats()
        return jsonify({"ok": True, "data": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pending")
def api_pending():
    try:
        data = _get_db_stats().get_pending_b2()
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    limit = int(request.args.get("limit", 20))
    tasks = task_mgr.list_tasks(limit=limit)
    return jsonify({"ok": True, "tasks": tasks})


@app.route("/api/tasks/start", methods=["POST"])
def api_start_task():
    data = request.get_json() or {}
    task_key = data.get("task_key", "")
    custom_args = data.get("args", [])

    if task_key not in TASK_DEFS:
        return jsonify({"ok": False, "error": f"未知任务: {task_key}"}), 400

    tdef = TASK_DEFS[task_key]
    args = custom_args if custom_args else tdef["default_args"]
    cmd = [sys.executable, "-u", str(SCRIPTS_BIN / tdef["script"])] + args

    task_id = task_mgr.submit_task(tdef["name"], cmd)
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/tasks/<task_id>/stop", methods=["POST"])
def api_stop_task(task_id):
    ok = task_mgr.stop_task(task_id)
    return jsonify({"ok": ok})


@app.route("/api/tasks/<task_id>")
def api_task_detail(task_id):
    task = task_mgr.get_task(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "task": task})


@app.route("/api/tasks/<task_id>/log")
def api_task_log(task_id):
    limit = int(request.args.get("limit", 200))
    logs = task_mgr.get_task_log(task_id, limit=limit)
    if logs is None:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "logs": logs})


# ── 代币搜索与资料查询 ──


@app.route("/api/assets/search")
def api_search_assets():
    q = (request.args.get("q", "") or "").strip()
    if not q or len(q) < 1:
        return jsonify({"ok": True, "assets": []})
    try:
        assets = _get_db_stats().search_assets(q, limit=20)
        return jsonify({"ok": True, "assets": assets})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/assets/<int:asset_id>/materials")
def api_asset_materials(asset_id: int):
    try:
        data = _get_db_stats().get_asset_materials(asset_id)
        if not data:
            return jsonify({"ok": False, "error": "资产不存在"}), 404
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/task_defs")
def api_task_defs():
    defs = {}
    for k, v in TASK_DEFS.items():
        defs[k] = {
            "name": v["name"],
            "description": v["description"],
            "category": v["category"],
            "default_args": v["default_args"],
        }
    return jsonify({"ok": True, "defs": defs})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
