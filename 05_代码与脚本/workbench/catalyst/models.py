"""
催化剂统一数据模型（平台无关）。

所有催化剂源（Binance CMS / Binance Square / CoinGecko / ...）
都输出 CatalystItem 列表，由核心模块统一做：去重、资产关联、落库。

新增平台只需：
1. 继承 BaseCatalystSource
2. 实现 fetch() 返回 list[CatalystItem]
3. 注册到 SOURCE_REGISTRY
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class CatalystItem:
    """一条催化剂事件（平台无关的统一结构）。

    核心字段：
    - source_code: 来源编码（如 binance_news, binance_square_news, coingecko_events）
    - source_item_id: 来源侧的唯一 ID（用于同源去重）
    - title / body_text: 标题和正文（用于 content_hash 跨源去重）
    - published_at: 发布时间（秒级时间戳）
    - related_pairs: 关联交易对列表（用于资产关联）
    """
    source_code: str
    source_item_id: str
    title: str
    body_text: str
    published_at: float  # 秒级时间戳

    # 可选字段
    source_item_code: str = ""          # 来源侧 code（如 CMS 的 articleCode）
    body_html: str = ""
    event_category: str = ""
    event_subcategory: str = ""
    related_pairs: list[str] = field(default_factory=list)
    source_url: str = ""
    seo_keywords: list[str] = field(default_factory=list)
    share_count: int = 0
    raw_json: dict | None = None

    # 计算属性：content_hash（跨源去重键）
    @property
    def content_hash(self) -> str:
        """计算内容哈希（sha256 of 归一化标题，标题太短时补正文前 100 字）。

        归一化规则（_normalize_text）：
        - 全小写 + 压缩空白
        - 剥离标题开头的媒体名 / 日期 / 「消息」前缀
          （如 "PANews 9月18日消息" / "BlockBeats 消息" / "火星财经消息"）
          使同一新闻在不同媒体的转载能被跨源去重合并
        - 数字与中文/百分号间的空格归一化（"25 个基点" = "25个基点"）
        """
        # 优先标题：跨媒体转载时标题最接近；标题太短（<20 字）再补正文前 100 字
        title = _normalize_text(self.title or "")
        if len(title) < 20:
            body = _normalize_text((self.body_text or "")[:100])
            text = title + "|" + body
        else:
            text = title
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 新闻媒体标题前缀（跨源去重时剥离）
_MEDIA_PREFIXES = [
    "panews", "blockbeats", "foresight news", "foresightnews",
    "marsbit", "火星财经", "chaincatcher", "odaily", "律动 blockbeats",
    "深潮 techflow", "techflow", "区块律动", "金色财经", "币世界",
    "巴比特", "coindesk", "cointelegraph", "the block", "decrypt",
]


def _normalize_text(text: str) -> str:
    """文本归一化：去多余空白、全小写、剥离媒体/日期前缀，用于哈希比较。

    若含 | 分隔符（title|body），对两部分分别剥离头部噪声。
    """
    if not text:
        return ""
    # 压缩空白
    text = re.sub(r"\s+", " ", text)
    # 全小写
    text = text.lower()
    text = text.strip()

    # 剥离标题和正文开头的媒体名 + 日期 + 「消息」前缀
    if "|" in text:
        title, rest = text.split("|", 1)
        title = _strip_head_noise(title)
        rest = _strip_head_noise(rest)
        text = title + "|" + rest
    else:
        # 无分隔符（纯标题/纯正文）：直接对整体剥离开头噪声
        text = _strip_head_noise(text)

    # 数字归一化：去掉数字与中文/百分号之间的空格
    # （"25 个基点" vs "25个基点"、"加息 25" vs "加息25"、"1.00 %" vs "1.00%" 视为同一）
    text = re.sub(r"(\d)\s+([%个百分点日时月年元枚亿万])", r"\1\2", text)
    text = re.sub(r"([%个百分点日时月年元枚亿万])\s+(\d)", r"\1\2", text)
    text = re.sub(r"([\u4e00-\u9fff])\s+(\d)", r"\1\2", text)
    text = re.sub(r"(\d)\s+([\u4e00-\u9fff])", r"\1\2", text)
    text = re.sub(r"(\d)\s+%", r"\1%", text)
    text = re.sub(r"%\s+([\u4e00-\u9fff])", r"%\1", text)  # "1.00% 上调" → "1.00%上调"
    return text.strip()


def _strip_head_noise(s: str) -> str:
    """剥离字符串开头的媒体名/日期/消息等前缀噪声。

    例：
      "panews 9月18日消息，日本央行..."  → "日本央行..."
      "blockbeats 消息，9 月 18 日，日本央行..." → "日本央行..."
      "火星财经消息，9 月 18 日，日本央行..." → "日本央行..."
    """
    if not s:
        return s
    # 循环剥离：媒体名 → 日期 → 消息字眼（最多 6 轮，覆盖 媒体+日期+消息+空格 组合）
    for _ in range(6):
        changed = False
        # 1) 媒体名前缀
        for p in _MEDIA_PREFIXES:
            if s.startswith(p):
                s = s[len(p):].lstrip(" ，,。·-—|：:")
                changed = True
                break
        if changed:
            continue
        # 2) 日期前缀：X月X日（允许空格）/ YYYY-MM-DD / YYYY/MM/DD
        m = re.match(r"^(\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/]\d{1,2}[-/]\d{1,2})", s)
        if m:
            s = s[m.end():].lstrip(" ，,。·-—|：:")
            continue
        # 3) 「消息」等引导词
        m = re.match(r"^(消息|报道|讯|快讯)", s)
        if m:
            s = s[m.end():].lstrip(" ，,。·-—|：:")
            continue
        break
    return s.strip(" ，,。·-—|：:")
