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


def _classify_degraded(items: list[str]) -> dict:
    """降级项分类：critical(核心)/warning(辅助)/info(增强)。
    核心降级：影响主决策的关键数据缺失（BTC价格、总市值、恐贪等）
    辅助降级：不影响主结论但缺了就不完整（稳定币、KOL、巨鲸、解锁等）
    增强降级：锦上添花的功能（AI摘要、叙事榜等）
    """
    critical_keywords = ("btc", "eth", "总市值", "market_cap", "fear_greed", "恐贪",
                         "1体量", "2盘面", "3情绪", "overview")
    info_keywords = ("ai_", "ai_summary", "narrative", "叙事", "meme", "chimney",
                     "smart_money", "resonance", "背离")

    critical, warning, info = [], [], []
    for item in items:
        low = str(item).lower()
        if any(k in low for k in critical_keywords):
            critical.append(item)
        elif any(k in low for k in info_keywords):
            info.append(item)
        else:
            warning.append(item)
    return {"critical": critical, "warning": warning, "info": info}


def _render_degraded_badge(brief: dict) -> str:
    """渲染降级项徽标：核心红色/辅助黄色/增强隐藏（仅核心才显示红色告警）。"""
    # 收集所有降级来源：brief.degraded + M9_degraded
    all_degraded = list(brief.get("degraded", []) or [])
    m9 = brief.get("M9_degraded", []) or []
    all_degraded.extend(m9)

    if not all_degraded:
        return ""

    tiers = _classify_degraded(all_degraded)
    parts = []

    # 核心降级：醒目红色
    if tiers["critical"]:
        parts.append(
            f'<div style="margin-top:8px;padding:8px 12px;background:#fef2f2;'
            f'border:1px solid #fecaca;border-radius:6px;color:#991b1b;font-size:12px">'
            f'🚨 <b>核心数据降级</b>：{", ".join(tiers["critical"])}</div>'
        )

    # 辅助降级：黄色，收起来
    if tiers["warning"]:
        parts.append(
            f'<div style="margin-top:6px;padding:6px 10px;background:#fef9c3;'
            f'border-radius:4px;color:#92400e;font-size:11px">'
            f'⚠️ 辅助数据缺失：{", ".join(tiers["warning"])}</div>'
        )

    # 增强降级：不显示（避免噪音，只在debug时看）

    return "".join(parts)


def _resolve_addr_label(raw_label, labels_arr, names_arr, addr):
    """地址标签解析：从数组列优先取，再回退单值字段，最后回退地址截断。
    标签优先级：label_names[0]（具体名称） > labels[0]（类型） > raw_label（单值） > 地址前8位。
    """
    # 1. 优先用 label_names 数组（具体名称，如 "Binance 14", "Gemini"）
    if names_arr and isinstance(names_arr, list) and names_arr:
        first = names_arr[0]
        if first and str(first).strip().lower() not in ("unknown", "", "none", "null"):
            return str(first).strip()
    # 2. 其次用 labels 数组（类型，如 "exchange", "smart_money"）
    if labels_arr and isinstance(labels_arr, list) and labels_arr:
        first = labels_arr[0]
        if first and str(first).strip().lower() not in ("unknown", "", "none", "null"):
            return str(first).strip()
    # 3. 回退到单值字段
    if raw_label and str(raw_label).strip().lower() not in ("unknown", "", "none", "null"):
        return str(raw_label).strip()
    # 4. 最后回退到地址截断
    addr = addr or ""
    if addr and len(addr) >= 8:
        return addr[:8] + "..."
    return "未知地址"


