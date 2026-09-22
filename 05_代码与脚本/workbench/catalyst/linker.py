"""
资产关联模块：将催化剂的交易对 → core.asset.asset_id（支持多资产）。

策略：
1. 从 related_pairs 提取每个交易对的 base symbol
2. 逐个查 asset_source_map（binance 源优先）→ asset_id
3. 去重后返回所有关联的 asset_id 列表

带缓存，避免重复查库。
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# symbol(大写) -> asset_id 缓存
_symbol_asset_cache: dict[str, int | None] = {}

# 非加密资产名称黑名单（SQL 正则，与 catalyst/asset_filter.py CRYPTO_FILTER_SQL 同步）。
# 命中即视为「美股 tokenized stock / 商品衍生」，不参与 crypto 催化剂信号映射。
# 线上审计 2026-09-15：582 条信号错连到 xStock / Robinhood Token / Backpack /
# Dinari 发行方资产，源头即映射阶段未排除。
_NON_CRYPTO_NAME_SQL = (
    r"tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ"
    r"|crude[[:space:]]+oil|brent"
    r"|robinhood[[:space:]]+token|backpack[[:space:]]+securities"
    r"|\mdinari\M|\mxstock\M"
)

# canonical 白名单（CAT-LINKER-DIRTY）：主流/高碰撞符号强制解析为真实主网币。
# 命中白名单即返回（要求有 CMC 排名），跳过 source_map / 脏词兜底，
# 避免 XRP→Bridged XRP、SOL→Trophy Tomato、BTC→Bitcoin Base 等仿盘抢占。
_CANONICAL_TOP_SYMBOLS = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "PEPE", "SHIB", "ADA",
    "DOT", "AVAX", "LINK", "LTC", "UNI", "AAVE", "MATIC", "POL", "TRX",
    "XLM", "FIL", "NEAR", "ARB", "OP", "SUI", "TON", "APT", "INJ", "SEI",
    "WIF", "BONK", "FLOKI", "ENA", "PENDLE", "JUP", "RENDER", "FET",
})

# 同名消歧（审计 2026-09-22 P3）：这些 symbol 与**商品/常用英文词**同名，Binance
# Square 的 `pairs` 常把宏观商品新闻（Bloomberg/LME/COMEX 铜、金、银、油…）标成
# `COPPERUSDT`/`XAUUSDT`，从而误连到同名 meme 币（实测 COPPER 连到 rank #4687 的
# `$COPPER`）。规则：命中本集合的 symbol，正文/标题必须出现**加密语境**（cashtag
# `$COPPER` / `COPPERUSDT` / token/meme/on-chain/代币/上线…）才认，否则跳过。
_COMMODITY_AMBIGUOUS_SYMBOLS = frozenset({
    "COPPER", "GOLD", "SILVER", "OIL", "GAS", "CRUDE", "BRENT", "IRON", "STEEL",
    "COAL", "URANIUM", "LITHIUM", "PLATINUM", "PALLADIUM", "COFFEE", "SUGAR",
    "WHEAT", "CORN", "WATER", "DIAMOND", "ALUMINUM", "ALUMINIUM", "NICKEL",
    "ZINC", "LEAD", "TIN", "COBALT", "XAU", "XAG", "XPT", "XPD",
})

_CRYPTO_CONTEXT_RE = re.compile(
    r"\$[A-Za-z0-9]{2,10}\b"                 # cashtag $COPPER
    r"|\b[A-Z0-9]{2,15}USDT\b"               # COPPERUSDT
    r"|\b(?:token|memecoin|meme|crypto|blockchain|on-?chain|defi|dex|cex|"
    r"airdrop|staking|listing|solana|ethereum|binance|perpetual|spot)\b"
    r"|代币|加密|链上|空投|上线|现货|合约|交易所|币安",
    re.IGNORECASE,
)


def has_crypto_context(text: str | None) -> bool:
    """正文/标题是否含加密语境（用于商品同名 symbol 消歧）。"""
    return bool(text and _CRYPTO_CONTEXT_RE.search(text))



def _resolve_canonical_top(conn, base: str) -> int | None:
    """白名单符号 → 真实主网资产（market_cap_rank 非空，取排名最小）。"""
    row = conn.execute(
        """
        SELECT asset_id
        FROM core.asset
        WHERE UPPER(canonical_symbol) = %s
          AND market_cap_rank IS NOT NULL
        ORDER BY market_cap_rank ASC, asset_id
        LIMIT 1
        """,
        (base,),
    ).fetchone()
    return row["asset_id"] if row else None

# 常见 quote 币种，用于拆分交易对
_QUOTE_ASSETS = (
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD",
    "BTC", "ETH", "BNB", "SOL", "XRP",
)


def extract_base_symbol(pair: str) -> str | None:
    """从交易对中提取 base symbol（大写）。

    优先匹配已知 quote 后缀，匹配不到返回 None。
    """
    if not pair:
        return None
    pair = pair.upper().strip()
    for quote in _QUOTE_ASSETS:
        if pair.endswith(quote) and len(pair) > len(quote):
            base = pair[: -len(quote)]
            # 至少 2 个字符，且不全是数字
            if len(base) >= 2 and any(c.isalpha() for c in base):
                return base
    return None


def extract_pairs_from_text(text: str) -> list[str]:
    """从正文中提取交易对（cashtag 兜底）。

    匹配 $BTC / BTCUSDT / $ETHUSDT 等形式。
    """
    if not text:
        return []
    pairs: list[str] = []
    seen: set[str] = set()

    # 模式 1：$XXXUSDT 或 $XXX
    for m in re.finditer(r"\$([A-Z0-9]{2,20})(USDT|USDC|BTC|ETH|BNB)?\b", text):
        base = m.group(1)
        quote = m.group(2) or "USDT"
        pair = base + quote
        if pair not in seen and any(c.isalpha() for c in base):
            seen.add(pair)
            pairs.append(pair)

    # 模式 2：直接 XXXUSDT（大写字母+数字 2-20 位 + USDT）
    for m in re.finditer(r"\b([A-Z0-9]{2,20})USDT\b", text):
        base = m.group(1)
        # 过滤明显不是币种的
        if not any(c.isalpha() for c in base):
            continue
        if base in ("USD", "USDC", "BUSD", "TUSD", "USDP", "FDUSD"):
            continue
        pair = base + "USDT"
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    return pairs


def map_pairs_to_asset_ids(
    pairs: list[str],
    conn,
    source_hint: str = "binance",
    context_text: str = "",
) -> list[int]:
    """将交易对列表映射为 asset_id 列表（多资产）。

    Args:
        pairs: 交易对列表（如 ["BTCUSDT", "ETHUSDT"]）
        conn: 数据库连接
        source_hint: 优先查的数据源（默认 binance，因为交易对来自币安）
        context_text: 标题+正文；用于「商品/常用词同名」消歧（见
            `_COMMODITY_AMBIGUOUS_SYMBOLS`）。为空时不做消歧（向后兼容）。

    Returns:
        asset_id 列表（去重，顺序按 pairs 出现顺序）
    """
    if not pairs:
        return []

    asset_ids: list[int] = []
    seen: set[int] = set()

    for pair in pairs:
        base = extract_base_symbol(pair)
        if not base:
            continue

        # 同名消歧：商品/常用词 symbol 需正文含加密语境，否则视为宏观商品新闻误标
        ambiguous = base in _COMMODITY_AMBIGUOUS_SYMBOLS
        if ambiguous and context_text and not has_crypto_context(context_text):
            logger.info("同名消歧：跳过商品/常用词 symbol %s（正文无加密语境）", base)
            continue

        # 查缓存（歧义符号不缓存，避免跨文污染）
        if not ambiguous and base in _symbol_asset_cache:
            aid = _symbol_asset_cache[base]
            if aid is not None and aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 白名单优先（CAT-LINKER-DIRTY）：主流符号强制取真实主网币
        if base in _CANONICAL_TOP_SYMBOLS:
            aid = _resolve_canonical_top(conn, base)
            _symbol_asset_cache[base] = aid
            if aid is not None and aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 查库：优先 source_hint 来源的 source_asset_key
        # （同 key 多条时按 CMC 排名升序选最靠前的，防止脏映射）
        # 注意：第一路精确 key 匹配也要排除 tokenized stock / 商品衍生资产。
        #       历史设计是「美股·商品通道合法资产，发送层过滤」，但线上审计 2026-09-15
        #       确认美股快讯（如 "Nvidia said ..."）经 cashtag 兜底会把 NVDA 连到
        #       NVDAX (xStock) / NVIDIA • Robinhood Token，产生 582 条错连信号，
        #       已定性为脏数据（CAT-CLEANUP v5.1 ③），故在映射源头直接排除。
        row = conn.execute(
            f"""
            SELECT a.asset_id
            FROM core.asset a
            JOIN core.asset_source_map m ON a.asset_id = m.asset_id
            WHERE m.source_code = %s
              AND UPPER(m.source_asset_key) = %s
              AND LOWER(COALESCE(a.canonical_name, '')) !~ '{_NON_CRYPTO_NAME_SQL}'
            ORDER BY a.market_cap_rank ASC NULLS LAST, a.asset_id
            LIMIT 1
            """,
            (source_hint, base),
        ).fetchone()
        if row:
            aid = row["asset_id"]
            _symbol_asset_cache[base] = aid
            if aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 退一步：asset 表的 canonical_symbol
        # （同 symbol 多行时避免取到脏行：
        #   1. 排除 tokenized/合成/商品衍生 等（CAT-CLEANUP v5.1 ③，源头排除）
        #   2. 排除真正仿盘标记（bridged/wrapped/intents/trophy/tomato/second chance/base coin）
        #      —— 不再用 meme 系（doge/shib/pepe/floki/inu/ai）无边界子串，避免误杀合法 meme 币（CAT-LINKER-NEG）
        #   3. 按 CMC 排名升序（rank 越小越主流），兜底用 asset_id
        # 线上审计 2026-09-15：BTC→Bitcoin Base/XRP→XRP AI/BNB→BNBTiger Inu
        # 部署复验 2026-09-15：SOL→Sol The Trophy Tomato / ETH→NEAR Intents Bridged ETH
        row = conn.execute(
            f"""
            SELECT asset_id
            FROM core.asset
            WHERE UPPER(canonical_symbol) = %s
              AND LOWER(COALESCE(canonical_name, '')) !~ '{_NON_CRYPTO_NAME_SQL}'
              AND LOWER(COALESCE(canonical_name, '')) !~ 'bridged|wrapped|intents|trophy|tomato|second[[:space:]]+chance|base[[:space:]]+coin'
            ORDER BY
                market_cap_rank ASC NULLS LAST,
                CASE WHEN LOWER(COALESCE(canonical_name, '')) LIKE '%%' || LOWER(%s) || '%%' THEN 0
                     WHEN LOWER(COALESCE(canonical_name, '')) ~ 'gold|silver|oil|gas' THEN 2
                     ELSE 1 END,
                asset_id
            LIMIT 1
            """,
            (base, base),
        ).fetchone()
        if row:
            aid = row["asset_id"]
            _symbol_asset_cache[base] = aid
            if aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 没找到，缓存 None
        _symbol_asset_cache[base] = None

    return asset_ids
