"""统一代币赛道（sector）分类 taxonomy。

把分散在多处的赛道信号（CMC tags / category_hint / CG categories / asset_type）
归一化到统一赛道枚举，供评分、采集优先级、解锁预警等下游逻辑引用。

设计原则：
- 只映射「明确赛道」的标签，泛化标签（ecosystem / smart-contracts / web3 等）
  不参与赛道判定，避免误判。
- 一个代币可命中多个赛道（多标签），返回 (sector, confidence) 列表；
  主赛道取置信度最高者。
"""

from __future__ import annotations

# ── 赛道枚举（对齐投研代币分类清单）──────────────────────────────
SECTORS = (
    "l1",          # 公链
    "l2",          # 二层 / Rollup
    "defi",        # DeFi 协议
    "meme",        # Meme / 土狗
    "gamefi",      # GameFi / NFT
    "rwa",         # 现实资产代币化
    "ai",          # AI + Crypto
    "stablecoin",  # 稳定币
    "cex_token",   # 中心化平台币
    "derivatives", # 衍生品协议
    "depin",       # DePIN
    "infra",       # 基础设施 / 其他有产品代币
    "other",       # 无法归类
)

SECTOR_LABELS = {
    "l1": "L1 公链",
    "l2": "L2 二层",
    "defi": "DeFi",
    "meme": "Meme",
    "gamefi": "GameFi / NFT",
    "rwa": "RWA",
    "ai": "AI + Crypto",
    "stablecoin": "稳定币",
    "cex_token": "平台币",
    "derivatives": "衍生品",
    "depin": "DePIN",
    "infra": "基础设施",
    "other": "其他",
}

# ── CMC tag → (sector, confidence) ──────────────────────────────
# 仅收录「明确赛道」标签；ecosystem 类（ethereum-ecosystem 等）表示
# 链生态归属而非赛道，不在此映射。
CMC_TAG_SECTOR_MAP: dict[str, tuple[str, float]] = {
    # L1 / L2
    "layer-1": ("l1", 0.9),
    "layer-2": ("l2", 0.9),
    "rollups": ("l2", 0.8),
    "rollups-as-a-service": ("l2", 0.7),
    "zero-knowledge-zk": ("l2", 0.6),
    "zk-rollups": ("l2", 0.85),
    "zk-coprocessors": ("l2", 0.6),
    # DeFi
    "defi": ("defi", 0.9),
    "decentralized-exchange-dex-token": ("defi", 0.8),
    "yield-farming": ("defi", 0.7),
    "lending": ("defi", 0.8),
    "borrowing": ("defi", 0.8),
    "liquid-staking-tokens-lsds": ("defi", 0.8),
    "liquid-restaking-tokens-lrts": ("defi", 0.8),
    "dex-tools": ("defi", 0.7),
    "launchpad": ("defi", 0.6),
    "index-fund": ("defi", 0.7),
    "insurance": ("defi", 0.7),
    "prediction-market": ("defi", 0.7),
    # 稳定币
    "stablecoin": ("stablecoin", 0.95),
    "asset-backed-stablecoin": ("stablecoin", 0.9),
    "fiat-stablecoin": ("stablecoin", 0.9),
    "usd-stablecoin": ("stablecoin", 0.9),
    "eur-stablecoin": ("stablecoin", 0.9),
    "algorithmic-stablecoin": ("stablecoin", 0.85),
    "yield-bearing-stablecoin": ("stablecoin", 0.9),
    "stablecoin-protocol": ("stablecoin", 0.8),
    # Meme
    "memes": ("meme", 0.9),
    "animal-memes": ("meme", 0.9),
    "cat-themed": ("meme", 0.8),
    "doggone-doggerel": ("meme", 0.8),
    # GameFi / NFT
    "gaming": ("gamefi", 0.9),
    "play-to-earn": ("gamefi", 0.9),
    "metaverse": ("gamefi", 0.8),
    "collectibles-nfts": ("gamefi", 0.8),
    "fan-token": ("gamefi", 0.7),
    "move-to-earn": ("gamefi", 0.8),
    "gambling": ("gamefi", 0.6),
    # RWA
    "real-world-assets-protocols": ("rwa", 0.9),
    "tokenized-assets": ("rwa", 0.8),
    "tokenized-stock": ("rwa", 0.9),
    "tradfi-assets-derivatives": ("rwa", 0.8),
    "rehypothecated-crypto": ("rwa", 0.8),
    "tokenized-etfs": ("rwa", 0.8),
    "real-estate": ("rwa", 0.8),
    # AI
    "ai-big-data": ("ai", 0.9),
    "ai-agents": ("ai", 0.9),
    "ai-applications": ("ai", 0.8),
    "distributed-computing": ("ai", 0.7),
    "ai-meme-coins": ("ai", 0.7),
    # 平台币
    "platform": ("cex_token", 0.7),
    "centralized-exchange-token": ("cex_token", 0.9),
    "centralized-exchange": ("cex_token", 0.85),
    "discount-token": ("cex_token", 0.8),
    "exchange-token": ("cex_token", 0.8),
    # 衍生品
    "synthetics": ("derivatives", 0.8),
    "derivatives": ("derivatives", 0.9),
    "options": ("derivatives", 0.8),
    "perpetual-dex": ("derivatives", 0.85),
    # DePIN
    "depin": ("depin", 0.9),
    "storage": ("depin", 0.7),
    "decentralized-storage": ("depin", 0.8),
    "dewi": ("depin", 0.8),
    "wireless": ("depin", 0.7),
    # 基础设施
    "infrastructure": ("infra", 0.85),
    "oracle": ("infra", 0.75),
    "bridge": ("infra", 0.7),
    "cross-chain": ("infra", 0.7),
    "node": ("infra", 0.6),
    "rpc-node": ("infra", 0.7),
    "name-service": ("infra", 0.7),
    "wallets": ("infra", 0.7),
    "privacy": ("infra", 0.7),
    "data-availability": ("infra", 0.8),
    # 支付/交换媒介 → 归 infra（基础设施类）
    "medium-of-exchange": ("infra", 0.6),
    "store-of-value": ("infra", 0.6),
    "mineable": ("infra", 0.5),
    "pow": ("infra", 0.5),
    "pos": ("infra", 0.5),
    "payments": ("infra", 0.65),
    "services": ("infra", 0.6),
    "marketplace": ("infra", 0.5),
    # 社交 / 其他低置信度标签
    "communications-social-media": ("infra", 0.5),
    "dao": ("infra", 0.4),
    "desci": ("ai", 0.5),
    "entertainment": ("gamefi", 0.4),
    "social-token": ("infra", 0.4),
    "media": ("infra", 0.4),
    "asset-management": ("defi", 0.5),
    "governance": ("defi", 0.4),
    "marketing": ("infra", 0.3),
    "education": ("infra", 0.3),
    "health": ("ai", 0.4),
    "energy": ("depin", 0.4),
    "hybrid-pow-pos": ("l1", 0.4),
    "move-vm": ("l1", 0.5),
    "web3": ("infra", 0.3),
}

