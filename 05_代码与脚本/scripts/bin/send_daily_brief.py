#!/usr/bin/env python3
"""每日早报邮件发送（第六刀）。

流程：build_daily_brief.py 生成 brief dict → 渲染 HTML → EmailNotifier 发送。
scheduler.py 注册：daily_brief_email（09:00 Asia/Shanghai，在 daily_brief_snapshot 之后）。

用法：
    python send_daily_brief.py              # 生成 + 发送
    python send_daily_brief.py --dry-run    # 仅打印 HTML，不发送
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

# 路径设置：复用 build_daily_brief.py 的逻辑
_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
for cand in (os.path.join(_code_root, "workbench"), "/app", _code_root):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

# scripts/src 加入 path（crypto_research 包）
_scripts_src = os.path.join(_code_root, "src")
if os.path.isdir(_scripts_src) and _scripts_src not in sys.path:
    sys.path.insert(0, _scripts_src)


def _fmt_num(v, decimals=0):
    """安全格式化数字，None → N/A。"""
    if v is None:
        return "N/A"
    try:
        f = float(v)
        return f"{f:,.{decimals}f}"
    except Exception:
        return str(v)


def _fmt_pct(v, decimals=1, signed=True):
    """安全格式化百分比，带颜色方向。"""
    if v is None:
        return "N/A", "#64748b"
    try:
        f = float(v)
    except Exception:
        return str(v), "#64748b"
    color = "#dc2626" if f > 0 else ("#16a34a" if f < 0 else "#64748b")
    sign = "+" if signed and f >= 0 else ""
    return f"{sign}{f:.{decimals}f}%", color


def _fmt_mcap(v):
    """市值/金额缩写：B / M / K。"""
    if v is None:
        return "N/A"
    try:
        f = float(v)
    except Exception:
        return str(v)
    if f >= 1e9:
        return f"${f/1e9:.1f}B"
    if f >= 1e6:
        return f"${f/1e6:.1f}M"
    if f >= 1e3:
        return f"${f/1e3:.0f}K"
    return f"${f:.0f}"


def render_brief_html(brief: dict) -> str:
    """早报 HTML 四屏版式：仪表盘 → 赛道资金流 → 机会清单 → 链上异动+事件。"""
    today = date.today().isoformat()
    m0 = brief.get("M0_tldr", {})
    diff = brief.get("DIFF", {})
    sector_flow = brief.get("sector_flow") or {}
    kol_onchain = brief.get("kol_onchain") or {}

    # 全部机会（HIGH + 其他）按评分排序
    all_opps = sorted(
        (brief.get("M4_opportunities") or []) + (brief.get("M4_watchlist") or []),
        key=lambda o: (o.get("conviction_score") if isinstance(o.get("conviction_score"), (int, float)) else 0),
        reverse=True,
    )

    html_parts = []
    # 外层容器
    html_parts.append(f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;max-width:680px;margin:auto;background:#f8fafc;padding:12px;color:#0f172a;line-height:1.45">
    """)

    # ════════════════════════════════════════════════════════
    # 第 1 屏：大盘仪表盘
    # ════════════════════════════════════════════════════════
    btc_price = m0.get("btc_price")
    btc_change = m0.get("btc_change_24h_pct")
    btc_chg_str, btc_chg_color = _fmt_pct(btc_change)
    fear_greed = m0.get("fear_greed")
    fg_label = m0.get("fear_greed_label", "")
    phase = m0.get("btc_cycle_phase", "—")

    # 总市值 & 24h 变化（从 diff 或者 M2_flow 取）
    m2 = brief.get("M2_flow") or {}
    total_mcap = m2.get("total_market_cap") or m0.get("total_market_cap")
    total_mcap_chg = diff.get("total_market_cap_pct") if diff else None
    mcap_chg_str, mcap_chg_color = _fmt_pct(total_mcap_chg)

    # 24h 总成交量
    total_vol = sector_flow.get("total_volume_24h")
    vol_str = _fmt_mcap(total_vol) if total_vol else "N/A"

    # 一句话结论（用 M0 的 TLDR 或者 AI 结论）
    tldr_text = m0.get("summary") or m0.get("tldr") or ""
    if not tldr_text:
        # 从已有指标拼一句话
        direction = "偏多" if (btc_change is not None and btc_change > 0) else "偏空"
        tldr_text = f"大盘今日{direction}，BTC 位于 {phase} 周期阶段。"

    html_parts.append(f"""
      <!-- 第1屏：大盘仪表盘卡片 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.06)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <div>
            <div style="font-size:16px;font-weight:700;color:#0f172a;letter-spacing:-0.3px">📊 加密大盘早报</div>
            <div style="font-size:11px;color:#94a3b8;margin-top:1px">{today} · Asia/Shanghai</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:10px;color:#94a3b8">BTC 周期</div>
            <div style="font-size:12px;font-weight:600;color:#475569">{phase}</div>
          </div>
        </div>

        <!-- 四宫格 Grid -->
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px">
          <!-- BTC -->
          <div style="background:linear-gradient(135deg,#1e293b,#334155);border-radius:8px;padding:10px 6px;text-align:center">
            <div style="font-size:10px;color:#94a3b8;margin-bottom:2px">BTC 价格</div>
            <div style="font-size:17px;font-weight:700;color:#fff;letter-spacing:-0.5px">{_fmt_mcap(btc_price) if btc_price else 'N/A'}</div>
            <div style="font-size:10px;color:{btc_chg_color};margin-top:1px;font-weight:500">{btc_chg_str}</div>
          </div>
          <!-- 总市值 -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">总市值</div>
            <div style="font-size:16px;font-weight:700;color:#0f172a">{_fmt_mcap(total_mcap)}</div>
            <div style="font-size:10px;color:{mcap_chg_color};margin-top:1px;font-weight:500">{mcap_chg_str}</div>
          </div>
          <!-- 恐贪 -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">恐贪指数</div>
            <div style="font-size:18px;font-weight:700;color:{_fear_greed_color(fear_greed)}">{fear_greed if fear_greed is not None else 'N/A'}</div>
            <div style="font-size:10px;color:#64748b;margin-top:1px">{fg_label or '—'}</div>
          </div>
          <!-- 24h成交量 -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">24h 成交量</div>
            <div style="font-size:15px;font-weight:700;color:#0f172a">{vol_str}</div>
            <div style="font-size:10px;color:#94a3b8;margin-top:2px">全市场</div>
          </div>
        </div>

        <!-- 今日观点 -->
        <div style="margin-top:10px;background:linear-gradient(90deg,#eff6ff,#f0f9ff);border-left:3px solid #3b82f6;border-radius:4px;padding:8px 12px;font-size:12px;color:#1e40af">
          <b>今日观点：</b>{tldr_text}
        </div>
      </div>
    """)

    # ════════════════════════════════════════════════════════
    # 第 2 屏：🏭 12 赛道资金流向
    # ════════════════════════════════════════════════════════
    html_parts.append("""
      <!-- 第2屏：赛道资金流卡片 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.06)">
        <div style="font-size:14px;font-weight:700;color:#0f172a;margin-bottom:8px;display:flex;align-items:center">
          <span style="margin-right:6px">🏭</span>12 赛道资金流向
          <span style="margin-left:auto;font-size:10px;color:#94a3b8;font-weight:400">按 7d 涨幅排序 · 条宽=市值占比</span>
        </div>
    """)

    sectors = sector_flow.get("sectors") or []
    if sectors and sector_flow.get("status") == "ok":
        # 找出最大市值用于进度条归一化
        max_mcap = max((float(s.get("market_cap") or 0)) for s in sectors) or 1
        n_sec = len(sectors)

        for idx, s in enumerate(sectors):
            label = s.get("sector_label") or s.get("sector_key") or "?"
            mcap = float(s.get("market_cap") or 0)
            mcap_7d = s.get("mcap_change_7d_pct")
            mcap_1d = s.get("mcap_change_1d_pct")
            chg_7d_str, chg_7d_color = _fmt_pct(mcap_7d)
            chg_1d_str, chg_1d_color = _fmt_pct(mcap_1d)
            bar_pct = max(3, min(100, (mcap / max_mcap) * 100)) if max_mcap > 0 else 3

            # 领涨币（显示用24h）
            leaders = s.get("leaders") or []
            leader_html = ""
            if leaders:
                parts = []
                for l in leaders[:3]:
                    sym = l.get("symbol", "?")
                    p24h = l.get("percent_change_24h")
                    p24h_str, p24h_color = _fmt_pct(p24h)
                    parts.append(
                        f'<span style="display:inline-block;background:#fff;border:1px solid #e2e8f0;border-radius:3px;padding:1px 5px;font-size:10.5px;margin-right:4px;margin-bottom:2px">'
                        f'<b style="color:#0f172a">{sym}</b> <span style="color:{p24h_color};font-weight:600">{p24h_str}</span></span>'
                    )
                leader_html = f'<div style="margin-top:4px;margin-left:28px">{"".join(parts)}</div>'

            # 排名徽章 + 涨跌背景
            is_top3 = idx < 3
            is_bottom2 = idx >= n_sec - 2
            if is_top3:
                row_bg = "#f0fdf4"
            elif is_bottom2:
                row_bg = "#fef2f2"
            else:
                row_bg = "#fafafa"

            if idx == 0:
                rank_badge = '<span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:#16a34a;color:#fff;font-size:10.5px;font-weight:700;border-radius:4px;margin-right:7px;flex-shrink:0">TOP</span>'
            elif idx < 3:
                rank_badge = f'<span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:#86efac;color:#166534;font-size:10.5px;font-weight:700;border-radius:4px;margin-right:7px;flex-shrink:0">{idx+1}</span>'
            elif is_bottom2:
                rank_badge = '<span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:#dc2626;color:#fff;font-size:10px;font-weight:700;border-radius:4px;margin-right:7px;flex-shrink:0">LAG</span>'
            else:
                rank_badge = f'<span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:#e2e8f0;color:#475569;font-size:10.5px;font-weight:600;border-radius:4px;margin-right:7px;flex-shrink:0">{idx+1}</span>'

            html_parts.append(f"""
            <div style="padding:7px 8px;margin-bottom:3px;border-radius:6px;background:{row_bg}">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:3px">
                <div style="display:flex;align-items:center;min-width:0">
                  {rank_badge}
                  <span style="font-size:12.5px;font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{label}</span>
                  <span style="color:#94a3b8;font-weight:400;font-size:10.5px;margin-left:5px;flex-shrink:0">{_fmt_mcap(mcap)}</span>
                </div>
                <div style="font-size:11.5px;white-space:nowrap;margin-left:8px">
                  <span style="color:{chg_7d_color};font-weight:700">7d {chg_7d_str}</span>
                  <span style="color:#cbd5e1;margin:0 4px">·</span>
                  <span style="color:{chg_1d_color}">1d {chg_1d_str}</span>
                </div>
              </div>
              <!-- 市值条形（8px加粗） -->
              <div style="height:8px;background:#e2e8f0;border-radius:4px;overflow:hidden;margin-left:28px">
                <div style="height:100%;width:{bar_pct}%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);border-radius:4px"></div>
              </div>
              {leader_html}
            </div>
            """)
    else:
        html_parts.append('<div style="color:#64748b;font-size:12px;padding:16px;text-align:center">暂无赛道数据</div>')

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 第 3 屏：🎯 机会清单
    # ════════════════════════════════════════════════════════
    # 判断是否为兜底数据（热门榜）
    is_fallback = bool(all_opps and all_opps[0].get("is_fallback"))
    section_title = "🔥 今日热门币种" if is_fallback else "🎯 机会清单"
    section_sub = "赛道×链上交叉筛选 · 按综合评分" if is_fallback else "按综合评分排序"

    html_parts.append(f"""
      <!-- 第3屏：机会清单卡片 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.06)">
        <div style="font-size:14px;font-weight:700;color:#0f172a;margin-bottom:2px;display:flex;align-items:center">
          <span style="margin-right:6px">{section_title.split()[0]}</span>{section_title.split(" ",1)[1] if " " in section_title else section_title}
        </div>
        <div style="font-size:10.5px;color:#94a3b8;margin-bottom:8px">{section_sub}</div>
    """)

    if all_opps:
        display_opps = all_opps[:8 if is_fallback else 6]
        for opp in display_opps:
            tier = opp.get("conviction_tier", "?")
            score = opp.get("conviction_score", 0)
            score_str = f"{score:.0f}" if isinstance(score, (int, float)) else str(score)
            target = opp.get("target", "?")
            direction = opp.get("direction", "?")
            trigger = opp.get("trigger_logic", "")
            sector = opp.get("sector", "")
            sources = opp.get("source_count") or opp.get("signals_count") or 0
            chain = opp.get("chain")
            contract = opp.get("contract_address")

            # 链标签 + 区块浏览器链接
            CHAIN_LABEL = {
                "ethereum": "ETH", "bsc": "BSC", "solana": "SOL",
                "polygon": "POLY", "arbitrum": "ARB", "base": "BASE",
                "optimism": "OP", "avalanche": "AVAX", "tron": "TRON",
                "ton": "TON", "sui": "SUI", "aptos": "APT",
            }
            CHAIN_EXPLORER = {
                "ethereum": "https://etherscan.io/address/{}",
                "bsc": "https://bscscan.com/address/{}",
                "solana": "https://solscan.io/token/{}",
                "polygon": "https://polygonscan.com/address/{}",
                "arbitrum": "https://arbiscan.io/address/{}",
                "base": "https://basescan.org/address/{}",
                "optimism": "https://optimistic.etherscan.io/address/{}",
                "avalanche": "https://snowtrace.io/address/{}",
                "tron": "https://tronscan.io/#/contract/{}",
                "ton": "https://tonscan.org/jetton/{}",
                "sui": "https://suiscan.xyz/coin/{}",
                "aptos": "https://aptoscan.com/coin/{}",
            }
            chain_label = CHAIN_LABEL.get(chain, chain.upper() if chain else "")
            chain_badge = f'<span style="background:#e2e8f0;color:#475569;font-size:10px;padding:1px 5px;border-radius:3px;font-weight:700;margin-left:6px">{chain_label}</span>' if chain_label else ""

            # 合约地址截断显示（前6后4）
            contract_display = ""
            if contract:
                short_contract = contract[:6] + "…" + contract[-4:] if len(contract) > 10 else contract
                if chain and chain in CHAIN_EXPLORER:
                    explorer_url = CHAIN_EXPLORER[chain].format(contract)
                    contract_display = f'<a href="{explorer_url}" target="_blank" style="color:#64748b;font-size:10px;font-family:Monaco,Consolas,monospace;text-decoration:none">{short_contract}</a>'
                else:
                    contract_display = f'<span style="color:#64748b;font-size:10px;font-family:Monaco,Consolas,monospace">{short_contract}</span>'

            if tier == "HIGH":
                accent = "#dc2626"
                badge_bg = "#fee2e2"
                badge_color = "#991b1b"
                card_bg = "#fff1f2"
            elif tier == "MED":
                accent = "#f59e0b"
                badge_bg = "#fef3c7"
                badge_color = "#92400e"
                card_bg = "#fffbeb"
            else:
                accent = "#94a3b8"
                badge_bg = "#f1f5f9"
                badge_color = "#475569"
                card_bg = "#f8fafc"

            dir_icon = "▲" if direction == "long" else "▼" if direction == "short" else "◆"
            dir_color = "#dc2626" if direction == "long" else "#16a34a" if direction == "short" else "#64748b"
            dir_cn = "看多" if direction == "long" else "看空" if direction == "short" else direction

            meta_parts = []
            if sector:
                meta_parts.append(f"🏷️ {sector}")
            if sources:
                meta_parts.append(f"📡 {sources}信号源")
            meta_str = " · ".join(meta_parts)

            html_parts.append(f"""
            <div style="padding:10px 12px;margin:5px 0;border-radius:7px;border-left:3px solid {accent};background:{card_bg}">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div style="display:flex;align-items:center;flex-wrap:wrap;gap:0">
                  <span style="font-size:14px;font-weight:700;color:#0f172a">{target}</span>
                  {chain_badge}
                  <span style="color:{dir_color};font-size:12px;font-weight:600;margin-left:8px">{dir_icon} {dir_cn}</span>
                </div>
                <div style="display:flex;align-items:center;gap:6px">
                  <span style="background:{badge_bg};color:{badge_color};font-size:10px;padding:1px 6px;border-radius:3px;font-weight:700">{tier}</span>
                  <span style="font-size:11px;color:#64748b;font-weight:600">{score_str}</span>
                </div>
              </div>
              {f'<div style="font-size:11px;color:#64748b;margin-top:3px">{meta_str}</div>' if meta_str else ''}
              <div style="color:#475569;font-size:11.5px;margin-top:4px;line-height:1.4">{trigger}</div>
              {f'<div style="margin-top:5px;display:flex;align-items:center;gap:6px">{contract_display}</div>' if contract_display else ''}
            </div>
            """)
    else:
        html_parts.append('<div style="color:#94a3b8;font-size:12px;padding:16px;text-align:center">暂无推荐机会</div>')

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 第 4 屏：🔗 链上异动 + 📅 近期事件
    # ════════════════════════════════════════════════════════
    html_parts.append("""
      <!-- 第4屏：链上异动 + 近期事件 双栏 -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
    """)

    # 左栏：链上信号
    signals = kol_onchain.get("signals") or []
    kol_count = len(kol_onchain.get("kols") or [])
    html_parts.append(f"""
      <div style="background:#fff;border-radius:10px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,0.06)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:2px">🔍 链上异动</div>
        <div style="font-size:10.5px;color:#94a3b8;margin-bottom:8px">{kol_onchain.get('hours',24)}h · {kol_count}位分析师</div>
    """)
    if signals and kol_onchain.get("status") == "ok":
        SUBTYPE_CN = {
            "exchange_flow": "交易所",
            "smart_money": "聪明钱",
            "accumulation": "大额吸筹",
            "whale_move": "巨鲸转账",
            "distribution": "大额派发",
            "liquidation": "爆仓清算",
        }
        DIR_CN = {
            "inflow": "流入",
            "outflow": "流出",
            "accumulating": "吸筹",
            "distributing": "派发",
        }
        for sig in signals[:6]:
            subtype = sig.get("signal_subtype") or ""
            subtype_cn = SUBTYPE_CN.get(subtype, subtype)
            direction = sig.get("event_direction") or ""
            dir_cn = DIR_CN.get(direction, direction)
            sym = sig.get("symbol") or sig.get("event_token") or "?"
            amount = sig.get("event_usd_value")
            amt_str = _fmt_mcap(amount) if amount else ""
            kol = sig.get("kol_name") or ""
            addr_label = sig.get("address_label") or ""
            exchange = sig.get("event_exchange") or ""

            is_bullish = (
                "in" in str(direction).lower()
                or "accum" in str(subtype).lower()
            )
            is_bearish = (
                "out" in str(direction).lower()
                or "distribut" in str(subtype).lower()
                or "liquidat" in str(subtype).lower()
            )
            if is_bullish:
                dot = "#dc2626"
                arrow = "▲"
            elif is_bearish:
                dot = "#16a34a"
                arrow = "▼"
            else:
                dot = "#64748b"
                arrow = "◆"

            desc_parts = []
            if exchange:
                desc_parts.append(f"交易所：{exchange}")
            if addr_label:
                desc_parts.append(addr_label)
            desc = " · ".join(desc_parts)

            html_parts.append(f"""
            <div style="padding:7px 8px;margin:3px 0;border-radius:5px;background:#fafafa;border-left:2px solid {dot}">
              <div style="display:flex;justify-content:space-between;align-items:center;font-size:11.5px">
                <div>
                  <span style="font-weight:700;color:#0f172a">{sym}</span>
                  <span style="color:{dot};font-weight:600;margin-left:4px">{arrow} {dir_cn}</span>
                  {f'<span style="color:#64748b;margin-left:4px">{amt_str}</span>' if amt_str else ''}
                </div>
                <span style="font-size:10px;color:#94a3b8">{kol}</span>
              </div>
              {f'<div style="font-size:10px;color:#64748b;margin-top:2px"><span style="background:#eef2ff;color:#4338ca;padding:0 4px;border-radius:2px;font-weight:500;margin-right:4px">{subtype_cn}</span>{desc}</div>' if subtype_cn or desc else ''}
            </div>
            """)
    else:
        html_parts.append('<div style="color:#94a3b8;font-size:11px;padding:15px;text-align:center">近 24h 无链上异动</div>')
    html_parts.append("</div>")

    # 右栏：催化剂日历
    catalyst = brief.get("M5_catalyst") or {}
    events = (catalyst.get("hardcoded") or []) + (catalyst.get("token_events") or [])
    try:
        events.sort(key=lambda e: e.get("date", "9999"))
    except Exception:
        pass

    html_parts.append("""
      <div style="background:#fff;border-radius:10px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,0.06)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:2px">📅 近期催化剂</div>
        <div style="font-size:10.5px;color:#94a3b8;margin-bottom:8px">宏观 & 代币事件</div>
    """)
    if events:
        for ev in events[:6]:
            ev_date = ev.get("date", "")
            ev_name = ev.get("event", "?")
            ev_type = ev.get("type", "")
            try:
                days_until = (date.fromisoformat(ev_date) - date.today()).days
                days_str = f"{days_until}d" if days_until > 0 else "今天" if days_until == 0 else "已过"
                days_color = "#dc2626" if 0 <= days_until <= 7 else ("#f59e0b" if 7 < days_until <= 14 else "#94a3b8")
            except Exception:
                days_str = ""
                days_color = "#94a3b8"

            type_badge = ""
            if ev_type == "macro":
                type_badge = '<span style="background:#e0e7ff;color:#4338ca;font-size:9.5px;padding:0 4px;border-radius:2px;font-weight:500;margin-right:5px">宏观</span>'
            elif ev_type == "unlock":
                type_badge = '<span style="background:#fef3c7;color:#92400e;font-size:9.5px;padding:0 4px;border-radius:2px;font-weight:500;margin-right:5px">解锁</span>'
            elif ev_type in ("listing", "exchange_listing"):
                type_badge = '<span style="background:#dcfce7;color:#166534;font-size:9.5px;padding:0 4px;border-radius:2px;font-weight:500;margin-right:5px">上币</span>'

            html_parts.append(f"""
            <div style="padding:6px 0;border-bottom:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center">
              <div style="font-size:11.5px;color:#334155;min-width:0;overflow:hidden;text-overflow:ellipsis">
                {type_badge}{ev_name}
              </div>
              <span style="font-size:10.5px;color:{days_color};font-weight:600;flex-shrink:0;margin-left:8px">{days_str}</span>
            </div>
            """)
    else:
        html_parts.append('<div style="color:#94a3b8;font-size:11px;padding:15px;text-align:center">暂无事件</div>')
    html_parts.append("</div></div>")

    # ════════════════════════════════════════════════════════
    # 其他信号（折叠显示，精简）
    # ════════════════════════════════════════════════════════
    extra_blocks = []

    # 宏观背离（M3）
    divs = brief.get("M3_divergence") or []
    if divs:
        items = []
        for d in divs[:3]:
            sig_name = d.get("signal", "?")
            label = d.get("label", "?")
            interp = d.get("interpretation", "")
            color = "#dc2626" if label == "DANGEROUS" else "#f59e0b"
            icon = "🔴" if label == "DANGEROUS" else "🟡"
            items.append(f'<span style="background:#fffbeb;color:#92400e;font-size:11px;padding:2px 6px;border-radius:4px;margin-right:4px">{icon} {sig_name}</span> {interp}')
        extra_blocks.append(("📡 宏观背离", "<br>".join(items)))

    # 聪明钱背离
    sm = brief.get("M4_smart_money") or {}
    if isinstance(sm, dict) and sm.get("status") == "ok" and (sm.get("bullish") or sm.get("bearish")):
        bull = sm.get("bullish") or []
        bear = sm.get("bearish") or []
        bull_str = " ".join(f'<span style="color:#16a34a">🐂{s.get("symbol","?")}</span>' for s in bull[:3])
        bear_str = " ".join(f'<span style="color:#dc2626">🐻{s.get("symbol","?")}</span>' for s in bear[:3])
        extra_blocks.append(("🐋 聪明钱", f"{bull_str} {bear_str}"))

    # 四烟囱
    chimney = brief.get("M4_chimney") or {}
    if isinstance(chimney, dict) and chimney.get("status") in ("ok", "partial"):
        ch_items = []
        for t in (chimney.get("tvl") or []):
            if t.get("type") == "category":
                for it in (t.get("items") or [])[:2]:
                    chg = it.get("tvl_change_7d_pct")
                    if chg is not None:
                        ch_items.append(f"{it.get('category','?')} {chg:+.1f}%")
        gh = chimney.get("github") or []
        if gh:
            ch_items.append(f"GitHub: {'/'.join(g.get('symbol','?') for g in gh[:2])}")
        funding = chimney.get("funding") or []
        if funding:
            ch_items.append(f"融资: {'/'.join(f.get('symbol','?') for f in funding[:2])}")
        if ch_items:
            extra_blocks.append(("🏭 基本面", " · ".join(ch_items[:4])))

    # Meme
    meme = brief.get("M4_meme") or {}
    if isinstance(meme, dict) and meme.get("status") == "ok":
        summary = meme.get("summary") or {}
        if summary:
            extra_blocks.append((
                "🐸 Meme",
                f"高危{summary.get('high',0)} · 中危{summary.get('medium',0)} · 低风险{summary.get('low',0)} · 排雷{summary.get('block',0)}"
            ))

    if extra_blocks:
        html_parts.append("""
          <!-- 其他信号卡片 -->
          <div style="background:#fff;border-radius:10px;padding:12px 14px;box-shadow:0 1px 3px rgba(0,0,0,0.06)">
            <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px">📌 其他信号</div>
        """)
        for title, content in extra_blocks:
            html_parts.append(f"""
            <div style="padding:6px 8px;margin:3px 0;background:#f8fafc;border-radius:5px;font-size:11.5px">
              <b style="color:#334155">{title}</b>
              <div style="color:#475569;margin-top:2px">{content}</div>
            </div>
            """)
        html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # AI 解读
    # ════════════════════════════════════════════════════════
    ai_narrative = brief.get("ai_narrative")
    if ai_narrative:
        ai_html = ai_narrative
        ai_html = ai_html.replace("\n\n", "</p><p>")
        ai_html = ai_html.replace("\n", "<br>")
        ai_html = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', ai_html)
        ai_html = re.sub(r'`(.*?)`', r'<code style="background:#e2e8f0;padding:1px 4px;border-radius:3px;font-size:10.5px">\1</code>', ai_html)
        html_parts.append(f'''
        <div style="margin-top:10px;padding:10px 12px;background:#f0f9ff;border-left:3px solid #3b82f6;border-radius:5px">
          <div style="font-weight:700;color:#1e40af;margin-bottom:5px;font-size:12px">🤖 AI 深度解读</div>
          <div style="color:#334155;font-size:11.5px;line-height:1.55">{ai_html}</div>
        </div>''')

    # 降级标注
    degraded = brief.get("degraded", [])
    if degraded:
        html_parts.append(f'<div style="margin-top:8px;padding:6px 10px;background:#fef9c3;border-radius:4px;color:#92400e;font-size:11px">⚠️ 降级项: {", ".join(degraded)}</div>')

    # 页脚
    html_parts.append("""
      <div style="text-align:center;font-size:10px;color:#94a3b8;margin-top:12px">
        数据仅供参考，不构成投资建议 · 加密大盘早报
      </div>
    </div>
    """)
    return "\n".join(html_parts)


