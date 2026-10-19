#!/usr/bin/env python3
"""
代币化股票「识别 + 分离存储」回归测试（离线，不连库）。

覆盖：
  A. 标题侧命中集合与旧 `_NON_CRYPTO_RE` **逐条等价**（放宽/收紧都算回归）
  B. 分类标签正确（bstock / xstock / tradfi / equity_bundle …）
  C. 真加密公告不被误杀
  D. `build_item(sink=…)` 只把代币化股票条目送进分离道，加密条目照常返回
  E. 资产侧强/弱特征分层 —— 分离存储用强特征，`SynFutures` 这类加密项目必须排除
  F. SQL 常量锁定（防三处常量各自漂移）

用法：
    python tests/test_tokenized_split.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workbench"))
sys.path.insert(0, str(ROOT / "src"))

from catalyst.tokenized import (                       # noqa: E402
    STRONG_ASSET_NAME_SQL, TOKENIZED_ASSET_NAME_SQL,
    classify_asset_name, classify_title, is_tokenized_asset,
)

# ── 旧实现（改造前 `_exchange_announce._NON_CRYPTO_RE` + `_EQ_BUNDLE_RE`）──
_OLD_TITLE_RE = re.compile(
    r"tradfi|tokeni[sz]ed\s+stocks?|pre[-\s]?ipo|stock\s+perps?"
    r"|xstocks?|bstocks?|robinhood\s+token|backpack\s+securities"
    r"|commodit|forex|cfds?\b|metals?\b|crude\s+oil|brent\b|natural\s+gas"
    r"|nasdaq|nyse|\bnyse\b|\betf\s+share"
    r"|equity|equities",
    re.IGNORECASE,
)
_OLD_BUNDLE_RE = re.compile(r"\bfor\s+[A-Z0-9]{2,6}(?:\s*,\s*[A-Z0-9]{2,6}){2,}")


def _old_is_non_crypto(title: str) -> bool:
    return bool(_OLD_TITLE_RE.search(title) or _OLD_BUNDLE_RE.search(title))


# ── 语料：线上真实标题（含正负样本）──────────────────────────────────
REAL_TITLES: list[str] = [
    # 应剔除（代币化股票 / TradFi）
    "OKX to list TradFi perpetual futures for TSLA and NVDA",
    "Unified Tokenized Stocks xSHLD, xSPY, xQQQ are now available",
    "Bybit to delist SNPSUSDT on Bybit TradFi Perpetual",
    "Bitget lists FASTRETAILINGUSDT hot stock perps",
    "JIO (Jio Platforms) Pre-IPO is coming to Bitget",
    "OKX will list perpetual futures for H100, ACN, NKE, BWET and SECZ",
    "Binance Futures launches USDⓈ-M NVDAXUSDT Perpetual with up to 20x",
    "Robinhood Token NVIDIA now tradable on OKX",
    "SpaceX (Backpack Securities) pre-market listing",
    "Dinari Tokenized Stock AAPL goes live",
    "Bitget adds crude oil and brent futures to TradFi zone",
    "OKX lists XAUUSD metals perpetual",
    "Bybit lists TQQQ Equity X-Perps",
    "Binance to list NASDAQ index perpetual",
    "New ETF share products available on Bybit",
    # 不应剔除（真加密公告）
    "Binance Will List ZAMA (ZAMA) with Seed Tag Applied",
    "NeoSoul (DNA) is now available in the AI zone",
    "OKX to list AXSUSD perpetual futures",
    "Bybit lists SOLUSDT perpetual",
    "Bitget lists CTUSDT spot trading pair",
    "Binance Will Delist DORA, ICX, STORJ, ZEUS and ELF",
    "Coinbase adds support for ARB token",
    "Aerodrome Finance (AERO) launches on Base",
    "Bitcoin breaks above 100,000 as ETF inflows accelerate",
    "Ethereum Foundation announces Pectra upgrade date",
    "Token2049 Singapore tickets now on sale",
    "Solana ecosystem TVL hits new high",
]

CASES: list[tuple[str, bool, str]] = []


def check(name: str, got, want, note: str = "") -> None:
    CASES.append((name, got == want, f"got={got!r} want={want!r} {note}".strip()))


def main() -> int:
    # ── A. 标题侧等价 ───────────────────────────────────────────────
    mismatch = []
    for t in REAL_TITLES:
        old = _old_is_non_crypto(t)
        new = classify_title(t) is not None
        if old != new:
            mismatch.append((t, old, new))
    check("A1 标题侧命中集合与旧实现等价", mismatch, [])

    # ── B. 分类标签 ─────────────────────────────────────────────────
    kind_cases = [
        ("Bybit to delist SNPSUSDT on Bybit TradFi Perpetual", "tradfi"),
        ("Unified Tokenized Stocks xSHLD is live", "tokenized_stock"),
        ("Robinhood Token NVIDIA now tradable", "robinhood_token"),
        ("SpaceX (Backpack Securities) listing", "backpack_securities"),
        ("JIO Pre-IPO is coming", "pre_ipo"),
        ("OKX to list perpetual futures for H100, ACN, NKE and SECZ", "equity_bundle"),
        ("Bybit lists TQQQ Equity X-Perps", "equity_perp"),
        ("Bitget adds brent futures", "commodity"),
        ("OKX lists XAUUSD metals perpetual", "metals"),
    ]
    for title, want in kind_cases:
        got = (classify_title(title) or (None,))[0]
        check(f"B[{want}] {title[:44]}", got, want)

    # ── C. 真加密公告不误杀 ─────────────────────────────────────────
    for t in [
        "Binance Will List ZAMA (ZAMA) with Seed Tag Applied",
        "NeoSoul (DNA) is now available in the AI zone",
        "OKX to list AXSUSD perpetual futures",
        "Bitget lists CTUSDT spot trading pair",
        "Binance Will Delist DORA, ICX, STORJ, ZEUS and ELF",
        "Aerodrome Finance (AERO) launches on Base",
    ]:
        check(f"C 不误杀 {t[:40]}", classify_title(t), None)

    # ── D. build_item 分流 ──────────────────────────────────────────
    from catalyst.sources._exchange_announce import build_item

    sink: list[dict] = []
    got = build_item(
        source_code="okx_announcement", item_id="tradfi-1",
        title="Bybit to delist SNPSUSDT on Bybit TradFi Perpetual",
        published_ms=1750000000000, sink=sink,
    )
    check("D1 代币化股票条目不进加密库", got, None)
    check("D2 该条目进了分离道", len(sink), 1)
    check("D3 分离道带分类标签", sink[0]["asset_class"] if sink else None, "tradfi")
    check("D4 分离道带提取标的", sink[0]["symbols"] if sink else None, ["SNPS"])
    check("D5 stage 标记为 source_filter", sink[0]["stage"] if sink else None, "source_filter")

    sink2: list[dict] = []
    got2 = build_item(
        source_code="okx_announcement", item_id="crypto-1",
        title="OKX to list ZAMAUSDT perpetual", published_ms=1750000000000, sink=sink2,
    )
    check("D6 真加密条目照常返回", bool(got2), True)
    check("D7 真加密条目不进分离道", sink2, [])

    # 营销活动 / 批量下架：不记录（无投研价值）
    sink3: list[dict] = []
    build_item(source_code="bybit_announcement", item_id="promo-1",
               title="KII Alpha Token Splash— Grab a share of 50,000 USDT",
               published_ms=1750000000000, sink=sink3)
    build_item(source_code="bybit_announcement", item_id="bulk-1",
               title="Bybit to Delist 8 Trading Pairs", published_ms=1750000000000, sink=sink3)
    check("D8 营销/批量下架不进分离道（避免噪音）", len(sink3), 0)

    # ── E. 资产侧强/弱特征分层 ──────────────────────────────────────
    strong_cases = ["NVIDIA Tokenized bStocks", "Robinhood Tokenized bStocks",
                    "Apple tokenized stock (xStock)", "PreStocks SpaceX",
                    "Crude Oil Futures (Derivatives)", "Brent Oil (Derivatives)",
                    "SpaceX (Backpack Securities)", "AAPL (Dinari Tokenized Stock)"]
    for n in strong_cases:
        check(f"E1 强特征 {n[:34]}",
              classify_asset_name(n, strong_only=True) is not None, True)

    # ⚠️ 弱特征：过滤仍然命中（行为不变），但不许进分离表
    for n in ["SynFutures", "BitDerivatives"]:
        check(f"E2 弱特征仍被过滤 {n}",
              is_tokenized_asset(n), True)
        check(f"E3 弱特征不进分离表 {n}",
              classify_asset_name(n, strong_only=True), None)

    for n in ["Bitcoin", "Ethereum", "Solana", "Aerodrome Finance", "Polymesh"]:
        check(f"E4 加密资产不误判 {n}", is_tokenized_asset(n), False)

    # ── F. SQL 常量锁定 ─────────────────────────────────────────────
    check(
        "F1 过滤用 SQL 常量未漂移（改动须同步 linker.py / db.py / asset_filter.py）",
        TOKENIZED_ASSET_NAME_SQL,
        r"tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ"
        r"|crude[[:space:]]+oil|brent"
        r"|robinhood[[:space:]]+token|backpack[[:space:]]+securities"
        r"|\mdinari\M|\mxstock\M",
    )
    check("F2 分离用 SQL 不含独立裸 futures 分支",
          bool(re.search(r"(?:^|\|)futures(?:$|\|)", STRONG_ASSET_NAME_SQL)), False)
    check("F2b 分离用 SQL 不含独立裸 derivativ 分支",
          bool(re.search(r"(?:^|\|)derivativ(?:$|\|)", STRONG_ASSET_NAME_SQL)), False)
    check("F3 分离用 SQL 保留 bstocks/xstock/dinari 强特征",
          all(k in STRONG_ASSET_NAME_SQL for k in ("stocks", "xstock", "dinari")), True)

    # ── 汇总 ────────────────────────────────────────────────────────
    fails = [c for c in CASES if not c[1]]
    for name, ok, note in CASES:
        if not ok:
            print(f"  ❌ {name}  {note}")
    print(f"\n{'=' * 62}")
    print(f"用例 {len(CASES)} 条，通过 {len(CASES) - len(fails)}，失败 {len(fails)}")
    print("=" * 62)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
