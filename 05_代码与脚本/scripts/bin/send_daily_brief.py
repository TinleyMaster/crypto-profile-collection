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


def render_brief_html(brief: dict) -> str:
    """将 brief dict 渲染为简洁 HTML 邮件。"""
    today = date.today().isoformat()
    m0 = brief.get("M0_tldr", {})
    diff = brief.get("DIFF", {})
    opportunities = sorted(
        (brief.get("M4_opportunities") or []) + (brief.get("M4_watchlist") or []),
        key=lambda o: (o.get("conviction_score") if isinstance(o.get("conviction_score"), (int, float)) else 0), reverse=True
    )[:5]

    # ── M0 头部 ──
    btc_price = m0.get("btc_price")
    btc_str = f"${btc_price:,.0f}" if btc_price else "N/A"
    fear_greed = m0.get("fear_greed")
    fg_str = f"{fear_greed}" if fear_greed is not None else "N/A"
    fg_label = m0.get("fear_greed_label", "")
    phase = m0.get("btc_cycle_phase", "unknown")
    btc_mvrv = m0.get("btc_mvrv_pct")
    mvrv_str = f" · MVRV {btc_mvrv:.0f}%" if btc_mvrv is not None else ""

    html_parts = [f"""
    <div style="font-family:sans-serif;max-width:680px;margin:auto;background:#fff;padding:20px">
      <h2 style="color:#1e293b;margin:0 0 8px">📊 加密大盘早报 {today}</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
        <tr>
          <td style="padding:8px;background:#f8fafc;border-radius:6px;text-align:center">
            <div style="font-size:24px;font-weight:bold">BTC {btc_str}</div>
            <div style="color:#64748b;font-size:12px">恐贪 {fg_str}{(' ' + fg_label) if fg_label else ''} · 周期 {phase}{mvrv_str}</div>
          </td>
        </tr>
      </table>
    """]

    # ── DIFF 段 ──
    if diff:
        html_parts.append('<div style="margin-bottom:16px"><b>📈 昨日变化</b><ul style="margin:4px 0;padding-left:20px">')
        for key, val in diff.items():
            if val is None:
                continue
            label = key.replace("_", " ").title()
            if isinstance(val, (int, float)):
                arrow = "↑" if val > 0 else ("↓" if val < 0 else "→")
                color = "#dc2626" if val < 0 else "#16a34a"
                html_parts.append(f'<li>{label}: <span style="color:{color}">{arrow} {val:+.1f}%</span></li>')
            elif isinstance(val, list):
                if val:  # 空列表不展示
                    html_parts.append(f'<li>{label}: {", ".join(str(x) for x in val)}</li>')
            elif isinstance(val, str):
                html_parts.append(f'<li>{label}: {val}</li>')
            # dict 等其他类型跳过
        html_parts.append("</ul></div>")

    # ── 宏观背离（M3）──
    divs = brief.get("M3_divergence") or []
    if divs:
        html_parts.append('<div style="margin-bottom:16px"><b>📡 宏观背离</b><ul style="margin:4px 0;padding-left:20px">')
        for d in divs:
            sig_name = d.get("signal", "?")
            label = d.get("label", "?")
            interp = d.get("interpretation", "")
            metrics = d.get("metrics") or {}
            metrics_str = " · ".join(f"{k}={v}" for k, v in metrics.items()) if metrics else ""
            color = "#dc2626" if label == "DANGEROUS" else "#f59e0b" if label == "DIVERGENT" else "#64748b"
            icon = "🔴" if label == "DANGEROUS" else "🟡"
            html_parts.append(
                f'<li>{icon} <b style="color:{color}">{label}</b> '
                f'({sig_name}) {interp}'
                f'{f""" <span style="color:#64748b;font-size:12px">{metrics_str}</span>""" if metrics_str else ""}</li>'
            )
        html_parts.append("</ul></div>")
    else:
        html_parts.append('<div style="margin-bottom:16px"><b>📡 宏观背离</b> <span style="color:#64748b">暂无异常信号</span></div>')

    # ── 催化剂日历（M5）──
    catalyst = brief.get("M5_catalyst") or {}
    events = catalyst.get("hardcoded") or []
    if events:
        today_date = date.today().isoformat()
        html_parts.append('<div style="margin-bottom:16px"><b>📅 催化剂日历</b><ul style="margin:4px 0;padding-left:20px">')
        for ev in events[:6]:  # 最多展示 6 条
            ev_date = ev.get("date", "")
            ev_name = ev.get("event", "?")
            ev_type = ev.get("type", "macro")
            # 标注距离今天天数
            try:
                days_until = (date.fromisoformat(ev_date) - date.today()).days
                days_str = f"（{days_until}天后）" if days_until > 0 else f"（今天）" if days_until == 0 else f"（已过）"
            except Exception:
                days_str = ""
            html_parts.append(f'<li><b>{ev_date}</b> {ev_name} {days_str}</li>')
        html_parts.append("</ul></div>")
    else:
        html_parts.append('<div style="margin-bottom:16px"><b>📅 催化剂日历</b> <span style="color:#64748b">暂无近期事件</span></div>')

    # ── 机会清单 ──
    if opportunities:
        html_parts.append('<div style="margin-bottom:16px"><b>🎯 机会清单</b>')
        for opp in opportunities[:5]:
            tier = opp.get("conviction_tier", opp.get("confidence", "?"))
            score = opp.get("conviction_score", "?")
            target = opp.get("target", "?")
            direction = opp.get("direction", "?")
            trigger = opp.get("trigger_logic", "")

            if tier == "HIGH":
                border = "border-left:4px solid #dc2626;background:#fef2f2"
            elif tier == "MED":
                border = "border-left:4px solid #f59e0b;background:#fffbeb"
            else:
                border = "border-left:4px solid #94a3b8;background:#f8fafc"

            html_parts.append(f"""
            <div style="padding:10px;margin:6px 0;border-radius:4px;{border}">
              <b>{target}</b> <span style="color:{'#dc2626' if direction=='long' else '#16a34a' if direction=='short' else '#64748b'}">{'↗' if direction=='long' else '↘' if direction=='short' else '→'} {direction}</span>
              <span style="float:right;color:#64748b">Tier: {tier} · Score: {score}</span>
              <div style="color:#475569;font-size:13px;margin-top:4px">{trigger}</div>
            </div>""")
        html_parts.append("</div>")

    # ── 共振榜（M4_resonance）──
    resonance = brief.get("M4_resonance") or {}
    resonance_signals = resonance.get("signals") if isinstance(resonance, dict) else None
    if resonance_signals:
        html_parts.append('<div style="margin-bottom:16px"><b>🎯 共振榜（共识动量 ∩ 宏观 conviction）</b>')
        for sig in resonance_signals[:5]:
            sym = sig.get("symbol", "?")
            direction = sig.get("direction", "?")
            conv_score = sig.get("conviction_score", "?")
            cons_score = sig.get("consensus_score", "?")
            source_count = sig.get("source_count", "?")
            trigger = sig.get("trigger_logic", "")
            action = sig.get("action_hint", "")

            if direction == "long":
                color = "#dc2626"
                icon = "↗"
            elif direction == "short":
                color = "#16a34a"
                icon = "↘"
            else:
                color = "#64748b"
                icon = "→"

            html_parts.append(f"""
            <div style="padding:10px;margin:6px 0;border-radius:4px;border-left:4px solid #7c3aed;background:#faf5ff">
              <b>{sym}</b> <span style="color:{color}">{icon} {direction}</span>
              <span style="float:right;color:#64748b">Conviction {conv_score} · 共识 {cons_score} · {source_count}源</span>
              <div style="color:#475569;font-size:13px;margin-top:4px">{trigger}</div>
              {'<div style="color:#7c3aed;font-size:12px;margin-top:4px">💡 ' + action + '</div>' if action else ''}
            </div>""")
        html_parts.append("</div>")
    else:
        html_parts.append('<div style="margin-bottom:16px"><b>🎯 共振榜</b> <span style="color:#64748b">暂无共识动量与宏观 conviction 共振标的</span></div>')

    # ── Meme 五维风险标签池（P0-4）──
    meme = brief.get("M4_meme") or {}
    if isinstance(meme, dict) and meme.get("status") == "ok":
        buckets = meme.get("buckets") or {}
        summary = meme.get("summary") or {}
        block = buckets.get("block") or []
        high = buckets.get("high") or []
        low = buckets.get("low") or []
        html_parts.append('<div style="margin-bottom:16px"><b>🐸 Meme 机会池 & 排雷</b>')
        if block:
            html_parts.append('<div style="font-size:13px;color:#dc2626;margin:4px 0"><b>🚫 一票否决/高危</b></div>')
            for item in block[:3]:
                flags = item.get("flags") or []
                flag_str = " | ".join(flags[:3]) if flags else ""
                html_parts.append(
                    f'<div style="font-size:13px;margin:2px 0;padding:4px 6px;background:#fef2f2;border-radius:4px">'
                    f'<b>{item.get("symbol", "?")}</b> {item.get("total_score")}分 '
                    f'{flag_str}</div>'
                )
        if low:
            html_parts.append('<div style="font-size:13px;color:#16a34a;margin:4px 0"><b>✅ 低风险观察池</b></div>')
            html_parts.append('<ul style="margin:4px 0;padding-left:20px;font-size:13px">' +
                "".join(f'<li>{it.get("symbol","?")} {it.get("total_score")}分 '
                        f'(合约{it.get("contract_label","?")}/流动性{it.get("liquidity_label","?")}/筹码{it.get("holder_label","?")})</li>'
                        for it in low[:5]) +
                '</ul>')
        if not block and not low and not high:
            html_parts.append('<div style="color:#64748b;font-size:13px">暂无有效标签数据</div>')
        html_parts.append(
            f'<div style="color:#64748b;font-size:12px">统计: block={summary.get("block",0)} high={summary.get("high",0)} '
            f'medium={summary.get("medium",0)} low={summary.get("low",0)}</div>'
        )
        html_parts.append("</div>")
    else:
        html_parts.append('<div style="margin-bottom:16px"><b>🐸 Meme 机会池 & 排雷</b> <span style="color:#64748b">暂无数据</span></div>')

    # ── 四烟囱信号（P1-3：TVL / GitHub / 融资 / 黑客）──
    chimney = brief.get("M4_chimney") or {}
    if isinstance(chimney, dict) and chimney.get("status") in ("ok", "partial"):
        html_parts.append('<div style="margin-bottom:16px"><b>🏭 四烟囱信号</b>')
        # TVL
        tvl_items = []
        for t in (chimney.get("tvl") or []):
            if t.get("type") == "category":
                for it in (t.get("items") or []):
                    chg = it.get("tvl_change_7d_pct")
                    if chg is not None:
                        tvl_items.append(f"{it.get('category','?')} {chg:+.1f}%")
            elif t.get("type") == "chain":
                for it in (t.get("items") or []):
                    chg = it.get("flow_7d_pct")
                    if chg is not None:
                        tvl_items.append(f"{it.get('chain','?')} 链 {chg:+.1f}%")
        if tvl_items:
            html_parts.append('<div style="font-size:13px"><b>TVL 异动:</b> ' + " / ".join(tvl_items[:6]) + '</div>')
        # GitHub
        gh = chimney.get("github") or []
        if gh:
            html_parts.append('<div style="font-size:13px;margin-top:4px"><b>GitHub:</b> ' +
                " / ".join(f"{g.get('symbol','?')} {g.get('direction','')}" for g in gh[:3]) + '</div>')
        # Funding
        funding = chimney.get("funding") or []
        if funding:
            html_parts.append('<div style="font-size:13px;margin-top:4px"><b>融资:</b> ' +
                " / ".join(f"{f.get('symbol','?')} {f.get('round','')} ${f.get('amount_m','?')}M" for f in funding[:3]) + '</div>')
        # Hacks
        hacks = chimney.get("hacks") or []
        if hacks:
            html_parts.append('<div style="font-size:13px;margin-top:4px;color:#dc2626"><b>安全事件:</b> ' +
                " / ".join(f"{h.get('symbol','?')} ${h.get('amount_usd',0)/1e6:.1f}M {h.get('technique','')}" for h in hacks[:3]) + '</div>')
        html_parts.append("</div>")
    else:
        html_parts.append('<div style="margin-bottom:16px"><b>🏭 四烟囱信号</b> <span style="color:#64748b">暂无数据</span></div>')

    # ── 聪明钱背离（P1-1）──
    sm = brief.get("M4_smart_money") or {}
    if isinstance(sm, dict) and sm.get("status") == "ok" and (sm.get("bullish") or sm.get("bearish")):
        html_parts.append('<div style="margin-bottom:16px"><b>🐋 聪明钱背离</b>')
        for s in (sm.get("bullish") or [])[:3]:
            html_parts.append(
                f'<div style="font-size:13px;margin:2px 0;padding:4px 6px;background:#f0fdf4;border-radius:4px">'
                f'<b>{s.get("symbol","?")}</b> {s.get("label","")} '
                f'置信{s.get("confidence","?")}% · {s.get("description","")}</div>'
            )
        for s in (sm.get("bearish") or [])[:3]:
            html_parts.append(
                f'<div style="font-size:13px;margin:2px 0;padding:4px 6px;background:#fef2f2;border-radius:4px">'
                f'<b>{s.get("symbol","?")}</b> {s.get("label","")} '
                f'置信{s.get("confidence","?")}% · {s.get("description","")}</div>'
            )
        html_parts.append("</div>")
    else:
        html_parts.append('<div style="margin-bottom:16px"><b>🐋 聪明钱背离</b> <span style="color:#64748b">暂无信号</span></div>')

    # ── 深加工：机构净流结构 + MVRV 分层 + 可操作建议（P2）──
    inst = brief.get("M2_institutional") or {}
    if isinstance(inst, dict) and inst.get("status") in ("ok", "partial"):
        html_parts.append('<div style="margin-bottom:16px"><b>🏦 机构面 & MVRV 分层</b>')
        inst_data = inst.get("institutional") or {}
        etf = inst_data.get("etf_net_flow_usd_m")
        cex = inst_data.get("cex_netflow_7d_usd")
        bias = inst_data.get("bias", "neutral")
        bias_text = {"accumulation": "累积", "distribution": "派发", "neutral": "中性"}.get(bias, bias)
        etf_str = f"{etf:+.0f}M" if isinstance(etf, (int, float)) else "N/A"
        cex_str = f"{'+' if cex > 0 else ''}{cex/1e9:.2f}B" if isinstance(cex, (int, float)) else "N/A"
        html_parts.append(
            f'<div style="font-size:13px">机构净流: ETF {etf_str} · CEX 7d 净流 {cex_str} → <b>{bias_text}</b></div>'
        )
        layers = inst.get("mvrv_layers") or {}
        html_parts.append(
            f'<div style="font-size:13px;margin-top:4px">MVRV: 深度低估{layers.get("deep_under",{}).get("count",0)} '
            f'低估{layers.get("under",{}).get("count",0)} 合理{layers.get("fair",{}).get("count",0)} 高估{layers.get("overvalued",{}).get("count",0)}</div>'
        )
        hints = inst.get("actionable_hints") or []
        if hints:
            html_parts.append('<ul style="margin:4px 0;padding-left:20px;font-size:13px">' +
                "".join(f'<li>{h}</li>' for h in hints[:3]) + '</ul>')
        html_parts.append("</div>")
    else:
        html_parts.append('<div style="margin-bottom:16px"><b>🏦 机构面 & MVRV 分层</b> <span style="color:#64748b">暂无数据</span></div>')

    # ── AI 解读（如果有） ──
    ai_narrative = brief.get("ai_narrative")
    if ai_narrative:
        # 最简 markdown → HTML 转换
        ai_html = ai_narrative
        ai_html = ai_html.replace("\n\n", "</p><p>")
        ai_html = ai_html.replace("\n", "<br>")
        ai_html = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', ai_html)
        ai_html = re.sub(r'`(.*?)`', r'<code style="background:#e2e8f0;padding:1px 4px;border-radius:3px">\1</code>', ai_html)
        html_parts.append(f'''
        <div style="margin:16px 0;padding:14px 16px;background:#f0f9ff;border-left:4px solid #3b82f6;border-radius:6px">
          <div style="font-weight:700;color:#1e40af;margin-bottom:8px;font-size:14px">🤖 AI 解读</div>
          <div style="color:#334155;font-size:13px;line-height:1.6">{ai_html}</div>
        </div>''')

    # ── 降级标注 ──
    degraded = brief.get("degraded", [])
    if degraded:
        html_parts.append(f'<div style="padding:8px;background:#fef9c3;border-radius:4px;color:#92400e;font-size:13px">⚠️ 降级项: {", ".join(degraded)}</div>')

    html_parts.append("</div>")
    return "\n".join(html_parts)


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
