#!/usr/bin/env python3
"""种子 biz.asset_issuer（代币→发行方/公司映射，关系图谱第四维「同一家公司」）。

人工梳理的发行方归属（多为可核实事实，不适合从公开 API 自动抓），可重复执行 upsert：

    python seed_asset_issuer.py               # 全量 upsert（source='manual'）
    python seed_asset_issuer.py --dry-run     # 只打印将写入 / 无法解析的符号
    python seed_asset_issuer.py --clear       # 先清空 source='manual' 的记录再写入

说明：
  - 按 core.asset.canonical_symbol 精确解析 asset_id（解析失败只警告，不中断）。
  - 稳定币也做映射（Circle/Tether/MakerDAO…），但分析宇宙会排除稳定币，
    其分组在相关度/回测中自动为空，不影响结论。
  - token_relation_graph.prepare 读本表作为 'issuer' 维度；通过稳定性回测的
    issuer 组随 refresh_stable_groups 写入 biz.stable_linkage_group（dim='issuer'），
    进入扫描归因「同稳定组可能联动」候选。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PROJECT_SRC = _HERE.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# 人工梳理：symbol → 发行方/公司（规范化名称，作为关系分组名）。
# 只收录可核实的归属；单成员组会被 min_members 过滤自然剔除。
CURATED: dict[str, str] = {
    # ── 稳定币发行方（分析宇宙排除稳定币，映射仅为完整性）──
    "USDC": "Circle", "USD1": "Circle", "USDP": "Circle",
    "USDT": "Tether", "XAUt": "Tether", "EURT": "Tether", "CNHT": "Tether",
    "DAI": "MakerDAO", "USDS": "MakerDAO", "MKR": "MakerDAO", "SKY": "MakerDAO",
    "FRAX": "Frax Finance", "FXS": "Frax Finance", "frxETH": "Frax Finance",
    "ENA": "Ethena Labs", "USDe": "Ethena Labs", "sUSDe": "Ethena Labs",
    "USDY": "Ondo", "ONDO": "Ondo",
    "PYUSD": "Paxos", "PAXG": "Paxos",
    "GHO": "Aave", "AAVE": "Aave",
    "RLUSD": "Ripple", "XRP": "Ripple",
    # ── 多币种发行方/生态主体（≥2 成员才有分组意义）──
    "LDO": "Lido", "wstETH": "Lido", "stETH": "Lido",
    "JTO": "Jito", "JITOSOL": "Jito",
    "MNDE": "Marinade", "MSOL": "Marinade",
    "TRX": "Tron Foundation", "SUN": "Tron Foundation", "JST": "Tron Foundation",
    "THETA": "Theta Labs", "TFUEL": "Theta Labs",
    "VET": "VeChain Foundation", "VTHO": "VeChain Foundation",
    "POL": "Polygon Labs", "MATIC": "Polygon Labs",
    "RNDR": "Render Network", "RENDER": "Render Network",
    # ── 单币发行方（归属事实，单成员组会被过滤）──
    "BNB": "Binance",
    "CRV": "Curve Finance",
    "UNI": "Uniswap",
    "PENDLE": "Pendle",
    "SNX": "Synthetix",
    "MORPHO": "Morpho",
    "ALGO": "Algorand Foundation",
    "NEAR": "NEAR Foundation",
    "APT": "Aptos Labs",
    "SUI": "Sui Foundation",
    "HBAR": "Hedera",
    "ADA": "Cardano Foundation",
    "ICP": "DFINITY",
    "FIL": "Filecoin Foundation",
    "AR": "Arweave",
    "IMX": "Immutable",
    "SAND": "The Sandbox",
    "APE": "Yuga Labs",
    "GALA": "Gala Games",
    "TAO": "Bittensor",
    "FET": "Fetch.ai Foundation",
    "GRT": "The Graph",
    "LINK": "Chainlink Labs",
    "AVAX": "Avalanche Foundation",
    "DOT": "Web3 Foundation",
    "ATOM": "Interchain Foundation",
    "SOL": "Solana Foundation",
    "ETH": "Ethereum Foundation",
    "INJ": "Injective Labs",
    "SEI": "Sei Foundation",
    "TIA": "Celestia Labs",
    "OP": "Optimism Foundation",
    "ARB": "Arbitrum Foundation",
}


def _load_sym2aid(conn) -> dict[str, int]:
    """canonical_symbol → asset_id（全量拉取建索引，避免逐条查询）。

    按 market_cap_rank 定序防张冠李戴：同一 canonical 有多条记录（如 TRX 有
    4 条，含 rank=None 的重复/下架资产）时，取市值排名最高者——与
    catalyst_attribution._resolve_asset_id 同款消歧，保证与宇宙解析一致。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT canonical_symbol, asset_id FROM core.asset "
                    "ORDER BY market_cap_rank NULLS LAST, asset_id")
        out: dict[str, int] = {}
        for sym, aid in cur.fetchall():
            if sym and aid is not None and sym not in out:
                out[sym] = aid
        return out


def run(conn, dry_run: bool, clear: bool) -> int:
    sym2aid = _load_sym2aid(conn)
    rows: list[tuple[int, str, str]] = []  # (asset_id, symbol, issuer)
    unresolved: list[str] = []
    for sym, issuer in CURATED.items():
        aid = sym2aid.get(sym)
        if aid is None:
            unresolved.append(sym)
            continue
        rows.append((aid, sym, issuer))
    rows.sort(key=lambda r: r[1])
    print(f"[seed_asset_issuer] 可解析 {len(rows)}/{len(CURATED)}"
          f"（未解析 {len(unresolved)}: {', '.join(unresolved) or '-'}）")
    if dry_run:
        for aid, sym, issuer in rows:
            print(f"  {sym:<8} -> {issuer}")
        return 0
    with conn.cursor() as cur:
        if clear:
            cur.execute("DELETE FROM biz.asset_issuer WHERE source = 'manual'")
            print(f"[seed_asset_issuer] 已清空 {cur.rowcount} 条 manual 记录")
        cur.executemany(
            """
            INSERT INTO biz.asset_issuer (asset_id, issuer, source)
            VALUES (%s, %s, 'manual')
            ON CONFLICT (asset_id) DO UPDATE
              SET issuer = EXCLUDED.issuer, source = 'manual', updated_at = NOW()
            """,
            [(aid, issuer) for aid, _, issuer in rows])
    print(f"[seed_asset_issuer] 已写入/更新 {len(rows)} 条（source=manual）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="种子 biz.asset_issuer（代币→发行方映射）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不落库")
    ap.add_argument("--clear", action="store_true", help="先清空 manual 记录再写入")
    args = ap.parse_args()
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    with get_connection(get_settings(require_database=True).database_url) as conn:
        return run(conn, args.dry_run, args.clear)


if __name__ == "__main__":
    sys.exit(main())
