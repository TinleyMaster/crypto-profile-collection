"""
代币经济学提取：多源聚合 → LLM 提取 → 结构化入库。

流程：
1. 收集资产的所有相关文档（tokenomics/whitepaper/docs + 官网子页面）
2. 抓取各页面内容（HTML → 纯文本）
3. 可选：拉取 CMC/CG supply 数据
4. 全部喂给 LLM，提取结构化 tokenomics 数据
5. 写入 biz.asset_tokenomics

用法:
    python phase_c_extract_tokenomics.py --asset-id 1234
    python phase_c_extract_tokenomics.py --symbol KOMA
    python phase_c_extract_tokenomics.py --asset-id 1234 --dry-run
    python phase_c_extract_tokenomics.py --asset-id 1234 --force  # 强制覆盖已有数据
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
from crypto_research.clients.llm_client import LLMClient


# ── 配置 ──────────────────────────────────────────────────

MAX_CONTENT_LENGTH = 8000    # 单页最大字符数（超过截断）
MAX_TOTAL_CONTENT = 30000    # 总内容最大字符数
MAX_PAGES = 10               # 最多收集多少页文档
FETCH_TIMEOUT = 15           # 页面抓取超时（秒）


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="代币经济学提取")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--asset-id", "--asset_id", type=int, dest="asset_id", help="资产 ID")
    g.add_argument("--symbol", type=str, help="代币符号（如 KOMA）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    parser.add_argument("--force", action="store_true", help="强制覆盖已有数据")
    parser.add_argument("--no-cmc", action="store_true", help="跳过 CMC 数据")
    parser.add_argument("--no-cg", action="store_true", help="跳过 CG 数据")
    return parser


# ── 数据收集 ──────────────────────────────────────────────

def resolve_asset(conn, asset_id: int | None, symbol: str | None) -> dict | None:
    """根据 asset_id 或 symbol 查找资产信息。"""
    query = """
        SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
               cg.source_asset_key AS coingecko_id,
               cmc.source_asset_key AS cmc_id
        FROM core.asset a
        LEFT JOIN core.asset_source_map cg
            ON cg.asset_id = a.asset_id AND cg.source_code = 'cg'
        LEFT JOIN core.asset_source_map cmc
            ON cmc.asset_id = a.asset_id AND cmc.source_code = 'cmc'
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


