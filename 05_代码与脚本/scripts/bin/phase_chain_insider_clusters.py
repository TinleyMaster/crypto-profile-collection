#!/usr/bin/env python3
"""Solana insider 网络聚类扫描：遍历 core.asset_contract WHERE chain='solana' → RugCheck /report。

用法：
    python phase_chain_insider_clusters.py --limit 50
    python phase_chain_insider_clusters.py --limit 200  # 建议每批上限
    python phase_chain_insider_clusters.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.insider_cluster_client import InsiderClusterClient

UPSERT_SQL = """
INSERT INTO biz.asset_insider_clusters (
    asset_id, chain, mint,
    graph_insiders_detected, insider_network_count,
    top_network_size, top_network_active_accounts, top_network_token_amount,
    total_supply, total_holders,
    insider_dominance, insider_account_ratio,
    bundle_flag, risk_label,
    networks_json,
    source, source_status, raw_json, scanned_at
) VALUES (
    %(asset_id)s, %(chain)s, %(mint)s,
    %(graph_insiders_detected)s, %(insider_network_count)s,
    %(top_network_size)s, %(top_network_active_accounts)s, %(top_network_token_amount)s,
    %(total_supply)s, %(total_holders)s,
    %(insider_dominance)s, %(insider_account_ratio)s,
    %(bundle_flag)s, %(risk_label)s,
    %(networks_json)s::jsonb,
    %(source)s, %(source_status)s, %(raw_json)s::jsonb, NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    mint = EXCLUDED.mint,
    graph_insiders_detected = EXCLUDED.graph_insiders_detected,
    insider_network_count = EXCLUDED.insider_network_count,
    top_network_size = EXCLUDED.top_network_size,
    top_network_active_accounts = EXCLUDED.top_network_active_accounts,
    top_network_token_amount = EXCLUDED.top_network_token_amount,
    total_supply = EXCLUDED.total_supply,
    total_holders = EXCLUDED.total_holders,
    insider_dominance = EXCLUDED.insider_dominance,
    insider_account_ratio = EXCLUDED.insider_account_ratio,
    bundle_flag = EXCLUDED.bundle_flag,
    risk_label = EXCLUDED.risk_label,
    networks_json = EXCLUDED.networks_json,
    source = EXCLUDED.source,
    source_status = EXCLUDED.source_status,
    raw_json = EXCLUDED.raw_json,
    scanned_at = NOW()
"""

UPSERT_KEYS = {
    "asset_id", "chain", "mint",
    "graph_insiders_detected", "insider_network_count",
    "top_network_size", "top_network_active_accounts", "top_network_token_amount",
    "total_supply", "total_holders",
    "insider_dominance", "insider_account_ratio",
    "bundle_flag", "risk_label",
    "networks_json",
    "source", "source_status", "raw_json",
}


def _load_yaml() -> dict:
    try:
        import yaml
        rules_path = Path(__file__).resolve().parents[2] / "workbench" / "market_rules.yaml"
        if rules_path.exists():
            with open(rules_path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _classify(result: dict, yaml_rules: dict) -> str:
    rules = yaml_rules.get("insider_cluster", {})
    dominance_high = rules.get("dominance_high", 0.20)
    dominance_medium = rules.get("dominance_medium", 0.10)
    acct_ratio_high = rules.get("account_ratio_high", 0.30)
    acct_ratio_medium = rules.get("account_ratio_medium", 0.15)

    dom = result.get("insider_dominance")
    ratio = result.get("insider_account_ratio")

    if dom is None and ratio is None:
        return "clean"

    high_signal = (dom is not None and dom >= dominance_high) or (ratio is not None and ratio >= acct_ratio_high)
    med_signal = (dom is not None and dom >= dominance_medium) or (ratio is not None and ratio >= acct_ratio_medium)

    if high_signal:
        return "high"
    if med_signal:
        return "medium"
    return "low"


def main() -> int:
    parser = argparse.ArgumentParser(description="Solana insider 网络聚类扫描")
    parser.add_argument("--limit", type=int, default=50, help="扫描资产数量上限（建议≤200）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    client = InsiderClusterClient()
    yaml_rules = _load_yaml()

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (ac.asset_id)
                    ac.asset_id, ac.chain, ac.contract_address,
                    a.total_supply
                FROM core.asset_contract ac
                JOIN core.asset a ON a.asset_id = ac.asset_id
                WHERE ac.chain IN ('solana', 'sol')
                  AND ac.contract_address IS NOT NULL
                  AND LENGTH(ac.contract_address) > 10
                ORDER BY ac.asset_id, ac.contract_address
                LIMIT %s
                """,
                (args.limit,),
            )
            assets = cur.fetchall()

        print(f"待扫描 Solana 资产: {len(assets)}")
        if not assets:
            print("无资产可扫描")
            return 0

        hit = 0
        miss = 0
        t0 = time.time()

        for i, row in enumerate(assets, 1):
            aid = row[0]
            addr = row[2]
            total_supply = row[3]

            print(f"  [{i}/{len(assets)}] asset_id={aid} {addr[:12]}... ", end="", flush=True)

            result = client.fetch(aid, addr, total_supply=float(total_supply) if total_supply else None)

            result["risk_label"] = _classify(result, yaml_rules)

            if result.get("networks_json") and isinstance(result["networks_json"], str):
                result["networks_json"] = result["networks_json"]
            if result.get("raw_json") and isinstance(result["raw_json"], dict):
                result["raw_json"] = json.dumps(result["raw_json"], ensure_ascii=False, default=str)

            status = result.get("source_status", "?")
            if status == "hit":
                print(f"✅ {result.get('risk_label')} (insiders={result.get('graph_insiders_detected')}, dominance={result.get('insider_dominance')})")
            else:
                print(f"❌ {status}")

            missing = UPSERT_KEYS - set(result.keys())
            if missing:
                print(f"  ERROR 缺失占位符 {missing}", file=sys.stderr)
                miss += 1
                continue

            if args.dry_run:
                hit += 1
                continue

            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, result)
            conn.commit()
            hit += 1

            if i < len(assets):
                time.sleep(1.0)

            if i % 20 == 0:
                elapsed = time.time() - t0
                print(f"  -- 进度 {i}/{len(assets)}, hit={hit}, miss={miss} --")

        elapsed = time.time() - t0
        print(f"\n{'=' * 60}")
        print(f"扫描完成: hit={hit}, miss={miss}, 耗时 {elapsed:.1f}s")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
