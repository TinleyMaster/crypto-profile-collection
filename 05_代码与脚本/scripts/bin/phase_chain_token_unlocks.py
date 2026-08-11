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
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

TIMEOUT = 30  # 页面总超时（秒）
NAV_TIMEOUT = 30  # 导航超时


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
    """根据 asset_id 或 symbol 查找资产信息，含 CoinGecko ID。"""
    query = """
        SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
               cg.source_asset_key AS coingecko_id
        FROM core.asset a
        LEFT JOIN core.asset_source_map cg
            ON cg.asset_id = a.asset_id AND cg.source_code = 'cg'
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


def guess_slug(asset: dict) -> str:
    """推断 tokenomist 的 token slug。优先 CoinGecko ID，兜底 symbol/name。"""
    cg_id = asset.get("coingecko_id")
    if cg_id:
        return cg_id.strip().lower()
    symbol = (asset.get("symbol") or "").strip().lower()
    name = (asset.get("name") or "").strip().lower().replace(" ", "-")
    # 常见映射修正
    slug_map = {
        "btc": "bitcoin",
        "eth": "ethereum",
        "bnb": "binancecoin",
        "sol": "solana",
        "matic": "polygon",
    }
    if symbol in slug_map:
        return slug_map[symbol]
    return symbol or name


# ── 页面爬取 ──────────────────────────────────────────────

def scrape_tokenomist(slug: str) -> dict | None:
    """用 Playwright 爬取 tokenomist 的解锁数据。"""
    base_url = f"https://tokenomist.ai/{slug}"
    unlock_url = f"{base_url}/unlock-events"

    print(f"  Tokenomist slug: {slug}")
    print(f"  目标 URL: {unlock_url}")

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
            browser = p.chromium.launch(headless=True)
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
            print("  加载 Overview 页面...")
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
            except PlaywrightTimeout:
                print("  [WARN] Overview 页面加载超时，尝试用已有内容")
            except Exception as e:
                print(f"  [ERROR] Overview 页面导航失败: {e}")
                browser.close()
                return None

            page.wait_for_timeout(5000)

            # 关闭广告弹窗
            _close_popups(page)

            # 提取 Overview 数据
            overview = _extract_overview(page, slug)
            result["overview"] = overview
            print(f"  Overview: {json.dumps(overview, ensure_ascii=False)}")

            # ── Step 2: 爬 Unlock Events 页面 ──
            print("  加载 Unlock Events 页面...")
            try:
                page.goto(unlock_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
            except PlaywrightTimeout:
                print("  [WARN] Unlock Events 页面加载超时")
            except Exception as e:
                print(f"  [WARN] Unlock Events 页面导航失败: {e}")
                browser.close()
                return result

            page.wait_for_timeout(5000)

            # 关闭广告弹窗
            _close_popups(page)

            # 提取解锁事件表
            events = _extract_unlock_events(page)
            result["unlock_events"] = events
            print(f"  Unlock Events: {len(events)} 条")

            browser.close()

    except Exception as e:
        print(f"  [ERROR] 页面爬取失败: {e}")
        import traceback
        traceback.print_exc()
        return None

    return result


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
            if el.is_visible(timeout=1000):
                el.click()
                print(f"  关闭弹窗: {sel}")
                page.wait_for_timeout(500)
        except Exception:
            pass

    # 按 Escape 关闭可能的模态框
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
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
    """从 Unlock Events 页面提取事件列表。"""
    events = []
    try:
        all_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        all_text = ""

    # inner_text() 把表格拆成多行，需要按 5 字段一组重组成行
    # 模式：每行 = Date / Value / Release% / Allocation / Release(状态)
    lines = [l.strip() for l in all_text.split("\n") if l.strip()]
    
    # 找到表格起始位置（"Notable Cliff Release Events" 之后）
    start_idx = -1
    for i, line in enumerate(lines):
        if "release events" in line.lower() or "all event" in line.lower():
            start_idx = i
            break
    
    # 从表格数据开始，每 5 个非空行组成一条记录
    i = start_idx + 1 if start_idx >= 0 else 0
    seen_dates = set()
    
    while i + 4 < len(lines):
        date_str = lines[i]
        value_str = lines[i + 1]
        pct_str = lines[i + 2]
        alloc_str = lines[i + 3]
        status_str = lines[i + 4]
        
        # 验证日期格式：如 "16 Aug 2026"
        date_match = re.match(r'^\d{1,2}\s+\w{3}\s+\d{4}$', date_str)
        value_match = re.match(r'^\$([\d.]+)([BMK]?)$', value_str)
        pct_match = re.match(r'^\+?([\d.]+)%$', pct_str)
        
        if date_match and value_match:
            if date_str in seen_dates:
                i += 5
                continue
            seen_dates.add(date_str)
            
            value_num = float(value_match.group(1))
            unit = value_match.group(2)
            if unit == "B":
                value_num *= 1_000_000_000
            elif unit == "M":
                value_num *= 1_000_000
            elif unit == "K":
                value_num *= 1_000
            
            pct = float(pct_match.group(1)) if pct_match else 0.0
            
            # 解析分配数量
            alloc_count = 1
            m2 = re.match(r'(\d+)\s+Allocation[s]?', alloc_str)
            if m2:
                alloc_count = int(m2.group(1))
            
            is_upcoming = "left" in status_str.lower()
            
            events.append({
                "date": date_str,
                "value_usd": round(value_num, 2),
                "value_str": value_str,
                "pct": pct,
                "allocations": alloc_count,
                "status": status_str,
                "is_upcoming": is_upcoming,
            })
            i += 5
        else:
            i += 1
    
    # 排序
    from datetime import datetime
    def parse_dt(s):
        try: return datetime.strptime(s, "%d %b %Y")
        except: return datetime(2000, 1, 1)
    
    upcoming = sorted([e for e in events if e["is_upcoming"]], key=lambda e: parse_dt(e["date"]))
    past = sorted([e for e in events if not e["is_upcoming"]], key=lambda e: parse_dt(e["date"]), reverse=True)
    return upcoming + past


def _parse_date(date_str: str):
    """将 '16 Aug 2026' 转为 datetime 对象。"""
    from datetime import datetime
    try:
        return datetime.strptime(date_str, "%d %b %Y")
    except Exception:
        return datetime(2000, 1, 1)


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
    print(f"  已写入数据库 (asset_id={asset_id})")


# ── 主流程 ─────────────────────────────────────────────────

def main() -> int:
    args = build_parser().parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 解析资产
        asset = resolve_asset(conn, args.asset_id, args.symbol)
        if not asset:
            print(json.dumps({"status": "error", "message": "资产未找到"}, ensure_ascii=False))
            return 1

        asset_id = asset["asset_id"]
        slug = guess_slug(asset)

        print(f"资产: {asset['symbol']} ({asset['name']}), asset_id={asset_id}")
        print(f"Tokenomist slug: {slug}")

        # 爬取
        data = scrape_tokenomist(slug)

        if data is None:
            print(json.dumps({"status": "error", "message": "页面爬取失败"}, ensure_ascii=False))
            return 1

        data["asset_id"] = asset_id
        data["symbol"] = asset["symbol"]
        data["name"] = asset["name"]
        data["status"] = "ok"

        # 写入数据库
        if args.save:
            ensure_table(conn)
            save_to_db(conn, asset_id, data)

        # JSON 输出
        output = {k: v for k, v in data.items() if k not in ("status",)}
        print(json.dumps({"status": "ok", **output}, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    sys.exit(main())