# ── CMC category_hint → (sector, confidence) ────────────────────
# category_hint 是 CMC 给出的单一主类别，作为 tags 缺失时的兜底。
CMC_CATEGORY_SECTOR_MAP: dict[str, tuple[str, float]] = {
    "layer-1": ("l1", 0.8),
    "layer-2": ("l2", 0.8),
    "defi": ("defi", 0.8),
    "memes": ("meme", 0.8),
    "gaming": ("gamefi", 0.7),
    "play-to-earn": ("gamefi", 0.7),
    "collectibles-nfts": ("gamefi", 0.7),
    "real-world-assets-protocols": ("rwa", 0.8),
    "tokenized-assets": ("rwa", 0.7),
    "tokenized-stock": ("rwa", 0.8),
    "ai-big-data": ("ai", 0.8),
    "ai-agents": ("ai", 0.8),
    "platform": ("cex_token", 0.6),
    "depin": ("depin", 0.8),
    "synthetics": ("derivatives", 0.7),
    # 稳定币
    "stablecoin": ("stablecoin", 0.9),
    "asset-backed-stablecoin": ("stablecoin", 0.85),
    "algorithmic-stablecoin": ("stablecoin", 0.8),
    # 其他常见 category
    "centralized-exchange": ("cex_token", 0.75),
    "marketplace": ("infra", 0.5),
    "services": ("infra", 0.55),
    "medium-of-exchange": ("infra", 0.55),
    "store-of-value": ("infra", 0.55),
    "mineable": ("infra", 0.5),
    "privacy": ("infra", 0.6),
    "infrastructure": ("infra", 0.75),
    "oracle": ("infra", 0.7),
    "wallets": ("infra", 0.65),
    # 其他常见 category
    "communications-social-media": ("infra", 0.5),
    "gambling": ("gamefi", 0.6),
    "dao": ("infra", 0.4),
    "desci": ("ai", 0.5),
    "entertainment": ("gamefi", 0.4),
    "health": ("ai", 0.4),
    "asset-management": ("defi", 0.5),
    "education": ("infra", 0.3),
    "media": ("infra", 0.4),
    "energy": ("depin", 0.4),
    "marketing": ("infra", 0.3),
    "hybrid-pow-pos": ("l1", 0.4),
    "binance-chain": ("l1", 0.5),
}


