"""
Phase B2 third_party 扩展：链上异常事件（hacks）结构化落库。

拉取 DefiLlama /hacks 全量列表，按 defillamaId 映射到 src_dl.protocol_list 再映射到
core.asset，写入 biz.asset_hacks（结构化表，非 URL 维度，因 /hacks.source 全为空）。

无法映射到资产的记录（defillamaId 为 null 或未在协议表中）直接跳过。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

DEFILLAMA_HACKS_URL = "https://api.llama.fi/hacks"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="补齐链上异常事件（hacks）结构化数据。")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入。")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP 读取超时(秒)。")
    return parser


def _fetch_hacks(timeout: int) -> list[dict] | None:
    try:
        req = urllib.request.Request(DEFILLAMA_HACKS_URL, headers={"User-Agent": "crypto-research/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data if isinstance(data, list) else None
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None


def _to_date(ts) -> "datetime.date | None":
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def extract_hack(asset_id: int, raw: dict) -> dict | None:
    name = raw.get("name")
    hack_date = _to_date(raw.get("date"))
    if not name or hack_date is None:
        return None  # 唯一键 (asset_id, name, hack_date) 不允许空
    return {
        "asset_id": asset_id,
        "defillama_id": raw.get("defillamaId"),
        "name": str(name),
        "technique": raw.get("technique"),
        "amount": raw.get("amount"),
        "returned_funds": raw.get("returnedFunds"),
        "chain": _as_list(raw.get("chain")),
        "target_type": raw.get("targetType"),
        "classification": raw.get("classification"),
        "bridge_hack": raw.get("bridgeHack"),
        "hack_date": hack_date,
        "source": raw.get("source") or None,
    }


def ensure_tables(conn) -> None:
    """确保 biz.asset_hacks 存在（云端自建，避免手动 DDL）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.asset_hacks (
                id             BIGSERIAL PRIMARY KEY,
                asset_id       BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
                defillama_id   TEXT,
                name           TEXT,
                technique      TEXT,
                amount         NUMERIC,
                returned_funds NUMERIC,
                chain          TEXT[],
                target_type    TEXT,
                classification TEXT,
                bridge_hack    BOOLEAN,
                hack_date      DATE,
                source         TEXT,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (asset_id, name, hack_date)
            )
        """)
    conn.commit()


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql

    settings = get_settings(require_database=True)
    upsert_sql = load_sql("biz/upsert_asset_hacks.sql")

    hacks = _fetch_hacks(args.timeout)
    if hacks is None:
        print(json.dumps({"status": "fetch_failed"}, ensure_ascii=False))
        return 1

    print(f"/hacks 事件总数: {len(hacks)}")

    # defillamaId -> asset_id 映射（仅已映射资产的协议）
    id_to_asset: dict[str, int] = {}
    with get_connection(settings.database_url) as conn:
        ensure_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT asm.asset_id, p.protocol_id
                FROM src_dl.protocol_list AS p
                INNER JOIN core.asset_source_map AS asm
                    ON asm.source_code = 'dl'
                   AND asm.source_asset_key = p.protocol_id
                """
            )
            for row in cur.fetchall():
                if row["protocol_id"]:
                    id_to_asset[row["protocol_id"]] = row["asset_id"]

    written = 0
    matched = 0
    skipped = 0
    preview_rows: list[dict] = []

    with get_connection(settings.database_url) as conn:
        for raw in hacks:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            defillama_id = raw.get("defillamaId")
            asset_id = id_to_asset.get(defillama_id) if defillama_id else None
            if asset_id is None:
                skipped += 1
                continue

            row = extract_hack(asset_id, raw)
            if row is None:
                skipped += 1
                continue

            if args.dry_run:
                preview_rows.append(row)
            else:
                fetch_one(conn, upsert_sql,
                    (row["asset_id"], row["defillama_id"], row["name"], row["technique"],
                     row["amount"], row["returned_funds"], row["chain"], row["target_type"],
                     row["classification"], row["bridge_hack"], row["hack_date"], row["source"]))
                written += 1
            matched += 1

        if not args.dry_run:
            conn.commit()

    if args.dry_run:
        result = {
            "mode": "dry-run",
            "total": len(hacks),
            "matched": matched,
            "rows": len(preview_rows),
            "skipped": skipped,
            "first_row": preview_rows[0] if preview_rows else None,
        }
    else:
        result = {
            "status": "complete",
            "total": len(hacks),
            "matched": matched,
            "written": written,
            "skipped": skipped,
        }
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
