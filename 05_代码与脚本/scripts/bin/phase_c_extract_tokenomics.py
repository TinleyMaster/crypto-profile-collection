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
from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response


# ── 配置 ──────────────────────────────────────────────────

MAX_CONTENT_LENGTH = 8000    # 单页最大字符数（超过截断）
MAX_TOTAL_CONTENT = 80000    # 总内容最大字符数
MAX_PAGES = 20               # 最多收集多少页文档
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
    parser.add_argument("--url", type=str, help="用户提供的 tokenomics 网址（直接抓取并 LLM 提取，跳过 tokenomics.com 搜索）")
    parser.add_argument("--ai", action="store_true", help="tokenomics.com 未命中时直接走 AI 测算（文档+LLM），不询问")
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


def collect_all_links(conn, asset_id: int) -> list[dict]:
    """收集该资产下所有可能的文档链接（不做筛选，交给 AI 选）。"""
    docs: list[dict] = []
    seen_urls: set[str] = set()

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # doc_asset 中的文档
        cur.execute(
            """
            SELECT da.doc_id, da.doc_type, da.source_url, da.file_name,
                   da.storage_path, da.mime_type
            FROM biz.doc_asset da
            WHERE da.asset_id = %s
            ORDER BY da.doc_id
            """,
            (asset_id,),
        )
        for row in cur.fetchall():
            url = row["source_url"]
            if url not in seen_urls:
                seen_urls.add(url)
                docs.append(row)

        # doc_source_entry 中该资产的全部文档入口链接。
        # 不做 deep_crawled_at / needs_browser 过滤：种子链接（官网/docs 等）
        # 即使未深度爬取也可能直接指向 tokenomics 页面，SPA 页面由
        # fetch_page_content 的 Playwright 渲染处理；最终相关性交给 AI 筛选。
        cur.execute(
            """
            SELECT dse.entry_id AS doc_id, dse.entry_type AS doc_type,
                   dse.entry_url AS source_url, NULL AS file_name,
                   NULL AS storage_path, NULL AS mime_type
            FROM biz.doc_source_entry dse
            WHERE dse.asset_id = %s
              AND dse.entity_type = 'asset'
              AND dse.entry_url IS NOT NULL
              AND TRIM(dse.entry_url) <> ''
            ORDER BY dse.entry_id
            """,
            (asset_id,),
        )
        for row in cur.fetchall():
            url = row["source_url"]
            if url not in seen_urls:
                seen_urls.add(url)
                docs.append(row)

    return docs


LINK_SELECTION_PROMPT = """你是一个加密货币投研分析助手。下面是一个代币的所有文档链接列表。

请从中选出与**代币经济学（tokenomics）**最相关的链接，最多选 20 个。

代币经济学相关内容包括：
- 白皮书（whitepaper）中描述代币分配、供应、用途的页面
- 专门介绍 tokenomics / 代币经济模型 / 分配 / 锁仓 / 排放的页面
- 质押（staking）、治理（governance）、经济模型（economics）相关页面
- 官方文档中涉及代币用途、费用结构的页面
- 博客/公告中关于代币分配的官方声明

不相关的内容：
- 纯技术架构、代码仓库、API 文档
- 非官方的第三方审计报告
- 社交媒体链接（Twitter/Telegram/Discord/Reddit）
- 区块链浏览器（etherscan/bscscan/solscan）
- 图片文件（.png/.jpg/.svg，除非文件名含 whitepaper/tokenomics）
- 视频/音频链接

请返回一个 JSON 数组，每个元素包含 url 和 reason（一句话说明为什么选它）。
按重要性从高到低排序。只输出 JSON，不要输出其他内容。

资产: {name} ({symbol})
文档链接:
{links}"""


