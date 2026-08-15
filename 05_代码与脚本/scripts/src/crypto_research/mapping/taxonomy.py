"""统一投研链接分类 taxonomy。

单一数据源：把「来源类型」（source_type）与「内容主题」（content_topics）
两个正交维度集中在此定义，供所有链接分类逻辑（CMC/CG/DexScreener/NotebookLM）
统一引用，消除历史上分散在 infer_entry_type / infer_doc_type / _classify_url
三处规则不一致的问题。
"""

from __future__ import annotations

# ── 来源类型（对齐 biz.doc_source_entry.entry_type）──────────────────────────
# 说明：历史上代码里大量使用 whitepaper_page，但数据库 CHECK 约束漏掉了它，
# 导致该值从未真正落库。这里把它纳入标准来源类型，配套迁移会同步修复 CHECK。
SOURCE_TYPES = (
    "official_website",
    "docs",
    "docs_portal",
    "whitepaper_page",
    "github",
    "medium",
    "announcement",
    "twitter",
    "telegram",
    "reddit",
    "facebook",
    "other",
)

# ── 内容主题（文档可承载的主题，对齐 21 类投研清单中的「链接类」维度）──────
# 含现有 biz.doc_asset.doc_type 的取值，保证向后兼容。
CONTENT_TOPICS = (
    "whitepaper",
    "docs",
    "audit",
    "deck",
    "tokenomics",
    "research",
    "announcement",
    "roadmap",
    "tge_ido",
    "lp_liquidity",
    "treasury_multisig",
    "team_vc",
    "dao_governance",
    "bug_bounty",
    "exchange_listing",
    "competitor",
    "major_event",
    "third_party_rating",
    "onchain_abnormal",
    "other",
)

CONTENT_TOPIC_LABELS = {
    "whitepaper": "白皮书",
    "docs": "技术文档",
    "audit": "审计报告",
    "deck": "项目 Deck",
    "tokenomics": "代币经济学",
    "research": "研究报告",
    "announcement": "公告",
    "roadmap": "路线图",
    "tge_ido": "TGE / IDO",
    "lp_liquidity": "LP 流动性",
    "treasury_multisig": "国库 / 多签",
    "team_vc": "团队 / VC",
    "dao_governance": "治理 DAO",
    "bug_bounty": "漏洞赏金",
    "exchange_listing": "交易所上线",
    "competitor": "竞品对比",
    "major_event": "重大公告 / 事件",
    "third_party_rating": "第三方评级",
    "onchain_abnormal": "链上异常事件",
    "other": "其他",
}

# 内容主题关键词规则：单词关键词直接子串匹配 URL/标签；
# 含空格的多词关键词在「归一化（-、_ 转空格）」后的文本上匹配。
CONTENT_TOPIC_KEYWORDS = {
    "whitepaper": ("whitepaper", "white paper", "litepaper", "lite paper"),
    "audit": ("audit", "security review", "certik", "hacken", "slowmist", "peckshield", "quantstamp", "trail of bits"),
    "tokenomics": ("tokenomics", "token economy", "token distribution", "token allocation", "vesting"),
    "deck": ("pitch deck", "deck"),
    "research": ("research", "analysis"),
    "roadmap": ("roadmap", "milestone", "q1 20", "q2 20", "q3 20", "q4 20"),
    "tge_ido": ("tge", "ido", "ieo", "presale", "public sale", "private sale", "launchpad", "token generation", "fair launch"),
    "lp_liquidity": ("liquidity", "amm", "pool", "lp lock", "locked liquidity", "trading pair"),
    "treasury_multisig": ("treasury", "multisig", "multi-sig", "gnosis", "safe.global", "vault", "dao treasury"),
    "team_vc": ("founder", "core team", "advisor", "investor", "venture", "funding", "seed round", "series a", "series b", "backed by", "financing"),
    "dao_governance": ("dao", "governance", "snapshot", "tally", "proposal", "voting", "vote"),
    "bug_bounty": ("bug bounty", "bounty", "immunefi", "hackerone", "disclosure", "cve-", "responsible disclosure"),
    "exchange_listing": ("listing", "listed on", "dexscreener", "trading pair", "trading pairs", "market listing"),
    "competitor": ("competitor", "comparison", " vs ", "benchmark", "peer review"),
    "major_event": ("announcement", "migration", "migrate", "upgrade", "rebrand", "airdrop", "mainnet launch"),
    "third_party_rating": ("defillama", "tokenomist", "cryptorank", "messari", "dappradar", "nansen", "glassnode"),
    "onchain_abnormal": ("hack", "exploit", "attack", "breach", "rug pull", "anomaly", "abnormal", "incident", "flash loan"),
}

# 域名 → 来源类型（跨数据源稳定，优先级最高）。
DOMAIN_SOURCE_TYPES = {
    "github.com": "github",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "t.me": "telegram",
    "medium.com": "medium",
    "reddit.com": "reddit",
    "facebook.com": "facebook",
    "discord.com": "other",
    "discord.gg": "other",
}
