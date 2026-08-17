"""邮件通知：解锁追踪提醒（到期提醒 + 空头趋势提醒）。

使用标准库 smtplib，支持 SSL（465）与 STARTTLS（587）。
配置通过环境变量提供：SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_TO / SMTP_FROM。
"""
from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from crypto_research.config import Settings


class EmailNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.smtp_host and s.smtp_user and s.smtp_pass and s.smtp_to)

    def send(self, subject: str, body_html: str) -> tuple[bool, str]:
        """发送邮件，返回 (是否成功, 说明)。未配置时返回 False。"""
        s = self.settings
        if not self.configured:
            return False, "SMTP 未配置（缺少 SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO）"

        to_addrs = [a.strip() for a in s.smtp_to.split(",") if a.strip()]
        if not to_addrs:
            return False, "收件人 SMTP_TO 为空"

        from_addr = s.smtp_from or s.smtp_user
        msg = MIMEText(body_html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("解锁追踪提醒", "utf-8")), from_addr))
        msg["To"] = ", ".join(to_addrs)

        try:
            if s.smtp_port == 465:
                server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
                server.starttls()
            server.login(s.smtp_user, s.smtp_pass)
            server.sendmail(from_addr, to_addrs, msg.as_string())
            server.quit()
            return True, "已发送"
        except Exception as e:
            return False, f"发送失败: {e}"


def build_unlock_alert_html(symbol: str, name: str, unlock_date: str,
                            unlock_pct: float | None, days_left: int) -> str:
    """构建解锁到期提醒邮件 HTML。"""
    pct = f"{unlock_pct:.2f}%" if unlock_pct is not None else "—"
    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:auto">
      <h2 style="color:#b45309">🔓 解锁到期提醒</h2>
      <p><b>{symbol}</b>（{name}）将于 <b>{unlock_date}</b>（{days_left} 天后）发生大额解锁。</p>
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="padding:6px;border:1px solid #eee">代币</td><td style="padding:6px;border:1px solid #eee">{symbol}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">解锁日期</td><td style="padding:6px;border:1px solid #eee">{unlock_date}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">解锁占比</td><td style="padding:6px;border:1px solid #eee">{pct}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">剩余天数</td><td style="padding:6px;border:1px solid #eee">{days_left} 天</td></tr>
      </table>
      <p style="color:#666">提示：解锁前后价格往往承压，请评估做空时机。</p>
    </div>
    """


def build_trend_alert_html(symbol: str, name: str, entry_price: float,
                           last_price: float, change_pct: float) -> str:
    """构建空头趋势提醒邮件 HTML。"""
    color = "#dc2626" if change_pct < 0 else "#16a34a"
    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:auto">
      <h2 style="color:{color}">📉 空头趋势提醒</h2>
      <p><b>{symbol}</b>（{name}）价格自加入追踪以来已下跌 <b style="color:{color}">{change_pct:.2f}%</b>，形成空头趋势。</p>
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="padding:6px;border:1px solid #eee">加入时价格</td><td style="padding:6px;border:1px solid #eee">${entry_price:.6f}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">最新价格</td><td style="padding:6px;border:1px solid #eee">${last_price:.6f}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">跌幅</td><td style="padding:6px;border:1px solid #eee;color:{color}">{change_pct:.2f}%</td></tr>
      </table>
      <p style="color:#666">解锁砸盘信号已出现，请关注空头机会与止损纪律。</p>
    </div>
    """


def build_whale_transfer_alert_html(symbol: str, name: str, chain: str,
                                    value_usd: float, to_exchange: bool) -> str:
    """构建大户转账监控提醒邮件 HTML（Meme 赛道替代解锁预警）。"""
    direction = "转入交易所" if to_exchange else "链上大额转账"
    color = "#dc2626" if to_exchange else "#b45309"
    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:auto">
      <h2 style="color:{color}">🐋 大户转账监控提醒</h2>
      <p><b>{symbol}</b>（{name}）检测到一笔{direction}。</p>
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="padding:6px;border:1px solid #eee">代币</td><td style="padding:6px;border:1px solid #eee">{symbol}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">链</td><td style="padding:6px;border:1px solid #eee">{chain}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">金额</td><td style="padding:6px;border:1px solid #eee;color:{color}">${value_usd:,.2f}</td></tr>
        <tr><td style="padding:6px;border:1px solid #eee">方向</td><td style="padding:6px;border:1px solid #eee">{direction}</td></tr>
      </table>
      <p style="color:#666">大户动向是 Meme 代币的重要风险信号，请评估抛压/拉盘风险。</p>
    </div>
    """
