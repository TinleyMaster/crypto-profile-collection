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

# 已确认在 core.asset_source_map 中**无数据**的 source_code（P3-C1c，2026-10-16）：
# 避免每条 pairs 都白查一次 JOIN（全量 ≈ 十万次）。仅进程级记忆，补数据后需重启才失效。
_empty_source_hints: set[str] = set()

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

# 美股 ticker 串台（审计_盘面异动告警邮件_3封_2026-09-26 §三.P0-4）：与加密币同名的
# **美股代码**。Binance Square 的 `pairs` 只给裸 ticker，而两道路径都拦不住：
#   · `_NON_CRYPTO_NAME_SQL` 按资产**名称**排除 tokenized/商品衍生 —— 加密 Dash 的名称
#     就是 "Dash"，不含任何脏词；
#   · `_COMMODITY_AMBIGUOUS_SYMBOLS` 只覆盖商品同名 —— DASH 不在其中，门禁根本不执行。
# 实测：DASH 卡片唯一 1 条催化剂是「**DoorDash** 与纽约市达成 1.315 亿美元和解，涉及
# 最低工资合规调查」（纳斯达克: DASH）—— 外卖平台合规新闻被判为该币**利空**，且因它是
# 唯一一条，直接决定「净空 1」→ 触发「⚠️ 共振相悖」→ 触发强度 ×0.75。**一条假利空
# 污染三个输出位**。规则与商品同名一致：命中本集合的 symbol，正文/标题必须含**加密语境**
# （`has_crypto_context`：`$DASH` / `DASHUSDT` / 币安 / 链上 / 代币…）才认。
# ⚠️ 本集合是**兜底清单、非全量** —— 完整清单需用 `core.asset.canonical_symbol` 与美股
#    ticker 全量比对产出（需连库，本轮未做）。每条都按「股票名（交易所: 代码）」记档，
#    便于后续核对与扩充。误纳入的代价很低（真加密新闻几乎必带加密语境会被放行），
#    漏纳入则残留本缺陷，故宁可多列。
_EQUITY_TICKER_COLLISIONS = frozenset({
    "DASH",   # DoorDash Inc.（NASDAQ: DASH）—— 实物复现，见上
    "APT",    # Alpha Pro Tech Ltd.（NYSE American: APT）
    "SUI",    # Sun Communities Inc.（NYSE: SUI）
    "SOL",    # Emeren Group Ltd.（NYSE: SOL）
    "TRX",    # TRX Gold Corp.（NYSE American: TRX）
    "LINK",   # Interlink Electronics Inc.（NASDAQ: LINK）
    "STX",    # Seagate Technology Holdings plc（NASDAQ: STX）
    "AR",     # Antero Resources Corp.（NYSE: AR）
    "OP",     # OceanPal Inc.（NASDAQ: OP）
    # ── 以下 7 条为 2026-09-26 只读普查**新确证**的同类串台（每条都有实物 cid）──
    "BB",     # BlackBerry Ltd.（NYSE: BB）—— cid 13616「BlackBerry reported Q2 revenue」
    "BX",     # Blackstone Inc.（NYSE: BX）—— cid 4164 / 13650「Blackstone 私募基金/赎回排队」
    "COST",   # Costco Wholesale Corp.（NASDAQ: COST）—— cid 14272 / 5343
    "DIS",    # The Walt Disney Company（NYSE: DIS）—— cid 13989「Bob Chapek 谈 Disney」
    "SHELL",  # Shell plc（NYSE: SHELL）—— cid 12572「LNG Canada 股东含 Shell Plc」
    "BR",     # Broadridge Financial Solutions（NYSE: BR）；另有**国家代码** BR ——
              # cid 13722「Brazil's central bank cut its 2026 growth forecast」
    "UBER",   # Uber Technologies Inc.（NYSE: UBER）—— cid 1642；且与
              # 「Uber Technologies, Inc. • Robinhood Token」（xStock）同名
    # ── 以下为 2026-10-16 P3-C 评测集实测 FP（撞名小币）──
    "SNPS",   # Synopsys Inc.（NASDAQ: SNPS）—— cid 32835/32844/33231
              # 「Bitget 上线 SNPS（Synopsys）股票永续合约」误连 Snaps(#23857)
    "RTX",    # RTX Corporation（NYSE: RTX）—— cid 1636「RTX has teamed up with
              # fusion-energy startup Xcimer…」误连同名小币
    "B3",     # 巴西证券交易所 B3（B3 S.A. - Brasil, Bolsa, Balcão）—— cid 29937
              # 「推出代币化资产平台」；该稿已由「代币化」语义剔除拦截（见严格 gate 注释），
              # 列入清单作双重保险
})

