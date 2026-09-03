"""Solana 钱包聚类：RugCheck 完整报告 /report 取 insiderNetworks。"""
from __future__ import annotations

import json
import time
from typing import Optional

import requests

TIMEOUT = 30


class InsiderClusterClient:
    BASE = "https://api.rugcheck.xyz/v1/tokens"

    def fetch(
        self,
        asset_id: int,
        mint: str,
        total_supply: Optional[float] = None,
    ) -> dict:
        url = f"{self.BASE}/{mint}/report"
        for attempt in range(3):
            try:
                r = requests.get(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "MemeResearch/1.0"},
                    timeout=TIMEOUT,
                )
                if r.status_code == 429:
                    wait = (2 ** attempt) + 1
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    return {
                        "asset_id": asset_id,
                        "chain": "solana",
                        "source": "rugcheck_report",
                        "source_status": "error",
                    }
                d = r.json()
                return self._parse(asset_id, mint, d, total_supply)
            except Exception:
                time.sleep(2 ** attempt)
                continue
        return {
            "asset_id": asset_id,
            "chain": "solana",
            "source": "rugcheck_report",
            "source_status": "error",
        }

    def _parse(self, asset_id: int, mint: str, d: dict, total_supply: Optional[float]) -> dict:
        import json as _json

        ins = d.get("insiderNetworks") or []
        rug_supply = d.get("token", {}).get("supply")
        holders = d.get("totalHolders") or 0
        g = d.get("graphInsidersDetected") or 0

        top = max(ins, key=lambda x: float(x.get("tokenAmount") or 0), default={})
        top_amt = float(top.get("tokenAmount") or 0)
        dominance = round(top_amt / float(rug_supply), 4) if rug_supply else None
        acct_ratio = round(g / holders, 4) if holders else None

        bundle = any(
            x.get("size", 0) <= 10
            and float(x.get("tokenAmount") or 0) / top_amt > 0.05
            for x in ins
            if top_amt > 0
        )

        return {
            "asset_id": asset_id,
            "chain": "solana",
            "mint": mint,
            "graph_insiders_detected": g,
            "insider_network_count": len(ins),
            "top_network_size": top.get("size"),
            "top_network_active_accounts": top.get("activeAccounts"),
            "top_network_token_amount": top_amt,
            "total_supply": rug_supply if rug_supply else (total_supply or None),
            "total_holders": holders,
            "insider_dominance": dominance,
            "insider_account_ratio": acct_ratio,
            "bundle_flag": bundle,
            "risk_label": "pending",
            "networks_json": _json.dumps(ins[:5], ensure_ascii=False),
            "source": "rugcheck_report",
            "source_status": "hit",
            "raw_json": {
                "graphInsidersDetected": g,
                "insiderNetworks": ins[:5],
                "totalHolders": holders,
                "total_supply": rug_supply,
                "rugged": d.get("rugged"),
            },
        }
