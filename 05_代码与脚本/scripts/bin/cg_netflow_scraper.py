#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CoinGlass 网页端 netflow 抓取器（绕过 capi 加密响应）— Python 版。

思路：用真实浏览器渲染 https://www.coinglass.com/InflowAndOutflow，
     前端 JS 自动解密并渲染到 DOM，直接读 DOM 精准数值（aria-label）。

与 JS 版（cg_netflow_scraper.js）等价，改用 Python Playwright，
复用容器已有的 playwright + chromium（无需 Node.js / 本机 Chrome）。

输出：结构化 JSON（主表逐笔 + 链上告警 + 按交易所/币种聚合净流）
  同时打印到 stdout（可被管道消费）并写入 cg_netflow_latest.json

用法：
  python cg_netflow_scraper.py [--no-write]

环境变量：
  CG_URL        - 覆盖默认 CoinGlass 页面 URL（离线测试用）
  CHROME_EXEC   - 覆盖 chromium 可执行文件路径（不设则用 playwright 自带）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
URL = os.environ.get("CG_URL") or "https://www.coinglass.com/InflowAndOutflow"
CHROME_EXEC = os.environ.get("CHROME_EXEC") or None

BACKOFF = [0, 30, 60]  # 秒；首次不睡，之后 30s / 60s 冷却
WAIT_STABLE_MAX = 45000  # 等待稳定最长 45s
WAIT_STABLE_TICK = 2000  # 每 2s 检查一次
WAIT_STABLE_THRESHOLD = 3  # 连续 3 次不变 => 渲染完成


def _num_attr(el) -> float | None:
    """从 DOM 元素中提取 .Number 的 aria-label 精确值。"""
    if el is None:
        return None
    n = el.query_selector(".Number")
    if n is None:
        return None
    label = n.get_attribute("aria-label")
    if not label:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", label)
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_page(page) -> dict:
    """解析主表 + 告警表。"""
    main, alert = [], []
    
    # A-1: 使用 Ant Design 专用选择器
    # 主表: tbody.ant-table-tbody > tr.ant-table-row（排除 measure-row 和 placeholder）
    main_rows = page.query_selector_all('table tbody.ant-table-tbody tr.ant-table-row')
    print(f"[parse] 主表行数 (ant-table-row): {len(main_rows)}", file=sys.stderr)
    
    for r in main_rows:
        tds = r.query_selector_all("td.ant-table-cell")
        if not tds or len(tds) < 5:
            continue
        
        # 提取 symbol
        sym_el = r.query_selector(".symbol-name")
        symbol = sym_el.inner_text().strip() if sym_el else re.sub(r"\s+", " ", tds[0].inner_text()).strip()
        
        main.append({
            "symbol": symbol,
            "exchange": tds[1].inner_text().strip(),
            "side": tds[2].inner_text().strip(),
            "qty": _num_attr(tds[3]),
            "qty_display": tds[3].inner_text().strip(),
            "value": _num_attr(tds[4]),
            "value_display": tds[4].inner_text().strip(),
            "time": tds[5].inner_text().strip() if len(tds) > 5 else "",
        })
    
    # 告警表（如有）
    alert_tables = page.query_selector_all('table')
    for t in alert_tables:
        headers = [th.inner_text().strip() for th in t.query_selector_all("thead th")]
        if "From" in headers and "To" in headers:
            alert_rows = t.query_selector_all('tbody.ant-table-tbody tr.ant-table-row')
            for r in alert_rows:
                tds = r.query_selector_all("td.ant-table-cell")
                if not tds or len(tds) < 4:
                    continue
                sym_el = r.query_selector(".symbol-name")
                symbol = sym_el.inner_text().strip() if sym_el else tds[0].inner_text().strip()
                alert.append({
                    "symbol": symbol,
                    "from": tds[1].inner_text().strip(),
                    "to": tds[2].inner_text().strip(),
                    "qty": _num_attr(tds[3]),
                    "qty_display": tds[3].inner_text().strip(),
                    "time": tds[4].inner_text().strip() if len(tds) > 4 else "",
                })
    
    print(f"[parse] main_rows={len(main)}, alert_rows={len(alert)}", file=sys.stderr)
    return {"main": main, "alert": alert}


