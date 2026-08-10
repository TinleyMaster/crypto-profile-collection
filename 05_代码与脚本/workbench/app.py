"""
加密货币投研资料采集系统 — Web 工作台
提供仪表盘、任务管理、日志查看等可视化操作界面。
"""

from __future__ import annotations

import os
import sys
import subprocess
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
# 顺序 = 流水线执行顺序：B1 数据源拉取 → B1 文档入口补充 → B2/B3 文档爬取 → B4 噪声清理 → 链上 → 诊断 → 维护
TASK_DEFS = {
    # ═══ B1: 数据源拉取 ═══
    "cg_bootstrap_assets": {
        "name": "CG 新增币种入库",
        "description": "将 CG 独有的币种补充到 core.asset（按 symbol 匹配），应先于拉取详情执行",
        "script": "bootstrap_cg_assets_from_list.py",
        "default_args": ["--limit", "500"],
        "category": "数据源采集",
    },
    "cg_coin_info": {
        "name": "CG 拉取币种详情",
        "description": "从 CoinGecko 拉取 coin_info，补充官网/文档/GitHub 链接（Demo 月配额 10k）",
        "script": "ingest_cg_coin_info.py",
        "default_args": ["--from-list-missing", "--limit", "200", "--max-calls", "5000", "--calls-per-minute", "60"],
        "category": "数据源采集",
        "hidden": True,
    },
    "cg_coin_info_auto": {
        "name": "CG 拉取币种详情（自动循环）",
        "description": "自动循环拉取 coin_info，直到全部完成或月配额用完（上限 8000 次）",
        "script": "ingest_cg_coin_info_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "cmc_ingest_info": {
        "name": "CMC 拉取币种详情",
        "description": "从 CoinMarketCap 拉取 asset_info（urls/描述/标签等），写入 src_cmc.cmc_asset_info",
        "script": "ingest_cmc_info.py",
        "default_args": ["--from-map-missing", "--limit", "200"],
        "category": "数据源采集",
    },
    "dl_ingest_protocols": {
        "name": "DL 拉取协议列表",
        "description": "从 DefiLlama 拉取全量协议列表，写入 src_dl.protocol_list",
        "script": "ingest_dl_protocols.py",
        "default_args": [],
        "category": "数据源采集",
    },

    # ═══ B1: 文档入口补充 ═══
    "cg_refresh_docs": {
        "name": "CG 补充文档入口",
        "description": "从 coin_info 的 links 中提取官网/文档/GitHub，写入 doc_source_entry",
        "script": "refresh_doc_source_entries_from_cg.py",
        "default_args": ["--limit", "200"],
        "category": "数据源采集",
        "hidden": True,
    },
    "cg_refresh_docs_auto": {
        "name": "CG 补充文档入口（自动循环）",
        "description": "自动循环，每批200资产，直到全部处理完",
        "script": "refresh_doc_source_entries_from_cg_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "cmc_refresh_docs": {
        "name": "CMC 补充文档入口",
        "description": "从 cmc_asset_info 的 urls 中提取官网/文档/GitHub/Twitter/Telegram 等，写入 doc_source_entry",
        "script": "refresh_doc_source_entries_from_cmc.py",
        "default_args": ["--limit", "200"],
        "category": "数据源采集",
        "hidden": True,
    },
    "cmc_refresh_docs_auto": {
        "name": "CMC 补充文档入口（自动循环）",
        "description": "自动循环，每批200资产，直到全部处理完",
        "script": "refresh_doc_source_entries_from_cmc_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "dl_refresh_docs": {
        "name": "DL 补充文档入口",
        "description": "从 DefiLlama protocol_list 的 url/twitter 提取官网链接，写入 doc_source_entry",
        "script": "refresh_doc_source_entries_from_dl.py",
        "default_args": ["--limit", "200"],
        "category": "数据源采集",
        "hidden": True,
    },
    "dl_refresh_docs_auto": {
        "name": "DL 补充文档入口（自动循环）",
        "description": "自动循环，每批200资产，直到全部处理完",
        "script": "refresh_doc_source_entries_from_dl_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "dexscreener_supplement": {
        "name": "DexScreener 补充文档入口",
        "description": "对无文档入口的资产，通过 DexScreener API 搜索官网/社交链接",
        "script": "supplement_doc_entries_from_dexscreener.py",
        "default_args": ["--limit", "50"],
        "category": "数据源采集",
        "hidden": True,
    },
    "dexscreener_supplement_auto": {
        "name": "DexScreener 补充文档入口（自动循环）",
        "description": "自动循环，每批50资产，通过 DexScreener 补充无文档入口资产",
        "script": "supplement_doc_entries_from_dexscreener_auto.py",
        "default_args": [],
        "category": "数据源采集",
        "hidden": True,
    },
    "binance_supplement": {
        "name": "Binance 补充文档入口",
        "description": "对无文档入口的资产，通过 Binance Web3 API 搜索官网/社交链接",
        "script": "supplement_doc_entries_from_binance.py",
        "default_args": ["--limit", "50"],
        "category": "数据源采集",
        "hidden": True,
    },
    "binance_supplement_auto": {
        "name": "Binance 补充文档入口（自动循环）",
        "description": "自动循环，每批50资产，通过 Binance Web3 搜索补充无文档入口资产",
        "script": "supplement_doc_entries_from_binance_auto.py",
        "default_args": [],
        "category": "数据源采集",
        "hidden": True,
    },
    "dual_supplement": {
        "name": "双源补充文档入口",
        "description": "对无文档入口的资产，同时搜索 DexScreener + Binance 补充官网/社交链接",
        "script": "supplement_doc_entries_dual.py",
        "default_args": ["--limit", "50"],
        "category": "数据源采集",
        "hidden": True,
    },
    "dual_supplement_auto": {
        "name": "双源补充文档入口（自动循环）",
        "description": "自动循环，每批50资产，双源(DexScreener+Binance)补充无文档入口资产",
        "script": "supplement_doc_entries_dual_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },

    # ═══ B2/B3: 文档爬取 ═══
    "b2_auto_loop": {
        "name": "B2 深度文档发现（自动循环）",
        "description": "从官网 HTML 抓取嵌入的 PDF/白皮书链接，含 SPA 检测",
        "script": "phase_b2_auto_loop.py",
        "default_args": [],
        "category": "文档采集",
    },
    "spa_browser_crawl": {
        "name": "B3 SPA 无头浏览器爬取",
        "description": "单次：用 Playwright 渲染 JS 页面，提取 B2 静态爬取无法处理的 SPA 网站文档链接",
        "script": "phase_b2_spa_browser_crawl.py",
        "default_args": ["--limit", "20", "--concurrency", "4"],
        "category": "数据源采集",
        "hidden": True,
    },
    "spa_browser_crawl_auto": {
        "name": "B3 SPA 无头浏览器爬取（自动循环）",
        "description": "自动循环，Playwright 渲染 JS 页面，批量处理 needs_browser=TRUE 的 SPA 网站",
        "script": "phase_b2_spa_browser_crawl_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "github_activity": {
        "name": "GitHub 开发活跃度采集",
        "description": "从 doc_source_entry 提取 GitHub 仓库，拉取开发活跃度数据",
        "script": "collect_github_activity.py",
        "default_args": ["--limit", "50"],
        "category": "数据源采集",
        "hidden": True,
    },

    # ═══ B4: AI 噪声清理 ═══
    "b2_ai_noise_clean_auto": {
        "name": "B4 AI 噪声清理（自动循环）",
        "description": "自动循环，每轮2000条，规则秒删+AI高速筛，总上限10万条",
        "script": "phase_b2_ai_noise_clean_auto.py",
        "default_args": [],
        "category": "AI 筛选",
        "hidden": True,
    },
    "b2_ai_noise_clean_by_asset": {
        "name": "B4 AI 噪声清理（按资产分组）",
        "description": "按资产聚合域名，AI 一次判断该资产所有域名是否噪声",
        "script": "phase_b2_ai_noise_clean_by_asset.py",
        "default_args": ["--execute"],
        "category": "AI 筛选",
        "hidden": True,
    },
    "b2_ai_noise_clean_by_asset_auto": {
        "name": "B4 AI 噪声清理（按资产·自动循环）",
        "description": "智能聚合：每轮20个资产，AI 按域名粒度批量判断，效率远超逐条模式",
        "script": "phase_b2_ai_noise_clean_by_asset_auto.py",
        "default_args": [],
        "category": "AI 筛选",
    },

    # ═══ C: 投研分析 ═══
    "c_extract_tokenomics": {
        "name": "C 代币经济学提取",
        "description": "多源聚合（官网/白皮书/Docs + API）→ LLM 提取结构化 tokenomics 数据",
        "script": "phase_c_extract_tokenomics.py",
        "default_args": [],
        "category": "投研分析",
        "requires_asset_id": True,
        "arg_label": "asset_id",
        "hidden": True,  # 主任务面板不显示，通过投研分析面板调用
    },

    # ═══ 链上数据 ═══
    "chain_holder_snapshot": {
        "name": "链上持仓快照采集",
        "description": "从 Etherscan/BSCScan 拉取代币 Top 持有者，计算持仓集中度",
        "script": "phase_chain_holder_snapshot.py",
        "default_args": ["--limit", "50"],
        "category": "链上数据",
        "hidden": True,
    },
    "chain_holder_snapshot_auto": {
        "name": "链上持仓快照采集（每日单次）",
        "description": "每天运行一次，拉取全部有合约地址资产的 Top 持有者数据",
        "script": "phase_chain_holder_snapshot_auto.py",
        "default_args": [],
        "category": "链上数据",
    },
    "chain_transfer_monitor": {
        "name": "大额转账监控",
        "description": "从 Etherscan/BSCScan 拉取大额转账，标记转入交易所的潜在砸盘信号",
        "script": "phase_chain_transfer_monitor.py",
        "default_args": ["--limit", "50"],
        "category": "链上数据",
        "hidden": True,
    },
    "chain_transfer_monitor_auto": {
        "name": "大额转账监控（告警模式·自动循环）",
        "description": "后台轮询增量大额转账，只标记转入交易所的告警，不存所有明细",
        "script": "phase_chain_transfer_monitor_auto.py",
        "default_args": [],
        "category": "链上数据",
        "hidden": True,
    },

    # ═══ 诊断 ═══
    "diag_noise": {
        "name": "噪声诊断报告",
        "description": "查看今日新增文档链接的噪声情况（域名分布、噪声占比、采样）",
        "script": "diag_noise_report.py",
        "default_args": [],
        "category": "诊断",
    },
    "diag_pipeline": {
        "name": "数据链路诊断",
        "description": "全链路检查：数据源→doc_source_entry→deep_crawl→AI检查，各环节健康度",
        "script": "diag_data_pipeline.py",
        "default_args": [],
        "category": "诊断",
    },
    "diag_high_entry": {
        "name": "高条目资产污染溯源",
        "description": "分析文档链接>500的代币，定位污染链路（种子→域名→噪声量）",
        "script": "diag_high_entry_assets.py",
        "default_args": [],
        "category": "诊断",
    },

    # ═══ 维护 ═══
    "cleanup_pollution": {
        "name": "清理 GitHub 跨仓库污染",
        "description": "删除 github.com deep_crawl 条目并重置爬取状态，修复 asset_id 大规模污染",
        "script": "diag_cleanup_pollution.py",
        "default_args": [],
        "category": "维护",
        "hidden": True,
    },
    "reset_high_entry": {
        "name": "重置高条目资产（>500条）",
        "description": "删除 >500条 deep_crawl 的资产的全部 deep_crawl 链接，重置 deep_crawled_at",
        "script": "reset_high_entry_assets.py",
        "default_args": ["--execute"],
        "category": "维护",
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
    # 投研分析类任务（requires_asset_id）允许通过面板调用，不受 hidden 限制
    if tdef.get("hidden") and not tdef.get("requires_asset_id"):
        return jsonify({"ok": False, "error": "该任务已隐藏"}), 400
    args = custom_args if custom_args else tdef["default_args"]
    # 如果任务需要 asset_id 参数，从请求中获取
    if tdef.get("requires_asset_id"):
        asset_id = data.get("asset_id")
        if not asset_id:
            return jsonify({"ok": False, "error": "缺少 asset_id 参数"}), 400
        arg_label = tdef.get("arg_label", "asset_id")
        args = [f"--{arg_label}", str(asset_id)] + args
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


@app.route("/api/assets/<int:asset_id>/tokenomics")
def api_asset_tokenomics(asset_id: int):
    try:
        data = _get_db_stats().get_asset_tokenomics(asset_id)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/assets/<int:asset_id>/reset-deep-crawl", methods=["POST"])
def api_reset_deep_crawl(asset_id: int):
    """重置 deep_crawled_at，允许 B2 重新爬取该资产。"""
    try:
        result = _get_db_stats().reset_deep_crawl(asset_id)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/assets/<int:asset_id>/add_entry", methods=["POST"])
def api_add_manual_entry(asset_id: int):
    """手动为资产添加官网链接。"""
    try:
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or data.get("entry_url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "缺少 url 参数"}), 400
        if not url.startswith("http"):
            return jsonify({"ok": False, "error": "URL 必须以 http 开头"}), 400
        result = _get_db_stats().add_manual_entry(asset_id, url)
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/task_defs")
def api_task_defs():
    defs = {}
    for k, v in TASK_DEFS.items():
        if v.get("hidden"):
            continue
        defs[k] = {
            "name": v["name"],
            "description": v["description"],
            "category": v["category"],
            "default_args": v["default_args"],
        }
    return jsonify({"ok": True, "defs": defs})


@app.route("/api/task_progress")
def api_task_progress():
    try:
        data = _get_db_stats().get_task_progress()
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── DexScreener 辅助添加 ──


@app.route("/api/dexscreener/search")
def api_dexscreener_search():
    """搜索 DexScreener 获取代币信息。"""
    q = (request.args.get("q", "") or "").strip()
    if not q or len(q) < 1:
        return jsonify({"ok": True, "tokens": []})
    try:
        tokens = _get_db_stats().search_dexscreener(q)
        return jsonify({"ok": True, "tokens": tokens})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/assets/create", methods=["POST"])
def api_create_asset():
    """从 DexScreener 数据或手动输入创建资产。"""
    try:
        data = request.get_json(silent=True) or {}
        symbol = (data.get("symbol") or "").strip()
        name = (data.get("name") or "").strip()
        asset_type = (data.get("asset_type") or "token").strip()
        links = data.get("links") or []

        if not symbol:
            return jsonify({"ok": False, "error": "缺少 symbol 参数"}), 400
        if not name:
            return jsonify({"ok": False, "error": "缺少 name 参数"}), 400

        result = _get_db_stats().create_asset_with_links(
            symbol=symbol,
            name=name,
            asset_type=asset_type,
            links=links,
        )
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/assets/<int:asset_id>/deep_crawl", methods=["POST"])
def api_trigger_deep_crawl(asset_id: int):
    """立即触发指定资产的 B2 深度文档爬取。"""
    try:
        script = str(SCRIPTS_BIN / "phase_b2_deep_doc_discovery.py")
        cmd = [
            sys.executable, "-u", script,
            "--asset-id", str(asset_id),
            "--limit", "100",
            "--workers", "10",
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPTS_BIN),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({
            "ok": True,
            "data": {
                "asset_id": asset_id,
                "pid": proc.pid,
                "message": "B2 深度爬取已触发，正在后台运行",
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── NotebookLM 投研精选 ──


@app.route("/api/notebooklm/links/<int:asset_id>")
def api_notebooklm_links(asset_id: int):
    """获取已缓存的 NotebookLM 精选链接。"""
    try:
        data = _get_db_stats().get_notebooklm_links(asset_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/notebooklm/curate/<int:asset_id>", methods=["POST"])
def api_notebooklm_curate(asset_id: int):
    """触发 NotebookLM 精选生成（配额粗筛 + AI 排序）。"""
    try:
        force = request.args.get("force", "0") == "1"
        data = _get_db_stats().curate_notebooklm(asset_id, force=force)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 链上数据监控 ──


@app.route("/api/onchain/holder/<int:asset_id>")
def api_onchain_holder(asset_id: int):
    """获取指定资产的最新持仓快照。"""
    try:
        data = _get_db_stats().get_onchain_holder_snapshot(asset_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/onchain/transfers")
def api_onchain_transfers():
    """获取大额转账记录。"""
    try:
        asset_id = request.args.get("asset_id", type=int)
        is_to_exchange = request.args.get("to_exchange")
        if is_to_exchange is not None:
            is_to_exchange = is_to_exchange.lower() in ("true", "1", "yes")
        limit = min(int(request.args.get("limit", 50)), 200)
        data = _get_db_stats().get_onchain_transfers(
            asset_id=asset_id,
            is_to_exchange=is_to_exchange,
            limit=limit,
        )
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/onchain/alerts")
def api_onchain_alerts():
    """获取链上告警摘要：24h 转入交易所大额转账。"""
    try:
        data = _get_db_stats().get_onchain_alert_summary()
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/onchain/query/<int:asset_id>")
def api_onchain_query(asset_id: int):
    """按需查询指定资产的链上数据（持仓 + 大额转账）。先查缓存，未命中则实时拉取。"""
    try:
        force = request.args.get("force", "0") == "1"
        data = _get_db_stats().query_onchain_data(asset_id, force=force)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/hot")
def api_market_hot():
    """每日投研推荐：多源交叉验证（Binance + CMC），按综合评分排序。"""
    try:
        from cross_market import get_cross_validated
        limit = int(request.args.get("limit", "30"))
        result = get_cross_validated(limit)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/gainers")
def api_market_gainers():
    """24h 涨幅榜：多源交叉验证。"""
    try:
        from cross_market import get_consensus_gainers
        limit = int(request.args.get("limit", "30"))
        result = get_consensus_gainers(limit)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/volume")
def api_market_volume():
    """24h 交易量榜：多源交叉验证。"""
    try:
        from cross_market import get_consensus_volume
        limit = int(request.args.get("limit", "30"))
        result = get_consensus_volume(limit)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