def collect_doc_pages(conn, asset_id: int) -> list[dict]:
    """收集该资产下所有可用于 tokenomics 提取的文档页面。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 优先 tokenomics/whitepaper/docs 类型的 doc_asset
        cur.execute(
            """
            SELECT da.doc_id, da.doc_type, da.source_url, da.file_name,
                   da.storage_path, da.mime_type
            FROM biz.doc_asset da
            WHERE da.asset_id = %s
              AND da.doc_type IN ('tokenomics', 'whitepaper', 'docs', 'deck', 'other')
            ORDER BY
                CASE da.doc_type
                    WHEN 'tokenomics' THEN 1
                    WHEN 'whitepaper' THEN 2
                    WHEN 'docs' THEN 3
                    WHEN 'deck' THEN 4
                    ELSE 5
                END, da.doc_id
            LIMIT %s
            """,
            (asset_id, MAX_PAGES),
        )
        docs = cur.fetchall()

        # 补充 doc_source_entry 中的相关页面（始终查询，与 doc_asset 合并）
        # 优先匹配 whitepaper/tokenomics/pdf 关键词，其次按 entry_type 优先级
        cur.execute(
            """
            SELECT dse.entry_id AS doc_id, dse.entry_type AS doc_type,
                   dse.entry_url AS source_url, NULL AS file_name,
                   NULL AS storage_path, NULL AS mime_type
            FROM biz.doc_source_entry dse
            WHERE dse.asset_id = %s
              AND dse.entry_type IN ('official_website', 'docs', 'docs_portal', 'whitepaper_page')
              AND dse.deep_crawled_at IS NOT NULL
              AND dse.needs_browser = FALSE
            ORDER BY
                CASE
                    WHEN dse.entry_url ILIKE '%%tokenomics%%' THEN 0
                    WHEN dse.entry_url ILIKE '%%whitepaper%%' THEN 1
                    WHEN dse.entry_url ILIKE '%%tokenom%%' THEN 1
                    WHEN dse.entry_url ILIKE '%%.pdf%%' THEN 2
                    ELSE 3
                END,
                CASE dse.entry_type
                    WHEN 'whitepaper_page' THEN 1
                    WHEN 'docs' THEN 2
                    WHEN 'docs_portal' THEN 3
                    WHEN 'official_website' THEN 4
                END, dse.entry_id
            LIMIT %s
            """,
            (asset_id, MAX_PAGES),
        )
        extra = cur.fetchall()

        # 合并去重（以 source_url 为键，doc_asset 优先保留）
        seen_urls = {d["source_url"] for d in docs}
        for e in extra:
            if e["source_url"] not in seen_urls:
                seen_urls.add(e["source_url"])
                docs.append(e)

        # 按优先级排序：doc_asset 优先，doc_source_entry 中 URL 关键词优先
        # 截断到 MAX_PAGES
        docs = docs[:MAX_PAGES]

    return docs


def _extract_page_images(url: str) -> list[dict]:
    """从页面提取 tokenomics 相关的图片（分配图/排放曲线等）。
    返回 [{"label": "分类名", "src_url": "原始URL", "alt": "alt文本"}, ...]"""
    IMAGE_KEYWORDS = [
        "allocation", "distribution", "emission", "schedule", "vesting",
        "tokenomics", "supply", "pie", "chart", "unlock",
    ]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            # 不拦截图片，仅拦截字体和样式
            page.route("**/*", lambda route: route.abort()
                if route.request.resource_type in ("font", "media", "stylesheet")
                else route.continue_()
            )

            try:
                page.goto(url, wait_until="networkidle", timeout=FETCH_TIMEOUT * 1000)
            except PlaywrightTimeout:
                pass  # 部分加载也可接受
            except Exception as e:
                print(f"  [IMG WARN] 页面加载失败 {url[:80]}: {e}")
                browser.close()
                return []

            page.wait_for_timeout(3000)

            # JS 提取：查找 img 标签，检测其附近文本是否含有关键词
            images = page.evaluate("""
                (keywords) => {
                    const results = [];
                    const imgs = document.querySelectorAll('img[src]');
                    const seen = new Set();
                    for (const img of imgs) {
                        const src = img.src || img.getAttribute('data-src') || '';
                        if (!src || src.startsWith('data:') || seen.has(src)) continue;
                        seen.add(src);

                        // 检查 alt / title / 父元素文本
                        const alt = (img.alt || '').toLowerCase();
                        let contextText = alt + ' ';
                        // 向上查找最近的 heading 或 figure caption
                        let el = img.closest('figure') || img.parentElement;
                        if (el) {
                            contextText += (el.textContent || '').toLowerCase();
                        }
                        // 再往上找一层
                        el = el ? el.parentElement : null;
                        if (el) {
                            contextText += ' ' + (el.textContent || '').toLowerCase();
                        }

                        let matched = false;
                        for (const kw of keywords) {
                            if (contextText.includes(kw)) {
                                matched = true;
                                break;
                            }
                        }
                        if (matched) {
                            results.push({
                                label: alt || img.alt || '',
                                src: src,
                                alt: img.alt || '',
                            });
                        }
                    }
                    return results;
                }
            """, IMAGE_KEYWORDS)

            browser.close()
            return images
    except Exception as e:
        print(f"  [IMG WARN] 图片提取失败 {url[:80]}: {e}")
        return []


def _download_and_save_images(images: list[dict], asset_id: int) -> list[dict]:
    """下载图片到本地存储，返回入库用的 dict 列表。"""
    import requests as req

    storage_dir = SCRIPT_DIR.parent.parent / "data" / "tokenomics_images" / str(asset_id)
    storage_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for i, img in enumerate(images):
        src = img["src"]
        label = img.get("label") or img.get("alt") or f"chart_{i}"
        try:
            resp = req.get(src, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

            # 确定扩展名
            ext = ".png"
            content_type = resp.headers.get("Content-Type", "")
            if "svg" in content_type:
                ext = ".svg"
            elif "jpeg" in content_type or "jpg" in content_type:
                ext = ".jpg"
            elif "webp" in content_type:
                ext = ".webp"
            elif "gif" in content_type:
                ext = ".gif"

            fname = f"{asset_id}_{i}{ext}"
            fpath = storage_dir / fname
            fpath.write_bytes(resp.content)

            saved.append({
                "label": label[:100],
                "src_url": src[:500],
                "file": str(fpath.relative_to(SCRIPT_DIR.parent.parent)),
                "size": len(resp.content),
            })
            print(f"  [IMG] 已保存: {fname} ({len(resp.content)} bytes)")
        except Exception as e:
            print(f"  [IMG WARN] 下载失败 {src[:80]}: {e}")
    return saved


def _fetch_pdf(url: str) -> str | None:
    """用 requests 下载 PDF 并用 PyPDF2 提取文本。"""
    try:
        import io
        import requests as req
        from PyPDF2 import PdfReader
    except ImportError as e:
        print(f"  [WARN] PDF 解析库缺失: {e}")
        return None

    try:
        resp = req.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        reader = PdfReader(io.BytesIO(resp.content))
        texts = []
        for page in reader.pages[:30]:  # 最多 30 页
            t = page.extract_text()
            if t:
                texts.append(t)
        return "\n\n".join(texts)
    except Exception as e:
        print(f"  [WARN] PDF 解析失败 {url[:80]}: {e}")
        return None


def fetch_page_content(url: str) -> str | None:
    """抓取页面内容，对 PDF 用 PyPDF2 解析，对 HTML 用 headless browser。"""
    # PDF 链接用专用解析器
    if url.lower().endswith(".pdf"):
        print(f"  [PDF] 解析 {url[:80]}")
        return _fetch_pdf(url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            # 拦截非必要资源
            page.route("**/*", lambda route: route.abort()
                if route.request.resource_type in ("image", "font", "media", "stylesheet")
                else route.continue_()
            )

            try:
                page.goto(url, wait_until="networkidle", timeout=FETCH_TIMEOUT * 1000)
            except PlaywrightTimeout:
                print(f"  [WARN] 页面加载超时 {url[:80]}，尝试获取已有内容")
            except Exception as e:
                print(f"  [WARN] 页面导航失败 {url[:80]}: {e}")
                browser.close()
                return None

            # 等待一下让动态内容渲染
            page.wait_for_timeout(3000)

            html = page.content()
            browser.close()

            if not html:
                return None

            # 提取文本：移除 script/style/nav/footer/header/noscript 标签
            for tag in ("script", "style", "nav", "footer", "header", "noscript"):
                html = re.sub(rf"<{tag}[\s>].*?</{tag}>", "", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(rf"<{tag}>.*?</{tag}>", "", html, flags=re.DOTALL | re.IGNORECASE)

            # 移除所有 HTML 标签
            text = re.sub(r"<[^>]+>", " ", html)
            # 解码 HTML 实体
            text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
            # 压缩空白
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = text.strip()
            return text
    except Exception as e:
        print(f"  [WARN] 抓取失败 {url[:80]}: {e}")
        return None


def get_cmc_supply(conn, asset_id: int) -> dict | None:
    """从 CMC 数据中获取 supply 信息。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT total_supply, fdv, circulating_supply
            FROM biz.asset_market_daily
            WHERE asset_id = %s AND source_code = 'cmc'
            ORDER BY market_date DESC LIMIT 1
            """,
            (asset_id,),
        )
        row = cur.fetchone()
        if row and any(v is not None for v in row.values()):
            return {
                "source": "CMC",
                "total_supply": row.get("total_supply"),
                "max_supply": row.get("fdv"),  # FDV 作为 max_supply 的近似
                "circulating_supply": row.get("circulating_supply"),
            }
    return None


def get_cg_supply(coingecko_id: str) -> dict | None:
    """从 CoinGecko API 获取 supply 数据。"""
    if not coingecko_id:
        return None
    try:
        settings = get_settings(require_database=False)
        from crypto_research.clients.coingecko_client import CoinGeckoClient
        cg = CoinGeckoClient(settings)
        data = cg.get_coin_by_id(coingecko_id)
        market = data.get("market_data", {})
        result = {
            "source": "CoinGecko",
            "total_supply": market.get("total_supply"),
            "max_supply": market.get("max_supply"),
            "circulating_supply": market.get("circulating_supply"),
        }
        if any(v is not None for v in result.values()):
            return result
    except Exception as e:
        print(f"  [WARN] CG API 失败: {e}")
    return None


# ── LLM 提取 ──────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个加密货币代币经济学数据分析专家。你的任务是从给定的多个文档中，
提取该代币的结构化代币经济学数据。

规则：
1. 从所有文档中综合提取，合并去重
2. 如果多个来源对同一字段有冲突，以官网为优先，白皮书次之
3. 没有的字段设为 null，不要编造数据
4. 数字字段只保留数值，去掉逗号、单位等
5. 百分比字段转为小数（如 12% → 12.00）
6. 分配比例各项之和应为 100%，如有偏差请标注
7. 标注每个字段的来源（source_urls 中的哪一页，或 API）

只输出 JSON，不要输出其他内容。JSON 格式：
{
  "total_supply": 数字或null,
  "max_supply": 数字或null,
  "circulating_supply": 数字或null,
  "buy_tax_pct": 数字或null,
  "sell_tax_pct": 数字或null,
  "tax_info": "字符串或null",
  "contract_renounced": true/false/null,
  "lp_locked": true/false/null,
  "lp_lock_info": "字符串或null",
  "allocation": [{"category": "分类名", "pct": 数字}, ...],
  "burn_info": "字符串或null",
  "emission_schedule": "字符串或null",
  "inflation_info": "字符串或null",
  "governance_info": "字符串或null",
  "utility_info": "字符串或null",
  "confidence": 0.0到1.0之间的数字,
  "notes": "提取备注（冲突、缺失、推断等）"
}"""


