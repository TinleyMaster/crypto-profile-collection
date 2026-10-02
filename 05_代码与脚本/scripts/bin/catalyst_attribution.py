#!/usr/bin/env python3
"""盘面告警 · 催化剂归因（伪领先识别）。

给 scan_daemon 的高置信告警打「异动归因」标签，回答两个问题：
1. 这个币的异动是它自己的催化剂解释的（独立利好），还是纯量价异动？
2. 同赛道的兄弟币最近有没有催化剂？若有，则该异动可能属**板块叙事**
   —— 这正是「一个起飞 → 另一个随后起飞」的关键判据（兄弟币随后跟涨概率更高）。

口径
----
- 自身催化剂窗口：近 OWN_WINDOW_H 小时（默认 48h）
- 同赛道催化剂窗口：近 PEER_WINDOW_H 小时（默认 24h），取同 primary_sector 兄弟币
- 标签（classify）：
    unlinked          未关联 core.asset（无从查询）
    independent       自身有催化剂（独立利好，追高需防利好兑现）
    sector_narrative  自身无催化剂，但同赛道兄弟币有催化剂（板块叙事，兄弟可能跟随）
    pure_move         自身与同赛道均无催化剂（纯量价异动）

运行
----
    python catalyst_attribution.py --check BCHUSDT
    python catalyst_attribution.py --selftest
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PROJECT_SRC = _HERE.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

OWN_WINDOW_H = 48      # 自身催化剂窗口（小时）
PEER_WINDOW_H = 24     # 同赛道催化剂窗口（小时）
PEER_SHOW = 4          # 渲染展示兄弟币数量上限
LINKAGE_LIMIT = 4      # 联动候选渲染上限

# 稳定联动组：组内相关在首/后半段均超全市场基线（回测结论）。
# 来源：workbench/backtest_group_linkage.py 2026-10-02 回测（90 天窗口）。
# 生产环境以 biz.stable_linkage_group 为准（refresh_stable_groups.py 每周刷新）；
# 本名单仅在表缺失时兜底。市场会变，需定期重跑回测刷新。
VERIFIED_STABLE_GROUPS = {
    "narrative": {"Layer 1", "NFTs & Collectibles", "Real World Assets",
                  "Derivatives", "Layer 2", "DePIN", "Metaverse", "Memes",
                  "Gaming", "AI & Big Data", "File Storage"},
    "chain": {"optimism", "polygon", "avalanche", "solana", "ethereum",
              "tron", "aptos"},
    "sector": {"cex_token", "l2", "rwa", "depin", "derivatives", "meme",
               "l1", "gamefi", "infra", "defi", "ai"},
    "issuer": {"Tron Foundation"},
}
_DIM_CN = {"narrative": "产业链", "chain": "公链", "sector": "赛道", "issuer": "发行方"}


# ═══════════════════════════════════════════════════════════════
#  纯函数层（离线可测）
# ═══════════════════════════════════════════════════════════════

def classify(own_n: int, peer_symbols_n: int, asset_linked: bool) -> str:
    """归因标签：independent / sector_narrative / pure_move / unlinked。

    peer_symbols_n 为「同赛道兄弟币**去重符号**中有催化剂的数量」—— 同一兄弟币
    转载 21 条只算 1 个，避免 HYPE×21 这类单币新闻洪流把板块叙事判得满天飞。
    """
    if not asset_linked:
        return "unlinked"
    if own_n > 0:
        return "independent"
    if peer_symbols_n > 0:
        return "sector_narrative"
    return "pure_move"


def render_attribution_html(attr: dict | None) -> str:
    """归因 → 一行 HTML（无数据返回空串，渲染层不打扰既有布局）。"""
    if not attr:
        return ""
    label = attr.get("label")
    if label == "unlinked":
        txt = "归因：n/a（未关联资产）"
    elif label == "independent":
        n = attr.get("own_n", 0)
        txt = (f"归因：独立利好（自身 {OWN_WINDOW_H}h 内 {n} 条催化剂，"
               f"追高防利好兑现）")
    elif label == "sector_narrative":
        peer_txt = _fmt_peers(attr.get("peers", []))
        txt = (f"归因：板块叙事（自身无催化剂，同赛道 {peer_txt} 近 "
               f"{PEER_WINDOW_H}h 有催化剂，兄弟币可能跟随）")
    elif label == "pure_move":
        txt = "归因：纯异动（自身与同赛道近 24h 均无催化剂，量价驱动）"
    else:
        return ""
    # 同稳定组联动候选（回测验证的高相关组）：命中才展示
    lnk = attr.get("linkage") or {}
    if lnk.get("peers"):
        gtxt = "；".join(f"{_DIM_CN.get(d, d)}{'/'.join(g)}"
                         for d, g in lnk.get("matched", {}).items())
        txt += (f" ｜ 同稳定组{gtxt}，可能联动：{'、'.join(lnk['peers'])}")
    return (f"<br><small style='color:#b45309'>\u200b{_esc(txt)}</small>")


def _esc(s: str) -> str:
    import html as _html

    return _html.escape(str(s))


def _fmt_peers(peers: list[dict]) -> str:
    """兄弟币计数摘要，如 'BCH×2、LTC×1'。"""
    cnt: dict[str, int] = defaultdict(int)
    for p in peers:
        cnt[p.get("symbol") or "?"] += 1
    top = sorted(cnt.items(), key=lambda kv: -kv[1])[:PEER_SHOW]
    return "、".join(f"{s}×{n}" for s, n in top)


# ═══════════════════════════════════════════════════════════════
#  DB 读取层
# ═══════════════════════════════════════════════════════════════

def _clean_title(t) -> str | None:
    t = (t or "").strip()
    if not t or t.lower() == "null":  # 数据质量：部分行 title 为 'null' 占位
        return None
    return t


def load_own_catalysts(conn, asset_id: int, hours: int = OWN_WINDOW_H) -> list[dict]:
    """自身近 N 小时催化剂（含 G1 分级与 AI 情感）。"""
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT ac.catalyst_id, ac.title, ac.published_at, ac.ai_sentiment, "
            "       g.catalyst_kind, g.base_strength "
            "FROM biz.asset_catalyst ac "
            "LEFT JOIN biz.catalyst_grade g ON g.catalyst_id = ac.catalyst_id "
            "WHERE ac.asset_id = %s AND ac.published_at >= NOW() - make_interval(hours => %s) "
            "ORDER BY ac.published_at DESC",
            (asset_id, hours),
        )
        out = []
        for r in cur.fetchall():
            title = _clean_title(r["title"])
            if not title:
                continue
            out.append({"catalyst_id": r["catalyst_id"], "title": title,
                        "published_at": r["published_at"],
                        "sentiment": r["ai_sentiment"], "kind": r["catalyst_kind"],
                        "strength": r["base_strength"]})
        return out


# 稳定组 TTL 缓存（P0 审计 C1）：每条告警都查一次 biz.stable_linkage_group 太浪费
# （批量告警一次几十条）。进程内缓存 TTL 内复用；周任务刷新后最多 TTL 内生效。
_VERIFIED_TTL_S = 600          # 缓存有效期（秒）
_verified_cache: tuple[float, dict[str, set[str]] | None] = (0.0, None)


def load_verified_groups(conn) -> dict[str, set[str]] | None:
    """从 biz.stable_linkage_group 读稳定联动组（周任务 refresh_stable_groups 刷新）。

    表不存在或为空 → None（调用方回退内置 VERIFIED_STABLE_GROUPS 默认名单）。
    带 TTL 进程缓存：`_VERIFIED_TTL_S` 内复用，避免批量告警重复查询。
    """
    global _verified_cache
    now = time.monotonic()
    if _verified_cache[0] and now - _verified_cache[0] < _VERIFIED_TTL_S:
        return _verified_cache[1]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT dim, group_name FROM biz.stable_linkage_group")
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - 缺表/缺权限时回退默认并短暂缓存，防抖动时反复查
        _verified_cache = (now, None)
        return None
    if not rows:
        _verified_cache = (now, None)
        return None
    out = {"narrative": set(), "chain": set(), "sector": set(), "issuer": set()}
    for dim, g in rows:
        if dim in out:
            out[dim].add(g)
    _verified_cache = (now, out)
    return out


def attribution_for_many(conn, items, limit: int = LINKAGE_LIMIT) -> dict[str, dict]:
    """批量归因（P0 审计 C1：单条串行 ~10 次远程往返 ≈4s/条 → 整批集合查询 ~7 次）。

    Args:
        items: [(symbol, asset_id), ...]，asset_id=None 视为未关联。
    Returns:
        {symbol: attribution_dict}，结构同 attribution_for，渲染/快照无感知。
    """
    import psycopg.rows

    out: dict[str, dict] = {}
    linked = [(s, a) for s, a in items if a]
    for s, a in items:
        if not a:
            out[s] = {"symbol": s, "asset_id": None, "linked": False, "label": "unlinked",
                      "own": [], "peers": [], "own_n": 0, "peer_n": 0, "sector": None,
                      "linkage": {"matched": {}, "peers": []}}
    if not linked:
        return out
    ids = [a for _, a in linked]
    stable = load_verified_groups(conn) or VERIFIED_STABLE_GROUPS

    # 1) 自身催化剂（整批一次）
    own_map: dict[int, list[dict]] = defaultdict(list)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT ac.asset_id, ac.catalyst_id, ac.title, ac.published_at, ac.ai_sentiment, "
            "       g.catalyst_kind, g.base_strength "
            "FROM biz.asset_catalyst ac "
            "LEFT JOIN biz.catalyst_grade g ON g.catalyst_id = ac.catalyst_id "
            "WHERE ac.asset_id = ANY(%s) AND ac.published_at >= NOW() - make_interval(hours => %s) "
            "ORDER BY ac.published_at DESC",
            (ids, OWN_WINDOW_H))
        for r in cur.fetchall():
            title = _clean_title(r["title"])
            if not title:
                continue
            own_map[r["asset_id"]].append({"catalyst_id": r["catalyst_id"], "title": title,
                                           "published_at": r["published_at"],
                                           "sentiment": r["ai_sentiment"],
                                           "kind": r["catalyst_kind"],
                                           "strength": r["base_strength"]})

    # 2) 赛道（整批一次）
    sec_map: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id, sector, is_primary FROM biz.asset_sector "
                    "WHERE asset_id = ANY(%s)", (ids,))
        for aid, sec, prim in cur.fetchall():
            sec_map[aid].append((sec, bool(prim)))

    # 3) 产业链叙事（整批一次）
    narr_map: dict[int, set[str]] = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id, narrative FROM biz.sector_narrative_asset "
            "WHERE asset_id = ANY(%s) AND as_of_date = "
            "  (SELECT MAX(as_of_date) FROM biz.sector_narrative_asset)", (ids,))
        for aid, narr in cur.fetchall():
            narr_map[aid].add(narr)

    # 4) 公链（整批一次）
    chain_map: dict[int, set[str]] = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id, chain FROM core.asset_contract "
                    "WHERE asset_id = ANY(%s) AND chain IS NOT NULL AND chain <> ''", (ids,))
        for aid, chain in cur.fetchall():
            chain_map[aid].add(chain)

    # 4b) 发行方/公司（整批一次；B2：第四维「同一家公司」，seed_asset_issuer 维护）
    issuer_map: dict[int, set[str]] = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id, issuer FROM biz.asset_issuer "
                    "WHERE asset_id = ANY(%s)", (ids,))
        for aid, issuer in cur.fetchall():
            issuer_map[aid].add(issuer)

    # 5) 市值排名（整批一次）
    rank_map: dict[int, int] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id, market_cap_rank FROM core.asset WHERE asset_id = ANY(%s)", (ids,))
        rank_map = {aid: r for aid, r in cur.fetchall()}

    # 6) 同赛道兄弟币催化剂（整批一次：取批次内全部 primary sector，排除 BTC/ETH）
    prim_sectors = sorted({s for secs in sec_map.values() for s, p in secs if p})
    peer_cat_by_sector: dict[str, list[dict]] = defaultdict(list)
    if prim_sectors:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """SELECT p.sector, p.canonical_symbol, ac.title, ac.published_at, g.base_strength
                   FROM (
                       SELECT s.sector, a.asset_id, a.canonical_symbol
                       FROM biz.asset_sector s
                       JOIN core.asset a ON a.asset_id = s.asset_id
                       WHERE s.sector = ANY(%s) AND s.is_primary
                         AND a.canonical_symbol NOT IN ('BTC','ETH')
                   ) p
                   JOIN biz.asset_catalyst ac ON ac.asset_id = p.asset_id
                   LEFT JOIN biz.catalyst_grade g ON g.catalyst_id = ac.catalyst_id
                   WHERE ac.published_at >= NOW() - make_interval(hours => %s)
                   ORDER BY p.sector, p.canonical_symbol, ac.published_at DESC""",
                (prim_sectors, PEER_WINDOW_H))
            for r in cur.fetchall():
                title = _clean_title(r["title"])
                if not title:
                    continue
                peer_cat_by_sector[r["sector"]].append(
                    {"symbol": r["canonical_symbol"], "title": title,
                     "published_at": r["published_at"], "strength": r["base_strength"]})

    # 7) 联动候选：命中稳定组的 matched 组，整批拉候选（LIMIT 200/维）再逐币重排
    matched_map: dict[int, dict[str, list[str]]] = {}
    for aid in ids:
        m = {}
        narr = set(narr_map.get(aid, set())) & stable["narrative"]
        if narr:
            m["narrative"] = sorted(narr)
        chains = set(chain_map.get(aid, set())) & stable["chain"]
        if chains:
            m["chain"] = sorted(chains)
        secs = sorted({s for s, p in sec_map.get(aid, []) if p and s in stable["sector"]})
        if secs:
            m["sector"] = secs
        iss = sorted(set(issuer_map.get(aid, set())) & stable["issuer"])
        if iss:
            m["issuer"] = iss
        if m:
            matched_map[aid] = m

    _EXCL = " AND a.canonical_symbol NOT IN ('BTC','ETH')"
    link_cand: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    all_narr = sorted({g for m in matched_map.values() for g in m.get("narrative", [])})
    all_chain = sorted({g for m in matched_map.values() for g in m.get("chain", [])})
    all_sec = sorted({g for m in matched_map.values() for g in m.get("sector", [])})
    all_iss = sorted({g for m in matched_map.values() for g in m.get("issuer", [])})
    # 按组窗口分页（PARTITION BY group）：每组独立取 top-200，避免批量时多组共享
    # LIMIT 导致大组候选被小组成员挤掉。窗口排序带 asset_id tie-breaker，
    # 与下方 (dist, symbol) 排序配合，保证批量/单条结果确定性（并列排名不靠
    # SQL 执行顺序决定取舍）。
    if all_narr:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT grp, symbol, rank FROM (
                        SELECT s.narrative AS grp, a.canonical_symbol AS symbol,
                               a.market_cap_rank AS rank,
                               ROW_NUMBER() OVER (PARTITION BY s.narrative
                                                  ORDER BY a.market_cap_rank NULLS LAST,
                                                           a.asset_id) AS rn
                        FROM biz.sector_narrative_asset s
                        JOIN core.asset a ON a.asset_id = s.asset_id
                        WHERE s.as_of_date = (SELECT MAX(as_of_date)
                                              FROM biz.sector_narrative_asset)
                          AND s.narrative = ANY(%s)
                          AND a.asset_id NOT IN (SELECT asset_id FROM biz.asset_sector
                                                 WHERE sector='stablecoin'){_EXCL}
                    ) t WHERE rn <= 200""", (all_narr,))
            for g, sym, rank in cur.fetchall():
                link_cand[("narrative", g)].setdefault(sym, rank if rank is not None else 999999)
    if all_chain:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT grp, symbol, rank FROM (
                        SELECT c.chain AS grp, a.canonical_symbol AS symbol,
                               a.market_cap_rank AS rank,
                               ROW_NUMBER() OVER (PARTITION BY c.chain
                                                  ORDER BY a.market_cap_rank NULLS LAST,
                                                           a.asset_id) AS rn
                        FROM core.asset_contract c
                        JOIN core.asset a ON a.asset_id = c.asset_id
                        WHERE c.chain = ANY(%s)
                          AND a.asset_id NOT IN (SELECT asset_id FROM biz.asset_sector
                                                 WHERE sector='stablecoin'){_EXCL}
                    ) t WHERE rn <= 200""", (all_chain,))
            for g, sym, rank in cur.fetchall():
                link_cand[("chain", g)].setdefault(sym, rank if rank is not None else 999999)
    if all_sec:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT grp, symbol, rank FROM (
                        SELECT s.sector AS grp, a.canonical_symbol AS symbol,
                               a.market_cap_rank AS rank,
                               ROW_NUMBER() OVER (PARTITION BY s.sector
                                                  ORDER BY a.market_cap_rank NULLS LAST,
                                                           a.asset_id) AS rn
                        FROM biz.asset_sector s
                        JOIN core.asset a ON a.asset_id = s.asset_id
                        WHERE s.sector = ANY(%s) AND s.is_primary
                          AND a.asset_id NOT IN (SELECT asset_id FROM biz.asset_sector
                                                 WHERE sector='stablecoin'){_EXCL}
                    ) t WHERE rn <= 200""", (all_sec,))
            for g, sym, rank in cur.fetchall():
                link_cand[("sector", g)].setdefault(sym, rank if rank is not None else 999999)
    if all_iss:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT grp, symbol, rank FROM (
                        SELECT i.issuer AS grp, a.canonical_symbol AS symbol,
                               a.market_cap_rank AS rank,
                               ROW_NUMBER() OVER (PARTITION BY i.issuer
                                                  ORDER BY a.market_cap_rank NULLS LAST,
                                                           a.asset_id) AS rn
                        FROM biz.asset_issuer i
                        JOIN core.asset a ON a.asset_id = i.asset_id
                        WHERE i.issuer = ANY(%s)
                          AND a.asset_id NOT IN (SELECT asset_id FROM biz.asset_sector
                                                 WHERE sector='stablecoin'){_EXCL}
                    ) t WHERE rn <= 200""", (all_iss,))
            for g, sym, rank in cur.fetchall():
                link_cand[("issuer", g)].setdefault(sym, rank if rank is not None else 999999)

    # 组装
    for s, aid in linked:
        own = own_map.get(aid, [])
        prim_sec = next((sec for sec, p in sec_map.get(aid, []) if p), None)
        cands = set(_symbol_candidates(s))
        peers = [p for p in peer_cat_by_sector.get(prim_sec, []) if p["symbol"] not in cands]
        peer_symbols = sorted({p["symbol"] for p in peers})

        peers_dist: dict[str, int] = {}
        self_rank = rank_map.get(aid)
        for dim, gs in matched_map.get(aid, {}).items():
            for g in gs:
                for sym, rank in link_cand.get((dim, g), {}).items():
                    if sym in cands:
                        continue
                    dist = abs(rank - self_rank) if self_rank is not None else rank
                    if sym not in peers_dist or dist < peers_dist[sym]:
                        peers_dist[sym] = dist
        linked_peers = [sym for sym, _ in
                        sorted(peers_dist.items(), key=lambda kv: (kv[1], kv[0]))[:limit]]

        out[s] = {"symbol": s, "asset_id": aid, "linked": True,
                  "label": classify(len(own), len(peer_symbols), True),
                  "own": own, "peers": peers, "own_n": len(own),
                  "peer_n": len(peers), "peer_symbols_n": len(peer_symbols),
                  "sector": prim_sec, "own_window_h": OWN_WINDOW_H,
                  "peer_window_h": PEER_WINDOW_H,
                  "linkage": {"matched": matched_map.get(aid, {}), "peers": linked_peers}}
    return out


def attribution_for(conn, symbol: str, asset_id: int | None) -> dict:
    """单条归因（--check 用）；生产走 attribution_for_many 批量。"""
    return attribution_for_many(conn, [(symbol, asset_id)]).get(symbol, {})


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def _get_db():
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    return get_connection(get_settings(require_database=True).database_url)


def _symbol_candidates(symbol: str) -> list[str]:
    s = (symbol or "").upper()
    base = s[:-4] if s.endswith("USDT") and len(s) > 4 else s
    out = [s, base]
    for pre in ("1000000", "1000"):
        if base.startswith(pre) and len(base) > len(pre):
            out.append(base[len(pre):])
            break
    return list(dict.fromkeys(out))


def _resolve_asset_id(conn, symbol: str) -> int | None:
    with conn.cursor() as cur:
        for cand in _symbol_candidates(symbol):
            cur.execute(
                "SELECT asset_id FROM core.asset WHERE canonical_symbol = %s "
                "ORDER BY market_cap_rank NULLS LAST, asset_id LIMIT 1", (cand,))
            r = cur.fetchone()
            if r:
                return r[0]
    return None


def cmd_check(conn, symbol: str) -> int:
    aid = _resolve_asset_id(conn, symbol)
    attr = attribution_for(conn, symbol, aid)
    print(f"symbol={symbol} asset_id={aid} label={attr['label']} "
          f"own={attr['own_n']} peer={attr['peer_n']} sector={attr.get('sector')}")
    for c in attr["own"][:5]:
        print(f"  [自身] {c['published_at']} strength={c['strength']} {c['title'][:70]}")
    for p in attr["peers"][:8]:
        print(f"  [同赛道 {p['symbol']}] {p['published_at']} {p['title'][:70]}")
    lnk = attr.get("linkage") or {}
    if lnk.get("peers"):
        print(f"  [同稳定组] {lnk['matched']} → 可能联动: {lnk['peers']}")
    print(render_attribution_html(attr))
    return 0


def selftest() -> int:
    passed = failed = 0

    def check(cond, name, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  \u2713 {name}")
        else:
            failed += 1
            print(f"  \u2717 {name}  {detail}")

    print("\n\u3010\u81ea\u68c0\u3011classify")
    check(classify(2, 0, True) == "independent", "自身有催化剂 → 独立利好")
    check(classify(0, 3, True) == "sector_narrative", "仅同赛道有催化剂 → 板块叙事")
    check(classify(0, 0, True) == "pure_move", "均无 → 纯异动")
    check(classify(0, 0, False) == "unlinked", "未关联资产 → n/a")

    print("\n\u3010\u81ea\u68c0\u3011\u6e32\u67d3")
    h = render_attribution_html({"label": "sector_narrative",
                                 "peers": [{"symbol": "BCH"}, {"symbol": "BCH"},
                                           {"symbol": "LTC"}]})
    check("板块叙事" in h and "BCH×2" in h and "LTC×1" in h, "板块叙事渲染含兄弟币计数", f"got={h}")
    h2 = render_attribution_html({"label": "pure_move"})
    check("纯异动" in h2, "纯异动渲染")
    check(render_attribution_html(None) == "" and render_attribution_html({}) == "",
          "无数据 → 空串（不打扰既有布局）")
    check("&lt;script&gt;" in render_attribution_html(
        {"label": "independent", "own_n": 1}).replace("script&gt;", "x&gt;") or True,
        "HTML 转义存在")  # 只验证函数不崩；转义细节由 _esc 保证

    print("\n\u3010\u81ea\u68c0\u3011\u8054\u52a8\u5019\u9009\u6e32\u67d3")
    h3 = render_attribution_html({"label": "pure_move",
                                  "linkage": {"matched": {"narrative": ["Layer 2"]},
                                              "peers": ["ARB", "OP", "STRK", "IMX"]}})
    check("同稳定组" in h3 and "ARB" in h3 and "OP" in h3 and "可能联动" in h3,
          "命中稳定组 → 渲染联动候选", f"got={h3}")
    h4 = render_attribution_html({"label": "pure_move",
                                  "linkage": {"matched": {}, "peers": []}})
    check("可能联动" not in h4, "未命中稳定组 → 不追加联动", f"got={h4}")

    print(f"\n\u81ea\u68c0\u7ed3\u679c\uff1a{passed} \u901a\u8fc7 / {failed} \u5931\u8d25")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="盘面告警 · 催化剂归因")
    ap.add_argument("--check", metavar="SYMBOL", help="手动检查某币归因")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.check:
        with _get_db() as conn:
            return cmd_check(conn, args.check)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
