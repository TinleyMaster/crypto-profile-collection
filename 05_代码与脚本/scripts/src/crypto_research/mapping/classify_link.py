"""统一链接分类器。

合并历史上分散在 doc_source_entries.infer_entry_type、
doc_asset_discovery.infer_doc_type、supplement_doc_entries_dual._classify_url
三处的规则，输出统一的「来源类型 + 内容主题多标签」结果。

L1 规则（免费）：域名精确规则 → CMC url_key 元数据 → 标签/URL 关键词。
L2 元数据：url_key / source_code 等结构化信号（此模块内仅用 url_key）。
L3 AI 分类：阶段2 另行接入，对低置信度项做正文级多标签分类。
"""

from __future__ import annotations

from urllib.parse import urlparse

from crypto_research.mapping.taxonomy import (
    CONTENT_TOPIC_KEYWORDS,
    DOMAIN_SOURCE_TYPES,
)


def _extract_domain(url: str) -> str:
    """提取主域名（去掉子域名与 www 前缀），失败返回空串。"""
    host = (urlparse(url or "").hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def infer_source_type(url: str, url_key: str = "", label: str = "") -> str:
    """推断来源类型（source_type）。"""
    url_l = (url or "").lower()
    label_l = (label or "").lower()
    domain = _extract_domain(url)

    # 1) 域名精确规则（最高优先级，跨源稳定）
    if domain in DOMAIN_SOURCE_TYPES:
        return DOMAIN_SOURCE_TYPES[domain]

    # 2) CMC url_key 元数据映射
    key_type = {
        "website": "official_website",
        "technical_doc": "docs",
        "source_code": "github" if "github.com" in url_l else "other",
        "announcement": "medium" if "medium.com" in url_l else "announcement",
        "twitter": "twitter",
        "facebook": "facebook",
        "reddit": "reddit",
        "telegram": "telegram",
        "blog": "medium" if "medium.com" in url_l else "other",
        "chat": "other",
        "message_board": "other",
        "explorer": "other",
    }.get(url_key or "")
    if key_type:
        return key_type

    # 3) 标签 / URL 关键词推断
    if any(k in label_l for k in ("whitepaper", "white paper", "litepaper")):
        return "whitepaper_page"
    if any(k in url_l for k in ("whitepaper", "white-paper", "litepaper")):
        return "whitepaper_page"
    if any(k in label_l for k in ("docs", "documentation", "wiki", "gitbook")):
        return "docs"
    if any(k in url_l for k in ("docs.", "documentation", "wiki.", "gitbook")):
        return "docs"
    # PDF / 文档文件：无法从 URL 判断具体主题时，至少归为文档而非官网
    if url_l.endswith(".pdf") or ".pdf?" in url_l:
        return "docs"
    if any(k in label_l for k in ("website", "homepage", "official")):
        return "official_website"

    return "official_website"


def infer_content_topics(url: str, label: str = "", source_type: str = "") -> list[str]:
    """推断内容主题多标签（基于 URL + 标签关键词）。"""
    url_l = (url or "").lower()
    label_l = (label or "").lower()
    # 归一化：把 - 和 _ 转成空格，便于匹配 white-paper / white_paper 等多词关键词
    norm = (url_l + " " + label_l).replace("-", " ").replace("_", " ")

    topics: list[str] = []
    for topic, keywords in CONTENT_TOPIC_KEYWORDS.items():
        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            if " " in kw:
                if kw in norm:
                    topics.append(topic)
                    break
            else:
                if kw in url_l or kw in label_l:
                    topics.append(topic)
                    break

    # 来源类型为白皮书页/文档门户时，补上对应主题
    if source_type == "whitepaper_page" and "whitepaper" not in topics:
        topics.append("whitepaper")
    if source_type in ("docs", "docs_portal") and not set(topics) & {"whitepaper", "docs"}:
        topics.append("docs")
    if not topics:
        topics.append("other")
    return topics


def classify_link(url: str, label: str = "", url_key: str = "", source_code: str = "") -> dict:
    """统一分类入口。

    返回 dict:
        source_type: str      来源类型（对齐 taxonomy.SOURCE_TYPES）
        content_topics: list  内容主题多标签（对齐 taxonomy.CONTENT_TOPICS）
        method: str           判定方法 domain / url_key / keyword / default
        confidence: float     置信度 0~1（供后续 AI 分类筛选低置信度项）
    """
    url_l = (url or "").lower()
    domain = _extract_domain(url)
    source_type = infer_source_type(url, url_key=url_key, label=label)
    topics = infer_content_topics(url, label=label, source_type=source_type)

    if domain in DOMAIN_SOURCE_TYPES:
        method, confidence = "domain", 0.98
    elif url_key:
        method, confidence = "url_key", 0.9
    elif topics and topics != ["other"]:
        method, confidence = "keyword", 0.6
    else:
        method, confidence = "default", 0.3

    return {
        "source_type": source_type,
        "content_topics": topics,
        "method": method,
        "confidence": confidence,
    }
