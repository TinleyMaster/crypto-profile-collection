"""
代币解锁数据采集：从 tokenomist.ai 用无头浏览器爬取解锁时间表。

用法:
    python phase_chain_token_unlocks.py --asset-id 1234
    python phase_chain_token_unlocks.py --symbol ARB
    python phase_chain_token_unlocks.py --asset-id 1234 --save  # 写入数据库
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

NAV_TIMEOUT = 20  # 页面导航超时（秒）
WAIT_MS = 3000      # 页面渲染等待（毫秒）


def _log(msg: str) -> None:
    """日志输出到 stderr，避免污染 stdout 的 JSON。"""
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="代币解锁数据采集")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--asset-id", "--asset_id", type=int, dest="asset_id", help="资产 ID")
    g.add_argument("--symbol", type=str, help="代币符号")
    p.add_argument("--save", action="store_true", help="写入数据库（默认只输出 JSON）")
    p.add_argument("--output-json", action="store_true", default=True, help="JSON 格式输出")
    return p


# ── 资产解析 ──────────────────────────────────────────────

def resolve_asset(conn, asset_id: int | None, symbol: str | None) -> dict | None:
    """根据 asset_id 或 symbol 查找资产信息。"""
    query = """
        SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
               asm_cg.source_asset_key AS coingecko_id
        FROM core.asset a
        LEFT JOIN core.asset_source_map asm_cg
            ON asm_cg.asset_id = a.asset_id AND asm_cg.source_code = 'cg'
        WHERE {}
    """
    if asset_id:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(query.format("a.asset_id = %s"), (asset_id,))
            return cur.fetchone()
    if symbol:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(query.format("UPPER(a.canonical_symbol) = UPPER(%s) LIMIT 1"), (symbol,))
            return cur.fetchone()
    return None


def guess_slugs(asset: dict) -> list[str]:
    """推断 tokenomist 的 token slug 候选列表。

    tokenomist 的 URL 规则: https://tokenomist.ai/{slug}
    slug 基本等于 CoinGecko 的 coin_id，即项目名的小写形式。
    如: Optimism → optimism, Arbitrum → arbitrum, Uniswap → uniswap
    """
    name = (asset.get("name") or "").strip()
    symbol = (asset.get("symbol") or "").strip().lower()

    slugs = []

    # 1. 主候选：项目名小写 + 连字符 → 覆盖绝大多数情况
    if name:
        name_slug = name.lower().replace(" ", "-")
        slugs.append(name_slug)

    # 2. symbol 兜底（如 BTC→bitcoin 等特殊映射）
    slug_map = {
        "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin",
        "sol": "solana", "matic": "polygon",
    }
    symbol_slug = slug_map.get(symbol, symbol)
    if symbol_slug and symbol_slug not in slugs:
        slugs.append(symbol_slug)

    # 3. 数据库 CG ID 作为参考（asset_source_map 可能不准，放最后）
    cg_id = asset.get("coingecko_id")
    if cg_id:
        cg_slug = cg_id.strip().lower()
        if cg_slug not in slugs:
            slugs.append(cg_slug)

    return slugs


def _search_tokenomist_slug(symbol: str, name: str) -> str | None:
    """通过 tokenomist 搜索 API 查找正确的 slug。"""
    queries = [symbol]
    if name and name.lower() != symbol.lower():
        queries.append(name)
    for q in queries:
        try:
            resp = requests.get(
                "https://tokenomist.ai/api/search",
                params={"q": q},
                headers={"Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            results = resp.json()
            if isinstance(results, list) and results:
                # 结果格式: [{slug: "...", name: "...", symbol: "..."}, ...]
                # 匹配 symbol 大小写不敏感
                for r in results:
                    r_symbol = (r.get("symbol") or "").strip().upper()
                    if r_symbol == symbol.upper():
                        slug = r.get("slug") or ""
                        if slug:
                            _log(f"  [搜索] 通过 API 找到 slug: {slug}")
                            return slug
                # 如果没有精确匹配，取第一个
                first_slug = (results[0].get("slug") or "").strip()
                if first_slug:
                    _log(f"  [搜索] 通过 API 找到 slug（首位）: {first_slug}")
                    return first_slug
        except Exception as e:
            _log(f"  [搜索API] 查询 '{q}' 失败: {e}")
    return None


# ── 页面爬取 ──────────────────────────────────────────────

def scrape_tokenomist(slugs: list[str], symbol: str = "") -> dict | None:
    """用 Playwright 爬取 tokenomist 的解锁数据。依次尝试 slugs，overview 为空则换下一个。"""
    for idx, slug in enumerate(slugs):
        is_fallback = idx > 0
        base_url = f"https://tokenomist.ai/{slug}"
        unlock_url = f"{base_url}/unlock-events"

        _log(f"  Tokenomist slug: {slug}{' (备选)' if is_fallback else ''}")
        _log(f"  目标 URL: {unlock_url}")

        result = {
            "source_url": base_url,
            "source_name": "tokenomist",
            "slug": slug,
            "overview": {},
            "unlock_events": [],
            "allocation": [],
        }

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ])
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()

                # 拦截非必要资源以加速
                page.route("**/*", lambda route: route.abort()
                    if route.request.resource_type in ("image", "font", "media", "stylesheet")
                    else route.continue_()
                )

                # ── Step 1: 爬 Overview 页面 ──
                _log("  加载 Overview 页面...")
                try:
                    page.goto(base_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
                except PlaywrightTimeout:
                    _log("  [WARN] Overview 页面加载超时，尝试用已有内容")
                except Exception as e:
                    _log(f"  [ERROR] Overview 页面导航失败: {e}")
                    browser.close()
                    continue

                page.wait_for_timeout(WAIT_MS)
                _close_popups(page)
                overview = _extract_overview(page, slug)
                result["overview"] = overview

                # 判断是否为有效页面：Overview 为空说明 slug 不对
                # - 不是最后一个 slug → 换下一个
                # - 是最后一个 slug → 也返回 None，不写空数据
                if not overview:
                    if idx + 1 < len(slugs):
                        _log(f"  Overview 为空，slug 可能不匹配，尝试备选...")
                    else:
                        _log(f"  Overview 为空且无更多备选 slug，该代币可能未被 tokenomist 收录")
                    browser.close()
                    continue

                _log(f"  Overview: {json.dumps(overview, ensure_ascii=False)}")

                # ── Step 2: 爬 Unlock Events 页面 ──
                _log("  加载 Unlock Events 页面...")
                try:
                    page.goto(unlock_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
                except PlaywrightTimeout:
                    _log("  [WARN] Unlock Events 页面加载超时")
                except Exception as e:
                    _log(f"  [WARN] Unlock Events 页面导航失败: {e}")
                    browser.close()
                    return result

                page.wait_for_timeout(WAIT_MS)
                _close_popups(page)
                events = _extract_unlock_events(page)
                result["unlock_events"] = events
                _log(f"  Unlock Events: {len(events)} 条")

                browser.close()
                return result

        except Exception as e:
            _log(f"  [ERROR] 页面爬取失败: {e}")
            _log(traceback.format_exc())
            continue

    # 所有 slug 都失败，尝试 tokenomist 搜索 API
    if symbol:
        searched_slug = _search_tokenomist_slug(symbol, "")
        if searched_slug and searched_slug not in slugs:
            _log(f"  [搜索兜底] 尝试搜索到的 slug: {searched_slug}")
            # 递归调用自己，只试这一个 slug
            return scrape_tokenomist([searched_slug], symbol)

    _log(f"  所有 slug 均失败，该代币可能未被 tokenomist 收录")
    return None


def _close_popups(page) -> None:
    """关闭 tokenomist 的广告弹窗和对话框。"""
    close_selectors = [
        'button:has-text("Dismiss")',
        'button:has-text("No thanks")',
        'button:has-text("Maybe later")',
        'button:has-text("Close")',
        '[aria-label="Close"]',
        '[aria-label="Dismiss"]',
        '.modal-close',
        '[class*="close"]',
    ]
    for sel in close_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=500):
                el.click()
                page.wait_for_timeout(200)
        except Exception:
            pass

    # 按 Escape 关闭可能的模态框
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass


def _extract_overview(page, slug: str) -> dict:
    """从 Overview 页面提取关键数据。"""
    overview: dict = {}

    try:
        full_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        full_text = ""

    # 释放进度: "57.36% Released 5.74B / 10.00B"
    m = re.search(r'([\d.]+)%\s*Released\s*([\d.]+[BMK]?)\s*/\s*([\d.]+[BMK]?)', full_text)
    if m:
        overview["released_pct"] = float(m.group(1))
        overview["released_amount_str"] = m.group(2)
        overview["total_amount_str"] = m.group(3)

    # 下一次解锁: "Aug 16 2026 92.65M ARB 1.61% ... $7.40M 5 days left"
    m = re.search(
        r'(\w{3}\s+\d{1,2}\s+\d{4})\s+([\d.]+[BMK]?)\s+\w+\s+([\d.]+)%\s+.*?\$([\d.]+[BMK]?)\s+(\d+\s+\w+\s+left)?',
        full_text
    )
    if m:
        overview["next_unlock_date"] = m.group(1)
        overview["next_unlock_amount_str"] = m.group(2)
        overview["next_unlock_pct"] = float(m.group(3))
        overview["next_unlock_value_str"] = "$" + m.group(4)
        if m.group(5):
            overview["next_unlock_when"] = m.group(5)

    # Market Cap / FDV
    m = re.search(r'Reported Market Cap\s*\$([\d.]+[BMK]?)', full_text)
    if m:
        overview["market_cap_str"] = "$" + m.group(1)

    m = re.search(r'Fully Diluted Value[^$]*\$([\d.]+[BMK]?)', full_text)
    if m:
        overview["fdv_str"] = "$" + m.group(1)

    # Float %
    m = re.search(r'Float\s*%\s*([\d.]+)%', full_text)
    if m:
        overview["float_pct"] = float(m.group(1))

    # 分配表（从 Overview 页面的 Allocation 部分）
    allocations = _extract_allocation(page)
    if allocations:
        overview["allocation"] = allocations

    return overview


def _extract_allocation(page) -> list[dict]:
    """提取分配表。"""
    allocations = []
    try:
        # 尝试找 allocation 表格行
        rows = page.locator("table tr, [class*='allocation'] [class*='row']").all()
        for row in rows[:15]:
            try:
                text = row.inner_text().strip()
                if not text:
                    continue
                # 模式: "Name 42.8% 4,278,000,000.00"
                m = re.match(r'(.+?)\s+([\d.]+)%\s+([\d,]+(?:\.\d+)?)', text)
                if m:
                    allocations.append({
                        "name": m.group(1).strip(),
                        "pct": float(m.group(2)),
                        "amount_str": m.group(3).replace(",", ""),
                    })
            except Exception:
                continue
    except Exception:
        pass
    return allocations[:10]


def _extract_unlock_events(page) -> list[dict]:
    """从 Unlock Events 页面提取事件列表。
    优先用 JS 直接解析 DOM 表格（可靠），失败回退到 innerText。
    """
    events = []

    # 方法 1：JS 提取 DOM 表格
    try:
        rows_data = page.evaluate("""
            () => {
                const tables = document.querySelectorAll('table');
                for (const table of tables) {
                    const headers = Array.from(table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td'));
                    const headerTexts = headers.map(h => h.textContent.trim().toLowerCase());
                    if (headerTexts.some(h => h === 'date') && headerTexts.some(h => h === 'value')) {
                        const rows = [];
                        const tbody = table.querySelector('tbody') || table;
                        const trs = tbody.querySelectorAll('tr');
                        for (const tr of trs) {
                            const tds = tr.querySelectorAll('td');
                            if (tds.length < 5) continue;
                            const date = tds[0].textContent.trim();
                            if (!/\\d{1,2}\\s+\\w{3}\\s+\\d{4}/.test(date)) continue;
                            rows.push({
                                date: date,
                                value: tds[1].textContent.trim(),
                                pct: tds[2].textContent.trim(),
                                allocation: tds[3].textContent.trim(),
                                status: tds[4].textContent.trim(),
                            });
                        }
                        return rows;
                    }
                }
                return [];
            }
        """)
        _log(f"  JS 提取表格: {len(rows_data)} 行")
    except Exception as e:
        _log(f"  JS 提取失败: {e}, 回退到文本解析")
        rows_data = None

    if rows_data is not None:
        seen_dates = set()
        for row in rows_data:
            date_str = row["date"]
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)

            value_str = row["value"]
            pct_str = row["pct"]
            status_str = row.get("status", "")
            alloc_str = row.get("allocation", "")

            value_match = re.match(r'^\$([\d.]+)([BMK]?)', value_str)
            if not value_match:
                continue

            value_num = float(value_match.group(1))
            unit = value_match.group(2)
            if unit == "B": value_num *= 1_000_000_000
            elif unit == "M": value_num *= 1_000_000
            elif unit == "K": value_num *= 1_000

            pct_match = re.match(r'^\+?([\d.]+)%$', pct_str)
            pct = float(pct_match.group(1)) if pct_match else 0.0

            alloc_count = 1
            m2 = re.match(r'(\d+)\s+Allocation[s]?', alloc_str)
            if m2: alloc_count = int(m2.group(1))

            is_upcoming = "left" in status_str.lower()

            events.append({
                "date": date_str, "value_usd": round(value_num, 2),
                "value_str": value_str, "pct": pct,
                "allocations": alloc_count, "status": status_str,
                "is_upcoming": is_upcoming,
            })

    # 方法 2：innerText 回退
    if not events:
        events = _extract_unlock_events_text(page)

    from datetime import datetime
    def parse_dt(s):
        try: return datetime.strptime(s, "%d %b %Y")
        except: return datetime(2000, 1, 1)

    upcoming = sorted([e for e in events if e["is_upcoming"]], key=lambda e: parse_dt(e["date"]))
    past = sorted([e for e in events if not e["is_upcoming"]], key=lambda e: parse_dt(e["date"]), reverse=True)
    return upcoming + past


def _extract_unlock_events_text(page) -> list[dict]:
    """innerText 文本解析（回退方案）。"""
    events = []
    try:
        all_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return events

    raw_lines = all_text.split("\n")
    content_lines = [l.strip() for l in raw_lines if l.strip() and l.strip() != "\t"]

    header_idx = -1
    for i in range(len(content_lines) - 4):
        if content_lines[i].lower() == "date" and content_lines[i + 1].lower() == "value":
            header_idx = i
            break
    if header_idx < 0:
        return events

    data_start = header_idx + 5
    date_re = re.compile(r'^\d{1,2}\s+\w{3}\s+\d{4}$')
    value_re = re.compile(r'^\$([\d.]+)([BMK]?)$')
    seen_dates = set()

    i = data_start
    while i + 4 < len(content_lines):
        date_str = content_lines[i]
        value_str = content_lines[i + 1]
        if date_re.match(date_str) and value_re.match(value_str):
            if date_str not in seen_dates:
                seen_dates.add(date_str)
                vm = value_re.match(value_str)
                vn = float(vm.group(1))
                u = vm.group(2)
                if u == "B": vn *= 1_000_000_000
                elif u == "M": vn *= 1_000_000
                elif u == "K": vn *= 1_000

                pm = re.match(r'^\+?([\d.]+)%$', content_lines[i + 2])
                pct = float(pm.group(1)) if pm else 0.0

                ac = 1
                am = re.match(r'(\d+)\s+Allocation[s]?', content_lines[i + 3])
                if am: ac = int(am.group(1))

                events.append({
                    "date": date_str, "value_usd": round(vn, 2), "value_str": value_str,
                    "pct": pct, "allocations": ac, "status": content_lines[i + 4],
                    "is_upcoming": "left" in content_lines[i + 4].lower(),
                })
            i += 5
        else:
            i += 1
    return events


# ── 存入数据库 ─────────────────────────────────────────────

def ensure_table(conn) -> None:
    """确保 biz.asset_token_unlocks 表存在。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.asset_token_unlocks (
                asset_id INTEGER PRIMARY KEY REFERENCES core.asset(asset_id),
                source_url TEXT,
                source_name TEXT DEFAULT 'tokenomist',
                slug TEXT,
                overview_json JSONB,
                unlock_events_json JSONB,
                scraped_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


def save_to_db(conn, asset_id: int, data: dict) -> None:
    """写入或更新 biz.asset_token_unlocks。"""
    import json as json_mod

    sql = """
        INSERT INTO biz.asset_token_unlocks (
            asset_id, source_url, source_name, slug,
            overview_json, unlock_events_json, scraped_at, updated_at
        ) VALUES (
            %(asset_id)s, %(source_url)s, %(source_name)s, %(slug)s,
            %(overview_json)s, %(unlock_events_json)s, NOW(), NOW()
        )
        ON CONFLICT (asset_id) DO UPDATE SET
            source_url = EXCLUDED.source_url,
            source_name = EXCLUDED.source_name,
            slug = EXCLUDED.slug,
            overview_json = EXCLUDED.overview_json,
            unlock_events_json = EXCLUDED.unlock_events_json,
            updated_at = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, {
            "asset_id": asset_id,
            "source_url": data.get("source_url"),
            "source_name": data.get("source_name", "tokenomist"),
            "slug": data.get("slug"),
            "overview_json": json_mod.dumps(data.get("overview", {}), ensure_ascii=False),
            "unlock_events_json": json_mod.dumps(data.get("unlock_events", []), ensure_ascii=False),
        })
    conn.commit()
    _log(f"  已写入数据库 (asset_id={asset_id})")


# ── 主流程 ─────────────────────────────────────────────────

def main() -> int:
    try:
        return _main()
    except Exception as e:
        _log(f"[FATAL] {e}")
        _log(traceback.format_exc())
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        return 2


def _check_playwright() -> str | None:
    """检查 Playwright + Chromium 是否可用，返回 None 表示正常。"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as e:
        return f"Playwright/Chromium 不可用: {e}"
    return None


def _main() -> int:
    args = build_parser().parse_args()

    # 预检 Playwright 可用性
    pw_error = _check_playwright()
    if pw_error:
        _log(f"[FATAL] {pw_error}")
        print(json.dumps({"status": "error", "message": pw_error}, ensure_ascii=False))
        return 2

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 解析资产
        asset = resolve_asset(conn, args.asset_id, args.symbol)
        if not asset:
            print(json.dumps({"status": "error", "message": "资产未找到"}, ensure_ascii=False))
            return 1

        asset_id = asset["asset_id"]
        slugs = guess_slugs(asset)

        _log(f"资产: {asset['symbol']} ({asset['name']}), asset_id={asset_id}")

        # 爬取（自动回退备选 slug）
        data = scrape_tokenomist(slugs, symbol=asset["symbol"])

        if data is None:
            # tokenomist 未收录 → 返回 not_found 状态，让上层触发 AI 测算
            print(json.dumps({
                "status": "not_found",
                "message": "该代币未被 tokenomist 收录",
                "asset_id": asset_id,
                "symbol": asset["symbol"],
                "name": asset["name"],
            }, ensure_ascii=False))
            return 0

        data["asset_id"] = asset_id
        data["symbol"] = asset["symbol"]
        data["name"] = asset["name"]
        data["status"] = "ok"

        # 写入数据库
        if args.save:
            ensure_table(conn)
            save_to_db(conn, asset_id, data)

        # JSON 输出到 stdout
        output = {k: v for k, v in data.items() if k not in ("status",)}
        print(json.dumps({"status": "ok", **output}, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    sys.exit(main())