def build_user_prompt(asset: dict, doc_contents: list[dict], api_data: list[dict]) -> str:
    """构建 LLM 用户提示词。"""
    parts = []

    parts.append(f"## 资产信息\n- 名称: {asset['name']}\n- 符号: {asset['symbol']}")

    if doc_contents:
        parts.append(f"\n## 文档内容（共 {len(doc_contents)} 页）\n")
        for i, doc in enumerate(doc_contents):
            content = doc["content"] or "(空)"
            parts.append(
                f"### 文档 #{i+1}: {doc['doc_type']}\n"
                f"URL: {doc['source_url']}\n"
                f"```\n{content[:MAX_CONTENT_LENGTH]}\n```\n"
            )

    if api_data:
        parts.append("\n## API 数据（参考）\n")
        for d in api_data:
            parts.append(json.dumps(d, ensure_ascii=False, default=str))

    full = "\n".join(parts)
    if len(full) > MAX_TOTAL_CONTENT:
        full = full[:MAX_TOTAL_CONTENT] + "\n\n[内容过长，已截断]"
    return full


def extract_with_llm(llm: LLMClient, asset: dict, doc_contents: list[dict],
                     api_data: list[dict]) -> dict | None:
    """调用 LLM 提取 tokenomics 数据。"""
    user_prompt = build_user_prompt(asset, doc_contents, api_data)

    print(f"  发送给 LLM 的内容长度: {len(user_prompt)} 字符")
    print(f"  API 数据源: {[d['source'] for d in api_data]}")

    try:
        raw = llm.chat(SYSTEM_PROMPT, user_prompt, temperature=0.1, max_tokens=4096)
    except Exception as e:
        print(f"  [ERROR] LLM 调用失败: {e}")
        return None

    # 解析 JSON
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            first_line_end = cleaned.find("\n")
            if first_line_end > 0:
                cleaned = cleaned[first_line_end + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"  [ERROR] LLM 返回 JSON 解析失败: {e}")
        print(f"  原始返回前 500 字符: {raw[:500]}")
        return None