# 门禁正则四条硬约束（都是只读复算 prod 后踩出来的，改动前请先跑
# `workbench/test_scan_alert_audit_20260926.py` 的 P0-4 段 + 反例样本）：
#
# ① 词表支的边界一律用 `(?<![A-Za-z])` / `(?![A-Za-z])` 而**不是** `\b`（与 scan_daemon 的
#    `_FP_PAIR_RE` 同坑）：`\b` 走 Unicode 词字符判定，中文也是词字符 ⇒
#    `Solana于9月18日将目标出块时间…`（prod cid 6173）里 `\bsolana\b` 判不出词尾，
#    **真实** Solana 新闻被误杀。取「非 ASCII 字母」而非「非字母数字」是为了**保住复数**
#    （`tokens` / `wallets` / `stablecoins` 若被 `(?![A-Za-z0-9])` 卡住，等于新引入一批漏词）。
# ② cashtag 支必须要求「至少一个 ASCII 字母」：否则 `$131.5 million`、`$163.3 million`
#    （cid 10473 DoorDash / cid 13616 BlackBerry / cid 14272 Costco 的正文）会被当成
#    `$131`/`$163` 型 cashtag ⇒ **美股财报稿直接过闸**，P0-4 白修。
# ③ 词表不能只列通用词：`Chainlink 战略储备增持 LINK`（cid 5808）、
#    `Tron 总交易额突破 30 万亿`（cid 13215）、`Optimism 批准 Upgrade 20`（cid 5622）、
#    `SOL 上涨突破 110 美元`（cid 6781）都**不含**通用词却都是真加密新闻。故补
#    撞名清单各 symbol 的加密侧项目名 + 加密专属信源 + 行情快讯口径。
#    只读复算：修 ① 前，近 30 天 358 条撞名 symbol 催化剂被误杀 32 条
#    （SOL 12 / TRX 5 / APT 4 / SUI 4 / OP 3 / LINK 2 / STX 2）—— **门禁漏词是有代价的**。
#    ⚠️ 但**不得**纳入 equity 侧高频词（etf / shares / revenue / earnings / settlement /
#    stock / analyst…），否则 DoorDash 类美股稿会重新过闸。
# ④ 本正则**同时**服务 `_COMMODITY_AMBIGUOUS_SYMBOLS`（商品 gate），故**不得**纳入
#    宏观/商品稿的高频词。实测代价：加了 `bitcoin` + `上涨|下跌|涨幅|行情|突破|新高|市值`
#    后，近 30 天商品同名 gate 有 9 条由「拦下」翻转为「放行」且全是噪音 ——
#    `金价下跌推动中国黄金进口`（cid 10154/10160）、`BTC/XAU 比率站上 50 周均线`
#    （cid 7433/7401/7405）、`Bitcoin and gold are hedges`（cid 4255）、
#    `美联储加息 / 油价逼近 100 美元`（cid 7335）等。宁可少几个词（漏掉几条行情快讯，
#    实测残差 ≈9 条/30 天），也不能把商品噪音放进来。
# ⑤ **通用词**（既不指向加密、也不指向商品/美股的日常词）同样会架空 gate ——
#    复验 `核验_催化剂门禁扩容与存量清理_a9e7901` §二C 抓到 4 条：
#      `矿工` ⇒ `澳洲煤矿矿工罢工，动力煤供应中断`（COAL gate 翻转）、
#               `Gold miner output rose 5%`（GOLD gate 翻转）；
#      `上线` ⇒ `迪士尼+ 上线新剧集`（DIS gate 失效）；
#      `stacks` ⇒ 撞上英文高频短语 `The company holds stacks of cash`。
#    处置：删 `miner|矿工|上线`、给 `stacks` 加 `(?!\s+of\b)`，并**补 `sui` 项目名**
#    抵消删 `上线` 的唯一代价（近 90 天只读复算：cid 6299「Sui 生态借贷协议 Suilend
#    已上线 2.0 版本」原本**只靠 `上线`** 放行 —— 属真加密新闻，不能误杀）。
#    只读复算（近 90 天 541 条撞名 symbol 催化剂）：修正后**仅 1 条**由放行翻转为拦下，
#    即 cid 11924「Copper futures hit a record $6.95 a pound…」（**商品噪音**，本该拦）。
#    ⚠️ 留档未处置：`现货` / `spot` 仍会把商品行情快讯放行（`现货黄金`/`spot silver`，
#    近 90 天 ≈47 条）；要同时保住「SOL 现货 ETF」这类真加密新闻，须按报告 §二C 建议 ③
#    **拆表**（商品 gate 宽松 / equity gate 只认交易对·cashtag·交易所·加密项目名），另开工单。
_CRYPTO_CONTEXT_RE = re.compile(
    # cashtag $COPPER（②：必须含字母，排除 `$131`）
    r"\$(?=[A-Za-z0-9]{2,10}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,10}"
    r"|(?<![A-Za-z0-9])[A-Z0-9]{2,15}USDT(?![A-Za-z0-9])"    # COPPERUSDT
    r"|(?<![A-Za-z0-9])[A-Z0-9]{2,15}/USDT(?![A-Za-z0-9])"   # SOL/USDT（行情快讯常用）
    r"|(?<![A-Za-z])(?:token|memecoin|meme|crypto|blockchain|on-?chain|onchain"
    r"|defi|dex|cex|airdrop|staking|listing|solana|ethereum|binance"
    r"|stablecoin|altcoin|wallet|whale|mainnet|testnet|validator|halving"
    r"|perpetual|spot|protocol"
    r"|tron|chainlink|optimism|aptos|arweave|starknet|sui"   # ③ 撞名 symbol 加密侧（sui 见 ⑤）
    r"|stacks(?!\s+of\b)"                                    # ⑤ `stacks of` 是英文高频短语
    r"|bouncebit|bedrock|myshell|blynex|distribute\.ai"
    r"|lookonchain|arkham|nansen|coindesk|cointelegraph|defillama|coingecko"
    r"|coinmarketcap|okx|bybit|coinbase|kraken|uniswap|aave|hyperliquid"
    r")(?![A-Za-z])"
    r"|代币|加密|链上|空投|现货|合约|交易所|币安|钱包|巨鲸|主网|质押",   # ⑤ 已删 上线|矿工
    re.IGNORECASE,
)


