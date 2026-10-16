#!/usr/bin/env python3
"""交易所净流异动扫描告警（基于剂量-反应回测结论落地，2026-10-07）。

背景（workbench/backtest_netflow_threshold.py，2026-10-07）：
  - 净流对价格**无独立方向预测力**；唯一显著关系 = 「单日净流 ≥5% 市值」的
    事件日，次日大概率跑输 BTC ≈2%（t≈2.2）——本质是事件日暴涨后的均值回归。
  - 故本扫描**不做多空断言**，只做：
      ① 异动提示（谁、多大净流、占市值多少、方向）
      ② 事件归因（近 7d 催化剂/解锁/上币 → 区分「事件型充提」vs「纯交易型充提」）
      ③ 风控提醒（极端净流日「次日勿追高」）
  - 净流/市值占比是可比口径；绝对金额受市值主导（无单调阈值）。

数据与口径：
  - 净流：biz.onchain_netflow_hourly（inflow−outflow，正=净流入交易所=抛压偏多）
  - 市值：core.asset.market_cap（最近快照，净流占比 = 窗口净流 / market_cap）
  - 窗口：默认滚动 24h 累计净流（事件日口径），排除进行中的当前小时（桶边界稳定）
  - 过滤：剔除稳定币/封装/质押/LP/合成等无信号噪音资产
  - 冷却：同资产在 --cooldown-hours 内已告警则跳过（biz.netflow_alert_log 留痕）

告警档位（对齐回测阈值）：
  - strong：|net_ratio| ≥ 0.05（5% 市值，事件日级，强提示）
  - watch ：|net_ratio| ≥ 0.01（1% 市值，关注级）

用法：
    python scan_netflow_alert.py                     # 默认窗口 24h，冷却 24h
    python scan_netflow_alert.py --window-hours 6    # 短窗口（小时级异动）
    python scan_netflow_alert.py --cooldown-hours 6  # 缩短冷却
    python scan_netflow_alert.py --dry-run           # 只打印，不发邮件不落库
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.notifier import EmailNotifier  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 事件归因（同目录模块，catalyst_attribution.py；scan_daemon 同样直接 import）
try:
    import catalyst_attribution  # noqa: E402
except Exception:  # noqa: BLE001 - 归因模块缺失时降级为「无归因」
    catalyst_attribution = None

# 噪音资产：稳定币 + 封装/质押/LP/合成 + 股票代币 + 已知往返壳（对齐 workbench/onchain_alert.py）
_NOISE_ASSET_TYPES = {"stablecoin", "wrapped", "lp_token", "synthetic"}
_NOISE_CATEGORIES = {"Tokenized Stock", "Tokenized Assets"}  # 股票代币（BNB/Solana 链 RWA）
_NOISE_SYMBOLS = {
    "STETH", "WSTETH", "CBETH", "CBBTC", "BBSOL", "SOLVBTC", "XAUT", "PAXG",
    "SNDKB", "SNDK", "BTCB", "WBTC", "WETH", "USDB", "EURT",
}
# 股票代币符号后缀（BNB 链 ~B / Solana ~x，如 GOOGLB/PLTRB/IRENx）：
# 同一 symbol 常挂多个 asset_id（coin/token 各一条），categories 只在一行上有，
# 故按 asset_id 过滤会漏 —— 用符号级正则兜底。
_STOCK_SUFFIX_RE = r"^[A-Z0-9]{2,}[BX]$"

WINDOW_H_DEFAULT = 24
COOLDOWN_H_DEFAULT = 24
STRONG_RATIO = 0.05   # ≥5% 市值 = 事件日级
WATCH_RATIO = 0.01    # ≥1% 市值 = 关注级
MIN_MCAP_USD = 1e6    # 市值下限：剔除极微盘噪音
MIN_NETFLOW_USD = 50e3  # 净流绝对金额下限：剔除过小金额的占比虚高


def _fmt_usd(x: float) -> str:
    ax = abs(x)
    if ax >= 1e9:
        return f"{x / 1e9:,.2f}B"
    if ax >= 1e6:
        return f"{x / 1e6:,.2f}M"
    if ax >= 1e3:
        return f"{x / 1e3:,.0f}K"
    return f"{x:,.0f}"


def scan(conn, window_h: int) -> list[dict]:
    """扫描窗口内净流异动，返回告警行列表（已过滤噪音 + 冷却）。"""
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
        # 窗口：最近 window_h 小时（不含进行中当前小时）；净流按资产累计
        cur.execute(
            """
            WITH nf AS (
                SELECT asset_id,
                       SUM(netflow_usd) AS netflow
                FROM biz.onchain_netflow_hourly
                WHERE bucket_hour >= date_trunc('hour', NOW()) - %s::interval
                  AND bucket_hour <  date_trunc('hour', NOW())
                GROUP BY 1
            ),
            a AS (
                SELECT asset_id, canonical_symbol, market_cap,
                       asset_type, categories
                FROM core.asset
                WHERE market_cap IS NOT NULL AND market_cap > 0
            )
            SELECT nf.asset_id, a.canonical_symbol, nf.netflow,
                   nf.netflow / a.market_cap AS ratio,
                   a.market_cap
            FROM nf JOIN a USING (asset_id)
            WHERE ABS(nf.netflow) / a.market_cap >= %s
              AND ABS(nf.netflow) >= %s
              AND a.market_cap >= %s
            ORDER BY ABS(nf.netflow) / a.market_cap DESC
            """,
            (timedelta(hours=window_h), WATCH_RATIO, MIN_NETFLOW_USD, MIN_MCAP_USD),
        )
        rows = cur.fetchall()

        # 噪音过滤（asset_type / categories 需二次查询，SQL 侧只带了 symbol/market_cap）
        ids = [r[0] for r in rows]
        noise_types: dict[int, str] = {}
        noise_syms: dict[int, str] = {}
        noise_cats: dict[int, set[str]] = {}
        if ids:
            cur.execute(
                "SELECT asset_id, asset_type, canonical_symbol, categories "
                "FROM core.asset WHERE asset_id = ANY(%s::int[])",
                (ids,),
            )
            for aid, atype, sym, cats in cur.fetchall():
                if atype in _NOISE_ASSET_TYPES:
                    noise_types[aid] = atype
                if sym and sym.upper() in _NOISE_SYMBOLS:
                    noise_syms[aid] = sym
                if cats and any(c in _NOISE_CATEGORIES for c in cats):
                    noise_cats[aid] = set(cats)

        # 冷却：查已告警记录
        cooldown: set[int] = set()
        if ids:
            cur.execute(
                "SELECT DISTINCT asset_id FROM biz.netflow_alert_log "
                "WHERE alerted_at >= NOW() - %s::interval",
                (timedelta(hours=COOLDOWN_H_DEFAULT),),
            )
            cooldown = {r[0] for r in cur.fetchall()}

    alerts: list[dict] = []
    for aid, sym, netflow, ratio, mcap in rows:
        if aid in noise_types:
            continue
        if aid in noise_syms:
            continue
        if aid in noise_cats:  # 股票代币 / Tokenized Assets（RWA）异动非加密信号
            continue
        if re.match(_STOCK_SUFFIX_RE, sym or ""):  # 符号级兜底：B/x 后缀股票代币
            continue
        if aid in cooldown:
            continue
        level = "strong" if abs(ratio) >= STRONG_RATIO else "watch"
        alerts.append({
            "asset_id": aid, "symbol": sym,
            "netflow": float(netflow), "ratio": float(ratio),
            "mcap": float(mcap), "level": level,
        })

    # 同 symbol 多 asset_id 去重（core.asset 存在同名多行，如 TREE×2/VAI×3）：
    # 告警语义按「币」，保留 |占比| 最大的一行，避免重复告警与归因覆盖。
    best: dict[str, dict] = {}
    for a in alerts:
        prev = best.get(a["symbol"])
        if prev is None or abs(a["ratio"]) > abs(prev["ratio"]):
            best[a["symbol"]] = a
    return list(best.values())


# ── 事件归因（区分「事件型充提」vs「纯交易型充提」）──────────────
# 回测结论：净流对价格无方向预测力，「≥5% 市值事件日」次日跑输 BTC ≈2% 系
# 事件日暴涨后的均值回归。故告警须区分该净流是否伴随事件（催化剂/解锁/上币）：
#   - 事件型充提：近 7d 有自身催化剂 或 有解锁/上币事件 —— 充提是事件的伴随
#   - 纯交易型充提：无任何事件 —— 更接近「资金行为」，信号更干净
_EVENT_TYPES = {  # ai_event_type 中判定为「事件」的类型（排除 market_update/other 等噪音）
    "listing", "unlock", "delisting", "funding", "partnership", "tech_upgrade",
    "security", "burn", "mint", "regulation",
}
_ATTR_WINDOW_H = 7 * 24  # 事件窗口：近 7 天


def enrich_events(conn, alerts: list[dict]) -> list[dict]:
    """为告警行补充事件归因（近 7d 催化剂 / 解锁 / 上币），返回增强后的行。

    依赖 catalyst_attribution.attribution_for_many（同目录模块，批量查询）；
    模块缺失时降级为无归因（label=pure_move，events 为空）。
    """
    if catalyst_attribution is None:
        for a in alerts:
            a["attr"] = {"label": "pure_move", "own": [], "peers": [],
                         "own_n": 0, "peer_n": 0, "sector": None,
                         "events": [], "unlocks": [], "listings": []}
        return alerts

    import psycopg.rows
    from collections import defaultdict

    ids = [a["asset_id"] for a in alerts]
    # 1) 催化剂归因（复用既有批量实现：自身 48h + 同赛道 24h）
    attr_map = catalyst_attribution.attribution_for_many(
        conn, [(a["symbol"], a["asset_id"]) for a in alerts])

    # 2) 近 7d 事件催化剂（自身，含解锁/上币/融资等，窗口 _ATTR_WINDOW_H）
    ev_map: dict[int, list[dict]] = defaultdict(list)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """SELECT ac.asset_id, ac.title_cn, ac.title, ac.published_at,
                      ac.ai_event_type, ac.ai_sentiment
               FROM biz.asset_catalyst ac
               WHERE ac.asset_id = ANY(%s)
                 AND ac.published_at >= NOW() - make_interval(hours => %s)
               ORDER BY ac.published_at DESC""",
            (ids, _ATTR_WINDOW_H))
        for r in cur.fetchall():
            title = (r["title_cn"] or r["title"] or "").strip()
            if not title:
                continue
            ev_map[r["asset_id"]].append({
                "title": title, "published_at": r["published_at"],
                "type": r["ai_event_type"], "sentiment": r["ai_sentiment"],
            })

    # 3) 解锁事件（近 7d 内已解锁 / 未来 7d 将解锁）
    ul_map: dict[int, list[dict]] = defaultdict(list)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """SELECT asset_id, unlock_date, unlock_value_usd,
                      unlock_ratio_circulating, risk_level, unlock_type
               FROM biz.asset_unlock_event
               WHERE asset_id = ANY(%s)
                 AND unlock_date BETWEEN NOW() - INTERVAL '7 days'
                                     AND NOW() + INTERVAL '7 days'
               ORDER BY unlock_date""",
            (ids,))
        for r in cur.fetchall():
            ul_map[r["asset_id"]].append({
                "date": r["unlock_date"], "value_usd": r["unlock_value_usd"],
                "ratio": r["unlock_ratio_circulating"], "risk": r["risk_level"],
                "unlock_type": r["unlock_type"],
            })

    # 4) 上币事件（近 7d，ai_event_type='listing'）
    ls_map: dict[int, list[dict]] = defaultdict(list)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """SELECT asset_id, title_cn, title, published_at
               FROM biz.asset_catalyst
               WHERE asset_id = ANY(%s)
                 AND ai_event_type = 'listing'
                 AND published_at >= NOW() - make_interval(hours => %s)
               ORDER BY published_at DESC""",
            (ids, _ATTR_WINDOW_H))
        for r in cur.fetchall():
            title = (r["title_cn"] or r["title"] or "").strip()
            if not title:
                continue
            ls_map[r["asset_id"]].append(
                {"title": title, "published_at": r["published_at"]})

    out: list[dict] = []
    for a in alerts:
        aid = a["asset_id"]
        attr = attr_map.get(a["symbol"], {})
        events = ev_map.get(aid, [])
        unlocks = ul_map.get(aid, [])
        listings = ls_map.get(aid, [])
        # 事件判定：自身催化剂（48h 归因层）+ 近 7d 事件催化剂 + 解锁 + 上币
        event_own = [e for e in events if (e["type"] or "") in _EVENT_TYPES]
        is_event = bool(attr.get("own_n") or event_own or unlocks or listings)
        a["attr"] = {
            **attr,
            "is_event": is_event,
            "event_kind": "事件型充提" if is_event else "纯交易型充提",
            "events": events[:3],
            "unlocks": unlocks[:2],
            "listings": listings[:2],
        }
        out.append(a)
    return out


def _short_title(s: str, maxlen: int = 28) -> str:
    """事件标题 → 短标签（首 28 字 + 省略号）。"""
    s = (s or "").strip()
    if len(s) <= maxlen:
        return s
    return s[:maxlen - 1] + "…"


# 事件类型 → 中文短标签（邮件归因列 13px 行内展示，需短）
_EVENT_TYPE_CN = {
    "listing": "上币", "delisting": "下币", "funding": "融资",
    "partnership": "合作", "tech_upgrade": "技术升级", "audit": "审计",
    "regulation": "监管", "hack": "安全事件", "burn": "销毁",
    "rebranding": "改名", "fork": "分叉", "airdrop": "空投",
    "nft_event": "NFT", "community": "社区", "team": "团队",
    "market_maker": "做市", "macro": "宏观", "unlock": "解锁",
    "roadmap": "路线图", "competitor": "竞品", "meme": "meme",
}


def _fmt_attr(a: dict) -> str:
    """归因 → 中文摘要（行内展示，带具体事件标题）。"""
    attr = a.get("attr") or {}
    if not attr:
        return ""
    if not attr.get("is_event"):
        return attr.get("event_kind", "纯交易型充提")

    parts: list[str] = []

    # 1) 催化剂事件（取前 2 条，最相关的放前面）
    events = attr.get("events") or []
    own_n = attr.get("own_n") or 0
    if events:
        for e in events[:2]:
            t_cn = _EVENT_TYPE_CN.get(e.get("type") or "", e.get("type") or "事件")
            ttl = _short_title(e.get("title", ""))
            dt = e.get("published_at")
            ds = f"{dt:%m-%d}" if dt else ""
            parts.append(f"{t_cn}·{ttl}({ds})")
        # 若还有更多，末尾补计数
        if len(events) > 2:
            parts[-1] += f" 等{len(events)}条"
    elif own_n:
        # 仅 own_n 有计数而 events 为空（归因层的 48h 同赛道推断，不一定有自身事件）
        parts.append(f"同赛道催化剂×{own_n}")

    # 2) 解锁事件
    for u in (attr.get("unlocks") or [])[:2]:
        ds = u["date"].strftime("%m-%d") if hasattr(u.get("date"), "strftime") else str(u.get("date", ""))
        ratio = u.get("ratio")
        ratio_s = f"占流通{ratio*100:.1f}%" if ratio is not None else ""
        risk = u.get("risk") or ""
        risk_s = f", {risk}" if risk else ""
        utype = u.get("unlock_type") or ""
        parts.append(f"解锁{ds}{ratio_s}{risk_s}{(' [' + utype + ']') if utype else ''}")

    # 3) 上币事件
    for ls in (attr.get("listings") or [])[:2]:
        dt = ls.get("published_at")
        ds = f"{dt:%m-%d}" if dt else ""
        parts.append(f"上币·{_short_title(ls.get('title',''))}({ds})")

    # 若三者都为空但 is_event=True（理论不该发生，留兜底）
    if not parts:
        parts.append("近7d有事件")

    # 事件型充提 前缀，后跟换行 + 要点（HTML 渲染为行内，但 <br> 后可换行）
    kind = attr.get("event_kind", "事件型充提")
    return f"<b>{kind}</b><br>" + "<br>".join(f"· {p}" for p in parts)


def _build_html(alerts: list[dict], window_h: int) -> str:
    """构建告警邮件 HTML（语义=异动提示+风控，不做多空断言）。"""
    now = datetime.now(timezone.utc)
    rows_html = []
    for a in alerts:
        direction = "净流入交易所" if a["netflow"] > 0 else "净流出交易所"
        color = "#dc2626" if a["level"] == "strong" else "#b45309"
        badge = "事件日级" if a["level"] == "strong" else "关注级"
        attr = a.get("attr") or {}
        is_event = attr.get("is_event", False)
        attr_color = "#b45309" if is_event else "#2563eb"
        attr_txt = _fmt_attr(a)
        rows_html.append(f"""
        <tr>
          <td style="padding:8px;border:1px solid #eee"><b>{a['symbol']}</b></td>
          <td style="padding:8px;border:1px solid #eee">{_fmt_usd(a['netflow'])}</td>
          <td style="padding:8px;border:1px solid #eee">{direction}</td>
          <td style="padding:8px;border:1px solid #eee;color:{color}"><b>{a['ratio']*100:.1f}%</b></td>
          <td style="padding:8px;border:1px solid #eee">{_fmt_usd(a['mcap'])}</td>
          <td style="padding:8px;border:1px solid #eee"><span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px">{badge}</span></td>
          <td style="padding:8px;border:1px solid #eee;color:{attr_color}"><b>{attr_txt}</b></td>
        </tr>""")
    return f"""
    <div style="font-family:sans-serif;max-width:840px;margin:auto">
      <h2 style="color:#b45309">🌊 交易所净流异动扫描</h2>
      <p>窗口：近 {window_h}h（截至 {now:%Y-%m-%d %H:%M} UTC）· 命中 {len(alerts)} 个资产</p>
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <tr style="background:#f5f5f5">
          <th style="padding:8px;border:1px solid #eee;text-align:left">代币</th>
          <th style="padding:8px;border:1px solid #eee;text-align:left">净流</th>
          <th style="padding:8px;border:1px solid #eee;text-align:left">方向</th>
          <th style="padding:8px;border:1px solid #eee;text-align:left">占市值</th>
          <th style="padding:8px;border:1px solid #eee;text-align:left">市值</th>
          <th style="padding:8px;border:1px solid #eee;text-align:left">级别</th>
          <th style="padding:8px;border:1px solid #eee;text-align:left">归因</th>
        </tr>
        {''.join(rows_html)}
      </table>
      <p style="color:#666;margin-top:16px">
        ⚠️ <b>风控提示</b>：回测显示单日净流 ≥5% 市值的「事件日」次日大概率跑输 BTC ≈2%（t≈2.2），
        系事件日暴涨后的均值回归。<br>
        <b>归因口径</b>：「事件型充提」= 近 7 天有催化剂/解锁/上币事件（充提是事件的伴随）；
        「纯交易型充提」= 无事件（更接近资金行为）。<br>
        本告警仅提示异动，<b>不代表看多/看空方向</b>——极端净流日请勿追高。
      </p>
    </div>
    """


def main() -> int:
    ap = argparse.ArgumentParser(description="交易所净流异动扫描告警")
    ap.add_argument("--window-hours", type=int, default=WINDOW_H_DEFAULT,
                    help=f"净流聚合窗口小时数，默认 {WINDOW_H_DEFAULT}")
    ap.add_argument("--cooldown-hours", type=int, default=COOLDOWN_H_DEFAULT,
                    help=f"同资产冷却小时数，默认 {COOLDOWN_H_DEFAULT}")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发邮件不落库")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        alerts = scan(conn, args.window_hours)
        if alerts:
            alerts = enrich_events(conn, alerts)

    if not alerts:
        print(f"[netflow-alert] 近 {args.window_hours}h 无净流异动（≥{WATCH_RATIO*100:.0f}% 市值）")
        return 0

    strong = [a for a in alerts if a["level"] == "strong"]
    n_event = sum(1 for a in alerts if (a.get("attr") or {}).get("is_event"))
    print(f"[netflow-alert] 命中 {len(alerts)} 个（strong={len(strong)}, watch={len(alerts)-len(strong)}"
          f"，事件型={n_event}, 纯交易={len(alerts)-n_event}）：")
    for a in alerts:
        tag = "🔴" if a["level"] == "strong" else "🟠"
        print(f"  {tag} {a['symbol']:<10} 净流 {_fmt_usd(a['netflow']):>10} "
              f"占比 {a['ratio']*100:+6.2f}%  市值 {_fmt_usd(a['mcap']):>8}  {_fmt_attr(a)}")

    if args.dry_run:
        print("[dry-run] 未发送邮件、未落库")
        return 0

    # 落库冷却记录（不阻断发信）
    try:
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                for a in alerts:
                    cur.execute(
                        "INSERT INTO biz.netflow_alert_log "
                        "(asset_id, window_h, netflow_usd, net_ratio, level) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (asset_id, alerted_at) DO NOTHING",
                        (a["asset_id"], args.window_hours, a["netflow"], a["ratio"], a["level"]),
                    )
    except Exception as exc:  # noqa: BLE001 - 冷却落库失败不阻断告警
        print(f"[netflow-alert] 冷却落库失败（跳过）: {exc}")

    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("[WARN] SMTP 未配置，跳过发送")
        return 0
    html = _build_html(alerts, args.window_hours)
    ok, msg = notifier.send(
        f"🌊 净流异动：{len(alerts)} 个资产（strong={len(strong)}）",
        html,
        from_name="净流异动扫描",
    )
    print(f"[netflow-alert] 邮件: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
