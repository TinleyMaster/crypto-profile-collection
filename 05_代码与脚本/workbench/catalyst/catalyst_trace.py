"""催化剂管道逐步追溯（trace）——每一步筛选留痕，便于定位问题与调参。

OPT-CATALYST-ALERT-001 衍生需求（2026-09-15）：
    每个 catalyst/asset 在每一阶段（L1 分类 → G1 分级 → G2 共振 → G6 信号
    → 慢通道 二阶/G3-G5/G7）是「通过」还是「被拦」，都要落痕，含阈值与原因，
    便于定位"哪一步出了问题"、据此调参。

输出两路（任何异常都不影响主流程）：
    1) JSONL 文件  workbench/output/catalyst_trace/YYYY-MM-DD.jsonl（完整审计，可 grep）
    2) stdout：被拦行始终打印；通过行仅在 verbose 时打印（避免全量刷屏）

用法（管道内，模块级即可，无需传参）：
    from catalyst.catalyst_trace import trace_step, reset, summary, set_verbose
    set_verbose(args.verbose)          # 管道入口
    reset()                             # 每轮运行开始前清零统计
    trace_step("G6_signal", catalyst_id=..., asset_id=...,
               passed=False, reason="composite=35 < C阈值40",
               metrics={"composite": 35, "tier": None})
    summary()                           # 结束时打印每步 通过/被拦 统计
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

_lock = threading.Lock()

# 默认 trace 目录：workbench/output/catalyst_trace/（与 ai_trace 同级）
_TRACE_BASE = os.environ.get(
    "CATALYST_TRACE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "output", "catalyst_trace"),
)

# 每步「通过/被拦」计数（跨 run_* 函数汇总，main() 里 reset / summary）
_STATS: dict[str, dict] = {}
_VERBOSE = False


def set_verbose(v: bool) -> None:
    """控制 stdout：True 时通过行也打印（默认只打印被拦行）。"""
    global _VERBOSE
    _VERBOSE = bool(v)


def reset() -> None:
    """清零分步统计（每轮管道运行开始时调用）。"""
    _STATS.clear()


def _trace_path() -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    try:
        os.makedirs(_TRACE_BASE, exist_ok=True)
    except Exception:
        pass
    return os.path.join(_TRACE_BASE, f"{day}.jsonl")


def _safe_json(v):
    """非基础类型统一转 str，保证 json.dumps 不炸。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def trace_step(stage: str, catalyst_id=None, asset_id=None, title=None,
               symbol=None, passed: bool = True, reason: str | None = None,
               metrics: dict | None = None) -> None:
    """写一条逐步追溯记录。

    Args:
        stage:      步骤标识（L1_classify / G1_grade / G2_resonance / G6_signal
                    / SO_second_order / G3G5_recalc）
        catalyst_id: 催化剂 ID
        asset_id:    资产 ID（G2/G6 等资产级步骤）
        title:       催化剂标题（前 80 字展示用；写入时尽量回填，否则追溯页
                     读取时会再按 catalyst_id 从库补全）
        symbol:      资产符号（如 ARB / BTC；资产级步骤写入时回填）
        passed:      True=通过进入下一步；False=在本步被拦下
        reason:      被拦原因（含阈值），如 "composite=35 < C阈值40"
        metrics:     该步关键数值 dict（便于调参），如 {"kind":"noise","base_strength":20}
    """
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "catalyst_id": catalyst_id,
        "asset_id": asset_id,
        "title": (title or "")[:80],
        "symbol": (str(symbol) if symbol is not None else "")[:20],
        "passed": bool(passed),
        "reason": reason,
        "metrics": {k: _safe_json(v) for k, v in (metrics or {}).items()},
    }
    line = json.dumps(rec, ensure_ascii=False)

    # 1) 文件：始终写（完整审计）
    try:
        with _lock:
            with open(_trace_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

    # 2) 分步计数
    try:
        s = _STATS.setdefault(stage, {"passed": 0, "dropped": 0})
        if rec["passed"]:
            s["passed"] += 1
        else:
            s["dropped"] += 1
    except Exception:
        pass

    # 3) stdout：被拦行始终打印；通过行仅 verbose
    try:
        flag = "✓" if rec["passed"] else "✗"
        head = f"[trace:{stage}] {flag} cat={catalyst_id}"
        if asset_id is not None:
            head += f" asset={asset_id}"
        if rec["symbol"]:
            head += f" [{rec['symbol']}]"
        if rec["title"]:
            head += f" {rec['title']}"
        tail = f" | {reason}" if reason else ""
        m = " ".join(f"{k}={v}" for k, v in rec["metrics"].items())
        if m:
            tail += f" | {m}"
        if not rec["passed"]:
            print(f"  {head}{tail}")
        elif _VERBOSE:
            print(f"  {head}{tail}")
    except Exception:
        pass


def summary() -> None:
    """打印本进程内累计的每步 通过/被拦 统计。"""
    if not _STATS:
        return
    try:
        print("\n── 催化剂逐步追溯统计（trace summary）──")
        print(f"  {'步骤':<20} {'通过':>6} {'被拦':>6}")
        for stage, s in sorted(_STATS.items()):
            print(f"  {stage:<20} {s['passed']:>6} {s['dropped']:>6}")
        print(f"  完整明细见 {_TRACE_BASE}")
    except Exception:
        pass


# =====================================================================
# 追溯页读取/展示辅助（供 workbench/app.py 的 /api/catalyst/trace 使用）
# 纯逻辑集中在此，app.py 只负责 HTTP 层；便于离线单测。
# （审计_催化剂追溯页_可读性_2026-09-29 P0/P1）
# =====================================================================

# 管道阶段顺序（统计卡排序 + 视图分层；与 phase_catalyst_pipeline 写入阶段一致）
TRACE_STAGE_ORDER = [
    "L1_classify", "G1_grade", "G2_resonance", "G6_signal",
    "SO_second_order", "SO_resonance_refresh", "G3G5_recalc",
]

# 字段释义（P1-2）：让非研发读者也能读懂每行指标。前端按 key 渲染 tooltip / 图例，
# 是唯一事实源（勿在模板里另抄一份）。
TRACE_METRIC_LEGEND = {
    "kind": {"label": "事件性质", "desc": "structural=结构性 / event=一次性事件 / noise=噪声（无关联资产或弱信号）"},
    "base_strength": {"label": "基础强度", "desc": "事件本身的强度分（0-100），尚未含共振/基本面加成"},
    "authority": {"label": "信源权威度", "desc": "信源权重分（0-100）"},
    "event_weight": {"label": "事件类型权重", "desc": "按事件类型给定的可交易性权重（0-100）"},
    "scope": {"label": "影响范围", "desc": "影响范围分（0-100）"},
    "tradable": {"label": "可交易", "desc": "是否关联到可交易标的（是/否）"},
    "event_type": {"label": "规则事件类型", "desc": "L1 规则分类得到的事件类型"},
    "resonance_score": {"label": "共振分", "desc": "价格是否已同向反应（0-100，越高越已定价）"},
    "resonance_state": {"label": "共振状态", "desc": "confirmed=价格已定价 / weak=弱反应 / pending=未反应（不进 G6）"},
    "res_state": {"label": "共振状态", "desc": "同 resonance_state（缩写）"},
    "excess_24h": {"label": "24h超额", "desc": "相对 BTC 的 24h 超额涨幅（%）"},
    "vol_z": {"label": "量能Z", "desc": "成交量 z-score，越大越异常放量"},
    "direction": {"label": "方向匹配", "desc": "价格方向与事件方向是否一致"},
    "tier": {"label": "信号档位", "desc": "A/B=可推送 / C=观察 / null=被拦"},
    "composite": {"label": "综合分", "desc": "共振+基本面+技术等加权总分（0-100）"},
    "rr": {"label": "盈亏比", "desc": "（目标-入场）/（入场-止损），低于阈值被拦"},
    "status": {"label": "信号状态", "desc": "open=可动作 / watch=观察 / invalid=无效"},
    "prev_status": {"label": "原状态", "desc": "本轮变化前的信号状态"},
    "mappings": {"label": "二阶映射数", "desc": "展开出的二阶受益资产数量"},
    "persistence": {"label": "持续性", "desc": "structural=持续 / one_off=一次性 / decaying=衰减"},
    "fundamental": {"label": "基本面", "desc": "G4 基本面是否通过"},
    "technical": {"label": "技术面", "desc": "G5 技术面状态：up/range/down"},
}


def trace_stage_rank(stage) -> int:
    """阶段在管道中的序号；未知阶段排最后。"""
    try:
        return TRACE_STAGE_ORDER.index(stage)
    except ValueError:
        return len(TRACE_STAGE_ORDER)


def trace_stage_sort_key(item) -> int:
    """统计卡按管道顺序排序（L1→G1→G2→G6…）。

    修正此前按字母序导致 G1→G2→G6→L1 与副标题管道顺序不一致的观感
    （审计 2026-09-29 P1-1）。
    """
    return trace_stage_rank(item.get("stage"))


def trace_is_key_row(rec) -> bool:
    """「关键决策」行 = 非 G6 通过行。

    G6 每日通过量级数千（默认 ts 倒序会 100% 淹没列表），上游决策阶段
    （L1/G1/G2）与被拦行才是审计要看的精华；故默认视图折叠 G6 通过行
    （审计 2026-09-29 P0-2）。G6 被拦行仍保留（含被拦原因）。
    """
    return not (rec.get("stage") == "G6_signal" and rec.get("passed"))


def trace_derive_pass_reason(rec):
    """给「通过」行生成一句人话原因（审计 2026-09-29 P1-3）。

    通过行原先 reason 恒为空，审计页最需要的「为什么通过」完全缺失。
    已有 reason 或被拦行原样返回；无 metrics 可依据时退化为「通过」。
    """
    if not rec.get("passed") or rec.get("reason"):
        return rec.get("reason")
    stage = rec.get("stage")
    m = rec.get("metrics") or {}
    if stage == "G6_signal":
        parts = []
        if m.get("tier"):
            parts.append(f"档位 {m['tier']}")
        if m.get("composite") is not None:
            parts.append(f"综合分 {m['composite']}")
        if m.get("rr") is not None:
            parts.append(f"盈亏比 {m['rr']}")
        return ("通过进入信号池：" + "，".join(parts)) if parts else "通过"
    if stage == "G2_resonance":
        return f"通过：共振分 {m.get('resonance_score')}（{m.get('resonance_state')}），进入 G6"
    if stage == "G1_grade":
        return f"通过：{m.get('kind') or ''}（基础强度 {m.get('base_strength')}）"
    if stage == "SO_second_order":
        return f"通过：展开 {m.get('mappings')} 个二阶受益资产"
    if stage == "G3G5_recalc":
        return f"通过：tier={m.get('tier')}，综合分 {m.get('composite')}"
    if stage == "SO_resonance_refresh":
        return "通过：状态迁移已回写"
    if stage == "L1_classify":
        return f"通过：规则分类为 {m.get('event_type')}"
    return "通过"


def trace_enrich_from_db(entries, conn) -> list:
    """读取层回填 title / symbol（审计 2026-09-29 P0-1）。

    历史 trace 行（尤其 G6/G2/G3G5 等资产级阶段）写入时未带 catalyst 标题与
    资产符号，页面只能显示裸 ID。这里按 catalyst_id / asset_id 批量从库补全，
    同时让 `q` 标题搜索对历史行也生效（P2-1）。无 DB / 查询失败时保持原样。
    """
    cat_ids = sorted({r.get("catalyst_id") for r in entries
                      if r.get("catalyst_id") is not None})
    asset_ids = sorted({r.get("asset_id") for r in entries
                        if r.get("asset_id") is not None})
    titles = {}
    symbols = {}
    if cat_ids:
        rows = conn.execute(
            "SELECT catalyst_id, COALESCE(NULLIF(title, ''), title_cn, '') AS t "
            "FROM biz.asset_catalyst WHERE catalyst_id = ANY(%s)",
            (list(cat_ids),),
        ).fetchall()
        for cid, t in rows:
            if t:
                titles[cid] = t
    if asset_ids:
        rows = conn.execute(
            "SELECT asset_id, canonical_symbol FROM core.asset WHERE asset_id = ANY(%s)",
            (list(asset_ids),),
        ).fetchall()
        for aid, sym in rows:
            if sym:
                symbols[aid] = sym
    for r in entries:
        if not r.get("title") and titles.get(r.get("catalyst_id")):
            r["title"] = str(titles[r["catalyst_id"]])[:80]
        if not r.get("symbol") and symbols.get(r.get("asset_id")):
            r["symbol"] = str(symbols[r["asset_id"]])
    return entries

