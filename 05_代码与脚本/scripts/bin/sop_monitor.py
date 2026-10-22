#!/usr/bin/env python3
"""代币埋伏追踪 SOP · 监控器（从 sop.sop_switches 单一真相重建）

背景
----
原 `代币研究所` 工作区的 monitor.py / sop.py 未随代码库迁移，但**数据仍在**：
`sop.sop_switches` 有 136 条开关（28 币）、`sop.token_pool` 32 币。
本脚本以该表为唯一真相重建监控能力 —— 不新增 schema、不改业务表，只读+算+出报告。

判读口径
--------
每个开关的 `trigger_json` 是结构化条件（比自然语言 `threshold` 可靠）：

    {"logic":"and","conds":[{"metric":"RSI","op":"<","val":35},
                             {"metric":"PX","op":">","ref":"MA20"}]}

已覆盖的 metric（实测词表，全库仅此 5 个）：
    RSI / PX / MA20 / MA50 / LSR / FUND / TVL
    · PX/RSI/MA20/MA50 ← binance_kline
    · LSR              ← binance_lsr
    · FUND             ← binance_funding
    · TVL              ← defillama_tvl

**重要**：`pending` / `digest` 只是「值得看一眼」，不是买入信号。
下单前必须重抓实时价（报告有滞后）。

用法
----
    python bin/sop_monitor.py --digest          # 生成日报（落 data/sop/digest/YYYY-MM-DD.md）
    python bin/sop_monitor.py --pending         # 只看接近触发的开关
    python bin/sop_monitor.py --json            # 结构化输出
    python bin/sop_monitor.py --token UNI       # 单币
    python bin/sop_monitor.py --dry-run         # 只读表，不抓网络（体检用）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import requests  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

METHOD_VERSION = "sop-monitor-rebuild-v1"
CST = timezone(timedelta(hours=8))
FAPI = "https://fapi.binance.com"
LLAMA = "https://api.llama.fi"

# 距触发多远仍算「值得看一眼」（按指标量纲分别设）
NEAR_BAND = {"RSI": 8.0, "LSR": 0.25, "FUND": 0.0002, "TVL": 0.10}

_session = requests.Session()
_session.headers.update({"User-Agent": "sop-monitor/1.0"})


# ═══════════════════════════════════════════════════════════════
#  指标计算
# ═══════════════════════════════════════════════════════════════
def wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder 平滑 RSI（与交易所常用口径一致）。样本不足返回 None。"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)


def sma(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return round(sum(vals[-n:]) / n, 8)


# ═══════════════════════════════════════════════════════════════
#  Fetcher 注册表
# ═══════════════════════════════════════════════════════════════
def fetch_kline(symbol: str, limit: int = 100) -> dict:
    r = _session.get(f"{FAPI}/fapi/v1/klines",
                     params={"symbol": symbol, "interval": "1d", "limit": limit},
                     timeout=15)
    r.raise_for_status()
    rows = r.json()
    closes = [float(x[4]) for x in rows]
    return {
        "PX": closes[-1] if closes else None,
        "RSI": wilder_rsi(closes),
        "MA20": sma(closes, 20),
        "MA50": sma(closes, 50),
        "_n": len(closes),
    }


def fetch_lsr(symbol: str) -> dict:
    r = _session.get(f"{FAPI}/futures/data/globalLongShortAccountRatio",
                     params={"symbol": symbol, "period": "1d", "limit": 2},
                     timeout=15)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return {"LSR": None, "_n": 0}
    return {"LSR": float(rows[-1]["longShortRatio"]), "_n": len(rows)}


def fetch_funding(symbol: str) -> dict:
    r = _session.get(f"{FAPI}/fapi/v1/premiumIndex",
                     params={"symbol": symbol}, timeout=15)
    r.raise_for_status()
    d = r.json()
    # lastFundingRate 为**本周期**费率（8h 一次）；trigger 里的阈值按 /8h 口径写
    return {"FUND": float(d.get("lastFundingRate") or 0), "_n": 1,
            "markPrice": float(d.get("markPrice") or 0)}


_protocols_cache: list | None = None


def _load_protocols() -> list:
    """懒加载 DefiLlama 协议列表（9 MB，仅在直连 slug 失败时才拉，一次进程内复用）。"""
    global _protocols_cache
    if _protocols_cache is None:
        r = _session.get(f"{LLAMA}/protocols", timeout=90)
        r.raise_for_status()
        _protocols_cache = r.json()
    return _protocols_cache


def _resolve_tvl_slug(slug: str) -> tuple[str | None, str | None]:
    """把 watch_json 里的 slug 解析成 DefiLlama 真实 slug。

    实测脏数据：`sop_switches.watch_json` 写作 `aerodrome` / `ondo` / `chip`，
    但 DefiLlama 实际为 `aerodrome-v1` / `ondo-yield-assets`，CHIP 则无对应协议。
    多个候选时**不自动选**（Aerodrome 有 v1/slipstream/ignition 三个池子，
    合并会算错 TVL），返回 None + 候选说明。

    返回 (真实 slug, 说明) 或 (None, 无法解析原因)。
    """
    try:
        protos = _load_protocols()
    except Exception as e:
        return None, "无法加载 DefiLlama 协议列表: %s: %s" % (type(e).__name__, e)

    if any(p.get("slug") == slug for p in protos):
        return slug, None

    if str(slug).isdigit():
        by_id = [p for p in protos if str(p.get("id")) == str(slug)]
        if len(by_id) == 1:
            return by_id[0]["slug"], "id %s → %r" % (slug, by_id[0]["slug"])
        return None, "id=%s 匹配到 %d 个协议，不自动选" % (slug, len(by_id))

    key = str(slug).upper().replace(" ", "")
    hits = [p for p in protos
            if key and key in (p.get("name") or "").upper().replace(" ", "")]
    if len(hits) == 1:
        return hits[0]["slug"], "slug %r → %r" % (slug, hits[0]["slug"])
    if len(hits) > 1:
        cand = ", ".join("%s(%s)" % (p.get("name"), p.get("slug")) for p in hits[:6])
        return None, "slug %r 有 %d 个候选，不自动选: %s" % (slug, len(hits), cand)
    return None, "slug %r 在 DefiLlama 无对应协议（该币可能不是协议类资产）" % slug


def fetch_tvl(slug: str) -> dict:
    """取协议 TVL。直连失败时自动解析真实 slug；无法解析返回 _reason 而非静默 None。"""
    note = None
    try:
        r = _session.get(f"{LLAMA}/protocol/{slug}", timeout=25)
        if r.status_code in (400, 404):
            real, note = _resolve_tvl_slug(slug)
            if not real:
                return {"TVL": None, "_n": 0, "_reason": note}
            r = _session.get(f"{LLAMA}/protocol/{real}", timeout=25)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return {"TVL": None, "_n": 0,
                "_reason": "抓取失败 %s: %s" % (type(e).__name__, e)}

    tvl = None
    chain_tvls = d.get("currentChainTvls") or {}
    if isinstance(chain_tvls, dict) and chain_tvls:
        # 排除 doublecounted / borrowed / staking 等汇总键，只加链级
        vals = [v for k, v in chain_tvls.items()
                if not k.endswith("-borrowed") and "-" not in k]
        tvl = sum(vals) if vals else None
    if tvl is None:
        tvl = d.get("tvl")
    if isinstance(tvl, list) and tvl:
        tvl = tvl[-1].get("totalLiquidityUSD")
    out = {"TVL": float(tvl) if tvl is not None else None, "_n": 1}
    if tvl is None:
        out["_reason"] = "DefiLlama 该协议 TVL 为空（可能非 TVL 型协议）"
    if note:
        out["_note"] = note
    return out


FETCHERS = {
    "binance_kline": fetch_kline,
    "binance_lsr": fetch_lsr,
    "binance_funding": fetch_funding,
    "defillama_tvl": fetch_tvl,
}

# 有 fetcher 但本脚本未实现取数的（如实标注，绝不静默跳过）
UNSUPPORTED = {
    "binance_oi", "coinglass_oi", "coinglass_liquidation", "defillama_fees",
    "alternative_fng", "github", "unlock_next", "tokenomics_burn", "catalyst_gov",
}


def fetch_all(watches: list[dict]) -> tuple[dict, dict, dict]:
    """按 watch_json 批量取数。返回 (metrics, errors, notes)。"""
    metrics: dict[str, dict] = {}
    errors: dict[str, str] = {}
    notes: dict[str, str] = {}
    for w in watches:
        kind = w.get("kind")
        if kind not in FETCHERS:
            continue
        ref = w.get("symbol") or w.get("slug")
        if not ref:
            continue
        key = f"{kind}:{ref}"
        if key in metrics or key in errors:
            continue
        fn = FETCHERS[kind]
        try:
            got = fn(ref)
            metrics[key] = got
            # _reason 表示「取到了但没数据/无法解析」，与 errors（异常）分开记
            if got.get("_reason"):
                notes[key] = got["_reason"]
            elif got.get("_note"):
                notes[key] = got["_note"]
            time.sleep(0.12)  # 温和限速，避免触发 Binance 权重限制
        except Exception as e:  # 单源失败不阻塞整轮
            errors[key] = f"{type(e).__name__}: {e}"
    return metrics, errors, notes


# ═══════════════════════════════════════════════════════════════
#  条件求值
# ═══════════════════════════════════════════════════════════════
def _resolve(operand: dict, m: dict):
    """取出操作数：字面量 val，或引用指标 ref。"""
    if "val" in operand and operand["val"] is not None:
        return float(operand["val"])
    if "ref" in operand and operand["ref"]:
        v = m.get(operand["ref"])
        return float(v) if v is not None else None
    return None


def _cmp(left, op, right):
    if left is None or right is None:
        return None
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    return None


def evaluate(trigger: dict, m: dict) -> dict:
    """逐条求值 trigger_json → {state, detail[], conds[]}。

    state: triggered / pending / not_triggered / no_data
      · no_data   任一所需指标缺值（不做猜测）
      · pending   全部有值但未满足，且至少一条「接近」阈值
    """
    conds = trigger.get("conds") or []
    if not conds:
        return {"state": "no_data", "detail": ["trigger 无条件"], "conds": []}

    results, near_any, missing = [], False, False

    for c in conds:
        metric = c.get("metric")
        op = c.get("op")
        left = m.get(metric) if metric not in ("PX",) else m.get("PX")
        # metric 可能是 PX/RSI/LSR/FUND/TVL；也可能是已被 ref 引用的对比量
        left_val = m.get(metric)
        right_val = _resolve(c, m)
        ok = _cmp(left_val, op, right_val)

        item = {
            "metric": metric, "op": op,
            "left": left_val, "right": right_val,
            "ref": c.get("ref"), "ok": ok,
        }
        if ok is None:
            missing = True
        else:
            # 接近度：仅对右侧为字面量的条件判定
            if "val" in c and c.get("val") is not None and left_val is not None:
                band = NEAR_BAND.get(metric)
                if band is not None and not ok:
                    gap = abs(left_val - float(c["val"]))
                    if gap <= band:
                        near_any = True
                        item["near"] = round(gap, 6)
        results.append(item)

    logic = (trigger.get("logic") or "and").lower()
    oks = [r["ok"] for r in results]
    if missing:
        state = "no_data"
    elif logic == "or":
        state = "triggered" if any(oks) else ("pending" if near_any else "not_triggered")
    else:
        state = "triggered" if all(oks) else ("pending" if near_any else "not_triggered")

    return {"state": state, "conds": results,
            "note_on": trigger.get("note_on")}


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════
def load_switches(conn, token: str | None = None) -> list[dict]:
    sql = """
        SELECT switch_id, token, switch_type, narrative, indicator, source,
               auto, freq, threshold, data_kind, data_ref, trigger_json, watch_json
        FROM sop.sop_switches
    """
    params = ()
    if token:
        sql += " WHERE upper(token) = upper(%s)"
        params = (token,)
    sql += " ORDER BY token, switch_id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out = []
    for r in rows:
        (sid, tok, stype, narr, ind, src, auto, freq, thr,
         dkind, dref, tj, wj) = r
        watches = []
        if wj:
            try:
                watches = json.loads(wj)
            except Exception:
                watches = []
        if not watches and dkind:
            watches = [{"kind": dkind, "symbol": dref, "slug": dref}]
        trig = None
        if tj:
            try:
                trig = json.loads(tj)
            except Exception:
                trig = None
        out.append({
            "switch_id": sid, "token": tok, "switch_type": stype,
            "narrative": narr, "indicator": ind, "source": src,
            "auto": auto, "freq": freq, "threshold": thr,
            "data_kind": dkind, "data_ref": dref,
            "trigger": trig, "watches": watches,
        })
    return out


def run(conn, token: str | None, dry_run: bool) -> dict:
    switches = load_switches(conn, token)

    # 收集所有 watch 项
    watches = []
    for sw in switches:
        for w in sw["watches"]:
            if w.get("kind"):
                watches.append(w)

    if dry_run:
        metrics, errors, notes = {}, {}, {}
    else:
        metrics, errors, notes = fetch_all(watches)

    triggered, pending, not_trig, no_data, manual, unsupported = [], [], [], [], [], []

    for sw in switches:
        trig = sw["trigger"]
        if trig is None:
            # 无结构化条件 → 人工/半自动项，进日历
            manual.append(sw)
            continue

        kind = sw.get("data_kind")
        if kind in UNSUPPORTED:
            unsupported.append(sw)
            continue

        # 汇总该开关的指标
        m: dict = {}
        for w in sw["watches"]:
            k = w.get("kind")
            ref = w.get("symbol") or w.get("slug")
            got = metrics.get(f"{k}:{ref}")
            if got:
                for mk, mv in got.items():
                    if not mk.startswith("_"):
                        m.setdefault(mk, mv)

        res = evaluate(trig, m)
        sw = dict(sw)
        sw["eval"] = res
        sw["metrics"] = {k: v for k, v in m.items()}
        state = res["state"]
        {"triggered": triggered, "pending": pending,
         "not_triggered": not_trig, "no_data": no_data}[state].append(sw)

    return {
        "as_of": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "method": METHOD_VERSION,
        "counts": {
            "total": len(switches), "triggered": len(triggered),
            "pending": len(pending), "not_triggered": len(not_trig),
            "no_data": len(no_data), "manual": len(manual),
            "unsupported": len(unsupported),
        },
        "triggered": triggered, "pending": pending,
        "not_triggered": not_trig, "no_data": no_data,
        "manual": manual, "unsupported": unsupported,
        "fetch_errors": errors,
        "data_notes": notes,
        "n_watches": len(watches),
    }


def fm(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "✅" if v else "✗"
    if isinstance(v, float):
        if abs(v) < 0.01 and v != 0:
            return f"{v:.6f}"
        return f"{v:,.{digits}f}"
    return str(v)


def fmt_conds(sw: dict) -> str:
    parts = []
    for c in sw["eval"]["conds"]:
        left = fm(c["left"])
        right = fm(c["right"]) if c["right"] is not None else "—"
        mark = "✅" if c["ok"] else ("?" if c["ok"] is None else "✗")
        parts.append(f"{c['metric']} {left} {c['op']} {right} {mark}")
    return " ｜ ".join(parts)


def render_md(res: dict) -> str:
    c = res["counts"]
    L = []
    L.append(f"# 代币埋伏监控日报 · {res['as_of']}")
    L.append("")
    L.append(f"> 口径 `{res['method']}` ｜ 真相源 `sop.sop_switches` ｜ 抓取 {res['n_watches']} 个数据点")
    L.append("")
    L.append("**⚠️ 本报告只是「值得看一眼」，不是买入信号。下单前必须重抓实时价。**")
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("| 状态 | 数量 |")
    L.append("|---|---:|")
    L.append(f"| 已触发 | {c['triggered']} |")
    L.append(f"| 接近触发 | {c['pending']} |")
    L.append(f"| 未触发 | {c['not_triggered']} |")
    L.append(f"| 数据缺失 | {c['no_data']} |")
    L.append(f"| 人工/事件项 | {c['manual']} |")
    L.append(f"| 待接入数据源 | {c['unsupported']} |")
    L.append(f"| **合计** | **{c['total']}** |")
    L.append("")

    if res["triggered"]:
        L.append("## 🟢 已触发（值得看一眼）")
        L.append("")
        L.append("| 开关 | 币 | 类型 | 条件求值 | 逻辑 |")
        L.append("|---|---|---|---|---|")
        for sw in res["triggered"]:
            L.append(f"| {sw['switch_id']} | {sw['token']} | {sw['switch_type']} "
                     f"| {fmt_conds(sw)} | {sw['narrative'] or ''} |")
        L.append("")

    if res["pending"]:
        L.append("## 🟡 接近触发")
        L.append("")
        L.append("| 开关 | 币 | 条件求值 | 逻辑 |")
        L.append("|---|---|---|---|")
        for sw in res["pending"]:
            L.append(f"| {sw['switch_id']} | {sw['token']} | {fmt_conds(sw)} "
                     f"| {sw['narrative'] or ''} |")
        L.append("")

    if res["no_data"]:
        L.append("## ⚪ 数据缺失（不猜）")
        L.append("")
        for sw in res["no_data"]:
            L.append(f"- `{sw['switch_id']}` {sw['token']} — {sw['threshold'] or ''}"
                     f"（指标：{sw['indicator'] or '—'}）")
        L.append("")

    if res.get("data_notes"):
        L.append("## 🔍 数据源备注（取到但无值/已自动解析）")
        L.append("")
        for k, v in list(res["data_notes"].items())[:20]:
            L.append(f"- `{k}` — {v}")
        L.append("")

    if res["manual"]:
        L.append(f"## 📅 人工/事件项（{len(res['manual'])} 条，需自己盯日历）")
        L.append("")
        L.append("| 开关 | 币 | 频率 | 触发条件 |")
        L.append("|---|---|---|---|")
        for sw in res["manual"]:
            L.append(f"| {sw['switch_id']} | {sw['token']} | {sw['freq'] or '—'} "
                     f"| {sw['threshold'] or '—'} |")
        L.append("")

    if res["unsupported"]:
        kinds = sorted({sw.get("data_kind") for sw in res["unsupported"] if sw.get("data_kind")})
        L.append(f"## 🔧 待接入数据源（{len(res['unsupported'])} 条）")
        L.append("")
        L.append("以下开关的 `data_kind` 本脚本尚未实现取数，**未参与判读**（不是「未触发」）：")
        L.append("")
        L.append("`" + "`, `".join(kinds) + "`")
        L.append("")

    if res["fetch_errors"]:
        L.append("## ⚠️ 抓取失败")
        L.append("")
        for k, v in list(res["fetch_errors"].items())[:20]:
            L.append(f"- `{k}` — {v}")
        L.append("")

    L.append("---")
    L.append("")
    L.append("> 未触发项已省略。全量见 `--json`。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="代币埋伏监控（sop_switches 重建版）")
    ap.add_argument("--digest", action="store_true", help="生成日报 markdown")
    ap.add_argument("--pending", action="store_true", help="只看接近触发")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--token")
    ap.add_argument("--dry-run", action="store_true", help="只读表不抓网络")
    ap.add_argument("--out-dir")
    args = ap.parse_args()

    with get_connection(get_settings(require_database=True).database_url) as conn:
        res = run(conn, args.token, args.dry_run)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.pending:
        print(f"接近触发 {len(res['pending'])} / 已触发 {len(res['triggered'])} "
              f"/ 合计 {res['counts']['total']}")
        for sw in res["pending"] + res["triggered"]:
            flag = "🟢" if sw["eval"]["state"] == "triggered" else "🟡"
            print(f"{flag} {sw['switch_id']:<12} {sw['token']:<6} {fmt_conds(sw)}")
            if sw["narrative"]:
                print(f"    {sw['narrative']}")
        return 0

    md = render_md(res)
    if args.digest:
        out_dir = Path(args.out_dir) if args.out_dir else \
            SCRIPT_DIR.parent / "data" / "sop" / "digest"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{datetime.now(CST).strftime('%Y-%m-%d')}.md"
        path.write_text(md, encoding="utf-8")
        print(f"[digest] 已写入 {path}")
        print(f"[digest] 已触发 {res['counts']['triggered']} ｜ "
              f"接近 {res['counts']['pending']} ｜ 数据缺失 {res['counts']['no_data']} ｜ "
              f"人工项 {res['counts']['manual']} ｜ 待接入 {res['counts']['unsupported']}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