def has_crypto_context(text: str | None) -> bool:
    """正文/标题是否含加密语境（用于商品同名 symbol 消歧）。"""
    return bool(text and _CRYPTO_CONTEXT_RE.search(text))


# ── P3-C-B（2026-10-16）：歧义消歧专用「严格加密语境」+ pairs 字面剥离 ─────────────
# 背景：直接用 `has_crypto_context` 做消歧 gate 会**恒放行**，实测两条成因（均为 FP）：
#   ① pairs 回响 —— `related_pairs` 的 `XAUUSDT` 经 context_text 传回，而宽口径正则含
#      `[A-Z0-9]{2,15}USDT` 支 ⇒ gate 由 pairs 自身点亮；
#   ② 词表含 TradFi 稿同样高频的词 —— 实测：
#      · 「现货黄金失守 4,150 美元/盎司」→ `现货` 过闸 → XAU 误连（cid 33958）
#      · 「现货钯金突破 1,300 美元」    → `现货` 过闸 → XPD 误连（cid 5852）
#      · 「韩国交易所启动熔断机制」      → `交易所` 过闸 → XAU 误连（cid 35922）
#      · 「Bitget 上线 ZCSH/SNPS 股票永续合约」→ `合约` 过闸 → SNPS 误连（cid 32835/32844/33231）
#      · 「Oil prices fell… Spot gold also declined」→ `spot` 过闸（cid 26975）
# 处置：消歧 gate 改用**严格词表**（剔除 现货/合约/交易所/spot/perpetual/listing），判定前
# 先**剥离 base 的字面形态**（`$XAU` / `XAUUSDT` / `XAU/USDT` / 裸 `XAU`）。
# ⚠️ `has_crypto_context` 保持宽口径不变 —— 其它调用方（gold 采样器护栏）依赖它。
_CRYPTO_CONTEXT_STRICT_RE = re.compile(
    r"\$(?=[A-Za-z0-9]{2,10}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,10}"
    r"|(?<![A-Za-z0-9])[A-Z0-9]{2,15}USDT(?![A-Za-z0-9])"    # XXXUSDT
    r"|(?<![A-Za-z0-9])[A-Z0-9]{2,15}/USDT(?![A-Za-z0-9])"   # XXX/USDT
    r"|(?<![A-Za-z])(?:token(?!i[sz])|memecoin|meme|crypto|blockchain|on-?chain|onchain"
    r"|defi|dex|cex|airdrop|staking|stablecoin|altcoin|wallet|whale"
    r"|mainnet|testnet|validator|halving|protocol"
    r"|solana|ethereum|tron|chainlink|optimism|aptos|arweave|starknet|sui"
    r"|stacks(?!\s+of\b)"
    r"|bouncebit|bedrock|myshell|blynex|distribute\.ai"
    r")(?![A-Za-z])"
    r"|代币(?!化)|加密|链上|空投|质押|主网|巨鲸|钱包",   # 已剔 现货|合约|交易所
    re.IGNORECASE,
)
# ⚠️ 严格 gate **另需剔除**:
#   · TradFi 稿高频词 —— 现货/合约/交易所/spot/perpetual/listing；
#   · **交易所名与信源名** —— binance/okx/bybit/coinbase/kraken/uniswap/aave/hyperliquid
#     + 中文「币安」，以及 lookonchain/arkham/nansen/coindesk/cointelegraph/defillama/
#     coingecko/coinmarketcap。理由：它们只证明「报道方是加密机构」，**不证明该 symbol
#     本身是加密资产** —— 实测 cid 33231「Bybit 新增新思科技（SNPS USDT）…股票永续」
#     正是靠 `Bybit` 过闸而误连 SNPS。
#   · **RWA 代币化语义** —— `代币化` / `tokenized` / `tokenization`（`token(?!i[sz])`、
#     `代币(?!化)`）。「巴西证券交易所 B3 推出**代币化**资产平台」（cid 29937）、
#     「使用**代币化**美国国债的三方回购」（cid 35116）说的是真实世界资产上链，
#     **不是某个币的事件**；而真加密新闻的「代币」多接上市/申领/空投。

