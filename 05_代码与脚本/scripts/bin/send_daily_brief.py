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
