"""
加密货币投研资料采集系统 — Web 工作台
提供仪表盘、任务管理、日志查看等可视化操作界面。
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import fcntl
import subprocess
import threading
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_from_directory

# Docker 环境：/app/scripts/... ；本地：05_代码与脚本/scripts/...
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
    SCRIPTS_BIN = Path("/app/scripts/bin")
    SQL_DIR = Path("/app/scripts/sql")
    TOKENOMICS_IMAGES_ROOT = Path("/app/data/tokenomics_images")
    DOCS_STORAGE_ROOT = Path(os.getenv("DOCS_STORAGE_ROOT", "/app/docs_storage"))
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent  # workbench/
    CODE_ROOT = WORKSPACE_ROOT.parent  # 05_代码与脚本/
    PROJECT_ROOT = CODE_ROOT.parent  # 项目根目录
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"
    SCRIPTS_BIN = CODE_ROOT / "scripts" / "bin"
    SQL_DIR = CODE_ROOT / "scripts" / "sql"
    TOKENOMICS_IMAGES_ROOT = CODE_ROOT / "data" / "tokenomics_images"
    DOCS_STORAGE_ROOT = Path(os.getenv("DOCS_STORAGE_ROOT", str(PROJECT_ROOT / "docs_storage")))

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

app = Flask(__name__)

from task_manager import TaskManager, _get_db  # noqa: E402
import psycopg.rows  # noqa: E402

task_mgr = TaskManager(max_concurrent=3)

# KOL 监控模块（可选，导入失败不影响主服务）
try:
    from kol.routes import kol_bp  # noqa: E402
    app.register_blueprint(kol_bp)
    _kol_loaded = True
except Exception as _e:
    _kol_loaded = False
    print(f"[WARN] KOL 模块加载失败，功能将不可用: {_e}")

# 解锁数据异步拉取状态（key: f"{asset_id}:{force}"）。
# 注意：gunicorn 多 worker 部署下内存 dict 不共享，需文件持久化（见下方 UNLOCK_STATE_FILE），
# 否则 POST 落在 A worker、status 轮询落在 B worker 时状态会丢失（表现为“拉取超时”）。


def _unlock_async_key(asset_id: int, force: bool) -> str:
    return f"{asset_id}:{1 if force else 0}"


# 重新爬取异步任务状态（key: str(asset_id)）
# 注意：gunicorn 多 worker 部署下内存 dict 不共享，需文件持久化，
# 否则 POST 落在 A worker、status 轮询落在 B worker 时状态会丢失（表现为“爬取失败”）。
RECRAWL_STATE_DIR = (
    Path("/app/task_state")
    if os.path.exists("/app/scripts/bin")
    else Path(__file__).resolve().parent / "task_state"
)
RECRAWL_STATE_DIR.mkdir(parents=True, exist_ok=True)
RECRAWL_STATE_FILE = RECRAWL_STATE_DIR / "recrawl_state.json"
RECRAWL_LOCK_FILE = RECRAWL_STATE_DIR / "recrawl_state.lock"

# /api/scheduler/feed 全量查询限流（每 5 秒最多 1 次全量查询）
# 文件持久化，兼容 gunicorn 多 worker
_FEED_FULL_TS_FILE = RECRAWL_STATE_DIR / "feed_full_ts.json"
_FEED_FULL_TS_LOCK = RECRAWL_STATE_DIR / "feed_full_ts.lock"


def _load_feed_full_ts() -> float:
    if not _FEED_FULL_TS_FILE.exists():
        return 0.0
    try:
        with open(_FEED_FULL_TS_FILE, "r", encoding="utf-8") as f:
            return float(json.load(f).get("ts", 0.0))
    except (json.JSONDecodeError, IOError, OSError, ValueError):
        return 0.0


def _save_feed_full_ts(ts: float) -> None:
    tmp = _FEED_FULL_TS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": ts}, f)
    os.replace(tmp, _FEED_FULL_TS_FILE)


class _RecrawlFileLock:
    """基于 fcntl 的跨进程文件锁（与 task_manager 一致）。"""

    def __init__(self, path: Path):
        self.path = path
        self._fd = None

    def __enter__(self):
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _load_recrawl_state() -> dict:
    if not RECRAWL_STATE_FILE.exists():
        return {}
    try:
        with open(RECRAWL_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _save_recrawl_state(state: dict) -> None:
    tmp = RECRAWL_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, RECRAWL_STATE_FILE)

# 解锁数据异步状态文件（与 recrawl 同理，gunicorn 多 worker 下跨进程共享）
UNLOCK_STATE_FILE = RECRAWL_STATE_DIR / "unlock_state.json"
UNLOCK_LOCK_FILE = RECRAWL_STATE_DIR / "unlock_state.lock"


def _load_unlock_state() -> dict:
    if not UNLOCK_STATE_FILE.exists():
        return {}
    try:
        with open(UNLOCK_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _save_unlock_state(state: dict) -> None:
    tmp = UNLOCK_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, UNLOCK_STATE_FILE)

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
    "cg_pipeline": {
        "name": "CG 一键流水线",
        "description": "按依赖顺序自动执行：①拉全量列表 → ②拉币种详情(循环) → ③新增币种入库(循环) → ④补充文档入口，失败即停",
        "script": "run_cg_pipeline.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "cg_bootstrap_assets": {
        "name": "CG 新增币种入库",
        "description": "将 CG 独有的币种补充到 core.asset（按 symbol 匹配），应先于拉取详情执行",
        "script": "bootstrap_cg_assets_from_list.py",
        "default_args": ["--limit", "500"],
        "category": "数据源采集",
        "hidden": True,
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
        "hidden": True,
    },
    "cmc_pipeline": {
        "name": "CMC 一键流水线",
        "description": "按依赖顺序自动执行：①拉全量列表 → ②拉币种详情(循环) → ③资产全量入库 → ④补充文档入口，失败即停",
        "script": "run_cmc_pipeline.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "cmc_ingest_map": {
        "name": "CMC 拉取全量币种列表",
        "description": "从 CMC API 拉取全量币种列表（listing/map），写入 src_cmc.cmc_asset_map。这是所有 CMC 后续步骤的前置步骤",
        "script": "ingest_cmc_map.py",
        "default_args": [],
        "category": "数据源采集",
        "hidden": True,
    },
    "cmc_ingest_info": {
        "name": "CMC 拉取币种详情",
        "description": "从 CoinMarketCap 拉取 asset_info（urls/描述/标签等），写入 src_cmc.cmc_asset_info",
        "script": "ingest_cmc_info.py",
        "default_args": ["--from-map-missing", "--limit", "200"],
        "category": "数据源采集",
        "hidden": True,
    },
    "dl_pipeline": {
        "name": "DL 一键流水线",
        "description": "按依赖顺序自动执行：①拉协议列表 → ②资产入库(循环) → ③补充文档入口，失败即停",
        "script": "run_dl_pipeline.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "dl_ingest_protocols": {
        "name": "DL 拉取协议列表",
        "description": "从 DefiLlama 拉取全量协议列表，写入 src_dl.protocol_list",
        "script": "ingest_dl_protocols.py",
        "default_args": [],
        "category": "数据源采集",
        "hidden": True,
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
        "hidden": True,
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
        "hidden": True,
    },
    "cmc_backfill_assets": {
        "name": "CMC 资产全量入库",
        "description": "从 src_cmc.cmc_asset_map 单次批量写入 core.asset（未映射的资产）",
        "script": "refresh_core_assets_from_cmc.py",
        "default_args": ["--limit", "500"],
        "category": "数据源采集",
        "hidden": True,
    },
    "cmc_backfill_historical": {
        "name": "CMC 历史行情回填",
        "description": "从 CMC 历史行情 API 回填日级行情数据到 biz.asset_market_daily，解决 market-history 仅 3 天问题。默认回填 top 500 币种最近 90 天",
        "script": "ingest_cmc_historical_quotes.py",
        "default_args": ["--days", "90", "--top", "500"],
        "category": "数据源采集",
    },
    "cmc_backfill_assets_auto": {
        "name": "CMC 资产全量入库（自动循环）",
        "description": "自动循环，每批500资产，直到所有 CMC 资产都写入 core.asset",
        "script": "backfill_core_assets_from_cmc_auto.py",
        "default_args": [],
        "category": "数据源采集",
        "hidden": True,
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
        "hidden": True,
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

    # ═══ 第三方专项（DefiLlama 评级/审计 + TGE/融资 + 链上异常）═══
    "third_party_auto": {
        "name": "第三方评级/审计回填（自动循环）",
        "description": "拉取 DefiLlama 协议详情，提取审计链接与评级页写入 doc_source_entry，直到全部处理完（rating 缺失标记断点续跑）",
        "script": "phase_b2_third_party_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "third_party_hacks": {
        "name": "链上异常事件采集（hacks）",
        "description": "拉取 DefiLlama /hacks 全量异常事件，按 defillamaId 映射资产，写入 biz.asset_hacks（结构化表）",
        "script": "phase_b2_third_party_hacks.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "third_party_raises_auto": {
        "name": "TGE/融资轮次采集（自动循环）",
        "description": "拉取 DefiLlama 协议详情 raises 字段，写入 biz.asset_raises（结构化表），直到全部处理完（dl_protocol_checked 标记断点续跑）",
        "script": "phase_b2_third_party_raises_auto.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "third_party": {
        "name": "第三方评级/审计回填（单批）",
        "description": "单批回填 DefiLlama 评级页与审计链接",
        "script": "phase_b2_third_party.py",
        "default_args": ["--limit", "200"],
        "category": "数据源采集",
        "hidden": True,
    },
    "third_party_raises": {
        "name": "TGE/融资轮次采集（单批）",
        "description": "单批回填 TGE/融资轮次",
        "script": "phase_b2_third_party_raises.py",
        "default_args": ["--limit", "200"],
        "category": "数据源采集",
        "hidden": True,
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
        "arg_label": "asset-id",
        "force_arg": "--force",  # 单币调用始终强制覆盖
        "hidden": True,  # 主任务面板不显示，通过投研分析面板调用
    },
    # ═══ 链上数据 ═══
    "chain_holder_snapshot": {
        "name": "链上持仓快照采集",
        "description": "从 Etherscan/BSCScan 拉取代币 Top 持有者，计算持仓集中度",
        "script": "phase_chain_holder_batch.py",
        "default_args": ["--all-chains", "--limit", "50"],
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
    "cmc_backfill_missing_urls": {
        "name": "CMC 补缺缺失链接",
        "description": "回填 CMC urls 中存在但 doc_source_entry 缺失的链接（如官网/文档，曾被 B2 覆盖 provenance 后误删）。每批200资产自动循环，直到补完，可与 AI 分类错峰执行避免争锁",
        "script": "backfill_doc_source_entries_from_cmc.py",
        "default_args": ["--batch-size", "200", "--max-batches", "100"],
        "category": "维护",
    },
    "backfill_ai_classify_official": {
        "name": "官网内容主题 AI 补分类",
        "description": "对 official_website 入口抓正文，用 LLM 补 content_topics（白皮书/文档/代币经济学/审计等），供投研资料完整性清单使用。已做 PG 重启自动重连，可断点续跑",
        "script": "backfill_ai_classify_links.py",
        "default_args": ["--entry-types", "official_website"],
        "category": "维护",
    },
    "backfill_ai_classify_retry_failed": {
        "name": "官网补分类失败重试",
        "description": "重跑官网内容主题 AI 补分类的失败项（classify_method='ai_failed' 的 official_website），多为临时网络/限速/LLM 抖动，可安全重试",
        "script": "backfill_ai_classify_links.py",
        "default_args": ["--method", "ai_failed", "--entry-types", "official_website"],
        "category": "维护",
    },
    "relabel_entry_types": {
        "name": "链接类型批量重标",
        "description": "按标准分类器（taxonomy/classify_link）重标 entry_type：修复 github.com 漏标、回滚 github.io 误标、把 whitepaper/litepaper URL 标为 whitepaper_page",
        "script": "backfill_classify_links.py",
        "default_args": ["--relabel-entry-types", "--upgrade-whitepaper"],
        "category": "维护",
        "hidden": True,
    },
    "cleanup_thesis_duplicates": {
        "name": "清理 thesis 重复行",
        "description": "删除 biz.research_thesis 中按 (asset_id, source_notebook_id) 重复的较旧记录，并补唯一约束",
        "script": "cleanup_research_thesis_duplicates.py",
        "default_args": [],
        "category": "维护",
        "hidden": True,
    },
    "ingest_dl_tvl_daily": {
        "name": "TVL 每日聚合",
        "description": "将 src_dl.protocol_list 的最新 TVL 快照聚合到 biz.protocol_metric_daily（幂等写入）",
        "script": "ingest_dl_tvl_daily.py",
        "default_args": [],
        "category": "数据源采集",
    },
    "kol_backtest_batch": {
        "name": "KOL 信号回测",
        "description": "对未回测的 KOL prediction 信号用日频行情做简化回测（默认止损10%/止盈20%/持仓30天），结果写回 kol_signal.backtest_* 并更新博主胜率",
        "script": "kol_backtest_batch.py",
        "default_args": [],
        "category": "KOL",
    },
    "social_heat_batch": {
        "name": "社交热度批量采集",
        "description": "按市值降序批量采集社交热度（跳过稳定币），每日 500 币",
        "script": "phase_c_social_heat_batch.py",
        "default_args": ["--limit", "500", "--delay", "0.5", "--timeout", "60"],
        "category": "投研数据提取",
    },
    "rescan_low_conf_no_content": {
        "name": "历史无正文回扫",
        "description": "回扫低置信度 ai_content（conf≤0.6）链接，重新抓正文确认：抓不到则标 needs_browser 交 SPA 重抓，抓到则保留",
        "script": "rescan_low_conf_no_content.py",
        "default_args": [],
        "category": "维护",
    },
    "probe_no_content_dryrun": {
        "name": "无正文链接甄别（预览）",
        "description": "探测无正文链接的 HTTP 状态，区分死链/反爬/JS渲染/可恢复，仅预览不删除",
        "script": "probe_no_content_links.py",
        "default_args": ["--dry-run"],
        "category": "维护",
    },
    "probe_no_content_execute": {
        "name": "无正文链接甄别（删死链）",
        "description": "探测无正文链接并删除真死链（404/域名失效），保留反爬/JS渲染链接供重抓",
        "script": "probe_no_content_links.py",
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


@app.route("/kol")
def kol_monitor():
    """KOL 信号监控面板。"""
    if not _kol_loaded:
        return "KOL 模块未加载（启动时导入失败），请检查日志。", 503
    return render_template("kol.html")


# ── API 路由 ──


@app.route("/api/dashboard")
def api_dashboard():
    try:
        stats = _get_db_stats().get_dashboard_stats()
        return jsonify({"ok": True, "data": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/coverage-by-tier")
def api_coverage_by_tier():
    """按市值分层统计各维度数据覆盖率。"""
    try:
        result = _get_db_stats().get_coverage_by_tier()
        return jsonify(result)
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
        # 单币调用默认强制覆盖已有数据
        force_arg = tdef.get("force_arg")
        if force_arg and force_arg not in args:
            args.append(force_arg)
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


@app.route("/api/tasks/<task_id>/result")
def api_task_result(task_id):
    """读取函数式后台任务的最终结果（submit_func_task 存入）。"""
    task = task_mgr.get_task(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    if task.get("status") in ("pending", "running"):
        return jsonify({"ok": True, "pending": True})
    result = task_mgr.get_task_result(task_id)
    if task.get("status") == "failed":
        return jsonify({"ok": False, "pending": False, "error": task.get("error") or "任务失败"})
    return jsonify({"ok": True, "pending": False, "result": result})


@app.route("/api/scheduler/feed")
def api_scheduler_feed():
    """聚合 sys.task_log 中最近的任务日志，用于前端实时日志侧栏。

    查询参数：
      - limit: int，默认 200，最大 500
      - since_ts: float，可选，epoch 秒，只返回大于该时间的新日志（增量拉取）
    """
    global _feed_full_last_ts

    # 解析 limit
    try:
        limit = int(request.args.get("limit", 200))
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 500))

    # 解析 since_ts
    since_ts = request.args.get("since_ts")
    since_ts_val = None
    if since_ts is not None:
        try:
            since_ts_val = float(since_ts)
        except (ValueError, TypeError):
            since_ts_val = None

    # 全量查询（无 since_ts）限流：每 5 秒最多 1 次，文件锁跨 worker 共享
    if since_ts_val is None:
        now = time.time()
        with _RecrawlFileLock(_FEED_FULL_TS_LOCK):
            last_ts = _load_feed_full_ts()
            if now - last_ts < 5.0:
                return jsonify({
                    "ok": False,
                    "error": "rate limited: 全量查询每 5 秒最多 1 次",
                    "retry_after": round(5.0 - (now - last_ts), 1),
                }), 429
            _save_feed_full_ts(now)

    try:
        with _get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                if since_ts_val is not None:
                    cur.execute(
                        """
                        SELECT tl.task_id,
                               t.name AS task_name,
                               t.status,
                               tl.line_no,
                               tl.content,
                               EXTRACT(EPOCH FROM tl.created_at) AS created_at
                        FROM sys.task_log tl
                        JOIN sys.task t USING (task_id)
                        WHERE tl.created_at >= to_timestamp(%s)
                        ORDER BY tl.created_at DESC
                        LIMIT %s
                        """,
                        (since_ts_val, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT tl.task_id,
                               t.name AS task_name,
                               t.status,
                               tl.line_no,
                               tl.content,
                               EXTRACT(EPOCH FROM tl.created_at) AS created_at
                        FROM sys.task_log tl
                        JOIN sys.task t USING (task_id)
                        ORDER BY tl.created_at DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                rows = cur.fetchall()

        logs = []
        latest_ts = since_ts_val or 0.0
        for row in rows:
            ts = float(row["created_at"])
            logs.append({
                "task_id": row["task_id"],
                "task_name": row["task_name"],
                "status": row["status"],
                "line_no": row["line_no"],
                "content": row["content"],
                "created_at": ts,
            })
            if ts > latest_ts:
                latest_ts = ts

        return jsonify({
            "ok": True,
            "logs": logs,
            "latest_ts": latest_ts,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 代币搜索与资料查询 ──


@app.route("/api/assets/search")
def api_search_assets():
    q = (request.args.get("q", "") or "").strip()
    tier = request.args.get("tier") or None
    if not q or len(q) < 1:
        return jsonify({"ok": True, "assets": []})
    try:
        try:
            limit = int(request.args.get("limit", 20))
        except (ValueError, TypeError):
            limit = 20
        limit = max(1, min(limit, 200))
        assets = _get_db_stats().search_assets(q, limit=limit, tier=tier)
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


@app.route("/api/assets/<int:asset_id>/whitepaper-summary")
def api_asset_whitepaper_summary(asset_id: int):
    """获取资产的白皮书结构化摘要。"""
    try:
        data = _get_db_stats().get_whitepaper_summary(asset_id)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/whitepaper/extract/<int:asset_id>", methods=["POST"])
def api_whitepaper_extract(asset_id: int):
    """按需提取白皮书摘要。"""
    force = (request.get_json(silent=True) or {}).get("force") == 1

    def _worker(log):
        import subprocess
        cmd = [
            sys.executable,
            str(SCRIPTS_BIN / "extract_whitepaper_summary.py"),
            "--asset_id", str(asset_id),
        ]
        if force:
            cmd.append("--force")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr[-500:]}
        return {"ok": True, "data": _get_db_stats().get_whitepaper_summary(asset_id)}

    task_id = task_mgr.submit_func_task(f"白皮书摘要: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/tokenomics/query/<int:asset_id>", methods=["POST"])
def api_tokenomics_query(asset_id: int):
    """按需提取代币经济学数据（tokenomics.com 优先，未命中返回 needs_url 供前端弹框）。"""
    force = (request.get_json(silent=True) or {}).get("force") == 1

    def _worker(log):
        return _get_db_stats().query_tokenomics(asset_id, force=force, log=log)

    task_id = task_mgr.submit_func_task(f"代币经济学: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/tokenomics/scrape-url/<int:asset_id>", methods=["POST"])
def api_tokenomics_scrape_url(asset_id: int):
    """按用户提供的网址抓取代币经济学数据（LLM 提取）。"""
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "缺少网址"}), 400

    def _worker(log):
        return _get_db_stats().query_tokenomics_by_url(asset_id, url, log=log)

    task_id = task_mgr.submit_func_task(f"代币经济学(网址): asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/tokenomics/ai/<int:asset_id>", methods=["POST"])
def api_tokenomics_ai(asset_id: int):
    """用户未提供网址时，直接触发 AI 测算代币经济学（文档 + LLM）。"""
    def _worker(log):
        return _get_db_stats().query_tokenomics_ai(asset_id, log=log)

    task_id = task_mgr.submit_func_task(f"代币经济学(AI测算): asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/tokenomics-images/<int:asset_id>/<path:filename>")
def api_tokenomics_image(asset_id: int, filename: str):
    """提供代币经济学提取保存的图片（分配图/排放曲线等）。"""
    directory = TOKENOMICS_IMAGES_ROOT / str(asset_id)
    if not directory.is_dir():
        return jsonify({"ok": False, "error": "图片目录不存在"}), 404
    return send_from_directory(directory, filename)


@app.route("/api/docs/<path:rel_path>")
def api_docs_file(rel_path: str):
    """提供文档存储中的文件（白皮书、审计报告等）。

    storage_path 为相对路径，相对于 DOCS_STORAGE_ROOT。
    路由格式：/api/docs/btc_2/whitepapers/bitcoin.pdf
    """
    # 安全检查：防止路径穿越
    safe_path = (DOCS_STORAGE_ROOT / rel_path).resolve()
    try:
        safe_path.relative_to(DOCS_STORAGE_ROOT.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "非法路径"}), 400

    if not safe_path.is_file():
        return jsonify({"ok": False, "error": "文件不存在"}), 404

    return send_from_directory(DOCS_STORAGE_ROOT, rel_path)


@app.route("/api/assets/<int:asset_id>/reset-deep-crawl", methods=["POST"])
def api_reset_deep_crawl(asset_id: int):
    """重置 deep_crawled_at，允许 B2 重新爬取该资产。"""
    try:
        result = _get_db_stats().reset_deep_crawl(asset_id)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _parse_discovered(output: str) -> int:
    """从 B2/B3 脚本输出中解析本轮新发现的文档链接数（discovered）。

    B2/B3 结束时都会输出一行 JSON：{"status": "complete", ..., "discovered": N}。
    优先解析该 JSON；失败时回退匹配摘要行 "+N docs"。
    """
    if not output:
        return 0
    for line in reversed(output.splitlines()):
        line = line.strip()
        if '"discovered"' in line and '"status"' in line:
            try:
                data = json.loads(line)
                return int(data.get("discovered") or 0)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
    m = re.search(r"\+(\d+)\s+docs", output)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 0


def _run_re_crawl_full(asset_id: int) -> dict:
    """执行完整重新爬取（清理爬取产物 → 逐层 B2/B3 爬取，最多 8 层）。"""
    b2_script = str(SCRIPTS_BIN / "phase_b2_deep_doc_discovery.py")
    b3_script = str(SCRIPTS_BIN / "phase_b2_spa_browser_crawl.py")

    if not os.path.exists(b2_script):
        return {"ok": False, "error": f"B2 脚本不存在: {b2_script}"}
    if not os.path.exists(b3_script):
        return {"ok": False, "error": f"B3 脚本不存在: {b3_script}"}

    rounds = []
    MAX_ROUNDS = 8  # 最多爬 8 层
    MAX_DOCS = 100  # 累计文档链接上限，超过即停止
    total_timeout = 900  # 单次脚本超时兜底（秒）

    try:
        reset_result = _get_db_stats().reset_full_crawl(asset_id)

        total_discovered = 0
        stopped_by_doc_limit = False

        for round_num in range(1, MAX_ROUNDS + 1):
            # B2 深度爬取（limit 调大，确保一次运行完整处理当前层）
            b2_result = subprocess.run(
                [sys.executable, "-u", b2_script, "--asset-id", str(asset_id),
                 "--limit", "1000", "--workers", "10"],
                cwd=str(SCRIPTS_BIN), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=min(600, total_timeout),
            )
            b2_ok = b2_result.returncode == 0
            b2_output = b2_result.stdout[-2000:] if b2_result.stdout else ""
            b2_new_docs = _parse_discovered(b2_output)
            total_discovered += b2_new_docs

            rounds.append({
                "round": round_num,
                "b2": {"ok": b2_ok, "new_docs": b2_new_docs, "output": b2_output},
                "b3": None,
            })

            # B3 SPA 爬取
            b3_result = subprocess.run(
                [sys.executable, "-u", b3_script, "--asset-id", str(asset_id),
                 "--limit", "100", "--concurrency", "4"],
                cwd=str(SCRIPTS_BIN), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=min(300, total_timeout),
            )
            b3_ok = b3_result.returncode == 0
            b3_output = b3_result.stdout[-2000:] if b3_result.stdout else ""
            b3_new_docs = _parse_discovered(b3_output)
            total_discovered += b3_new_docs

            rounds[-1]["b3"] = {"ok": b3_ok, "new_docs": b3_new_docs, "output": b3_output}

            # 收敛判断：B2 与 B3 本轮都未发现新链接 → 停止
            if b2_new_docs == 0 and b3_new_docs == 0:
                break

            # 累计文档链接超过 100 条 → 停止继续爬取
            if total_discovered >= MAX_DOCS:
                stopped_by_doc_limit = True
                break

        return {
            "ok": True,
            "data": {
                "reset": reset_result,
                "rounds": rounds,
                "total_rounds": len(rounds),
                "total_discovered": total_discovered,
                "stopped_by_doc_limit": stopped_by_doc_limit,
            },
        }
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"爬取超时: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.route("/api/assets/<int:asset_id>/re-crawl-full", methods=["POST"])
def api_re_crawl_full(asset_id: int):
    """完整重新爬取（异步后台执行，避免长请求导致网关超时）。

    启动后前端应轮询 /api/assets/<asset_id>/re-crawl-full/status 获取结果。
    """
    key = str(asset_id)
    with _RecrawlFileLock(RECRAWL_LOCK_FILE):
        state = _load_recrawl_state()
        existing = state.get(key)
        if existing and existing.get("status") == "running":
            return jsonify({"ok": True, "pending": True})
        state[key] = {"status": "running", "result": None}
        _save_recrawl_state(state)

    def _worker():
        try:
            result = _run_re_crawl_full(asset_id)
        except Exception as e:
            result = {"ok": False, "error": str(e) or e.__class__.__name__}
        with _RecrawlFileLock(RECRAWL_LOCK_FILE):
            st = _load_recrawl_state()
            st[key] = {"status": "done", "result": result}
            _save_recrawl_state(st)

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "pending": True})


@app.route("/api/assets/<int:asset_id>/re-crawl-full/status")
def api_re_crawl_full_status(asset_id: int):
    """查询重新爬取异步任务状态。"""
    key = str(asset_id)
    with _RecrawlFileLock(RECRAWL_LOCK_FILE):
        state = _load_recrawl_state()
        item = state.get(key)
    if not item:
        return jsonify({"ok": True, "pending": False, "not_started": True})
    if item.get("status") == "running":
        return jsonify({"ok": True, "pending": True})
    return jsonify({"ok": True, "pending": False, "result": item.get("result")})


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


@app.route("/api/entries/<int:entry_id>/entry-type", methods=["POST"])
def api_update_entry_type(entry_id: int):
    """修改某条 doc_source_entry 的来源类型（entry_type）。"""
    try:
        data = request.get_json(silent=True) or {}
        entry_type = (data.get("entry_type") or "").strip()
        if not entry_type:
            return jsonify({"ok": False, "error": "缺少 entry_type 参数"}), 400
        result = _get_db_stats().update_entry_type(entry_id, entry_type)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error")}), 400
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/assets/<int:asset_id>/ai-classify", methods=["POST"])
def api_ai_classify(asset_id: int):
    """对单个资产的未精确分类链接做 AI 内容主题分类（后台任务 + 实时日志）。"""

    def _worker(log):
        return _get_db_stats().ai_classify_asset(asset_id, log=log)

    task_id = task_mgr.submit_func_task(f"AI精确分类: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/assets/<int:asset_id>/ai-noise-clean", methods=["POST"])
def api_ai_noise_clean(asset_id: int):
    """对指定资产执行 AI 噪声清理。"""
    script = str(SCRIPTS_BIN / "phase_b2_ai_noise_clean_by_asset.py")
    if not os.path.exists(script):
        return jsonify({"ok": False, "error": f"脚本不存在: {script}"}), 500
    try:
        result = subprocess.run(
            [sys.executable, "-u", script, "--asset-id", str(asset_id), "--execute"],
            cwd=str(SCRIPTS_BIN), capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-1000:]
            return jsonify({"ok": False, "error": "AI 噪声清理失败", "stderr": err}), 500

        stdout = result.stdout or ""
        summary: dict = {}
        for line in stdout.splitlines():
            line = line.strip()
            m = re.match(
                r"^(处理资产|判断域名|噪声域名|噪声链接|保留链接|总检查数):\s*([\d,]+)",
                line,
            )
            if m:
                key_map = {
                    "处理资产": "assets",
                    "判断域名": "domains",
                    "噪声域名": "noise_domains",
                    "噪声链接": "noise_links",
                    "保留链接": "kept_links",
                    "总检查数": "checked",
                }
                summary[key_map[m.group(1)]] = int(m.group(2).replace(",", ""))

        return jsonify({
            "ok": True,
            "data": {
                "asset_id": asset_id,
                "summary": summary,
                "log": stdout[-4000:],
            },
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "AI 噪声清理超时（300秒）"}), 504
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
    """触发 NotebookLM 精选生成（配额粗筛 + AI 排序）。后台任务 + 实时日志，返回 task_id 供前端轮询。"""
    force = request.args.get("force", "0") == "1"

    def _worker(log):
        return _get_db_stats().curate_notebooklm(asset_id, force=force, log=log)

    task_id = task_mgr.submit_func_task(f"NotebookLM 精选: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


# ── 一键投研（NotebookLM 风格） ──


@app.route("/api/research/<int:asset_id>/notebook")
def api_research_notebook(asset_id: int):
    """打开（不存在则创建）一个代币对应的一键投研笔记本。支持 ?refresh=1 强制重采快照。"""
    force_refresh = (request.args.get("refresh", "") or "").strip() == "1"
    try:
        data = _get_db_stats().get_or_create_research_notebook(asset_id, force_refresh=force_refresh)
        if data.get("ok"):
            return jsonify(data)
        return jsonify(data), 404 if "不存在" in (data.get("error") or "") else 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/fill-missing", methods=["POST"])
def api_research_fill_missing(asset_id: int):
    """一键补齐单个代币缺失的投研资料（后台任务 + 实时日志）。"""
    def _worker(log):
        return _get_db_stats().fill_missing_materials(asset_id, log=log)

    task_id = task_mgr.submit_func_task(f"补齐缺失: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/research/<int:asset_id>/thesis", methods=["POST"])
def api_research_thesis(asset_id: int):
    """生成结构化研究结论（后台任务 + 实时日志）。"""
    def _worker(log):
        return _get_db_stats().generate_research_thesis(asset_id, log=log)

    task_id = task_mgr.submit_func_task(f"生成研究结论: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/research/<int:asset_id>/tokenomics")
def api_research_tokenomics(asset_id: int):
    """代币经济学结构化数据。"""
    try:
        data = _get_db_stats().get_asset_tokenomics(asset_id)
        return jsonify({"ok": True, "data": data or {}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/competitors")
def api_research_competitors(asset_id: int):
    """同赛道竞品结构化对比。"""
    try:
        limit = int(request.args.get("limit", "8"))
        result = _get_db_stats().get_sector_competitors(asset_id, limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/divergence")
def api_research_divergence(asset_id: int):
    """情绪 × 价格 × 链上 背离检测。"""
    try:
        result = _get_db_stats().get_divergence_signals(asset_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/derivatives")
def api_research_derivatives(asset_id: int):
    """衍生品资金面数据（多交易所聚合：资金费率/OI/CVD）。"""
    try:
        force = request.args.get("refresh", "").lower() in ("1", "true", "yes")
        result = _get_db_stats().get_asset_derivatives(asset_id, force_refresh=force)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/market-history")
def api_research_market_history(asset_id: int):
    """行情历史时间序列（日级，价格/市值/成交量等）。"""
    try:
        days = int(request.args.get("days", 30))
        source = request.args.get("source", "cmc")
        result = _get_db_stats().get_asset_market_history(asset_id, days=days, source_code=source)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/signals")
def api_research_signals(asset_id: int):
    """单资产异动信号检测（价格/成交量/OI/资金费率/解锁等 diff）。"""
    try:
        result = _get_db_stats().detect_asset_signals(asset_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/correlation-matrix")
def api_research_correlation_matrix():
    """资产间价格收益相关性矩阵（Pearson）。

    支持两种模式：
      - tier + top_n：按市值分层取 top N
      - asset_ids：指定资产 ID 列表（逗号分隔）
    """
    try:
        tier = request.args.get("tier", "top100")
        top_n = request.args.get("top_n", default=30, type=int)
        days = request.args.get("days", default=90, type=int)
        metric = request.args.get("metric", "price")
        asset_ids_str = request.args.get("asset_ids", "")

        asset_ids = None
        if asset_ids_str:
            asset_ids = [int(x) for x in asset_ids_str.split(",") if x.strip()]

        result = _get_db_stats().compute_correlation_matrix(
            asset_ids=asset_ids,
            tier=tier,
            top_n=top_n,
            days=days,
            metric=metric,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/cex-netflow")
def api_research_cex_netflow(asset_id: int):
    """链上 CEX 净流入/流出（基于交易所钱包标签 + 大额转账日志）。"""
    try:
        hours = request.args.get("hours", default=24, type=int)
        result = _get_db_stats().get_cex_netflow(asset_id, hours)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/<int:asset_id>/whale-flow")
def api_research_whale_flow(asset_id: int):
    """鲸鱼/聪明钱行为流分析（持仓变化 + 大额转账流向）。"""
    try:
        result = _get_db_stats().get_whale_flow(asset_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/research/notebook/<int:notebook_id>/ask", methods=["POST"])
def api_research_ask(notebook_id: int):
    """基于笔记本资料库进行 AI 问答（后台任务 + 实时日志）。"""
    question = (request.get_json(silent=True) or {}).get("question", "").strip()
    if not question:
        return jsonify({"ok": False, "error": "缺少 question 参数"}), 400

    def _worker(log):
        return _get_db_stats().ask_research_notebook(notebook_id, question, log=log)

    task_id = task_mgr.submit_func_task(f"一键投研问答: notebook {notebook_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/research/<int:asset_id>")
def research_page(asset_id: int):
    """一键投研独立页面（新标签页打开）。"""
    return render_template("research.html", asset_id=asset_id)


@app.route("/api/research/source/content")
def api_research_source_content():
    """抓取单个来源 URL 的正文（HTML/PDF），供投研页按文件类型展开查看资料内容。"""
    url = (request.args.get("url", "") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "缺少 url 参数"}), 400
    try:
        content = _get_db_stats().fetch_research_source_content(url)
        return jsonify({"ok": True, "content": content})
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


@app.route("/api/onchain/holder/<int:asset_id>/trend")
def api_onchain_holder_trend(asset_id: int):
    """链上持仓趋势（时间序列）。"""
    try:
        days = request.args.get("days", default=30, type=int)
        data = _get_db_stats().get_onchain_holder_trend(asset_id, days)
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


@app.route("/api/onchain/query/<int:asset_id>", methods=["GET", "POST"])
def api_onchain_query(asset_id: int):
    """按需查询指定资产的链上数据（后台任务 + 实时日志）。返回 task_id 供前端轮询。"""
    if request.method == "POST":
        force = (request.get_json(silent=True) or {}).get("force") == 1
    else:
        force = request.args.get("force", "0") == "1"

    def _worker(log):
        return _get_db_stats().query_onchain_data(asset_id, force=force, log=log)

    task_id = task_mgr.submit_func_task(f"链上数据: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/unlocks/<int:asset_id>")
def api_unlocks_get(asset_id: int):
    """读取已缓存的代币解锁数据（只读，不触发爬取）。"""
    try:
        data = _get_db_stats().get_asset_unlocks(asset_id)
        if data:
            return jsonify({"ok": True, "has_unlock_data": True, "data": data})
        # 无缓存数据：返回 200 + 明确语义，而非 404 错误。
        # 该接口只读、不触发爬取，无法区分「从未抓取」与「已确认无解锁计划」，
        # 因此统一提示「暂无已知解锁计划（或尚未抓取）」，前端据此展示友好状态。
        return jsonify({
            "ok": True,
            "has_unlock_data": False,
            "data": None,
            "message": "该资产暂无已知解锁计划（或尚未抓取 tokenomics.com 数据），可点击拉取。",
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/unlocks/query/<int:asset_id>", methods=["GET", "POST"])
def api_unlocks_query(asset_id: int):
    """按需拉取指定资产的代币解锁数据（后台任务 + 实时日志）。返回 task_id 供前端轮询。"""
    if request.method == "POST":
        force = (request.get_json(silent=True) or {}).get("force") == 1
    else:
        force = request.args.get("force", "0") == "1"

    def _worker(log):
        return _get_db_stats().query_token_unlocks(asset_id, force=force, log=log)

    task_id = task_mgr.submit_func_task(f"解锁数据: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/unlocks/scrape-url/<int:asset_id>", methods=["POST"])
def api_unlocks_scrape_url(asset_id: int):
    """按用户提供的 tokenomics 网址抓取解锁数据（后台任务 + 实时日志）。"""
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "缺少网址"}), 400

    def _worker(log):
        return _get_db_stats().query_unlocks_by_url(asset_id, url, log=log)

    task_id = task_mgr.submit_func_task(f"解锁数据(网址): asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/unlocks/ai-estimate/<int:asset_id>", methods=["POST"])
def api_unlocks_ai_estimate(asset_id: int):
    """用户未提供网址时，直接触发 AI 测算解锁数据（后台任务 + 实时日志）。"""
    def _worker(log):
        return _get_db_stats().query_unlocks_ai(asset_id, log=log)

    task_id = task_mgr.submit_func_task(f"解锁数据(AI测算): asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/unlocks/event-impact/<int:asset_id>")
def api_unlocks_event_impact(asset_id: int):
    """解锁事件研究：分析历史解锁事件前后的价格走势。"""
    try:
        window = int(request.args.get("window", 14))
        data = _get_db_stats().analyze_unlock_event_impact(asset_id, window_days=window)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/social/<int:asset_id>")
def api_social_get(asset_id: int):
    """读取已缓存的社交热度数据（只读，不触发拉取）。"""
    try:
        data = _get_db_stats().get_asset_social_heat(asset_id)
        if data:
            return jsonify({"ok": True, "data": data})
        return jsonify({"ok": False, "error": "无缓存数据"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/social/query/<int:asset_id>", methods=["GET", "POST"])
def api_social_query(asset_id: int):
    """按需拉取指定资产的社交热度数据（后台任务 + 实时日志）。"""
    if request.method == "POST":
        force = (request.get_json(silent=True) or {}).get("force") == 1
    else:
        force = request.args.get("force", "0") == "1"

    def _worker(log):
        return _get_db_stats().query_social_heat(asset_id, force=force, log=log)

    task_id = task_mgr.submit_func_task(f"社交热度: asset {asset_id}", _worker)
    return jsonify({"ok": True, "pending": True, "task_id": task_id})


@app.route("/api/social/leaderboard")
def api_social_leaderboard():
    """社交热度排行榜：按市值分层展示社交热度最高的资产 + 情绪分布。"""
    try:
        tier = request.args.get("tier", "all")
        limit = request.args.get("limit", default=20, type=int)
        sort_by = request.args.get("sort_by", "score")
        result = _get_db_stats().get_social_heat_leaderboard(
            tier=tier if tier != "all" else None,
            limit=limit,
            sort_by=sort_by,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/holders/query/<int:asset_id>")
def api_holders_query(asset_id: int):
    """按需拉取指定资产的持仓分布快照（从区块浏览器爬取）。"""
    chain = request.args.get("chain", "bsc")
    try:
        data = _get_db_stats().query_holder_snapshot(asset_id, chain=chain)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/holders/<int:asset_id>")
def api_holders_get(asset_id: int):
    """读取已保存的持仓分布数据。"""
    try:
        data = _get_db_stats().get_token_holders(asset_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/hot")
def api_market_hot():
    """每日投研推荐：多源交叉验证（Binance + CMC），按综合评分排序。"""
    try:
        from cross_market import get_cross_validated
        limit = int(request.args.get("limit", "30"))
        tier = request.args.get("tier") or None
        result = get_cross_validated(limit, tier=tier)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/gainers")
def api_market_gainers():
    """24h 涨幅榜：多源交叉验证。"""
    try:
        from cross_market import get_consensus_gainers
        limit = int(request.args.get("limit", "30"))
        tier = request.args.get("tier") or None
        result = get_consensus_gainers(limit, tier=tier)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/volume")
def api_market_volume():
    """24h 交易量榜：多源交叉验证。"""
    try:
        from cross_market import get_consensus_volume
        limit = int(request.args.get("limit", "30"))
        tier = request.args.get("tier") or None
        result = get_consensus_volume(limit, tier=tier)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/sector-heatmap")
def api_market_sector_heatmap():
    """赛道轮动热力图：按赛道聚合多源交叉验证结果。"""
    try:
        from cross_market import get_sector_heatmap
        limit = int(request.args.get("limit", "20"))
        result = get_sector_heatmap(limit)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/daily-diff")
def api_daily_diff():
    """每日 diff 变化榜：涨跌幅/成交量异动/解锁抛压等。"""
    try:
        diff_date = request.args.get("date")
        categories = request.args.get("categories")
        cat_list = categories.split(",") if categories else None
        result = _get_db_stats().get_daily_diff_summary(diff_date=diff_date, categories=cat_list)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market/backtest")
def api_market_backtest():
    """每日推荐质量回测。"""
    try:
        days = request.args.get("days", default=30, type=int)
        top_n = request.args.get("top_n", default=10, type=int)
        result = _get_db_stats().get_recommendation_backtest(days, top_n)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 解锁追踪列表（watchlist） ──

@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_list():
    """解锁追踪列表（含跌幅、到期天数）。"""
    try:
        data = _get_db_stats().list_watchlist()
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    """加入解锁追踪列表。body: {asset_id, short_plan_note, target_unlock_date, target_unlock_pct}"""
    try:
        body = request.get_json(silent=True) or {}
        asset_id = body.get("asset_id")
        if not asset_id:
            return jsonify({"ok": False, "error": "缺少 asset_id"}), 400
        data = _get_db_stats().add_watchlist(
            asset_id=int(asset_id),
            short_plan_note=body.get("short_plan_note", "") or "",
            target_unlock_date=body.get("target_unlock_date"),
            target_unlock_pct=body.get("target_unlock_pct"),
        )
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/watchlist/<int:watch_id>", methods=["DELETE"])
def api_watchlist_remove(watch_id: int):
    """从解锁追踪列表移除。"""
    try:
        data = _get_db_stats().remove_watchlist(watch_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