# 剥离时豁免的“非 ticker”串（避免把 `USD` 之类当 symbol 处理）
_STRIP_SKIP = frozenset({"USD", "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD"})


def strip_symbol_literals(text: str | None, base: str | None) -> str:
    """剥离文本中 base 的交易对/cashtag 字面形态（歧义 gate 的前置处理）。

    消除 `related_pairs` 经 context_text 传回造成的「pairs 自身点亮 gate」回响。
    例：base=XAU → `$XAU` / `XAUUSDT` / `XAU/USDT` / 裸 `XAU` 全部替换为空格。
    """
    if not text or not base or base.upper() in _STRIP_SKIP:
        return text or ""
    b = re.escape(base)
    pattern = (
        r"\$?" + b + r"\s*/\s*(?:USDT|USDC|BUSD|FDUSD|TUSD|USDP|USD)\b"
        r"|\$" + b + r"(?![A-Za-z0-9])"
        r"|(?<![A-Za-z0-9])" + b + r"(?![A-Za-z0-9])"
    )
    return re.sub(pattern, " ", text, flags=re.IGNORECASE)


def has_strict_crypto_context(text: str | None, base: str | None = None) -> bool:
    """严格加密语境判定（**歧义 gate 专用**）：先剥离 base 字面，再查严格词表。"""
    return bool(_CRYPTO_CONTEXT_STRICT_RE.search(strip_symbol_literals(text, base)))



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
# 稳定币 quote：`WUSDT` → base=`W` 合法（单字符 ticker 是真实币）
_STABLE_QUOTES = frozenset({"USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD"})