def aggregate_netflow(main_rows: list[dict]) -> dict:
    """按 (exchange, symbol) 聚合净流。"""
    agg: dict[str, dict] = {}
    for r in main_rows:
        side = (r.get("side") or "").lower()
        if side not in ("inflow", "outflow"):
            continue
        key = f"{r.get('exchange')}||{r.get('symbol')}"
        a = agg.setdefault(key, {
            "exchange": r.get("exchange"), "symbol": r.get("symbol"),
            "inflow_qty": 0.0, "outflow_qty": 0.0, "inflow_usd": 0.0, "outflow_usd": 0.0,
            "tx": 0,
        })
        a["tx"] += 1
        if side == "inflow":
            a["inflow_qty"] += r.get("qty") or 0
            a["inflow_usd"] += r.get("value") or 0
        else:
            a["outflow_qty"] += r.get("qty") or 0
            a["outflow_usd"] += r.get("value") or 0

    netflow = []
    for a in agg.values():
        netflow.append({
            "exchange": a["exchange"], "symbol": a["symbol"], "tx": a["tx"],
            "inflow_qty": round(a["inflow_qty"], 4), "outflow_qty": round(a["outflow_qty"], 4),
            "net_qty": round(a["inflow_qty"] - a["outflow_qty"], 4),
            "inflow_usd": round(a["inflow_usd"], 2), "outflow_usd": round(a["outflow_usd"], 2),
            "net_usd": round(a["inflow_usd"] - a["outflow_usd"], 2),
        })
    netflow.sort(key=lambda x: x["net_usd"], reverse=True)
    return netflow


def wait_for_data(page, timeout_ms: int = 90000) -> int:
    """A-2: 显式等待 tr.ant-table-row 计数 > 0，超时返回 0。"""
    selector = 'table tbody.ant-table-tbody tr.ant-table-row'
    waited = 0
    tick = 2000
    
    while waited < timeout_ms:
        rc = page.evaluate(f"() => document.querySelectorAll('{selector}').length")
        if rc > 0:
            # 等待数据稳定（连续 2 次相同）
            page.wait_for_timeout(2000)
            rc2 = page.evaluate(f"() => document.querySelectorAll('{selector}').length")
            if rc2 > 0:
                print(f"[wait] 数据行就绪: {rc2} 行", file=sys.stderr)
                return rc2
        page.wait_for_timeout(tick)
        waited += tick
        if waited % 10000 == 0:
            print(f"[wait] 已等待 {waited/1000}s，当前行数={rc}", file=sys.stderr)
    
    print(f"[wait] 超时 ({timeout_ms/1000}s)，行数=0", file=sys.stderr)
    return 0


def write_output(out: dict, no_write: bool) -> None:
    json_str = json.dumps(out, ensure_ascii=False, indent=2)
    if not no_write:
        fp = SCRIPT_DIR / "cg_netflow_latest.json"
        fp.write_text(json_str, encoding="utf-8")
        print(f"[written] {fp}", file=sys.stderr)
    print(json_str)