# ── 入库 ──────────────────────────────────────────────────

def save_tokenomics(conn, asset_id: int, source_urls: list[str],
                    data: dict, dry_run: bool = False) -> None:
    """写入或更新 biz.asset_tokenomics。"""
    sql = """
        INSERT INTO biz.asset_tokenomics (
            asset_id, source_urls,
            total_supply, max_supply, circulating_supply,
            buy_tax_pct, sell_tax_pct, tax_info,
            contract_renounced, lp_locked, lp_lock_info,
            allocation_json, burn_info, emission_schedule,
            inflation_info, governance_info, utility_info,
            raw_text, extracted_by, confidence, extraction_notes,
            chart_images
        ) VALUES (
            %(asset_id)s, %(source_urls)s,
            %(total_supply)s, %(max_supply)s, %(circulating_supply)s,
            %(buy_tax_pct)s, %(sell_tax_pct)s, %(tax_info)s,
            %(contract_renounced)s, %(lp_locked)s, %(lp_lock_info)s,
            %(allocation_json)s, %(burn_info)s, %(emission_schedule)s,
            %(inflation_info)s, %(governance_info)s, %(utility_info)s,
            %(raw_text)s, %(extracted_by)s, %(confidence)s, %(extraction_notes)s,
            %(chart_images)s
        )
        ON CONFLICT (asset_id) DO UPDATE SET
            source_urls = EXCLUDED.source_urls,
            total_supply = EXCLUDED.total_supply,
            max_supply = EXCLUDED.max_supply,
            circulating_supply = EXCLUDED.circulating_supply,
            buy_tax_pct = EXCLUDED.buy_tax_pct,
            sell_tax_pct = EXCLUDED.sell_tax_pct,
            tax_info = EXCLUDED.tax_info,
            contract_renounced = EXCLUDED.contract_renounced,
            lp_locked = EXCLUDED.lp_locked,
            lp_lock_info = EXCLUDED.lp_lock_info,
            allocation_json = EXCLUDED.allocation_json,
            burn_info = EXCLUDED.burn_info,
            emission_schedule = EXCLUDED.emission_schedule,
            inflation_info = EXCLUDED.inflation_info,
            governance_info = EXCLUDED.governance_info,
            utility_info = EXCLUDED.utility_info,
            raw_text = EXCLUDED.raw_text,
            extracted_by = EXCLUDED.extracted_by,
            confidence = EXCLUDED.confidence,
            extraction_notes = EXCLUDED.extraction_notes,
            chart_images = EXCLUDED.chart_images,
            updated_at = NOW()
    """
    params = {
        "asset_id": asset_id,
        "source_urls": source_urls,
        "total_supply": data.get("total_supply"),
        "max_supply": data.get("max_supply"),
        "circulating_supply": data.get("circulating_supply"),
        "buy_tax_pct": data.get("buy_tax_pct"),
        "sell_tax_pct": data.get("sell_tax_pct"),
        "tax_info": data.get("tax_info"),
        "contract_renounced": data.get("contract_renounced"),
        "lp_locked": data.get("lp_locked"),
        "lp_lock_info": data.get("lp_lock_info"),
        "allocation_json": json.dumps(data.get("allocation"), ensure_ascii=False) if data.get("allocation") else None,
        "burn_info": data.get("burn_info"),
        "emission_schedule": data.get("emission_schedule"),
        "inflation_info": data.get("inflation_info"),
        "governance_info": data.get("governance_info"),
        "utility_info": data.get("utility_info"),
        "raw_text": json.dumps(data, ensure_ascii=False, default=str),
        "extracted_by": "llm",
        "confidence": data.get("confidence"),
        "extraction_notes": data.get("notes"),
        "chart_images": json.dumps(data.get("chart_images"), ensure_ascii=False) if data.get("chart_images") else None,
    }

    if dry_run:
        print("\n[Dry-run] 将写入以下数据:")
        print(json.dumps(params, ensure_ascii=False, default=str, indent=2))
        return

    with conn.cursor() as cur:
        cur.execute(sql, params)
    print("  已写入 biz.asset_tokenomics")