def select_relevant_links(llm: LLMClient, asset: dict, all_links: list[dict]) -> list[str]:
    """调用 LLM 从所有链接中选出代币经济学相关的链接。"""
    if len(all_links) <= MAX_PAGES:
        return [d["source_url"] for d in all_links]

    # 构建链接列表文本
    link_list = "\n".join(
        f"  [{i+1}] [{d['doc_type']}] {d['source_url']}"
        for i, d in enumerate(all_links)
    )

    prompt = LINK_SELECTION_PROMPT.format(
        name=asset["name"], symbol=asset["symbol"], links=link_list,
    )

    print(f"  发送 {len(all_links)} 个链接给 LLM 筛选...")
    try:
        raw = llm.chat(
            "你是一个加密货币投研分析助手。只输出 JSON，不要输出其他内容。",
            prompt, temperature=0.1, max_tokens=4096,
        )
    except Exception as e:
        print(f"  [WARN] LLM 链接筛选失败: {e}，回退取前 {MAX_PAGES} 个")
        return [d["source_url"] for d in all_links[:MAX_PAGES]]

    # 解析 JSON
    try:
        selected = extract_json_from_llm_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [WARN] LLM 返回 JSON 解析失败，回退取前 {MAX_PAGES} 个")
        return [d["source_url"] for d in all_links[:MAX_PAGES]]

    # 提取 URL
    urls = []
    for item in selected:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict):
            u = item.get("url", "")
            reason = item.get("reason", "")
            print(f"    -> [{reason[:60]}] {u[:80]}")
            urls.append(u)

    # 补充 doc_asset 中的链接（始终保留）
    extra = [d["source_url"] for d in all_links
             if d.get("file_name") and d["source_url"] not in urls]
    urls.extend(extra)

    return urls[:MAX_PAGES]


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


# ── tokenomics.com 结构化数据源 ──────────────────────────────

def scrape_tokenomics_com(asset: dict) -> dict | None:
    """从 app.tokenomics.com 抓取结构化代币经济学数据。

    复用 phase_chain_token_unlocks 的 slug 推导 + 结构化爬虫，
    额外抓取 revenue / valuation / FAQ 子板块。失败返回 None（不阻断流程）。
    """
    try:
        _bin = Path(__file__).resolve().parent
        if str(_bin) not in sys.path:
            sys.path.insert(0, str(_bin))
        from phase_chain_token_unlocks import guess_slugs, scrape_tokenomist

        slugs = guess_slugs(asset)
        print(f"  tokenomics.com 尝试 slugs: {slugs}")
        data = scrape_tokenomist(slugs, symbol=asset.get("symbol", ""), name=asset.get("name", ""), include_extras=True)
        if data:
            print(f"  tokenomics.com 命中: {data.get('source_url')}")
        return data
    except Exception as e:
        print(f"  [WARN] tokenomics.com 抓取失败: {e}")
        return None


def _format_tokenomics_com(data: dict) -> str:
    """把 tokenomics.com 结构化数据格式化为 LLM 最高优先级文本块。"""
    lines = ["## tokenomics.com 平台结构化数据（最高优先级，请优先以此为准）",
             f"数据来源: {data.get('source_url', '')}"]

    ov = data.get("overview") or {}
    for label, key in (
        ("TGE 日期", "tge_date"),
        ("最大总供应量", "max_supply_str"),
        ("总供应量", "total_amount_str"),
    ):
        if ov.get(key):
            lines.append(f"- {label}: {ov[key]}")
    if ov.get("released_pct") is not None:
        lines.append(f"- 已释放比例: {ov['released_pct']}%")
    if ov.get("locked_pct") is not None:
        lines.append(f"- 锁定比例: {ov['locked_pct']}%")

    alloc = ov.get("allocation") or data.get("allocation") or []
    if alloc:
        lines.append("\n### 代币分配 (allocation)")
        for a in alloc:
            lines.append(f"- {a.get('name')}: {a.get('pct')}%")

    rounds = ov.get("investor_rounds") or []
    if rounds:
        lines.append("\n### 投资者轮次与条款 (investor rounds)")
        for r in rounds:
            lines.append(f"- {json.dumps(r, ensure_ascii=False, default=str)}")

    faq = ov.get("faq") or []
    if faq:
        lines.append("\n### 代币经济学 FAQ")
        for f in faq:
            lines.append(f"Q: {f.get('q')}")
            lines.append(f"A: {f.get('a')}")

    events = data.get("unlock_events") or []
    if events:
        lines.append(f"\n### 解锁事件 (unlock events，共 {len(events)} 条)")
        for e in events[:40]:
            lines.append(f"- {json.dumps(e, ensure_ascii=False, default=str)}")

    rev = data.get("revenue") or {}
    if rev.get("faq"):
        lines.append("\n### 协议收入 FAQ (revenue)")
        for f in rev["faq"]:
            lines.append(f"Q: {f.get('q')}")
            lines.append(f"A: {f.get('a')}")
    if rev.get("tables"):
        lines.append("\n### 收入报表 (revenue statement)")
        for t in rev["tables"]:
            for row in t:
                lines.append(" | ".join(row))

    val = data.get("valuation") or {}
    if val.get("text"):
        lines.append("\n### 估值数据 (valuation)")
        lines.append(val["text"][:2000])

    return "\n".join(lines)