def run(no_write: bool = False) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_EXEC,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--no-proxy-server",
                "--disable-web-security",
                "--disable-features=VizDisplayCompositor",
            ],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "accept-language": "en-US,en;q=0.9",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
        )
        
        # 注入反检测脚本
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)
        
        page = context.new_page()
        
        print(f"[fetch] 访问 {URL}", file=sys.stderr)
        resp = page.goto(URL, wait_until="networkidle", timeout=60000)
        status = resp.status if resp else None
        print(f"[fetch] HTTP={status}", file=sys.stderr)
        
        # A-2: 显式等待数据行
        row_count = wait_for_data(page, timeout_ms=90000)
        print(f"[fetch] 行数={row_count}", file=sys.stderr)

        # A-6: 监控降级保护 - HTTP=200 但 0 行判失败
        if row_count == 0 and status == 200:
            # 收集诊断信息
            diag = page.evaluate(
                "() => ({ title: document.title, bodyLen: document.body.innerText.length,"
                " hasAntTable: !!document.querySelector('.ant-table'),"
                " hasTbody: !!document.querySelector('tbody.ant-table-tbody'),"
                " tbodyRows: document.querySelectorAll('tbody.ant-table-tbody tr').length,"
                " allTables: document.querySelectorAll('table').length,"
                " bodyHead: document.body.innerText.slice(0, 500) })"
            )
            print(f"[FAIL] HTTP=200 但行数=0（疑似反爬/结构变更）。诊断: {json.dumps(diag, ensure_ascii=False)}", file=sys.stderr)
            
            # A-5: 触发结构变更监控告警
            _check_structure_change(page, row_count, status)
            
            # 写降级 JSON
            degraded = _make_degraded_json(status, "HTTP=200 但数据行=0（疑似反爬/结构变更）")
            if not no_write:
                (SCRIPT_DIR / "cg_netflow_latest.json").write_text(
                    json.dumps(degraded, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
            return 2
                print(f"[written degraded] {SCRIPT_DIR / 'cg_netflow_latest.json'}", file=sys.stderr)
            browser.close()
            return 2

        parsed = parse_page(page)
        browser.close()

    netflow = aggregate_netflow(parsed["main"])
    total_in = sum((r.get("value") or 0) for r in parsed["main"]
                   if (r.get("side") or "").lower() == "inflow")
    total_out = sum((r.get("value") or 0) for r in parsed["main"]
                    if (r.get("side") or "").lower() == "outflow")

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "http_status": status,
        "main_rows": len(parsed["main"]),
        "alert_rows": len(parsed["alert"]),
        "main_table": parsed["main"],
        "alert_history": parsed["alert"],
        "netflow_by_exchange_coin": netflow,
        "summary": {
            "total_inflow_usd": round(total_in, 2),
            "total_outflow_usd": round(total_out, 2),
            "net_usd": round(total_in - total_out, 2),
            "distinct_coins": len(netflow),
        },
    }

    # 页面渲染出行但无主表数据（数据 XHR 被节流/未加载）=> 同样降级，避免消费侧误读
    if len(parsed["main"]) == 0:
        out["degraded"] = True
        out["degrade_reason"] = "主表无数据行（数据源限流或未加载）"

    write_output(out, no_write)
    
    # A-5: 成功抓取后保存结构基线
    if len(parsed["main"]) > 0:
        _save_baseline({
            "last_success_ts": datetime.now(timezone.utc).isoformat(),
            "row_count": len(parsed["main"]),
            "col_count": 6,
            "selectors_present": {
                "tbody.ant-table-tbody": True,
                "tr.ant-table-row": True,
                "td.ant-table-cell": True,
                "img[src*=cdn.coinglasscdn.com]": True,
            },
            "page_http": status,
        })
        print(f"[baseline] 已保存基线: {len(parsed['main'])} 行", file=sys.stderr)
    
    return 0


# ═══ A-5: 结构变更监控 ═══

BASELINE_FILE = SCRIPT_DIR / "cg_netflow_baseline.json"


def _load_baseline() -> dict | None:
    """加载结构基线。"""
    if not BASELINE_FILE.exists():
        return None
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _save_baseline(data: dict) -> None:
    """保存结构基线。"""
    tmp = BASELINE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BASELINE_FILE)