def classify_cmc_sectors(tags: list[str] | None,
                         category_hint: str | None) -> list[tuple[str, float]]:
    """根据 CMC tags + category_hint 归一化出赛道列表。

    返回 [(sector, confidence), ...]，按置信度降序；无明确赛道返回空列表。
    """
    best: dict[str, float] = {}

    for tag in (tags or []):
        hit = CMC_TAG_SECTOR_MAP.get((tag or "").strip().lower())
        if hit:
            sector, conf = hit
            best[sector] = max(best.get(sector, 0.0), conf)

    hint = (category_hint or "").strip().lower()
    if hint:
        hit = CMC_CATEGORY_SECTOR_MAP.get(hint)
        if hit:
            sector, conf = hit
            best[sector] = max(best.get(sector, 0.0), conf)

    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


# ── CoinGecko category → (sector, confidence) ────────────────────
# CG 的分类命名风格与 CMC 不同，单独映射。
# 注意："XXX Ecosystem" 类表示链生态归属而非赛道，不参与映射。
CG_CATEGORY_SECTOR_MAP: dict[str, tuple[str, float]] = {
    # L1 / L2
    "layer 1 (l1)": ("l1", 0.85),
    "smart contract platform": ("l1", 0.7),
    "layer 2 (l2)": ("l2", 0.85),
    "zero-knowledge (zk)": ("l2", 0.6),
    "zk rollups": ("l2", 0.8),
    "rollups-as-a-service": ("l2", 0.7),
    # DeFi
    "decentralized finance (defi)": ("defi", 0.9),
    "decentralized exchange (dex)": ("defi", 0.8),
    "dex aggregators": ("defi", 0.75),
    "yield farming": ("defi", 0.75),
    "lending": ("defi", 0.8),
    "borrowing": ("defi", 0.8),
    "liquid staking": ("defi", 0.8),
    "liquid staking tokens": ("defi", 0.8),
    "liquid restaking": ("defi", 0.8),
    "restaking": ("defi", 0.75),
    "yield aggregator": ("defi", 0.75),
    "indexes": ("defi", 0.7),
    "insurance": ("defi", 0.7),
    "prediction market": ("defi", 0.7),
    "launchpad": ("defi", 0.65),
    "governance": ("defi", 0.5),
    "synthetics": ("derivatives", 0.8),
    # 稳定币
    "stablecoins": ("stablecoin", 0.9),
    "algorithmic stablecoin": ("stablecoin", 0.85),
    "algo-stables": ("stablecoin", 0.85),
    "bridged stablecoin": ("stablecoin", 0.8),
    "stablecoin issuer": ("stablecoin", 0.8),
    "dual-token stablecoin": ("stablecoin", 0.8),
    "partially algorithmic stablecoin": ("stablecoin", 0.8),
    "stablecoin wrapper": ("stablecoin", 0.7),
    "crypto-backed tokens": ("stablecoin", 0.6),
    # Meme
    "meme": ("meme", 0.9),
    "dog-themed": ("meme", 0.85),
    "cat-themed": ("meme", 0.85),
    "ai meme": ("meme", 0.7),
    # GameFi / NFT
    "gaming (gamefi)": ("gamefi", 0.9),
    "play to earn": ("gamefi", 0.85),
    "metaverse": ("gamefi", 0.8),
    "nft": ("gamefi", 0.8),
    "nft marketplace": ("gamefi", 0.75),
    "fan token": ("gamefi", 0.7),
    "move to earn": ("gamefi", 0.8),
    "gamified mining": ("gamefi", 0.7),
    "luck games": ("gamefi", 0.6),
    "physical tcg": ("gamefi", 0.7),
    "nftfi": ("gamefi", 0.65),
    "nft lending": ("gamefi", 0.65),
    # RWA
    "real world assets (rwa)": ("rwa", 0.9),
    "tokenized assets": ("rwa", 0.8),
    "tokenized stock": ("rwa", 0.9),
    "tokenized exchange-traded funds (etfs)": ("rwa", 0.85),
    "tokenized exchange-traded product (etps)": ("rwa", 0.85),
    "rwa lending": ("rwa", 0.8),
    # AI
    "artificial intelligence (ai)": ("ai", 0.9),
    "ai agents": ("ai", 0.9),
    "ai applications": ("ai", 0.8),
    "decentralized ai": ("ai", 0.8),
    # 平台币
    "exchange-based tokens": ("cex_token", 0.85),
    "cex": ("cex_token", 0.8),
    "cedefi": ("cex_token", 0.7),
    # 衍生品
    "derivatives": ("derivatives", 0.9),
    "options": ("derivatives", 0.8),
    "perpetual dex": ("derivatives", 0.85),
    "options vault": ("derivatives", 0.75),
    "interest rate derivatives": ("derivatives", 0.75),
    "exotic options": ("derivatives", 0.7),
    # DePIN
    "depin": ("depin", 0.9),
    # 基础设施
    "infrastructure": ("infra", 0.85),
    "oracle": ("infra", 0.75),
    "bridge": ("infra", 0.7),
    "canonical bridge": ("infra", 0.7),
    "cross chain bridge": ("infra", 0.7),
    "bridge aggregator": ("infra", 0.65),
    "bridge aggregators": ("infra", 0.65),
    "wallets": ("infra", 0.7),
    "privacy": ("infra", 0.7),
    "domains": ("infra", 0.7),
    "name service": ("infra", 0.7),
    "developer tools": ("infra", 0.7),
    "mev": ("infra", 0.7),
    "block builders": ("infra", 0.7),
    "identity & reputation": ("infra", 0.65),
    "data availability": ("infra", 0.8),
    "interface": ("infra", 0.6),
    "services": ("infra", 0.6),
    "payments": ("infra", 0.65),
    "chain": ("l1", 0.6),
    # DeFi 补充
    "prediction markets": ("defi", 0.7),
    "synthetic asset": ("derivatives", 0.75),
    "asset management": ("defi", 0.6),
    "liquid restaking tokens": ("defi", 0.8),
    "yield-bearing tokens": ("defi", 0.6),
    # 稳定币补充
    "yield-bearing stablecoin": ("stablecoin", 0.9),
    # Meme 补充
    "parody meme": ("meme", 0.7),
    "desci meme": ("meme", 0.6),
    # GameFi 补充
    "gambling (gamblefi)": ("gamefi", 0.75),
    "entertainment": ("gamefi", 0.5),
    # RWA 补充
    "tokenized btc": ("rwa", 0.7),
    # AI 补充
    "decentralized science (desci)": ("ai", 0.6),
    "healthcare": ("ai", 0.5),
    "analytics": ("ai", 0.5),
    # 基础设施补充
    "payment solutions": ("infra", 0.65),
    "decentralized identifier (did)": ("infra", 0.6),
    "socialfi": ("infra", 0.55),
    "dao": ("infra", 0.4),
    "media": ("infra", 0.4),
    "education": ("infra", 0.3),
    "e-commerce": ("infra", 0.4),
    "retail": ("infra", 0.3),
    "charity": ("infra", 0.3),
    "regenerative finance (refi)": ("defi", 0.5),
    "telegram apps": ("infra", 0.4),
    "trading bots": ("infra", 0.5),
    "music": ("infra", 0.3),
    "neobank": ("infra", 0.5),
    "mobile mining": ("depin", 0.5),
    "proof of work (pow)": ("l1", 0.4),
    "multiplier denominated tokens": ("defi", 0.4),
    "btcfi protocol": ("defi", 0.6),
    "surge launchpad": ("defi", 0.5),
    "discord bots": ("infra", 0.4),
}