# ── LLM 提取 ──────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个加密货币代币经济学数据分析专家。你的任务是从给定的多个文档中，
提取该代币的结构化代币经济学数据。

规则：
1. 从所有文档中综合提取，合并去重
2. 如果多个来源对同一字段有冲突，优先级为：tokenomics.com 平台结构化数据 > 官网 > 白皮书 > 其他投研资料
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


def build_user_prompt(asset: dict, doc_contents: list[dict], api_data: list[dict],
                      tokenomics_com_data: dict | None = None) -> str:
    """构建 LLM 用户提示词。tokenomics.com 结构化数据作为最高优先级排在最前。"""
    parts = []

    parts.append(f"## 资产信息\n- 名称: {asset['name']}\n- 符号: {asset['symbol']}")

    if tokenomics_com_data:
        parts.append("\n" + _format_tokenomics_com(tokenomics_com_data))

    if doc_contents:
        parts.append(f"\n## 投研资料（作为补充，共 {len(doc_contents)} 页）\n")
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
                     api_data: list[dict], tokenomics_com_data: dict | None = None) -> dict | None:
    """调用 LLM 提取 tokenomics 数据。"""
    user_prompt = build_user_prompt(asset, doc_contents, api_data, tokenomics_com_data)

    print(f"  发送给 LLM 的内容长度: {len(user_prompt)} 字符")
    print(f"  API 数据源: {[d['source'] for d in api_data]}")

    try:
        raw = llm.chat(SYSTEM_PROMPT, user_prompt, temperature=0.1, max_tokens=4096)
    except Exception as e:
        print(f"  [ERROR] LLM 调用失败: {e}")
        return None

    # 解析 JSON（增强提取）
    try:
        return extract_json_from_llm_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [ERROR] LLM 返回 JSON 解析失败: {e}")
        print(f"  原始返回前 500 字符: {raw[:500]}")
        return None


# ── 入库 ──────────────────────────────────────────────────