def _fear_greed_color(value):
    """恐贪指数颜色。"""
    if value is None:
        return "#64748b"
    try:
        v = int(value)
    except Exception:
        return "#64748b"
    if v >= 75:
        return "#16a34a"  # 极度贪婪 - 绿
    if v >= 55:
        return "#65a30d"  # 贪婪
    if v >= 45:
        return "#64748b"  # 中性
    if v >= 25:
        return "#f59e0b"  # 恐惧
    return "#dc2626"  # 极度恐惧 - 红


def main():
    parser = argparse.ArgumentParser(description="每日早报邮件发送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印 HTML，不发送")
    args = parser.parse_args()

    # 1. 生成 brief
    try:
        from build_daily_brief import main as build_brief
        brief = build_brief()
    except Exception as e:
        print(f"[ERROR] 生成 brief 失败: {e}")
        return 1

    # 1.5 AI 叙事（独立 try/except，绝不阻断邮件发送）
    try:
        from crypto_research.config import get_settings as _get_settings
        from crypto_research.brief_ai import generate_ai_narrative
        _settings = _get_settings(require_database=False)
        ai_text = generate_ai_narrative(_settings, brief)
        if ai_text:
            brief["ai_narrative"] = ai_text
            print(f"[OK] AI 叙事已生成（{len(ai_text)} 字符）")
        else:
            brief.setdefault("degraded", []).append("ai_narrative")
            print("[INFO] AI 叙事未生成（LLM 不可用或返回空），已降级")
    except Exception as e:
        brief.setdefault("degraded", []).append("ai_narrative")
        print(f"[WARN] AI 叙事生成异常（已降级）: {e}")

    # 2. 渲染 HTML
    try:
        html = render_brief_html(brief)
    except Exception as e:
        print(f"[ERROR] HTML 渲染失败: {e}")
        return 1

    if args.dry_run:
        print(html)
        return 0

    # 3. 发送邮件
    try:
        from crypto_research.config import get_settings
        from crypto_research.clients.notifier import EmailNotifier

        settings = get_settings(require_database=False)
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[WARN] SMTP 未配置，跳过邮件发送")
            print(html)
            return 0

        m0 = brief.get("M0_tldr", {})
        subject = f"📊 加密大盘早报 {m0.get('date', date.today().isoformat())}"
        ok, msg = notifier.send(subject=subject, body_html=html)
        if ok:
            print(f"[OK] 早报邮件已发送: {msg}")
        else:
            print(f"[ERROR] 邮件发送失败: {msg}")
            return 1
    except Exception as e:
        print(f"[ERROR] 邮件发送异常: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
