"""
KOL 信号邮件提醒。

复用系统已有的 EmailNotifier 封装，发送 HTML 格式邮件。
邮件内容包含：
  - 信号详情（方向、标的、入场条件、止损、止盈、杠杆、置信度）
  - 原文摘要
  - 原文链接
  - 发帖时间 + 检测延迟
  - 交叉验证信息（解锁、链上转账、资金费率、OI、社交热度）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 路径兼容
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
    CODE_ROOT = WORKSPACE_ROOT.parent
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.clients.notifier import EmailNotifier  # noqa: E402

from .db import get_conn  # noqa: E402

_settings = get_settings(require_database=False)
_notifier = EmailNotifier(_settings) if _settings.smtp_host else None


def _get_notifier() -> EmailNotifier | None:
    global _notifier, _settings
    if _notifier is None:
        _settings = get_settings(require_database=False)
        if _settings.smtp_host:
            _notifier = EmailNotifier(_settings)
    return _notifier if _notifier and _notifier.configured else None


def get_cross_validation_data(asset_id: int | None) -> dict:
    """
    获取币种的交叉验证数据，附在邮件中作为参考。

    查询内容：
      - 未来 7 天有无大额解锁
      - 过去 24h 有无链上大额转账到交易所
      - 当前资金费率是否极端
      - 未平仓合约变化
      - 社交热度趋势

    数据缺失时返回空字符串，不影响邮件发送。
    """
    if not asset_id:
        return {}

    data: dict = {}

    try:
        with get_conn() as conn:
            # 1. 未来 7 天解锁
            row = conn.execute(
                "SELECT unlock_date, unlock_amount, unlock_pct_of_supply "
                "FROM biz.asset_unlock_event "
                "WHERE asset_id = %s "
                "  AND unlock_date >= CURRENT_DATE "
                "  AND unlock_date <= CURRENT_DATE + INTERVAL '7 days' "
                "ORDER BY unlock_date LIMIT 3",
                (asset_id,),
            ).fetchall()
            if row:
                data["unlocks"] = [
                    {
                        "date": str(r["unlock_date"]),
                        "amount": float(r["unlock_amount"]) if r["unlock_amount"] else None,
                        "pct": float(r["unlock_pct_of_supply"]) if r["unlock_pct_of_supply"] else None,
                    }
                    for r in row
                ]

            # 2. 过去 24h 大额转账到交易所
            row = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(value_usd), 0) as total_usd "
                "FROM biz.onchain_transfer_log "
                "WHERE asset_id = %s "
                "  AND to_exchange = TRUE "
                "  AND created_at >= NOW() - INTERVAL '24 hours'",
                (asset_id,),
            ).fetchone()
            if row and row["cnt"] > 0:
                data["whale_transfers_24h"] = {
                    "count": row["cnt"],
                    "total_usd": float(row["total_usd"]),
                }

            # 3. 最新衍生品数据（资金费率、OI）
            row = conn.execute(
                "SELECT funding_rate, open_interest, oi_change_24h, "
                "       cvd_24h, liquidations_long_24h, liquidations_short_24h "
                "FROM biz.asset_derivatives "
                "WHERE asset_id = %s "
                "ORDER BY updated_at DESC LIMIT 1",
                (asset_id,),
            ).fetchone()
            if row:
                data["derivatives"] = {
                    "funding_rate": float(row["funding_rate"]) if row["funding_rate"] else None,
                    "open_interest": float(row["open_interest"]) if row["open_interest"] else None,
                    "oi_change_24h": float(row["oi_change_24h"]) if row["oi_change_24h"] else None,
                    "cvd_24h": float(row["cvd_24h"]) if row["cvd_24h"] else None,
                }
    except Exception as e:
        print(f"[KOL][notifier] 交叉验证数据查询失败: {e}")

    return data


def build_signal_alert_html(signal: dict, cross_data: dict) -> str:
    """构建信号提醒邮件 HTML。"""
    direction_cn = {"long": "做多", "short": "做空", "neutral": "中性"}.get(
        signal.get("direction", ""), signal.get("direction", "—")
    )
    direction_color = "#dc2626" if signal.get("direction") == "long" else (
        "#16a34a" if signal.get("direction") == "short" else "#6b7280"
    )
    post_type_cn = {
        "prediction": "实时喊单",
        "after_action": "事后晒单",
        "analysis": "行情分析",
    }.get(signal.get("post_type", ""), signal.get("post_type", ""))

    symbol = signal.get("symbol") or "—"
    entry_condition = signal.get("entry_condition") or "—"
    entry_price = f"${signal['entry_price']:,.2f}" if signal.get("entry_price") else "—"
    stop_loss = f"${signal['stop_loss']:,.2f}" if signal.get("stop_loss") else "—"
    take_profit = f"${signal['take_profit']:,.2f}" if signal.get("take_profit") else "—"
    leverage = f"{signal['leverage']:.1f}x" if signal.get("leverage") else "—"
    confidence = f"{signal['confidence']*100:.1f}%" if signal.get("confidence") else "—"

    # 原文摘要（截取前 300 字）
    content = signal.get("content_text", "")
    summary = content[:300] + ("..." if len(content) > 300 else "")

    # 发帖时间和检测延迟
    posted_at = signal.get("posted_at", "")
    try:
        if isinstance(posted_at, str) and posted_at:
            dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
            posted_at_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            delay = (datetime.now(timezone.utc) - dt).total_seconds()
            if delay < 60:
                delay_str = f"{delay:.0f} 秒"
            elif delay < 3600:
                delay_str = f"{delay/60:.1f} 分钟"
            else:
                delay_str = f"{delay/3600:.1f} 小时"
        else:
            posted_at_str = str(posted_at) if posted_at else "—"
            delay_str = "—"
    except Exception:
        posted_at_str = str(posted_at) if posted_at else "—"
        delay_str = "—"

    post_url = signal.get("post_url", "#")
    profile_nickname = signal.get("profile_nickname", "未知博主")
    platform = signal.get("platform_code", "")

    # 交叉验证 HTML
    cross_html = _build_cross_validation_html(cross_data)

    return f"""
    <div style="font-family:sans-serif;max-width:640px;margin:auto;padding:20px">
      <div style="background:#f8fafc;border-radius:8px;padding:16px 20px;margin-bottom:20px">
        <div style="color:#64748b;font-size:13px;margin-bottom:4px">KOL 实时信号</div>
        <h2 style="margin:0;color:{direction_color};font-size:22px">
          {direction_cn} {symbol} — {entry_condition}
        </h2>
        <div style="color:#64748b;margin-top:8px;font-size:14px">
          博主：<b>{profile_nickname}</b>（{platform}）
        </div>
      </div>

      <h3 style="color:#1e293b;border-bottom:2px solid #e2e8f0;padding-bottom:6px">信号详情</h3>
      <table style="border-collapse:collapse;width:100%;margin-bottom:20px">
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc;width:30%">类型</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0">{post_type_cn}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">方向</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;color:{direction_color}"><b>{direction_cn}</b></td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">标的</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0"><b>{symbol}</b></td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">入场条件</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0">{entry_condition}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">入场价格</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0">{entry_price}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">止损</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;color:#dc2626">{stop_loss}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">止盈</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;color:#16a34a">{take_profit}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">杠杆</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0">{leverage}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc">置信度</td>
          <td style="padding:8px 12px;border:1px solid #e2e8f0">{confidence}</td>
        </tr>
      </table>

      <h3 style="color:#1e293b;border-bottom:2px solid #e2e8f0;padding-bottom:6px">原文摘要</h3>
      <div style="background:#f8fafc;border-radius:6px;padding:12px 16px;margin-bottom:20px;
           white-space:pre-wrap;font-size:14px;line-height:1.6;color:#334155">
{summary}
      </div>

      <div style="margin-bottom:20px;font-size:13px;color:#64748b">
        <div>发帖时间：{posted_at_str}</div>
        <div>检测延迟：{delay_str}</div>
        <div>原文链接：<a href="{post_url}" target="_blank" style="color:#3b82f6">{post_url}</a></div>
      </div>

      {cross_html}

      <div style="margin-top:24px;padding-top:16px;border-top:1px solid #e2e8f0;
           font-size:12px;color:#94a3b8;text-align:center">
        此邮件由 KOL 信号监控系统自动发送，仅供参考，不构成投资建议。
      </div>
    </div>
    """


def _build_cross_validation_html(data: dict) -> str:
    """构建交叉验证数据的 HTML 片段。"""
    if not data:
        return ""

    sections = []

    # 解锁
    unlocks = data.get("unlocks", [])
    if unlocks:
        rows = ""
        for u in unlocks:
            pct = f"{u['pct']:.2f}%" if u.get("pct") else "—"
            amount = f"{u['amount']:,.0f}" if u.get("amount") else "—"
            rows += f"<tr><td style='padding:6px 10px;border:1px solid #e2e8f0'>{u['date']}</td>"
            rows += f"<td style='padding:6px 10px;border:1px solid #e2e8f0'>{amount}</td>"
            rows += f"<td style='padding:6px 10px;border:1px solid #e2e8f0;color:#b45309'>{pct}</td></tr>"
        sections.append(f"""
        <div style="margin-bottom:16px">
          <h4 style="color:#b45309;margin:0 0 8px 0">🔓 未来 7 天解锁提醒</h4>
          <table style="border-collapse:collapse;width:100%;font-size:13px">
            <tr style="background:#f8fafc">
              <th style="padding:6px 10px;border:1px solid #e2e8f0;text-align:left">日期</th>
              <th style="padding:6px 10px;border:1px solid #e2e8f0;text-align:left">数量</th>
              <th style="padding:6px 10px;border:1px solid #e2e8f0;text-align:left">占比</th>
            </tr>
            {rows}
          </table>
        </div>
        """)

    # 大额转账
    whale = data.get("whale_transfers_24h")
    if whale:
        sections.append(f"""
        <div style="margin-bottom:16px">
          <h4 style="color:#dc2626;margin:0 0 8px 0">🐋 24h 大额转账到交易所</h4>
          <div style="font-size:14px">
            共 <b>{whale['count']}</b> 笔，合计 <b style="color:#dc2626">${whale['total_usd']:,.0f}</b>
          </div>
        </div>
        """)

    # 衍生品
    deriv = data.get("derivatives")
    if deriv:
        fr = deriv.get("funding_rate")
        fr_str = f"{fr*100:.4f}%" if fr is not None else "—"
        fr_color = "#dc2626" if fr and fr > 0.001 else (
            "#16a34a" if fr and fr < -0.001 else "#64748b"
        )
        oi = deriv.get("open_interest")
        oi_str = f"${oi:,.0f}" if oi else "—"
        oi_change = deriv.get("oi_change_24h")
        oi_change_str = f"{oi_change*100:+.2f}%" if oi_change is not None else "—"
        oi_change_color = "#dc2626" if oi_change and oi_change > 0 else (
            "#16a34a" if oi_change and oi_change < 0 else "#64748b"
        )
        sections.append(f"""
        <div style="margin-bottom:16px">
          <h4 style="color:#0891b2;margin:0 0 8px 0">📊 衍生品数据</h4>
          <table style="border-collapse:collapse;width:100%;font-size:13px">
            <tr>
              <td style="padding:6px 10px;border:1px solid #e2e8f0;background:#f8fafc;width:40%">资金费率</td>
              <td style="padding:6px 10px;border:1px solid #e2e8f0;color:{fr_color}"><b>{fr_str}</b></td>
            </tr>
            <tr>
              <td style="padding:6px 10px;border:1px solid #e2e8f0;background:#f8fafc">未平仓合约 (OI)</td>
              <td style="padding:6px 10px;border:1px solid #e2e8f0">{oi_str}</td>
            </tr>
            <tr>
              <td style="padding:6px 10px;border:1px solid #e2e8f0;background:#f8fafc">OI 24h 变化</td>
              <td style="padding:6px 10px;border:1px solid #e2e8f0;color:{oi_change_color}">{oi_change_str}</td>
            </tr>
          </table>
        </div>
        """)

    if not sections:
        return ""

    return f"""
    <h3 style="color:#1e293b;border-bottom:2px solid #e2e8f0;padding-bottom:6px">
      📡 交叉验证参考
    </h3>
    {''.join(sections)}
    """


def send_signal_alert(signal: dict) -> tuple[bool, str]:
    """
    发送信号提醒邮件。

    Args:
        signal: 信号字典（需包含 post 关联信息）

    Returns:
        (是否成功, 说明)
    """
    notifier = _get_notifier()
    if not notifier:
        return False, "SMTP 未配置"

    # 获取交叉验证数据
    cross_data = get_cross_validation_data(signal.get("asset_id"))

    # 构建邮件
    direction_cn = {"long": "做多", "short": "做空", "neutral": "中性"}.get(
        signal.get("direction", ""), signal.get("direction", "")
    )
    symbol = signal.get("symbol") or "未知币种"
    entry = signal.get("entry_condition") or "新信号"
    nickname = signal.get("profile_nickname", "KOL")

    subject = f"【实时信号】{nickname} - {direction_cn}{symbol} - {entry}"
    html = build_signal_alert_html(signal, cross_data)

    success, msg = notifier.send(subject, html)
    return success, msg