def classify_cg_sectors(categories: list[str] | None) -> list[tuple[str, float]]:
    """根据 CoinGecko categories 归一化出赛道列表。

    返回 [(sector, confidence), ...]，按置信度降序；无明确赛道返回空列表。
    """
    best: dict[str, float] = {}

    for cat in (categories or []):
        hit = CG_CATEGORY_SECTOR_MAP.get((cat or "").strip().lower())
        if hit:
            sector, conf = hit
            best[sector] = max(best.get(sector, 0.0), conf)

    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


# ── DefiLlama category → (sector, confidence) ────────────────────
# DL 以 DeFi 协议分类为主，赛道粒度较细。
DL_CATEGORY_SECTOR_MAP: dict[str, tuple[str, float]] = {
    # DeFi 子类 → defi
    "dexs": ("defi", 0.9),
    "yield": ("defi", 0.85),
    "lending": ("defi", 0.85),
    "derivatives": ("derivatives", 0.9),
    "liquid staking": ("defi", 0.8),
    "farm": ("defi", 0.75),
    "launchpad": ("defi", 0.65),
    "cdp": ("defi", 0.8),
    "yield aggregator": ("defi", 0.8),
    "dex aggregator": ("defi", 0.75),
    "indexes": ("defi", 0.7),
    "prediction market": ("defi", 0.7),
    "insurance": ("defi", 0.7),
    "liquid restaking": ("defi", 0.8),
    "restaking": ("defi", 0.75),
    "reserve currency": ("defi", 0.7),
    "leveraged farming": ("defi", 0.7),
    "liquidity manager": ("defi", 0.7),
    "liquidations": ("defi", 0.65),
    "cdp manager": ("defi", 0.65),
    "staking pool": ("defi", 0.7),
    "staking rental": ("defi", 0.6),
    "basis trading": ("defi", 0.7),
    "options": ("derivatives", 0.8),
    "synthetics": ("derivatives", 0.8),
    "options vault": ("derivatives", 0.75),
    "interest rate derivatives": ("derivatives", 0.7),
    "exotic options": ("derivatives", 0.65),
    # 稳定币
    "algo-stables": ("stablecoin", 0.85),
    "stablecoin issuer": ("stablecoin", 0.8),
    "dual-token stablecoin": ("stablecoin", 0.8),
    "partially algorithmic stablecoin": ("stablecoin", 0.8),
    "stablecoin wrapper": ("stablecoin", 0.7),
    # RWA
    "rwa": ("rwa", 0.85),
    "rwa lending": ("rwa", 0.8),
    # GameFi / NFT
    "gaming": ("gamefi", 0.8),
    "nft marketplace": ("gamefi", 0.75),
    "gamified mining": ("gamefi", 0.7),
    "luck games": ("gamefi", 0.6),
    "physical tcg": ("gamefi", 0.7),
    "nftfi": ("gamefi", 0.65),
    "nft lending": ("gamefi", 0.65),
    "nft automated strategies": ("gamefi", 0.6),
    "nft launchpad": ("gamefi", 0.6),
    # AI
    "ai agents": ("ai", 0.85),
    "decentralized ai": ("ai", 0.8),
    # 平台币
    "cex": ("cex_token", 0.8),
    "cedefi": ("cex_token", 0.7),
    # DePIN
    "depin": ("depin", 0.85),
    # 基础设施
    "bridge": ("infra", 0.7),
    "canonical bridge": ("infra", 0.7),
    "cross chain bridge": ("infra", 0.7),
    "bridge aggregator": ("infra", 0.65),
    "bridge aggregators": ("infra", 0.65),
    "oracle": ("infra", 0.75),
    "wallets": ("infra", 0.7),
    "privacy": ("infra", 0.7),
    "domains": ("infra", 0.7),
    "developer tools": ("infra", 0.7),
    "mev": ("infra", 0.7),
    "block builders": ("infra", 0.7),
    "identity & reputation": ("infra", 0.65),
    "interface": ("infra", 0.6),
    "services": ("infra", 0.6),
    "payments": ("infra", 0.65),
    "chain": ("l1", 0.6),
    "sofi": ("defi", 0.7),
    "dao service provider": ("infra", 0.6),
    "foundation": ("infra", 0.5),
    "governance incentives": ("defi", 0.5),
    "ponzi": ("meme", 0.5),
    "meme": ("meme", 0.8),
    # 工具/应用类
    "trading app": ("infra", 0.5),
    "telegram bot": ("infra", 0.5),
    "onchain capital allocator": ("defi", 0.6),
    "yield lottery": ("defi", 0.5),
    "dca tools": ("defi", 0.5),
    "charity fundraising": ("infra", 0.3),
    "crypto card issuer": ("infra", 0.5),
    "token locker": ("infra", 0.5),
    "coins tracker": ("infra", 0.4),
    "uncollateralized lending": ("defi", 0.7),
    "ve-incentive automator": ("defi", 0.5),
    "risk curators": ("defi", 0.5),
}