# ── 主流程 ──────────────────────────────────────────────────

def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 0. 确保 chart_images 列存在
        with conn.cursor() as cur:
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'biz' AND table_name = 'asset_tokenomics'
                        AND column_name = 'chart_images'
                    ) THEN
                        ALTER TABLE biz.asset_tokenomics ADD COLUMN chart_images JSONB;
                    END IF;
                END $$;
            """)
        conn.commit()

        # 1. 查找资产
        asset = resolve_asset(conn, args.asset_id, args.symbol)
        if not asset:
            print(f"ERROR: 未找到资产 (asset_id={args.asset_id}, symbol={args.symbol})")
            sys.exit(1)

        print(f"资产: {asset['symbol']} ({asset['name']}) [asset_id={asset['asset_id']}]")

        # 2. 检查是否已有数据
        if not args.force:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM biz.asset_tokenomics WHERE asset_id = %s",
                    (asset["asset_id"],),
                )
                if cur.fetchone():
                    print("  已有 tokenomics 数据，跳过（使用 --force 强制覆盖）")
                    return

        # 3. 收集文档
        docs = collect_doc_pages(conn, asset["asset_id"])
        print(f"  收集到 {len(docs)} 个文档页面")

        doc_contents = []
        for doc in docs:
            print(f"  抓取: [{doc['doc_type']}] {doc['source_url'][:80]}")
            content = fetch_page_content(doc["source_url"])
            if content:
                doc_contents.append({
                    "doc_type": doc["doc_type"],
                    "source_url": doc["source_url"],
                    "content": content,
                })
                print(f"    -> {len(content)} 字符")
            else:
                print(f"    -> 抓取失败或非 HTML 页面")

        if not doc_contents:
            print("ERROR: 没有成功抓取到任何页面内容")
            sys.exit(1)

        # 4. 收集 API 数据
        api_data = []

        if not args.no_cmc:
            cmc = get_cmc_supply(conn, asset["asset_id"])
            if cmc:
                api_data.append(cmc)
                print(f"  CMC supply: {json.dumps({k: v for k, v in cmc.items() if v is not None}, default=str)}")

        if not args.no_cg and asset.get("coingecko_id"):
            cg = get_cg_supply(asset["coingecko_id"])
            if cg:
                api_data.append(cg)
                print(f"  CG supply: {json.dumps({k: v for k, v in cg.items() if v is not None}, default=str)}")

        # 5. LLM 提取
        llm = LLMClient(settings, rpm=30)
        if not llm.is_available():
            print("ERROR: LLM 未配置")
            sys.exit(1)

        print("  调用 LLM 提取 tokenomics...")
        start = time.monotonic()
        result = extract_with_llm(llm, asset, doc_contents, api_data)
        elapsed = time.monotonic() - start

        if not result:
            print("ERROR: LLM 提取失败")
            sys.exit(1)

        print(f"  LLM 提取完成，耗时 {elapsed:.1f}s")
        print(f"  置信度: {result.get('confidence')}")

        # 5b. 合并 API supply 数据（CG 优先于 CMC，API 精确数值覆盖 LLM 提取）
        for api_entry in api_data:
            for key in ("total_supply", "max_supply", "circulating_supply"):
                val = api_entry.get(key)
                if val is not None:
                    result[key] = val
        print(f"  API supply 合并: total={result.get('total_supply')}, "
              f"max={result.get('max_supply')}, circ={result.get('circulating_supply')}")

        # 5c. 提取 tokenomics 页面中的关键图片（分配图/排放曲线等）
        chart_images = []
        # 选择最合适的 tokenomics 页面 URL：优先含 tokenomics 关键词的
        best_url = None
        for d in doc_contents:
            if "tokenomics" in d["source_url"].lower():
                best_url = d["source_url"]
                break
        if not best_url and doc_contents:
            best_url = doc_contents[0]["source_url"]
        if best_url:
            print(f"  提取图片: {best_url[:80]}")
            raw_images = _extract_page_images(best_url)
            if raw_images:
                print(f"  发现 {len(raw_images)} 张相关图片，开始下载...")
                chart_images = _download_and_save_images(raw_images, asset["asset_id"])
            else:
                print("  未发现相关图片")
        result["chart_images"] = chart_images

        # 6. 入库
        source_urls = [d["source_url"] for d in doc_contents]
        save_tokenomics(conn, asset["asset_id"], source_urls, result, dry_run=args.dry_run)

        if not args.dry_run:
            print("完成！")


if __name__ == "__main__":
    main()