def _to_int(s) -> int | None:
    """把 tokenomist 的供应量字符串转成 int（失败返回 None）。

    支持单位后缀：K=千, M=百万, B=十亿（不区分大小写）。
    例："244.08M" → 244080000, "1.2B" → 1200000000, "500K" → 500000
    """
    if s is None:
        return None
    try:
        raw = str(s).replace(",", "").strip()
        if not raw:
            return None
        # 检查单位后缀
        multiplier = 1
        if raw[-1].upper() in ("K", "M", "B"):
            suffix = raw[-1].upper()
            num_part = raw[:-1]
            if suffix == "K":
                multiplier = 1_000
            elif suffix == "M":
                multiplier = 1_000_000
            elif suffix == "B":
                multiplier = 1_000_000_000
            return int(float(num_part) * multiplier)
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def _tokenomics_from_tokenomist(data: dict) -> dict:
    """把 tokenomics.com 结构化数据转成 biz.asset_tokenomics 需要的字段。

    命中 tokenomics.com 后直接使用平台结构化数据入库，不再调用 LLM。
    """
    ov = data.get("overview") or {}
    total = _to_int(ov.get("total_amount_str") or ov.get("max_supply_str"))
    max_supply = _to_int(ov.get("max_supply_str"))

    # allocation: tokenomist 用 name，asset_tokenomics 用 category
    allocation = [
        {"category": a.get("name"), "pct": a.get("pct")}
        for a in (ov.get("allocation") or [])
        if a.get("name") is not None
    ]

    # 从 FAQ 提取 emission / vesting / 项目简介 / circulating supply
    faq = ov.get("faq") or []
    emission_schedule = None
    utility_info = None
    circulating_supply = None
    for f in faq:
        q = (f.get("q") or "").lower()
        a = (f.get("a") or "").strip()
        if not a:
            continue
        if not emission_schedule and ("emission" in q or "vesting" in q):
            emission_schedule = a
        if not utility_info and ("what is" in q):
            utility_info = a
        if circulating_supply is None and "circulating" in (q + " " + a.lower()):
            m = re.search(r'([\d.]+)\s*%\s*of\s*total', a, re.IGNORECASE)
            if m and total:
                circulating_supply = round(total * float(m.group(1)) / 100.0)
            else:
                m2 = re.search(r'([\d,]+(?:\.\d+)?)\s+[A-Z]{2,}\s+is\s+currently\s+circulating', a, re.IGNORECASE)
                if m2:
                    circulating_supply = _to_int(m2.group(1))

    tge_date = ov.get("tge_date")
    if tge_date:
        parts = [emission_schedule, f"TGE: {tge_date}"] if emission_schedule else [f"TGE: {tge_date}"]
        emission_schedule = "\n".join(parts)

    return {
        "total_supply": total,
        "max_supply": max_supply,
        "circulating_supply": circulating_supply,  # FAQ 提取，可被 CMC/CG API 覆盖
        "buy_tax_pct": None,
        "sell_tax_pct": None,
        "tax_info": None,
        "contract_renounced": None,
        "lp_locked": None,
        "lp_lock_info": None,
        "allocation": allocation,
        "burn_info": None,
        "emission_schedule": emission_schedule,
        "inflation_info": None,
        "governance_info": None,
        "utility_info": utility_info,
        "confidence": 1.0,
        "notes": f"数据来自 tokenomics.com 结构化平台 ({data.get('source_url', '')})，未使用 LLM 提取",
        "extracted_by": "tokenomist",
        "raw_text": json.dumps(data, ensure_ascii=False, default=str),
    }