# 裸 ticker 形态（无 quote 后缀）：`related_pairs` 常直接给 `CT` / `NEAR` / `CASHCAT`
# 这类**裸 symbol**（源未拼 USDT），原实现一律返回 None ⇒ 全部漏连。
# 实测（P3-C，2026-10-16）：CT / CASHCAT×3 / PUFFER×2 / AEON×2 / DGAI×2 / DRV / NEAR×2 /
# WBTC 等 14 条正样本因此漏连，且全部在 core.asset 有 market_cap_rank（本可正确归属）。
_BARE_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}$")
# 裸串里不作 base 的稳定币/法币（作为事件主语无意义）
_BARE_SYMBOL_SKIP = frozenset({"USD", "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD"})


def _split_quote(pair_upper: str) -> tuple[str, str] | None:
    """按 quote 后缀拆分交易对；返回 (base, quote)，不匹配返回 None。

    ⚠️ 两类 quote 的 base 长度下限不同（P3-C1b，2026-10-16）：
      · **稳定币 quote**（USDT…）→ base ≥ 1：`WUSDT`→`W`、`GUSDT`→`G` 是真实币；
      · **币本位 quote**（BTC/ETH/BNB/SOL/XRP）→ base ≥ 2，否则 `WBTC` 会被错拆成
        `W`+`BTC`（Wormhole），而它其实是 wrapped BTC 的符号本身。
    """
    for quote in _QUOTE_ASSETS:
        if pair_upper.endswith(quote) and len(pair_upper) > len(quote):
            base = pair_upper[: -len(quote)]
            min_len = 1 if quote in _STABLE_QUOTES else 2
            if len(base) >= min_len and any(c.isalpha() for c in base):
                return base, quote
    return None


def extract_base_symbol(pair: str) -> str | None:
    """从交易对中提取 base symbol（大写）。

    ① 优先按 quote 后缀拆分（`XXXUSDT` → `XXX`）；
    ② 无后缀时，若整串是合法 ticker 形态（`^[A-Z0-9]{1,10}$` 且含字母）则返回自身（裸 symbol）。
    """
    if not pair:
        return None
    pair = pair.upper().strip()
    sp = _split_quote(pair)
    if sp:
        return sp[0]
    # P3-C1a：裸 symbol 兜底
    if (
        2 <= len(pair) <= 10
        and _BARE_SYMBOL_RE.match(pair)
        and any(c.isalpha() for c in pair)
        and pair not in _BARE_SYMBOL_SKIP
    ):
        return pair
    return None