def _fmt_mcap(v):
    """市值/金额缩写：B / M / K。"""
    if v is None:
        return "N/A"
    try:
        f = float(v)
        if f != f or f == float("inf") or f == float("-inf"):  # NaN or Inf
            return "—"
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
    """
    早报 HTML V2 — 6 大模块 + AI 定调。
    模块顺序：AI定调 → 大盘脉搏 → 赛道轮动 → 机构资金 → 链上异动 → 催化剂 → 机会清单
    """
    today = date.today().isoformat()
    m0 = brief.get("M0_tldr", {})
    ai_summary = brief.get("M0_ai_summary") or {}
    diff = brief.get("DIFF", {})
    sector_flow = brief.get("M2_sector_flow") or {}
    etf_flow = brief.get("M2_etf_flow") or {}
    whale_moves = brief.get("M2_whale_moves") or {}
    exchange_flow = brief.get("M2_exchange_flow") or {}
    holder_conc = brief.get("M2_holder_concentration") or {}
    stab = brief.get("M2_stablecoin") or {}
    upcoming_unlocks = brief.get("M6_upcoming_unlocks") or {}
    kol_onchain = brief.get("kol_onchain") or {}

    # 全部机会按评分排序
    all_opps = sorted(
        (brief.get("M8_opportunities") or []) + (brief.get("M8_watchlist") or []),
        key=lambda o: (o.get("conviction_score") if isinstance(o.get("conviction_score"), (int, float)) else 0),
        reverse=True,
    )

    html_parts = []
    # 外层容器
    html_parts.append(f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;max-width:680px;margin:auto;background:#f1f5f9;padding:10px;color:#0f172a;line-height:1.5">
    """)

    # ════════════════════════════════════════════════════════
    # 模块 0：🔥 AI 今日定调（最顶部，最醒目）
    # ════════════════════════════════════════════════════════
    ai_headline = ai_summary.get("headline") or ""
    ai_regime = ai_summary.get("market_regime") or ""
    ai_bias = ai_summary.get("bias") or ""
    ai_conviction = ai_summary.get("conviction") or "medium"
    ai_key_drivers = ai_summary.get("key_drivers") or []
    ai_sector_rotation = ai_summary.get("sector_rotation") or ""
    ai_trade_suggestions = ai_summary.get("trade_suggestions") or []
    ai_risk_warnings = ai_summary.get("risk_warnings") or []
    ai_watchlist = ai_summary.get("watchlist") or []

    if ai_summary.get("status") == "ok" and ai_headline:
        # 方向颜色
        bias_color = "#ef4444" if "多" in str(ai_bias) else "#22c55e" if "空" in str(ai_bias) else "#f59e0b"
        conviction_cn = {"high": "高", "medium": "中", "low": "低"}.get(str(ai_conviction).lower(), "中")
        conviction_pct = {"high": 85, "medium": 65, "low": 40}.get(str(ai_conviction).lower(), 50)

        html_parts.append(f"""
          <!-- 模块0：AI今日定调 -->
          <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:16px 18px;margin-bottom:10px;color:#fff;position:relative;overflow:hidden">
            <div style="position:absolute;top:-20px;right:-10px;font-size:80px;opacity:0.08">🤖</div>
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
              <div>
                <div style="font-size:11px;color:#94a3b8;letter-spacing:1px;text-transform:uppercase;margin-bottom:2px">AI Morning Call</div>
                <div style="font-size:18px;font-weight:800;letter-spacing:-0.5px;line-height:1.3">{ai_headline}</div>
              </div>
              <div style="text-align:right;flex-shrink:0;margin-left:12px">
                <div style="font-size:10px;color:#64748b">置信度</div>
                <div style="font-size:18px;font-weight:700;color:{bias_color}">{conviction_pct}%</div>
                <div style="font-size:9px;color:#64748b">{conviction_cn}</div>
              </div>
            </div>
            <div style="display:flex;gap:8px;margin-bottom:10px">
              <span style="font-size:10.5px;background:rgba(255,255,255,0.1);padding:2px 8px;border-radius:4px;color:#e2e8f0">市场：{ai_regime or '—'}</span>
              <span style="font-size:10.5px;background:{bias_color}22;padding:2px 8px;border-radius:4px;color:{bias_color}">方向：{ai_bias or '—'}</span>
            </div>
        """)

        # 核心驱动因素
        if ai_key_drivers:
            driver_html = "<br>".join(f"• {d}" for d in ai_key_drivers[:4])
            html_parts.append(f"""
            <div style="font-size:12px;color:#cbd5e1;line-height:1.7;margin-bottom:10px;background:rgba(255,255,255,0.05);border-radius:6px;padding:8px 12px">
              {driver_html}
            </div>
            """)

        # 赛道轮动
        if ai_sector_rotation:
            html_parts.append(f"""
            <div style="font-size:11.5px;color:#e2e8f0;margin-bottom:10px">
              <span style="color:#f59e0b;font-weight:700">🔄 赛道轮动：</span>{ai_sector_rotation}
            </div>
            """)

        # 具体交易建议
        if ai_trade_suggestions:
            html_parts.append(f"""
            <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;font-weight:600;letter-spacing:0.5px">💡 具体交易方向</div>
            """)
            for s in ai_trade_suggestions[:4]:
                asset = s.get("asset") or "?"
                direction = s.get("direction") or ""
                horizon = s.get("horizon") or ""
                reason = s.get("reason") or ""
                conf = str(s.get("confidence") or "").lower()

                dir_color = "#ef4444" if "多" in str(direction) else "#22c55e" if "空" in str(direction) else "#eab308"
                dir_icon = "▲" if "多" in str(direction) else "▼" if "空" in str(direction) else "◆"
                conf_cn = {"high": "高", "medium": "中", "low": "低"}.get(conf, conf or "—")

                html_parts.append(f"""
                <div style="background:rgba(255,255,255,0.08);border-radius:6px;padding:8px 10px;margin-bottom:5px;border-left:3px solid {dir_color}">
                  <div style="display:flex;justify-content:space-between;align-items:center">
                    <div style="font-size:13px;font-weight:700">{asset} <span style="color:{dir_color};font-size:12px;margin-left:4px">{dir_icon} {direction}</span></div>
                    <span style="font-size:10px;background:rgba(255,255,255,0.1);padding:1px 6px;border-radius:3px;color:#94a3b8">{horizon}</span>
                  </div>
                  {f'<div style="font-size:11px;color:#94a3b8;margin-top:2px;line-height:1.5">{reason}</div>' if reason else ''}
                </div>
                """)

        # 关注列表
        if ai_watchlist:
            watch_str = " · ".join(ai_watchlist[:6])
            html_parts.append(f"""
            <div style="font-size:11px;color:#94a3b8;margin-top:10px">
              <span style="color:#f59e0b;font-weight:600">🎯 重点关注：</span>{watch_str}
            </div>
            """)

        # 风险提示
        if ai_risk_warnings:
            risk_html = "<br>".join(f"⚠️ {r}" for r in ai_risk_warnings[:3])
            html_parts.append(f"""
            <div style="font-size:11px;color:#fca5a5;margin-top:8px;line-height:1.6">
              {risk_html}
            </div>
            """)

        html_parts.append("</div>")
    else:
        # AI 不可用时的降级：用 M0 TLDR
        tldr_text = m0.get("summary") or m0.get("tldr") or ""
        html_parts.append(f"""
          <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:14px 16px;margin-bottom:10px;color:#fff">
            <div style="font-size:11px;color:#94a3b8;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Morning Brief</div>
            <div style="font-size:18px;font-weight:800;margin-bottom:8px">加密大盘早报</div>
            <div style="font-size:13px;color:#e2e8f0;line-height:1.5">{tldr_text or '今日大盘数据更新中...'}</div>
          </div>
        """)

    # ════════════════════════════════════════════════════════
    # 模块 1：📊 大盘脉搏
    # ════════════════════════════════════════════════════════
    btc_price = m0.get("btc_price")
    btc_change = m0.get("btc_change_24h_pct")
    btc_chg_str, btc_chg_color = _fmt_pct(btc_change)
    fear_greed = m0.get("fear_greed")
    fg_label = m0.get("fear_greed_label", "")
    phase = m0.get("btc_cycle_phase", "—")
    eth_price = m0.get("eth_price")
    eth_change = m0.get("eth_change_24h_pct")
    eth_chg_str, eth_chg_color = _fmt_pct(eth_change)

    m2 = brief.get("M2_flow") or {}
    total_mcap = m2.get("total_market_cap") or m0.get("total_market_cap")
    total_mcap_chg = diff.get("total_market_cap_pct") if diff else None
    mcap_chg_str, mcap_chg_color = _fmt_pct(total_mcap_chg)

    total_vol = sector_flow.get("total_volume_24h")
    vol_str = _fmt_mcap(total_vol) if total_vol else "N/A"

    # 波动率
    btc_vol = m0.get("btc_volatility_7d") or m2.get("btc_volatility_7d")
    btc_vol_str = f"{btc_vol}%" if btc_vol is not None else "—"

    html_parts.append(f"""
      <!-- 模块1：大盘脉搏 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <div style="font-size:13px;font-weight:700;color:#0f172a">📊 大盘脉搏</div>
          <div style="font-size:10px;color:#94a3b8">{today}</div>
        </div>

        <!-- 两排指标 -->
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px">
          <!-- BTC -->
          <div style="background:linear-gradient(135deg,#f8fafc,#f1f5f9);border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">BTC</div>
            <div style="font-size:16px;font-weight:700;color:#0f172a;letter-spacing:-0.3px">{_fmt_mcap(btc_price) if btc_price else 'N/A'}</div>
            <div style="font-size:10px;color:{btc_chg_color};margin-top:1px;font-weight:600">{btc_chg_str}</div>
          </div>
          <!-- ETH -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">ETH</div>
            <div style="font-size:16px;font-weight:700;color:#0f172a">{_fmt_mcap(eth_price) if eth_price else 'N/A'}</div>
            <div style="font-size:10px;color:{eth_chg_color};margin-top:1px;font-weight:600">{eth_chg_str}</div>
          </div>
          <!-- 总市值 -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">总市值</div>
            <div style="font-size:15px;font-weight:700;color:#0f172a">{_fmt_mcap(total_mcap)}</div>
            <div style="font-size:10px;color:{mcap_chg_color};margin-top:1px;font-weight:600">{mcap_chg_str}</div>
          </div>
          <!-- 恐贪 -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">恐贪指数</div>
            <div style="font-size:17px;font-weight:700;color:{_fear_greed_color(fear_greed)}">{fear_greed if fear_greed is not None else 'N/A'}</div>
            <div style="font-size:10px;color:#64748b;margin-top:1px">{fg_label or '—'}</div>
          </div>
        </div>

        <!-- 底部附加指标 -->
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px">
          <div style="text-align:center;background:#f8fafc;border-radius:6px;padding:6px 4px">
            <div style="font-size:9.5px;color:#94a3b8">24h 成交量</div>
            <div style="font-size:13px;font-weight:700;color:#334155">{vol_str}</div>
          </div>
          <div style="text-align:center;background:#f8fafc;border-radius:6px;padding:6px 4px">
            <div style="font-size:9.5px;color:#94a3b8">BTC 周期</div>
            <div style="font-size:13px;font-weight:700;color:#334155">{phase}</div>
          </div>
          <div style="text-align:center;background:#f8fafc;border-radius:6px;padding:6px 4px">
            <div style="font-size:9.5px;color:#94a3b8">BTC 7日波动率</div>
            <div style="font-size:13px;font-weight:700;color:#334155">{btc_vol_str}</div>
          </div>
        </div>
      </div>
    """)

    # ════════════════════════════════════════════════════════
    # 模块 2：🏭 赛道轮动（叙事榜 + 领涨币）
    # ════════════════════════════════════════════════════════
    narratives = brief.get("narrative_flow", {}).get("ranked") or []

    html_parts.append(f"""
      <!-- 模块2：赛道轮动 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px;display:flex;align-items:center">
          <span style="margin-right:6px">🏭</span>赛道轮动
          <span style="margin-left:auto;font-size:10px;color:#94a3b8;font-weight:400">7日市值变化 · 综合评分</span>
        </div>
    """)

    if narratives:
        max_score = max((float(n.get("composite_score") or 0)) for n in narratives) or 1
        for idx, n in enumerate(narratives[:8]):
            name = n.get("narrative", "?")
            score = float(n.get("composite_score") or 0)
            mcap7d = n.get("mcap_change_7d_pct")
            tvl7d = n.get("tvl_change_7d_pct")
            trend = n.get("trend_label", "")
            top_coins = n.get("top_coins") or []

            chg_str, chg_color = _fmt_pct(mcap7d)
            tvl_str, tvl_color = _fmt_pct(tvl7d) if tvl7d is not None else ("—", "#94a3b8")
            # 无 TVL 数据时隐藏 TVL 标签，避免误导
            tvl_html = ""
            if tvl7d is not None:
                tvl_html = f'<span style="font-size:10px;color:{tvl_color}">TVL {tvl_str}</span>'
            bar_pct = max(3, min(100, (score / max_score) * 100))

            # 趋势标签
            trend_badge = ""
            if trend == "加速上涨":
                trend_badge = '<span style="background:#dcfce7;color:#166534;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">加速↑</span>'
            elif trend == "反弹":
                trend_badge = '<span style="background:#dbeafe;color:#1e40af;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">反弹</span>'
            elif trend == "横盘":
                trend_badge = '<span style="background:#f1f5f9;color:#64748b;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">横盘</span>'
            elif trend == "回调":
                trend_badge = '<span style="background:#fee2e2;color:#991b1b;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">回调↓</span>'

            # 领涨币
            coins_html = ""
            if top_coins:
                coin_parts = []
                for c in top_coins[:3]:
                    sym = c if isinstance(c, str) else c.get("symbol", "?")
                    coin_parts.append(f'<span style="font-size:10px;color:#64748b">{sym}</span>')
                coins_html = f'<div style="font-size:10px;color:#94a3b8;margin-top:3px">领涨：{" · ".join(coin_parts)}</div>'

            html_parts.append(f"""
              <div style="padding:7px 8px;margin-bottom:4px;border-radius:6px;background:#fafafa">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:3px">
                  <div style="display:flex;align-items:center;min-width:0">
                    <span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:#e2e8f0;color:#475569;font-size:10.5px;font-weight:700;border-radius:4px;margin-right:6px;flex-shrink:0">{idx+1}</span>
                    <span style="font-size:12.5px;font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{name}</span>
                    {trend_badge}
                  </div>
                  <div style="display:flex;align-items:center;gap:6px;margin-left:6px;flex-shrink:0">
                    {tvl_html}
                    <span style="font-size:12px;color:{chg_color};font-weight:700">{chg_str}</span>
                  </div>
                </div>
                <div style="height:4px;background:#e2e8f0;border-radius:2px;overflow:hidden;margin-left:26px">
                  <div style="height:100%;width:{bar_pct}%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);border-radius:2px"></div>
                </div>
                {coins_html}
              </div>
            """)
    else:
        html_parts.append('<div style="color:#94a3b8;font-size:11px;padding:14px;text-align:center">暂无赛道数据</div>')

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 3：💰 机构资金流（ETF + 稳定币 + 交易所净流）
    # ════════════════════════════════════════════════════════
    html_parts.append(f"""
      <!-- 模块3：机构资金流 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px">💰 机构资金流</div>
    """)

    # ETF 资金流
    etf_assets = etf_flow.get("assets") or []
    if etf_flow.get("status") == "ok" and etf_assets:
        # 从 assets 里提取 BTC、ETH 和总净流入
        btc_net = None
        eth_net = None
        total_net = 0
        for a in etf_assets:
            sym = (a.get("symbol") or "").upper()
            flow_7d = a.get("flow_7d_usd") or 0
            if sym == "BTC":
                btc_net = flow_7d
            elif sym == "ETH":
                eth_net = flow_7d
            total_net += flow_7d

        def _fmt_flow(v):
            if v is None:
                return "—", "#94a3b8"
            try:
                v = float(v)
            except Exception:
                return "—", "#94a3b8"
            sign = "+" if v >= 0 else ""
            color = "#dc2626" if v > 0 else "#16a34a" if v < 0 else "#64748b"
            if abs(v) >= 1e9:
                return f"{sign}${v/1e9:.2f}B", color
            elif abs(v) >= 1e6:
                return f"{sign}${v/1e6:.0f}M", color
            else:
                return f"{sign}${v:.0f}", color

        btc_net_str, btc_net_color = _fmt_flow(btc_net)
        eth_net_str, eth_net_color = _fmt_flow(eth_net)
        total_net_str, total_net_color = _fmt_flow(total_net)

        html_parts.append(f"""
          <!-- ETF 子模块 -->
          <div style="background:linear-gradient(135deg,#f0f9ff,#e0f2fe);border-radius:8px;padding:10px 12px;margin-bottom:8px">
            <div style="font-size:11.5px;font-weight:700;color:#0369a1;margin-bottom:6px">📈 ETF 资金流（7日累计）</div>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px">
              <div style="text-align:center">
                <div style="font-size:10px;color:#64748b">BTC ETF</div>
                <div style="font-size:14px;font-weight:700;color:{btc_net_color}">{btc_net_str}</div>
              </div>
              <div style="text-align:center">
                <div style="font-size:10px;color:#64748b">ETH ETF</div>
                <div style="font-size:14px;font-weight:700;color:{eth_net_color}">{eth_net_str}</div>
              </div>
              <div style="text-align:center">
                <div style="font-size:10px;color:#64748b">合计净流入</div>
                <div style="font-size:14px;font-weight:700;color:{total_net_color}">{total_net_str}</div>
              </div>
            </div>
          </div>
        """)

    # 稳定币供应
    if isinstance(stab, dict) and stab.get("status") == "ok":
        total_usd = stab.get("total_usd")
        change_7d_pct = stab.get("change_7d_pct")
        change_1d_pct = stab.get("change_1d_pct")

        # 7日供应变化金额（近似：总供应量 * 7日变化率）
        supply_change_str = ""
        if change_7d_pct is not None and total_usd is not None:
            try:
                sc = float(total_usd) * float(change_7d_pct) / 100.0
                sign = "+" if sc >= 0 else ""
                color = "#dc2626" if sc > 0 else "#16a34a" if sc < 0 else "#64748b"
                if abs(sc) >= 1e9:
                    supply_change_str = f'<span style="color:{color};font-weight:700">{sign}${sc/1e9:.2f}B</span>'
                else:
                    supply_change_str = f'<span style="color:{color};font-weight:700">{sign}${sc/1e6:.0f}M</span>'
            except Exception:
                supply_change_str = "—"
        elif change_7d_pct is not None:
            chg_str, chg_color = _fmt_pct(change_7d_pct)
            supply_change_str = f'<span style="color:{chg_color};font-weight:700">{chg_str}</span>'

        # 顶部3稳定币变化（如果有 top_3）
        top_3 = stab.get("top_3") or []
        stable_html = ""
        if top_3:
            for s in top_3[:3]:
                sym = s.get("symbol", "?")
                chg = s.get("change_7d")
                chg_str, chg_color = _fmt_pct(chg)
                stable_html += f'<span style="font-size:10.5px;background:#fff;padding:2px 8px;border-radius:4px;color:#334155;margin-right:4px">{sym} <span style="color:{chg_color};font-weight:600">{chg_str}</span></span>'
        else:
            # 兜底：显示1日和7日变化率
            if change_1d_pct is not None:
                c1_str, c1_color = _fmt_pct(change_1d_pct)
                stable_html += f'<span style="font-size:10.5px;background:#fff;padding:2px 8px;border-radius:4px;color:#334155;margin-right:4px">1日 <span style="color:{c1_color};font-weight:600">{c1_str}</span></span>'
            if change_7d_pct is not None:
                c7_str, c7_color = _fmt_pct(change_7d_pct)
                stable_html += f'<span style="font-size:10.5px;background:#fff;padding:2px 8px;border-radius:4px;color:#334155;margin-right:4px">7日 <span style="color:{c7_color};font-weight:600">{c7_str}</span></span>'
            if total_usd is not None:
                stable_html += f'<span style="font-size:10.5px;background:#fff;padding:2px 8px;border-radius:4px;color:#334155;margin-right:4px">总供应 <span style="color:#166534;font-weight:600">${total_usd/1e9:.0f}B</span></span>'

        html_parts.append(f"""
          <!-- 稳定币子模块 -->
          <div style="background:linear-gradient(135deg,#f0fdf4,#dcfce7);border-radius:8px;padding:10px 12px;margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
              <div style="font-size:11.5px;font-weight:700;color:#166534">💵 稳定币供应（7日）</div>
              {supply_change_str}
            </div>
            <div>{stable_html}</div>
          </div>
        """)

    # 交易所净流量
    exchange_assets = exchange_flow.get("assets") or []
    if exchange_flow.get("status") == "ok" and exchange_assets:
        # 从 assets 里分出净流入/净流出
        top_in = sorted(
            [a for a in exchange_assets if (a.get("net_flow_usd") or 0) > 0],
            key=lambda x: x.get("net_flow_usd") or 0,
            reverse=True,
        )
        top_out = sorted(
            [a for a in exchange_assets if (a.get("net_flow_usd") or 0) < 0],
            key=lambda x: abs(x.get("net_flow_usd") or 0),
            reverse=True,
        )

        html_parts.append("""
          <!-- 交易所净流子模块 -->
          <div style="background:#fafafa;border-radius:8px;padding:10px 12px">
            <div style="font-size:11.5px;font-weight:700;color:#475569;margin-bottom:6px">🏦 交易所净流量 TOP（7日）</div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse">
              <tr>
                <td width="50%" valign="top" style="padding-right:6px">
                  <div style="font-size:10px;color:#dc2626;font-weight:600;margin-bottom:4px">▲ 净流入（提币/看多）</div>
        """)

        for item in top_in[:5]:
            sym = item.get("symbol", "?")
            net = item.get("net_flow_usd")
            net_str = _fmt_mcap(net) if net else "—"
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #f1f5f9;font-size:11px">
                <span style="color:#334155;font-weight:600">{sym}</span>
                <span style="color:#dc2626;font-weight:600">+{net_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
                <td width="50%" valign="top" style="padding-left:6px;border-left:1px solid #e2e8f0">
                  <div style="font-size:10px;color:#16a34a;font-weight:600;margin-bottom:4px">▼ 净流出（充币/看空）</div>
        """)

        for item in top_out[:5]:
            sym = item.get("symbol", "?")
            net = item.get("net_flow_usd")
            net_str = _fmt_mcap(abs(float(net))) if net else "—"
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #f1f5f9;font-size:11px">
                <span style="color:#334155;font-weight:600">{sym}</span>
                <span style="color:#16a34a;font-weight:600">-{net_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
              </tr>
            </table>
          </div>
        """)

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 4：🐳 链上异动（巨鲸转账 + 持仓集中度 + KOL链上信号）
    # ════════════════════════════════════════════════════════
    html_parts.append(f"""
      <!-- 模块4：链上异动 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px">🐳 链上异动</div>
    """)

    # 大额转账
    if whale_moves.get("status") == "ok":
        transfers = whale_moves.get("transfers") or []
        if transfers:
            html_parts.append(f"""
              <div style="font-size:11.5px;font-weight:700;color:#7c3aed;margin-bottom:5px">💸 大额转账（24h Top {len(transfers[:6])}）</div>
            """)
            for t in transfers[:6]:
                sym = t.get("symbol", "?")
                amount_usd = t.get("value_usd") or t.get("amount_usd")
                amt_str = _fmt_mcap(amount_usd) if amount_usd else "—"

                from_label = _resolve_addr_label(
                    t.get("from_label"), t.get("from_labels"),
                    t.get("from_label_names"), t.get("from_address"))
                to_label = _resolve_addr_label(
                    t.get("to_label"), t.get("to_labels"),
                    t.get("to_label_names"), t.get("to_address"))
                direction = t.get("direction") or ""

                # 判断方向：从交易所转出 = 看多；转入交易所 = 看空
                is_inflow = "exchange" in str(direction).lower() and "in" in str(direction).lower()
                is_outflow = "exchange" in str(direction).lower() and "out" in str(direction).lower()
                dot_color = "#16a34a" if is_outflow else "#dc2626" if is_inflow else "#7c3aed"

                html_parts.append(f"""
                  <div style="padding:5px 8px;margin-bottom:3px;border-radius:5px;background:#fafafa;border-left:2px solid {dot_color};font-size:11px">
                    <div style="display:flex;justify-content:space-between;align-items:center">
                      <span style="font-weight:700;color:#0f172a">{sym}</span>
                      <span style="color:#475569;font-weight:600">{amt_str}</span>
                    </div>
                    <div style="font-size:10px;color:#64748b;margin-top:1px">
                      {from_label} → {to_label}
                    </div>
                  </div>
                """)

    # 持仓集中度
    if holder_conc.get("status") == "ok":
        top_concentrated = holder_conc.get("most_concentrated") or []
        whales_buying = holder_conc.get("whale_buying") or []
        whales_selling = holder_conc.get("whale_selling") or []

        html_parts.append(f"""
          <div style="margin-top:8px">
            <div style="font-size:11.5px;font-weight:700;color:#b45309;margin-bottom:5px">🎯 巨鲸动向</div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse">
              <tr>
                <td width="50%" valign="top" style="padding-right:6px">
                  <div style="font-size:10px;color:#dc2626;font-weight:600;margin-bottom:3px">增持中</div>
        """)

        for item in whales_buying[:5]:
            sym = item.get("symbol", "?")
            chg = item.get("whale_balance_change_7d_pct") or item.get("whale_change_pct")
            chg_str, _ = _fmt_pct(chg)
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:2px 0;font-size:10.5px">
                <span style="color:#334155">{sym}</span>
                <span style="color:#dc2626;font-weight:600">+{chg_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
                <td width="50%" valign="top" style="padding-left:6px;border-left:1px solid #f1f5f9">
                  <div style="font-size:10px;color:#16a34a;font-weight:600;margin-bottom:3px">减持中</div>
        """)

        for item in whales_selling[:5]:
            sym = item.get("symbol", "?")
            chg = item.get("whale_balance_change_7d_pct") or item.get("whale_change_pct")
            chg_str, _ = _fmt_pct(chg)
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:2px 0;font-size:10.5px">
                <span style="color:#334155">{sym}</span>
                <span style="color:#16a34a;font-weight:600">{chg_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
              </tr>
            </table>
          </div>
        """)

    # KOL 链上信号（兜底）
    signals = kol_onchain.get("signals") or []
    if signals and kol_onchain.get("status") == "ok":
        # 过滤掉 symbol 明显无效的信号（长度>12、含非字母数字、常见误判词）
        INVALID_SYMBOLS = {"LAPTOP", "PHONE", "TABLET", "DESKTOP", "COMPUTER", "MOBILE"}
        def _is_valid_sym(s):
            if not s:
                return False
            s = str(s).strip().upper()
            if not s or len(s) > 12 or len(s) < 2:
                return False
            if s in INVALID_SYMBOLS:
                return False
            if not s.replace(".", "").replace("-", "").isalnum():
                return False
            return True

        valid_signals = [s for s in signals if _is_valid_sym(s.get("symbol") or s.get("event_token"))]
        if not valid_signals:
            valid_signals = signals  # 全部过滤掉时兜底，避免空列表

        kol_count = len(kol_onchain.get("kols") or [])
        html_parts.append(f"""
          <div style="margin-top:8px">
            <div style="font-size:11.5px;font-weight:700;color:#0891b2;margin-bottom:5px">🔍 KOL 链上信号（{kol_count}位分析师）</div>
        """)
        SUBTYPE_CN = {
            "exchange_flow": "交易所", "smart_money": "聪明钱",
            "accumulation": "大额吸筹", "whale_move": "巨鲸转账",
            "distribution": "大额派发", "liquidation": "爆仓清算",
        }
        for sig in valid_signals[:4]:
            subtype = sig.get("signal_subtype") or ""
            subtype_cn = SUBTYPE_CN.get(subtype, subtype)
            sym = (sig.get("event_token")
                   or sig.get("symbol")
                   or "?")
            if isinstance(sym, str):
                sym = sym.strip().upper()
            kol = sig.get("kol_name") or ""
            is_bullish = "in" in str(sig.get("event_direction", "")).lower() or "accum" in subtype.lower()
            dot = "#dc2626" if is_bullish else "#16a34a"

            html_parts.append(f"""
              <div style="padding:4px 8px;margin-bottom:2px;border-radius:4px;background:#fafafa;font-size:10.5px;border-left:2px solid {dot}">
                <span style="font-weight:700;color:#0f172a">{sym}</span>
                <span style="color:#64748b;margin-left:4px">{subtype_cn}</span>
                <span style="color:#94a3b8;margin-left:6px;float:right">{kol}</span>
              </div>
            """)
        html_parts.append("</div>")

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 5：📅 催化剂（解锁 + 宏观事件）
    # ════════════════════════════════════════════════════════
    catalyst = brief.get("M6_catalyst") or {}
    macro_events = catalyst.get("hardcoded") or []
    token_events = catalyst.get("token_events") or []
    unlock_list = upcoming_unlocks.get("unlocks") or []

    html_parts.append(f"""
      <!-- 模块5：催化剂 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px">📅 近期催化剂</div>
    """)

    # 解锁事件（更重要，放前面）
    if unlock_list:
        html_parts.append(f"""
          <div style="font-size:11.5px;font-weight:700;color:#dc2626;margin-bottom:5px">🔓 即将解锁（未来14天）</div>
        """)
        for u in unlock_list[:6]:
            sym = u.get("symbol") or u.get("token") or "?"
            unlock_date = u.get("unlock_date") or u.get("date") or ""
            amount = u.get("amount") or u.get("unlock_amount") or ""
            value_usd = u.get("value_usd") or u.get("unlock_value_usd")
            pct = (u.get("unlock_ratio_circulating")
                   or u.get("unlock_ratio_total")
                   or u.get("unlock_ratio_mcap")
                   or u.get("pct_of_supply")
                   or u.get("unlock_pct"))
            # 判断比值类型，用于提示标签
            circ_src = u.get("unlock_ratio_circulating_src")  # 'source' / 'computed' / None
            approx_prefix = "~" if circ_src == "computed" else ""
            pct_type = "流通"
            if pct is None:
                pct_label = "—"
            elif u.get("unlock_ratio_circulating") is not None and pct == u.get("unlock_ratio_circulating"):
                pct_label = f"占流通 {approx_prefix}{float(pct):.2f}%"
            elif u.get("unlock_ratio_total") is not None and pct == u.get("unlock_ratio_total"):
                pct_label = f"占总供给 {float(pct):.2f}%"
            elif u.get("unlock_ratio_mcap") is not None and pct == u.get("unlock_ratio_mcap"):
                pct_label = f"占市值 {float(pct):.2f}%"
            else:
                pct_label = f"{float(pct):.2f}%"
            try:
                days_until = (date.fromisoformat(str(unlock_date)[:10]) - date.today()).days
                days_str = f"{days_until}天后" if days_until > 0 else "今天" if days_until == 0 else "已过"
                days_color = "#dc2626" if 0 <= days_until <= 3 else ("#f59e0b" if days_until <= 7 else "#64748b")
            except Exception:
                days_str = str(unlock_date)[:10] if unlock_date else "—"
                days_color = "#64748b"

            value_str = _fmt_mcap(value_usd) if value_usd else "—"

            html_parts.append(f"""
              <div style="padding:5px 8px;margin-bottom:3px;border-radius:5px;background:#fef2f2;border-left:2px solid #dc2626;font-size:11px">
                <div style="display:flex;justify-content:space-between;align-items:center">
                  <span style="font-weight:700;color:#0f172a">{sym}</span>
                  <span style="font-size:10px;color:{days_color};font-weight:600">{days_str}</span>
                </div>
                <div style="font-size:10px;color:#64748b;margin-top:1px">
                  解锁 {value_str} · {pct_label}
                </div>
              </div>
            """)

    # 宏观 & 代币事件
    all_events = macro_events + token_events
    try:
        all_events.sort(key=lambda e: e.get("date", "9999"))
    except Exception:
        pass

    if all_events:
        html_parts.append(f"""
          <div style="margin-top:8px">
            <div style="font-size:11.5px;font-weight:700;color:#6366f1;margin-bottom:5px">📆 宏观 & 代币事件</div>
        """)
        for ev in all_events[:6]:
            ev_date = ev.get("date", "")
            ev_name = ev.get("event", "?")
            ev_type = ev.get("type", "")
            try:
                days_until = (date.fromisoformat(ev_date) - date.today()).days
                days_str = f"{days_until}d" if days_until > 0 else "今天" if days_until == 0 else "已过"
                days_color = "#dc2626" if 0 <= days_until <= 7 else ("#f59e0b" if days_until <= 14 else "#94a3b8")
            except Exception:
                days_str = ""
                days_color = "#94a3b8"

            type_badge = ""
            if ev_type == "macro":
                type_badge = '<span style="background:#e0e7ff;color:#4338ca;font-size:9px;padding:0 4px;border-radius:2px;font-weight:600;margin-right:4px">宏观</span>'
            elif ev_type == "unlock":
                type_badge = '<span style="background:#fef3c7;color:#92400e;font-size:9px;padding:0 4px;border-radius:2px;font-weight:600;margin-right:4px">解锁</span>'
            elif ev_type in ("listing", "exchange_listing"):
                type_badge = '<span style="background:#dcfce7;color:#166534;font-size:9px;padding:0 4px;border-radius:2px;font-weight:600;margin-right:4px">上币</span>'

            html_parts.append(f"""
              <div style="padding:4px 0;border-bottom:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center;font-size:11px">
                <div style="min-width:0;overflow:hidden;text-overflow:ellipsis">
                  {type_badge}<span style="color:#334155">{ev_name}</span>
                </div>
                <span style="font-size:10px;color:{days_color};font-weight:600;flex-shrink:0;margin-left:8px">{days_str}</span>
              </div>
            """)
        html_parts.append("</div>")

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 6：🎯 机会清单
    # ════════════════════════════════════════════════════════
    is_fallback = bool(all_opps and all_opps[0].get("is_fallback"))
    section_title = "🔥 今日热门币种" if is_fallback else "🎯 精选机会"

    html_parts.append(f"""
      <!-- 模块6：机会清单 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:2px">
          {section_title}
        </div>
        <div style="font-size:10.5px;color:#94a3b8;margin-bottom:8px">按综合评分排序 · 仅供参考</div>
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
            signal_sources = opp.get("signal_sources") or []

            SRC_LABEL = {
                "sector_leader": ("📈", "赛道领涨", "#059669", "#d1fae5"),
                "smart_money": ("🔵", "聪明钱", "#2563eb", "#dbeafe"),
                "exchange_flow": ("🏦", "交易所", "#d97706", "#fef3c7"),
                "accumulation": ("🟢", "吸筹", "#059669", "#d1fae5"),
                "whale_move": ("🐳", "巨鲸", "#7c3aed", "#ede9fe"),
                "distribution": ("🔴", "派发", "#dc2626", "#fee2e2"),
            }

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

            # 信号源标签
            src_html = ""
            if signal_sources:
                src_tags = []
                for src in signal_sources[:3]:
                    if src in SRC_LABEL:
                        icon, name, color, bg = SRC_LABEL[src]
                        src_tags.append(
                            f'<span style="background:{bg};color:{color};font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600">{icon} {name}</span>'
                        )
                src_html = " ".join(src_tags)

            meta_parts = []
            if sector:
                meta_parts.append(f"🏷️ {sector}")
            meta_str = " · ".join(meta_parts)

            html_parts.append(f"""
            <div style="padding:9px 11px;margin:5px 0;border-radius:7px;border-left:3px solid {accent};background:{card_bg}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
                <div style="display:flex;align-items:center;flex-wrap:wrap">
                  <span style="font-size:14px;font-weight:700;color:#0f172a">{target}</span>
                  <span style="color:{dir_color};font-size:12px;font-weight:600;margin-left:8px">{dir_icon} {dir_cn}</span>
                </div>
                <div style="display:flex;align-items:center;gap:5px">
                  <span style="background:{badge_bg};color:{badge_color};font-size:10px;padding:1px 6px;border-radius:3px;font-weight:700">{tier}</span>
                  <span style="font-size:11px;color:#64748b;font-weight:600">{score_str}分</span>
                </div>
              </div>
              {f'<div style="font-size:10.5px;color:#64748b">{meta_str}</div>' if meta_str else ''}
              {src_html}
              <div style="color:#475569;font-size:11px;margin-top:4px;line-height:1.4">{trigger}</div>
            </div>
            """)
    else:
        html_parts.append('<div style="color:#94a3b8;font-size:12px;padding:16px;text-align:center">暂无推荐机会</div>')

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 其他信号（精简折叠区）
    # ════════════════════════════════════════════════════════
    extra_blocks = []

    # 宏观背离
    divs_data = brief.get("M7_divergence") or []
    if divs_data:
        items = []
        for d in divs_data[:3]:
            sig_name = d.get("signal", "?")
            label = d.get("label", "?")
            interp = d.get("interpretation", "")
            icon = "🔴" if label == "DANGEROUS" else "🟡"
            items.append(f'<span style="background:#fee2e2;color:#991b1b;font-size:11px;padding:2px 6px;border-radius:4px;margin-right:4px">{icon} {sig_name}</span> {interp}')
        extra_blocks.append(("📡 宏观背离", "<br>".join(items)))

    # 聪明钱背离
    sm = brief.get("M8_smart_money") or {}
    if isinstance(sm, dict) and sm.get("status") == "ok" and (sm.get("bullish") or sm.get("bearish")):
        bull = sm.get("bullish") or []
        bear = sm.get("bearish") or []
        bull_str = " ".join(f'<span style="color:#16a34a;font-weight:600">🐂{s.get("symbol","?")}</span>' for s in bull[:3])
        bear_str = " ".join(f'<span style="color:#dc2626;font-weight:600">🐻{s.get("symbol","?")}</span>' for s in bear[:3])
        extra_blocks.append(("🐋 聪明钱", f"{bull_str} {bear_str}"))

    # Meme 风险
    meme = brief.get("M8_meme") or {}
    if isinstance(meme, dict) and meme.get("status") == "ok":
        summary = meme.get("summary") or {}
        if summary:
            extra_blocks.append((
                "🐸 Meme 风险",
                f"高危{summary.get('high',0)} · 中危{summary.get('medium',0)} · 低风险{summary.get('low',0)} · 排雷{summary.get('block',0)}"
            ))

    if extra_blocks:
        html_parts.append("""
          <!-- 其他信号 -->
          <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
            <div style="font-size:12px;font-weight:700;color:#0f172a;margin-bottom:8px">📌 其他信号</div>
        """)
        for title, content in extra_blocks:
            html_parts.append(f"""
            <div style="padding:6px 8px;margin:3px 0;background:#f8fafc;border-radius:5px;font-size:11.5px">
              <b style="color:#334155">{title}</b>
              <div style="color:#475569;margin-top:2px">{content}</div>
            </div>
            """)
        html_parts.append("</div>")

    # 降级标注（分层：核心红/辅助黄/增强隐藏）
    degraded_badge = _render_degraded_badge(brief)
    if degraded_badge:
        html_parts.append(degraded_badge)

    # 页脚
    html_parts.append("""
      <div style="text-align:center;font-size:10px;color:#94a3b8;margin-top:12px;padding-bottom:8px">
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

    # 2. 数据契约标准化 + 健康检查
    try:
        from brief_data_model import normalize_brief, check_brief_health
        brief = normalize_brief(brief)
        health = check_brief_health(brief)
        print(f"[INFO] 数据健康度: {health['score']}/100")
        if health["critical"]:
            print(f"[WARN] 核心数据缺失: {health['critical']}")
        if health["warning"]:
            print(f"[INFO] 辅助数据缺失: {health['warning']}")
    except Exception as e:
        print(f"[WARN] 数据标准化/健康检查失败，跳过: {e}")
        health = {"score": 0, "critical": [], "warning": []}

    # 3. 渲染 HTML
    try:
        html = render_brief_html(brief)
    except Exception as e:
        print(f"[ERROR] HTML 渲染失败: {e}")
        return 1

    if args.dry_run:
        print(html)
        return 0

    # 4. 发送邮件
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