def save_tokenomist_full(conn, asset_id: int, data: dict,
                         api_data: list[dict] | None = None,
                         dry_run: bool = False) -> None:
    """命中 tokenomics.com 后，把四个子板块分别入库，并写入综合 tokenomics。

    - biz.asset_token_unlocks: overview / unlock_events / revenue / valuation（分别保存）
    - biz.asset_tokenomics: 由 overview 结构化字段构造（extracted_by=tokenomist）
    """
    from phase_chain_token_unlocks import ensure_table, save_to_db

    ensure_table(conn)
    save_to_db(conn, asset_id, data)

    tokenomics = _tokenomics_from_tokenomist(data)

    # 合并 API supply（CG/CMC 精确数值覆盖，circulating 尤其需要）
    for api_entry in (api_data or []):
        for key in ("total_supply", "max_supply", "circulating_supply"):
            val = api_entry.get(key)
            if val is not None:
                tokenomics[key] = val

    source_urls = [data["source_url"]] if data.get("source_url") else []
    save_tokenomics(conn, asset_id, source_urls, tokenomics, dry_run=dry_run)


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
        "raw_text": data.get("raw_text") or json.dumps(data, ensure_ascii=False, default=str),
        "extracted_by": data.get("extracted_by", "llm"),
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

        # 3. 收集 API supply 数据（CMC/CG，命中 tokenomics.com 时也用于补 circulating）
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

        # 优先抓取 tokenomics.com 结构化数据（含 overview/unlocks/revenue/valuation）
        tokenomics_com_data = scrape_tokenomics_com(asset)

        # 命中 tokenomics.com：四个子板块分别入库，跳过文档解析与 LLM
        if tokenomics_com_data:
            print("  tokenomics.com 命中，直接使用结构化数据入库（跳过文档解析与 LLM）")
            save_tokenomist_full(conn, asset["asset_id"], tokenomics_com_data,
                                 api_data=api_data, dry_run=args.dry_run)
            if not args.dry_run:
                print("完成！")
                print(json.dumps({"status": "ok", "source": "tokenomist",
                                  "asset_id": asset["asset_id"]}, ensure_ascii=False))
            return

        llm = LLMClient(settings, rpm=30)

        # 4. 未命中 tokenomics.com：优先使用用户提供的网址抓取
        if args.url:
            print(f"  使用用户提供的网址抓取 tokenomics: {args.url}")
            if not llm.is_available():
                print("ERROR: LLM 未配置")
                sys.exit(1)
            content = fetch_page_content(args.url)
            if not content:
                print(json.dumps({"status": "error",
                                  "message": f"网址抓取失败或非 HTML 页面: {args.url}"},
                                 ensure_ascii=False))
                sys.exit(1)
            print(f"    -> {len(content)} 字符")
            doc_contents = [{"doc_type": "tokenomics_url",
                             "source_url": args.url, "content": content}]
            result = extract_with_llm(llm, asset, doc_contents, api_data)
            if not result:
                print(json.dumps({"status": "error", "message": "LLM 提取失败"},
                                 ensure_ascii=False))
                sys.exit(1)
            for api_entry in api_data:
                for key in ("total_supply", "max_supply", "circulating_supply"):
                    val = api_entry.get(key)
                    if val is not None:
                        result[key] = val
            result["chart_images"] = []
            save_tokenomics(conn, asset["asset_id"], [args.url], result, dry_run=args.dry_run)
            if not args.dry_run:
                print("完成！")
                print(json.dumps({"status": "ok", "source": "llm",
                                  "asset_id": asset["asset_id"]}, ensure_ascii=False))
            return

        # 5. 未命中且未指定 --ai：提示上层询问用户是否提供网址（前端弹框）
        if not args.ai:
            print(json.dumps({
                "status": "not_found",
                "message": "tokenomics.com 未收录该代币，请提供 tokenomics 网址或改用 AI 测算",
                "asset_id": asset["asset_id"],
                "symbol": asset["symbol"],
                "name": asset["name"],
            }, ensure_ascii=False))
            return

        # 6. --ai：收集所有链接，AI 筛选 + LLM 提取
        all_links = collect_all_links(conn, asset["asset_id"])
        print(f"  收集到 {len(all_links)} 个候选链接")

        if not llm.is_available():
            print("ERROR: LLM 未配置")
            sys.exit(1)

        selected_urls = select_relevant_links(llm, asset, all_links)
        print(f"  AI 筛选出 {len(selected_urls)} 个相关链接")

        doc_contents = []
        for url in selected_urls:
            print(f"  抓取: {url[:80]}")
            # 查找原始 doc_type（用于日志标记）
            doc_type = "unknown"
            for d in all_links:
                if d["source_url"] == url:
                    doc_type = d["doc_type"]
                    break

            content = fetch_page_content(url)
            if content:
                doc_contents.append({
                    "doc_type": doc_type,
                    "source_url": url,
                    "content": content,
                })
                print(f"    -> {len(content)} 字符")
            else:
                print(f"    -> 抓取失败或非 HTML 页面")

        if not doc_contents:
            print("ERROR: 没有成功抓取到任何页面内容")
            sys.exit(1)

        # 5. LLM 提取
        print("  调用 LLM 提取 tokenomics...")
        start = time.monotonic()
        result = extract_with_llm(llm, asset, doc_contents, api_data, tokenomics_com_data)
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
        if tokenomics_com_data and tokenomics_com_data.get("source_url"):
            source_urls.insert(0, tokenomics_com_data["source_url"])
        save_tokenomics(conn, asset["asset_id"], source_urls, result, dry_run=args.dry_run)

        if not args.dry_run:
            print("完成！")
            print(json.dumps({"status": "ok", "source": "llm",
                              "asset_id": asset["asset_id"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()