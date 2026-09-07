#!/usr/bin/env python3
"""
Ingest DeFiLlama chain TVL daily snapshots.

Populates:
  - src_dl.chain_tvl_snapshot  (daily TVL + flow per chain)

Data sources:
  - /v2/chains                    → latest TVL, 1d/7d changes per chain (one call)
  - /v2/historicalChainTvl/{name} → daily historical TVL per chain (one per chain)

Usage:
    python ingest_llama_chain_tvl.py
    python ingest_llama_chain_tvl.py --top-n 30
    python ingest_llama_chain_tvl.py --chain Ethereum
    python ingest_llama_chain_tvl.py --dry-run

Notes:
  - /v2/chains gives ~200 chains in one request; use as the authoritative list.
  - /v2/historicalChainTvl returns all daily data points for a chain.
    We ingest only the latest point (today) and let backfill be a separate concern.
  - For flow calculations, we compare latest TVL against the point ~1d / ~7d / ~30d ago
    from the historical series (more accurate than DeFiLlama's change_1d/7d fields
    which are computed at different times of day).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

CHAIN_INTER_REQUEST_SLEEP = 1.0  # 链之间限速（秒），DeFiLlama 免费版较宽松
SKIP_WINDOW_HOURS = 18           # 续传窗口：18 小时内已成功入的链跳过

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest DeFiLlama chain TVL snapshots into src_dl.chain_tvl_snapshot."
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=50,
        help="Number of top chains (by TVL) to fetch historical TVL for. Default: 50",
    )
    parser.add_argument(
        "--chain",
        type=str,
        default=None,
        help="Only ingest a single chain by name (case-sensitive, uses DeFiLlama chain name).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    return parser


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_recently_ingested(conn, chain_key: str, window_hours: int = 24) -> bool:
    """近 window_hours 内已成功入库该链则跳过。"""
    from crypto_research.db.upsert import fetch_one

    row = fetch_one(
        conn,
        """
        SELECT 1 FROM sys.ingest_run
        WHERE endpoint_code = 'llama_chain_hist_tvl'
          AND status = 'success'
          AND finished_at >= NOW() - (%s::int || ' hours')::interval
          AND request_params->>'chain_key' = %s
        LIMIT 1
        """,
        (window_hours, chain_key),
    )
    return bool(row)


def load_sql(relative_path: str) -> str:
    from crypto_research.db.upsert import load_sql as _load_sql
    return _load_sql(relative_path)


def _record_run(
    conn,
    endpoint_code: str,
    request_params: dict,
    request_url: str,
    payload: Any,
    fetched_at: str,
    source_code: str = "defillama",
) -> tuple[int, int]:
    from crypto_research.db.upsert import fetch_one
    from crypto_research.utils.hash_utils import md5_text
    from crypto_research.utils.json_utils import stable_json_dumps

    payload_text = stable_json_dumps(payload)
    payload_hash = md5_text(payload_text)

    run_row = fetch_one(
        conn,
        load_sql("sys/insert_ingest_run.sql"),
        (
            source_code,
            endpoint_code,
            "WF_LLAMA_CHAIN_TVL",
            json.dumps(request_params, ensure_ascii=False),
            request_url,
        ),
    )
    run_id = run_row["run_id"]

    raw_row = fetch_one(
        conn,
        load_sql("raw/insert_api_response.sql"),
        (
            run_id,
            source_code,
            endpoint_code,
            json.dumps(request_params, ensure_ascii=False, sort_keys=True),
            None,
            "page:single",
            payload_text,
            payload_hash,
            fetched_at,
        ),
    )
    return raw_row["response_id"], run_id


def _finish_run(conn, run_id: int, status: str, rows: int, error: str | None = None) -> None:
    from crypto_research.db.upsert import fetch_one

    fetch_one(
        conn,
        load_sql("sys/finish_ingest_run.sql"),
        (
            status,
            200 if status == "success" else None,
            rows,
            rows,
            0,
            error,
            run_id,
        ),
    )


def _parse_chains_list(payload: list[dict]) -> list[dict]:
    """解析 /v2/chains 返回的链列表。"""
    rows: list[dict] = []
    for item in payload or []:
        name = item.get("name") or ""
        if not name:
            continue
        rows.append({
            "chain_key": name.lower(),
            "chain_name": name,
            "tvl_usd": _safe_float(item.get("tvl")),
            "tvl_change_1d": _safe_float(item.get("change_1d")),
            "tvl_change_7d": _safe_float(item.get("change_7d")),
            "tvl_change_30d": _safe_float(item.get("change_1m")),
            "symbol": item.get("symbol"),
            "gecko_id": item.get("gecko_id"),
            "cmc_id": str(item.get("cmcId")) if item.get("cmcId") else None,
        })
    # 按 TVL 降序
    rows.sort(key=lambda x: -(x["tvl_usd"] or 0))
    return rows


def _compute_flows_from_history(history: list[dict]) -> dict:
    """从历史 TVL 序列中计算 1d/7d/30d 净流入。

    history: [{date: unix_ts, tvl: float}, ...]
    Returns {tvl_usd, tvl_prev_1d, tvl_prev_7d, tvl_prev_30d,
              flow_1d_usd, flow_7d_usd, flow_30d_usd,
              tvl_change_1d, tvl_change_7d, tvl_change_30d, snapshot_date}
    """
    if not history:
        return {}

    pts = [p for p in history if isinstance(p, dict) and p.get("tvl") is not None]
    if not pts:
        return {}

    # 找到最新数据点
    latest_ts = max(p["date"] for p in pts)
    latest = next(p for p in pts if p["date"] == latest_ts)
    tvl_now = _safe_float(latest["tvl"]) or 0.0
    snap_date = date.fromtimestamp(latest_ts)

    def find_prev(days: int) -> float | None:
        target = latest_ts - days * 86400
        # 找 <= target 的最近一个点
        cands = [p for p in pts if p["date"] <= target]
        if not cands:
            # 没有足够历史，兜底用最早的点
            cands = [min(pts, key=lambda p: p["date"])]
        prev = min(cands, key=lambda p: target - p["date"])
        return _safe_float(prev.get("tvl"))

    prev_1d = find_prev(1)
    prev_7d = find_prev(7)
    prev_30d = find_prev(30)

    def flow_and_pct(prev: float | None) -> tuple[float | None, float | None]:
        if prev is None or prev <= 0:
            return None, None
        f = tvl_now - prev
        pct = (f / prev) * 100
        return f, pct

    f1d, p1d = flow_and_pct(prev_1d)
    f7d, p7d = flow_and_pct(prev_7d)
    f30d, p30d = flow_and_pct(prev_30d)

    return {
        "tvl_usd": tvl_now,
        "tvl_prev_1d": prev_1d,
        "tvl_prev_7d": prev_7d,
        "tvl_prev_30d": prev_30d,
        "flow_1d_usd": f1d,
        "flow_7d_usd": f7d,
        "flow_30d_usd": f30d,
        "tvl_change_1d": p1d,
        "tvl_change_7d": p7d,
        "tvl_change_30d": p30d,
        "snapshot_date": snap_date,
    }


def ingest_chain_list(client, conn, dry_run: bool) -> tuple[list[dict], int | None]:
    """拉取全量链列表（/v2/chains），返回解析后的列表。"""
    fetched_at = datetime.now(timezone.utc).isoformat()
    payload = client.get_chains()
    chains = _parse_chains_list(payload)

    if dry_run:
        print(json.dumps({
            "mode": "dry-run",
            "endpoint": "chains",
            "chain_count": len(chains),
            "top_5": [
                {"name": c["chain_name"], "tvl": c["tvl_usd"], "change_7d": c["tvl_change_7d"]}
                for c in chains[:5]
            ],
        }, ensure_ascii=False, indent=2))
        return chains, None

    response_id, run_id = _record_run(
        conn,
        "llama_chains",
        {"source": "defillama", "endpoint": "/v2/chains"},
        f"{client.settings.defillama_base_url}/v2/chains",
        payload,
        fetched_at,
    )
    _finish_run(conn, run_id, "success", len(chains))
    return chains, response_id


def ingest_single_chain_hist(
    client,
    conn,
    chain_name: str,
    chain_key: str,
    dry_run: bool,
) -> tuple[dict, int | None]:
    """拉取单链历史 TVL 并计算净流入。"""
    fetched_at = datetime.now(timezone.utc).isoformat()
    history = client.get_chain_historical_tvl(chain_name)
    flows = _compute_flows_from_history(history)

    if not flows:
        return {}, None

    if dry_run:
        return flows, None

    response_id, run_id = _record_run(
        conn,
        "llama_chain_hist_tvl",
        {"chain_name": chain_name, "chain_key": chain_key},
        f"{client.settings.defillama_base_url}/v2/historicalChainTvl/{chain_name}",
        history,
        fetched_at,
    )

    snap_date = flows["snapshot_date"]
    upsert_sql = load_sql("src_dl/upsert_chain_tvl_snapshot.sql")
    from crypto_research.db.upsert import execute_many

    try:
        execute_many(conn, upsert_sql, [(
            chain_key,
            chain_name,
            snap_date,
            flows.get("tvl_usd"),
            flows.get("tvl_change_1d"),
            flows.get("tvl_change_7d"),
            flows.get("tvl_change_30d"),
            flows.get("flow_1d_usd"),
            flows.get("flow_7d_usd"),
            flows.get("flow_30d_usd"),
            response_id,
            fetched_at,
        )])
    except Exception as exc:
        _finish_run(conn, run_id, "failed", 0, str(exc))
        raise
    _finish_run(conn, run_id, "success", 1)
    return flows, response_id


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.defillama_client import DefiLlamaClient
    from crypto_research.config import get_settings

    settings = get_settings(require_database=not args.dry_run)
    client = DefiLlamaClient(settings)

    if args.dry_run:
        chains, _ = ingest_chain_list(client, None, dry_run=True)
        if args.chain:
            target = args.chain
        elif chains:
            target = chains[0]["chain_name"]
        else:
            print("No chains available")
            return 0

        print(f"\n--- Historical TVL for: {target} ---")
        flows, _ = ingest_single_chain_hist(client, None, target, target.lower(), dry_run=True)
        print(json.dumps(flows, ensure_ascii=False, indent=2, default=str))
        return 0

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    from crypto_research.db.conn import get_connection

    with get_connection(settings.database_url) as conn:
        # 1. 拉全量链列表
        chains, _ = ingest_chain_list(client, conn, dry_run=False)
        print(f"Total chains from DeFiLlama: {len(chains)}")

        # 2. 筛选要拉历史的链
        if args.chain:
            target_chains = [c for c in chains if c["chain_name"] == args.chain]
            if not target_chains:
                print(f"Chain not found: {args.chain}", file=sys.stderr)
                return 1
        else:
            target_chains = chains[: args.top_n]

        processed = 0
        failed = 0
        skipped = 0

        for c in target_chains:
            name = c["chain_name"]
            key = c["chain_key"]

            # 续传：已在窗口内成功则跳过
            if _is_recently_ingested(conn, key, window_hours=SKIP_WINDOW_HOURS):
                print(f"  {name}: skipped (ingested <{SKIP_WINDOW_HOURS}h ago)")
                skipped += 1
                processed += 1
                continue

            try:
                flows, _ = ingest_single_chain_hist(client, conn, name, key, dry_run=False)
                tvl = flows.get("tvl_usd", 0) or 0
                f7d = flows.get("flow_7d_usd")
                f7d_str = f"flow_7d=${f7d/1e9:.2f}B" if f7d is not None else "no flow data"
                print(f"  {name}: TVL=${tvl/1e9:.2f}B, {f7d_str}")
                processed += 1
                time.sleep(CHAIN_INTER_REQUEST_SLEEP)
            except Exception as exc:
                print(f"  {name}: failed - {exc}", file=sys.stderr)
                failed += 1
                # 不中断，继续下一条

    print(json.dumps({
        "status": "success" if failed == 0 else "partial",
        "total_chains": len(chains),
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
    }, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
