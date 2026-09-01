"""Binance bapi 存活探测 + 失败邮件告警（BUG-BAPI-HEALTH-001）。

对两条核心 Binance 公开 bapi 管道做轻量存活探测：
  ① 公告 CMS（catalyst/binance_news.py BinanceNewsScraper）
  ② 广场 PGC（kol/scraper.py BinanceSquareScraper）

规则（§3.3 防误报）：
  - 连续 2 次失败才发告警邮件；恢复后发恢复通知（避免告警疲劳）。
  - HTTP 429 限流仅记日志，不计入「不可用」告警。
  - SMTP 未配置时探测照跑，仅不发送（不报错崩溃）。

用法：
    python binance_bapi_healthcheck.py          # 跑一轮并打印 JSON
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# 路径兼容（复用 kol/notifier.py 模式）：注入 scripts/src 以便 import crypto_research.*
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent
    CODE_ROOT = WORKSPACE_ROOT.parent.parent
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"
if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

TIMEOUT = 10
DETECT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/",
}


def _probe(url: str, method: str = "GET", json: dict | None = None,
           params: dict | None = None) -> tuple[bool, str]:
    """轻量存活探测。返回 (ok, diag)。"""
    try:
        r = requests.request(
            method, url, json=json, params=params,
            headers=DETECT_HEADERS, timeout=TIMEOUT,
        )
    except Exception as e:
        return False, f"请求异常: {type(e).__name__}: {e}"
    if r.status_code == 429:
        return False, "HTTP 429 限流"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        d = r.json()
    except Exception as e:
        return False, f"响应非 JSON: {e}"
    if d.get("code") != "000000":
        return False, f"网关 code={d.get('code')} msg={d.get('message')}"
    return True, "ok"


# 探测端点（与 scrapers 实际端点一致，生产已验证可用）
_PROBES = {
    "binance_news_cms": (
        "公告CMS",
        "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
        dict(method="POST", json={"type": 1, "catalogId": 49, "pageNo": 1, "pageSize": 1}),
    ),
    "binance_square_pgc": (
        "广场PGC",
        "https://www.binance.com/bapi/composite/v1/friendly/pgc/content/querySquareHomePageContentsWithFilter",
        dict(method="GET", params={"timeOffset": "-1", "filterType": "ALL", "topicId": ""}),
    ),
}

_FAIL: dict[str, int] = {}   # 连续失败计数（429 不累计）
_NOTIFIER = None


def _get_notifier():
    """惰性构造 EmailNotifier；SMTP 未配/异常返回 None（不崩溃）。"""
    global _NOTIFIER
    if _NOTIFIER is None:
        try:
            from crypto_research.clients.notifier import EmailNotifier
            from crypto_research.config import get_settings

            s = get_settings(require_database=False)
            _NOTIFIER = EmailNotifier(s) if s.smtp_host else None
        except Exception:
            _NOTIFIER = False
    return _NOTIFIER if _NOTIFIER else None


def _bjt_now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def _alert_html(cn: str, url: str, diag: str) -> str:
    return (
        "<h3>&#9888; Binance bapi 不可用：{cn}</h3>"
        "<p><b>管道</b>：{cn}</p>"
        "<p><b>探测端点</b>：<code>{url}</code></p>"
        "<p><b>探测时间（北京）</b>：{ts}</p>"
        "<p><b>失败原因</b>：{diag}</p>"
        "<p><b>影响</b>：催化剂 / KOL 信号将停止更新，请检查币安 bapi 网关。</p>"
    ).format(cn=cn, url=url, ts=_bjt_now(), diag=diag)


def _recover_html(cn: str) -> str:
    return (
        "<h3>&#9989; Binance bapi 恢复：{cn}</h3>"
        "<p><b>管道</b>：{cn}</p>"
        "<p><b>恢复时间（北京）</b>：{ts}</p>"
        "<p>探测已恢复正常，信号管道继续更新。</p>"
    ).format(cn=cn, ts=_bjt_now())


def run_healthcheck() -> dict:
    """探测全部管道；连续 2 次失败发告警，恢复发恢复通知。返回 {管道: {ok, diag, cn}}。"""
    out: dict = {}
    notifier = _get_notifier()

    for name, (cn, url, kw) in _PROBES.items():
        ok, diag = _probe(url, **kw)
        out[name] = {"ok": ok, "diag": diag, "cn": cn}

        if ok:
            if _FAIL.get(name, 0) >= 2 and notifier:
                try:
                    notifier.send(f"【恢复通知】Binance bapi 恢复 - {cn}", _recover_html(cn))
                    logger.info("恢复通知已发送: %s", cn)
                except Exception as e:
                    logger.error("恢复通知发送失败 %s: %s", cn, e)
            _FAIL[name] = 0
            continue

        if diag == "HTTP 429 限流":
            # 限流不计入「不可用」告警（避免抖动误报），仅记日志
            logger.warning("[%s] 429 限流（不计入告警）", name)
            _FAIL[name] = 0
            continue

        _FAIL[name] = _FAIL.get(name, 0) + 1
        if _FAIL[name] >= 2 and notifier:
            try:
                notifier.send(f"【数据告警】Binance bapi 不可用 - {cn}", _alert_html(cn, url, diag))
                logger.info("告警邮件已发送: %s", cn)
            except Exception as e:
                logger.error("告警邮件发送失败 %s: %s", cn, e)

    return out


if __name__ == "__main__":
    print(json.dumps(run_healthcheck(), ensure_ascii=False, indent=2))