def classify_dl_sectors(category: str | None) -> list[tuple[str, float]]:
    """根据 DefiLlama category 归一化出赛道列表。

    DL 是单值分类，返回最多一个赛道。
    """
    if not category:
        return []
    hit = DL_CATEGORY_SECTOR_MAP.get(category.strip().lower())
    if hit:
        return [hit]
    return []


def merge_sectors(*sources: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """合并多来源的赛道信号，同一赛道取最高置信度。

    参数：多个来源的 [(sector, confidence), ...] 列表。
    返回：合并后的 [(sector, confidence), ...]，按置信度降序。
    """
    best: dict[str, float] = {}
    for source in sources:
        for sector, conf in source:
            best[sector] = max(best.get(sector, 0.0), conf)
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


def primary_sector(sectors: list[tuple[str, float]]) -> str:
    """取置信度最高的赛道；无赛道返回 'other'。"""
    return sectors[0][0] if sectors else "other"


# ── 分赛道评分权重（市场五维）──────────────────────────────────
# 说明：不同赛道投研逻辑不同，市场五维（交易量/涨幅/笔数/买入占比/动量）
# 的权重应随之微调。这是「一套公式套所有代币」问题的第一步修正；
# 更细的赛道适配维度（DeFi 的 TVL、Meme 的持币集中度、L1 的开发者活跃）
# 由上游在调用时叠加，不在市场热度分里强行塞入。

# 默认权重（对齐 workbench/binance_market.py 的 SCORE_WEIGHTS）
DEFAULT_SCORE_WEIGHTS = {
    "volume": 0.30,       # 24h 交易量
    "change_24h": 0.25,   # 24h 涨跌幅
    "txns": 0.20,         # 交易笔数
    "buy_ratio": 0.15,    # 买入占比
    "momentum": 0.10,     # 短期动量（1h/5m）
}

# 各赛道的市场五维权重微调
SECTOR_SCORE_WEIGHTS: dict[str, dict[str, float]] = {
    # DeFi：重真实交易量与流动性，降低单纯涨幅权重
    "defi": {"volume": 0.35, "change_24h": 0.15, "txns": 0.20,
             "buy_ratio": 0.10, "momentum": 0.20},
    # Meme：重涨幅与买入占比（叙事驱动），轻交易量
    "meme": {"volume": 0.20, "change_24h": 0.30, "txns": 0.15,
             "buy_ratio": 0.25, "momentum": 0.10},
    # L1/L2：重交易活跃度与动量（生态活跃），轻单纯涨幅
    "l1": {"volume": 0.25, "change_24h": 0.15, "txns": 0.25,
           "buy_ratio": 0.10, "momentum": 0.25},
    "l2": {"volume": 0.25, "change_24h": 0.15, "txns": 0.25,
           "buy_ratio": 0.10, "momentum": 0.25},
    # AI：均衡，略重动量（叙事轮动快）
    "ai": {"volume": 0.30, "change_24h": 0.20, "txns": 0.20,
           "buy_ratio": 0.15, "momentum": 0.15},
    # 平台币：重交易量与动量
    "cex_token": {"volume": 0.35, "change_24h": 0.20, "txns": 0.15,
                  "buy_ratio": 0.10, "momentum": 0.20},
    # 衍生品：重交易量与动量
    "derivatives": {"volume": 0.35, "change_24h": 0.15, "txns": 0.20,
                    "buy_ratio": 0.10, "momentum": 0.20},
    # GameFi / RWA / DePIN：接近默认，略降涨幅权重
    "gamefi": {"volume": 0.30, "change_24h": 0.20, "txns": 0.20,
               "buy_ratio": 0.15, "momentum": 0.15},
    "rwa": {"volume": 0.30, "change_24h": 0.20, "txns": 0.20,
            "buy_ratio": 0.15, "momentum": 0.15},
    "depin": {"volume": 0.30, "change_24h": 0.20, "txns": 0.20,
              "buy_ratio": 0.15, "momentum": 0.15},
}


def get_sector_weights(sector: str | None) -> dict[str, float]:
    """返回某赛道的市场五维评分权重；未配置或未知赛道返回默认权重。"""
    if sector and sector in SECTOR_SCORE_WEIGHTS:
        return SECTOR_SCORE_WEIGHTS[sector]
    return DEFAULT_SCORE_WEIGHTS


# ── 分赛道采集优先级 ─────────────────────────────────────────────
# 说明：不同赛道投研资料侧重点不同，采集调度分两层：
#   1) 资产级优先级 SECTOR_COLLECT_PRIORITY：批量补齐时先采哪个赛道；
#   2) 主题级优先级 SECTOR_TOPIC_PRIORITY：单币缺失清单里哪些主题更该先补。
# 主题取值对齐 taxonomy.CONTENT_TOPICS（20 类），只列「值得优先采集」的主题，
# 未列出的主题按最低优先级排在后面。

# 资产级采集优先级权重（越大越先采集）。依据：赛道叙事的时效性 + 资料稀缺度。
# L2/AI 叙事轮动快、需及时跟踪；L1 成熟且资料齐全，优先级适中；Meme 深爬价值低。
SECTOR_COLLECT_PRIORITY: dict[str, float] = {
    "l2": 95.0,
    "ai": 90.0,
    "defi": 85.0,
    "rwa": 80.0,
    "gamefi": 75.0,
    "l1": 70.0,
    "depin": 65.0,
    "cex_token": 60.0,
    "derivatives": 55.0,
    "infra": 50.0,
    "meme": 45.0,
    "other": 30.0,
}

# 主题级采集优先级：赛道 → 按优先级降序的内容主题列表。
# 对齐用户 8 类代币投研清单的「采集权重表」，映射到 CONTENT_TOPICS。
SECTOR_TOPIC_PRIORITY: dict[str, list[str]] = {
    # L1/L2：重技术文档、审计、路线图、研报（生态活跃度优先）
    "l1": ["docs", "audit", "roadmap", "research", "tokenomics", "whitepaper", "major_event"],
    "l2": ["docs", "audit", "roadmap", "research", "tokenomics", "whitepaper", "major_event"],
    # DeFi：重审计、代币经济学、LP 流动性、治理、漏洞赏金
    "defi": ["audit", "tokenomics", "lp_liquidity", "dao_governance", "bug_bounty", "docs", "whitepaper"],
    # Meme：重交易所上线、TGE、重大事件、链上异常（深爬文档价值低）
    "meme": ["exchange_listing", "tge_ido", "major_event", "onchain_abnormal", "announcement", "whitepaper"],
    # GameFi：重白皮书、代币经济学、团队/VC、路线图、竞品
    "gamefi": ["whitepaper", "tokenomics", "team_vc", "roadmap", "competitor", "audit", "tge_ido"],
    # RWA：重审计、代币经济学、国库/多签、第三方评级、团队/VC（合规优先）
    "rwa": ["audit", "tokenomics", "treasury_multisig", "third_party_rating", "team_vc", "roadmap"],
    # AI：重技术文档、团队/VC、代币经济学、路线图、研报
    "ai": ["docs", "team_vc", "tokenomics", "roadmap", "research", "whitepaper"],
    # 平台币：重代币经济学（销毁机制）、交易所上线、第三方评级、研报、重大事件
    "cex_token": ["tokenomics", "exchange_listing", "third_party_rating", "research", "major_event"],
    # 衍生品：重 LP 流动性、链上异常（清算）、审计、代币经济学、研报
    "derivatives": ["lp_liquidity", "onchain_abnormal", "audit", "tokenomics", "research"],
    # DePIN：重技术文档、路线图、代币经济学、团队/VC
    "depin": ["docs", "roadmap", "tokenomics", "team_vc", "whitepaper"],
    # 基础设施：重技术文档、路线图、代币经济学、审计、研报
    "infra": ["docs", "roadmap", "tokenomics", "audit", "research"],
    # 其他：通用兜底
    "other": ["whitepaper", "docs", "audit", "tokenomics", "roadmap", "research"],
}

# 主题优先级兜底：所有 CONTENT_TOPICS 的默认排序（未在赛道矩阵中列出的排最后）。
DEFAULT_TOPIC_PRIORITY: list[str] = [
    "whitepaper", "docs", "audit", "tokenomics", "roadmap", "research",
    "team_vc", "lp_liquidity", "treasury_multisig", "tge_ido",
    "exchange_listing", "major_event", "announcement", "third_party_rating",
    "dao_governance", "bug_bounty", "competitor", "onchain_abnormal",
    "deck", "other",
]


def get_sector_collect_priority(sector: str | None) -> float:
    """返回某赛道的资产级采集优先级权重；未知赛道回退 other。"""
    return SECTOR_COLLECT_PRIORITY.get(sector or "other", SECTOR_COLLECT_PRIORITY["other"])


def get_sector_topic_priorities(sector: str | None) -> list[str]:
    """返回某赛道的主题采集优先级列表（降序）；未知赛道回退 other。"""
    return SECTOR_TOPIC_PRIORITY.get(sector or "other", SECTOR_TOPIC_PRIORITY["other"])


# ── 分赛道投研资料展示 ─────────────────────────────────────────────
# 说明：投研页「资料完整性」清单按赛道只展示该赛道关心的资料类型，无关类型
# 隐藏，避免清单冗长。基础资料（官网/白皮书/GitHub/合约/链上/社交）所有赛道
# 均展示；主题类资料按 SECTOR_TOPIC_PRIORITY 命中显示。

SECTOR_BASE_MATERIAL_KEYS: tuple[str, ...] = (
    "official_website",       # 官网
    "whitepaper_docs",        # 白皮书 / 文档
    "github_repo",            # GitHub 仓库
    "contract_address",       # 合约地址
    "onchain_holder_data",    # 链上持仓数据
    "social_heat",            # 社交热度
)

# content_topic → 主题类资料类型 key（对齐 db_stats._MATERIAL_TOPIC_MAP）。
# 仅收录能在投研清单里对应到独立资料类型的主题。
_TOPIC_MATERIAL_KEYS: dict[str, str] = {
    "audit": "audit_report",
    "tokenomics": "tokenomics",
    "tge_ido": "tge_ido_info",
    "lp_liquidity": "lp_liquidity_info",
    "treasury_multisig": "treasury_multisig",
    "team_vc": "team_vc",
    "roadmap": "roadmap",
    "dao_governance": "dao_governance",
    "bug_bounty": "bug_bounty",
    "exchange_listing": "exchange_listing",
    "competitor": "competitor_material",
    "major_event": "major_event_announcement",
    "announcement": "major_event_announcement",
    "third_party_rating": "third_party_rating",
    "onchain_abnormal": "onchain_abnormal_event",
}


def get_sector_visible_material_keys(sector: str | None) -> set[str]:
    """返回某赛道在投研页应展示的资料类型 key 集合（对齐 db_stats.RESEARCH_MATERIAL_TYPES）。

    - 基础资料所有赛道展示；
    - 代币解锁数据除 Meme（无 vesting）外均展示；
    - 主题类资料按 SECTOR_TOPIC_PRIORITY 命中展示。
    """
    keys = set(SECTOR_BASE_MATERIAL_KEYS)
    keys.add("token_unlock_data")
    if sector == "meme":
        keys.discard("token_unlock_data")
    for topic in get_sector_topic_priorities(sector):
        mk = _TOPIC_MATERIAL_KEYS.get(topic)
        if mk:
            keys.add(mk)
    return keys


def topic_priority_rank(sector: str | None, topic: str) -> int:
    """返回 topic 在该赛道的优先级序号（0 = 最优先），未列出则排在默认兜底之后。

    用于缺失清单按赛道排序：重点主题的缺失排最前，引导优先补齐。
    """
    pri = get_sector_topic_priorities(sector)
    if topic in pri:
        return pri.index(topic)
    if topic in DEFAULT_TOPIC_PRIORITY:
        return len(pri) + DEFAULT_TOPIC_PRIORITY.index(topic)
    return len(pri) + len(DEFAULT_TOPIC_PRIORITY)


# ── 分赛道解锁预警 ───────────────────────────────────────────────
# 说明：不同赛道解锁对价格的影响不同，预警提前天数应分赛道调整。
#   - L2/AI：代币解锁密集且叙事敏感，提前 21 天预警（最早）。
#   - 多数赛道：默认提前 14 天。
#   - Meme：通常无 vesting/解锁，解锁预警意义低，改为监控大户转账
#     （预警天数设为 0，由监控脚本切换到大户转账告警）。

# 解锁预警默认提前天数（对齐 phase_watchlist_monitor.py 的 UNLOCK_ALERT_DAYS）
DEFAULT_UNLOCK_ALERT_DAYS = 14

SECTOR_UNLOCK_ALERT_DAYS: dict[str, int] = {
    "l2": 21,          # 二层代币解锁密集，提前预警
    "ai": 21,          # AI 叙事敏感，提前预警
    "meme": 0,         # Meme 无 vesting，改监控大户转账
}


def get_sector_unlock_alert_days(sector: str | None) -> int:
    """返回某赛道的解锁预警提前天数；0 表示不解锁预警（改走大户转账监控）。

    未配置的赛道回退默认 14 天。
    """
    if sector and sector in SECTOR_UNLOCK_ALERT_DAYS:
        return SECTOR_UNLOCK_ALERT_DAYS[sector]
    return DEFAULT_UNLOCK_ALERT_DAYS