def is_bare_symbol(pair: str) -> bool:
    """pair 是否为**裸 symbol**（无 quote 后缀）—— 弱信号，调用方需额外佐证。

    币安广场对美股/宏观/商品稿同样标裸 ticker（实测 SHY/B3/GM/MS/ACN/PEP/RTX/V/C/M/
    BLK/ON/CAG/RBLX/HTZ/TLT/IEF/AGG 等），故裸 symbol 不可仅凭 pairs 认领。
    """
    if not pair:
        return False
    return _split_quote(pair.upper().strip()) is None


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

    # 模式 3：斜杠形态 `CT/USDT`（P3-C1，2026-10-16）—— 正文/标题常见，
    # 原实现只认 `$CT` 与 `CTUSDT`，实测 cid 19112「OKX 将于 9 月 30 日上线
    # CT/USDT 现货交易」因此漏连。
    for m in re.finditer(r"\b([A-Z0-9]{2,20})/USDT\b", text):
        base = m.group(1)
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
        context_text: 标题+正文；用于「同名撞车」消歧 —— 商品/常用词
            （`_COMMODITY_AMBIGUOUS_SYMBOLS`）与美股 ticker
            （`_EQUITY_TICKER_COLLISIONS`）。为空时不做消歧（向后兼容）。

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

        # 白名单优先（CAT-LINKER-DIRTY）：主流符号强制取真实主网币。
        # ⚠️ P3-C1d（2026-10-16）：白名单**先于**歧义 gate —— `_CANONICAL_TOP_SYMBOLS` 与
        #    `_EQUITY_TICKER_COLLISIONS` 有交集（SOL/LINK/TRX/APT/SUI/OP），实测
        #    `SOLUSDT` 被 gate 误杀（cid 21875/21862「SOL holdings」稿无加密语）。权衡：
        #    白名单仅 36 个主流币，其撞名股票均为小盘股、新闻量远小于加密侧；
        #    **漏连是确定损失、误连是小概率**，故白名单内一律取真币、不进 gate。
        if base in _CANONICAL_TOP_SYMBOLS:
            aid = _resolve_canonical_top(conn, base)
            _symbol_asset_cache[base] = aid
            if aid is not None and aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 同名消歧：商品/常用词 symbol 与**美股 ticker 撞名**者，需正文含**严格**加密语境，
        # 否则视为宏观商品新闻 / 美股快讯误标（后者见 `_EQUITY_TICKER_COLLISIONS`）。
        # P3-C-B（2026-10-16）：改用 `has_strict_crypto_context`（先剥离 pairs 字面、再查严格
        # 词表）—— 原 `has_crypto_context` 会被 pairs 回响与「现货/合约/交易所/spot」等
        # TradFi 稿高频词恒放行（5 类实测 FP 见 `_CRYPTO_CONTEXT_STRICT_RE` 注释）。
        ambiguous = (base in _COMMODITY_AMBIGUOUS_SYMBOLS
                     or base in _EQUITY_TICKER_COLLISIONS)
        if ambiguous and context_text and not has_strict_crypto_context(context_text, base):
            logger.info("同名消歧：跳过商品/美股同名 symbol %s（正文无严格加密语境）", base)
            continue

        # 查缓存（歧义符号不缓存，避免跨文污染）
        if not ambiguous and base in _symbol_asset_cache:
            aid = _symbol_asset_cache[base]
            if aid is not None and aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 查库：优先 source_hint 来源的 source_asset_key
        # （同 key 多条时按 CMC 排名升序选最靠前的，防止脏映射）
        #
        # ⚠️ P3-C1c 实测（2026-10-16，只读复算 prod）：`core.asset_source_map` 中
        #    **没有任何源以 ticker 为 key** —— cg 是 slug（`bitcoin`…）、cmc/dl 是数字 id
        #    （`1`/`2786`…）、cm 是小写 slug（24 行，形如 `ltc`/`doge` 尚可当 ticker）。
        #    而调用方传的 `source_hint='binance'` **表内 0 行** ⇒ 本路对 ticker 输入**恒空**。
        #    ⇒ 原工单的「改 source_hint 回落」经实测**判为无解**（无 ticker key 源可回落），
        #      改为「空源记忆」短路：首次查空即记档，后续跳过（保留本路以备将来补数据）。
        # 注意：第一路精确 key 匹配也要排除 tokenized stock / 商品衍生资产。
        #       历史设计是「美股·商品通道合法资产，发送层过滤」，但线上审计 2026-09-15
        #       确认美股快讯（如 "Nvidia said ..."）经 cashtag 兜底会把 NVDA 连到
        #       NVDAX (xStock) / NVIDIA • Robinhood Token，产生 582 条错连信号，
        #       已定性为脏数据（CAT-CLEANUP v5.1 ③），故在映射源头直接排除。
        row = None
        if source_hint not in _empty_source_hints:
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
            if row is None:
                _empty_source_hints.add(source_hint)
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