def _check_structure_change(page, row_count: int, http_status: int) -> list[str]:
    """检查结构变更，返回触发的告警信号列表。"""
    alerts = []
    baseline = _load_baseline()
    
    # 收集当前指纹
    fingerprint = {
        "row_count": row_count,
        "http_status": http_status,
        "selectors_present": {
            "tbody.ant-table-tbody": bool(page.query_selector('tbody.ant-table-tbody')),
            "tr.ant-table-row": bool(page.query_selector('tr.ant-table-row')),
            "td.ant-table-cell": bool(page.query_selector('td.ant-table-cell')),
            "img[src*=cdn.coinglasscdn.com]": bool(page.query_selector('img[src*="cdn.coinglasscdn.com"]')),
        },
    }
    
    # 检测告警条件
    if row_count == 0:
        alerts.append("空数据：tr.ant-table-row 计数=0")
    
    if not fingerprint["selectors_present"]["tbody.ant-table-tbody"]:
        alerts.append("选择器缺失：tbody.ant-table-tbody 不存在")
    
    if not fingerprint["selectors_present"]["tr.ant-table-row"]:
        alerts.append("选择器缺失：tr.ant-table-row 不存在")
    
    if http_status != 200:
        alerts.append(f"网络异常：HTTP={http_status}")
    
    # 与基线对比
    if baseline:
        if baseline.get("col_count") and fingerprint.get("col_count"):
            if fingerprint["col_count"] != baseline["col_count"]:
                alerts.append(f"列数漂移：{baseline['col_count']}→{fingerprint['col_count']}")
        
        if not fingerprint["selectors_present"].get("img[src*=cdn.coinglasscdn.com]"):
            if baseline.get("selectors_present", {}).get("img[src*=cdn.coinglasscdn.com]"):
                alerts.append("首列图标丢失：img[src*=cdn.coinglasscdn.com] 缺失")
    
    # 如果有告警，发送邮件
    if alerts:
        _send_alert_email(alerts, baseline, fingerprint)
    
    return alerts


def _send_alert_email(alerts: list[str], baseline: dict | None, fingerprint: dict) -> None:
    """发送结构变更告警邮件。"""
    try:
        from crypto_research.config import get_settings
        settings = get_settings(require_database=False)
        
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = os.environ.get("SMTP_PORT", "465")
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASS")
        smtp_to = os.environ.get("SMTP_TO")
        smtp_from = os.environ.get("SMTP_FROM", smtp_user)
        
        if not all([smtp_host, smtp_user, smtp_pass, smtp_to]):
            print("[alert] SMTP 未配置，跳过邮件告警", file=sys.stderr)
            return
        
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subject = f"[CoinGlass 净流] 页面结构变更告警 {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        
        baseline_info = ""
        if baseline:
            baseline_info = f"""
上次成功基线：
  行数: {baseline.get('row_count', '?')}
  列数: {baseline.get('col_count', '?')}
  时间: {baseline.get('last_success_ts', '?')}
"""
        
        body = f"""CoinGlass 净流抓取器结构变更告警

时间: {now}
命中信号:
{chr(10).join(f'  - {a}' for a in alerts)}

当前指纹:
  行数: {fingerprint.get('row_count', 0)}
  HTTP: {fingerprint.get('http_status', '?')}
  选择器: {json.dumps(fingerprint.get('selectors_present', {}), indent=4)}
{baseline_info}
建议：
  1. 检查 A-1 选择器是否需更新
  2. 若反复失败，回流方案 B（open-api-v4 spot/coin/netflow）
"""
        
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = smtp_from
        msg["To"] = smtp_to
        msg.attach(MIMEText(body, "plain", "utf-8"))
        
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port)) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        
        print(f"[alert] 告警邮件已发送至 {smtp_to}", file=sys.stderr)
        
    except Exception as e:
        print(f"[alert] 邮件发送失败: {e}", file=sys.stderr)


def _make_degraded_json(http_status: int, reason: str) -> dict:
    """生成降级 JSON。"""
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "http_status": http_status,
        "main_rows": 0,
        "alert_rows": 0,
        "main_table": [],
        "alert_history": [],
        "netflow_by_exchange_coin": [],
        "summary": {"total_inflow_usd": 0, "total_outflow_usd": 0, "net_usd": 0, "distinct_coins": 0},
        "degraded": True,
        "degrade_reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="CoinGlass netflow 抓取器（Python 版）")
    parser.add_argument("--no-write", action="store_true", help="不写文件，仅 stdout")
    args = parser.parse_args()
    try:
        return run(no_write=args.no_write)
    except Exception as e:
        print(f"FATAL {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())