"""
Phase B2 third_party 扩展：TGE / 融资轮次（raises）结构化落库。

对已映射 DefiLlama 的资产，调用 /protocol/{slug} 详情接口，提取 raises 字段，
写入 biz.asset_raises（结构化表，非 URL 维度，因 DefiLlama raises.source 无稳定 URL）。

断点续跑：以 biz.asset_raises.defillama_id 已存在的协议作为「已处理」标记。
注意：raises 为空的协议不产生记录，重跑时会重复拉取，但单轮全量处理下无影响。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

DEFILLAMA_PROTOCOL_URL = "https://api.llama.fi/protocol/{}"
RATE_LIMIT_DELAY = 0.3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="补齐 TGE/融资轮次（raises）结构化数据。")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入。")
    parser.add_argument("--limit", type=int, default=0, help="最多处理协议数，0 表示不限制（全量）。")
    parser.add_argument("--asset-id", type=int, default=None, help="仅处理指定资产ID。")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP 读取超时(秒)。")
    return parser


def _fetch_protocol(slug: str, timeout: int) -> dict | None:
    url = DEFILLAMA_PROTOCOL_URL.format(urllib.parse.quote(slug))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-research/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
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


def _to_numeric(v):
    """将 DefiLlama 金额/估值字段规范化为数值，兼容 '3,000'、'$3,000' 等格式；无法解析返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("$", "").replace(" ", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def extract_raises(asset_id: int, protocol_id: str, protocol: dict) -> list[dict]:
    """从协议详情的 raises 字段提取融资轮次，返回待写入行（不含 id）。"""
    rows: list[dict] = []
    for raw in (protocol.get("raises") or []):
        if not isinstance(raw, dict):
            continue
        round_name = raw.get("round") or "Unknown"
        raise_date = _to_date(raw.get("date"))
        if raise_date is None:
            continue  # 唯一键 (asset_id, round, raise_date) 中日期不允许空
        rows.append({
            "asset_id": asset_id,
            "defillama_id": protocol_id,
            "protocol_name": raw.get("name") or protocol.get("name"),
            "round": str(round_name),
            "raise_date": raise_date,
            "amount": _to_numeric(raw.get("amount")),
            "chains": _as_list(raw.get("chains")),
            "sector": raw.get("sector"),
            "category": raw.get("category"),
            "lead_investors": _as_list(raw.get("leadInvestors")),
            "other_investors": _as_list(raw.get("otherInvestors")),
            "valuation": _to_numeric(raw.get("valuation")),
            "source": raw.get("source") or None,
        })
    return rows


def ensure_tables(conn) -> None:
    """确保 biz.asset_raises 与 biz.dl_protocol_checked 存在（云端自建，避免手动 DDL）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.asset_raises (
                id              BIGSERIAL PRIMARY KEY,
                asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
                defillama_id    TEXT,
                protocol_name   TEXT,
                round           TEXT,
                raise_date      DATE,
                amount          NUMERIC,
                chains          TEXT[],
                sector          TEXT,
                category        TEXT,
                lead_investors  TEXT[],
                other_investors TEXT[],
                valuation       NUMERIC,
                source          TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (asset_id, round, raise_date)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.dl_protocol_checked (
                protocol_id TEXT PRIMARY KEY,
                checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    upsert_sql = load_sql("biz/upsert_asset_raises.sql")
    mark_sql = (
        "INSERT INTO biz.dl_protocol_checked (protocol_id) VALUES (%s) "
        "ON CONFLICT (protocol_id) DO UPDATE SET checked_at = NOW()"
    )

    if args.asset_id is not None:
        candidate_sql = """
            SELECT asm.asset_id, p.protocol_id, p.slug, p.name
            FROM core.asset_source_map AS asm
            INNER JOIN src_dl.protocol_list AS p ON p.protocol_id = asm.source_asset_key
            WHERE asm.source_code = 'dl'
              AND asm.asset_id = %s
              AND p.slug IS NOT NULL AND TRIM(p.slug) != ''
            ORDER BY p.protocol_id
            LIMIT 1
        """
        candidate_params: tuple = (args.asset_id,)
    else:
        candidate_sql = """
            SELECT asm.asset_id, p.protocol_id, p.slug, p.name
            FROM src_dl.protocol_list AS p
            INNER JOIN core.asset_source_map AS asm
                ON asm.source_code = 'dl'
               AND asm.source_asset_key = p.protocol_id
            WHERE p.slug IS NOT NULL
              AND TRIM(p.slug) != ''
              AND NOT EXISTS (
                  SELECT 1 FROM biz.dl_protocol_checked c WHERE c.protocol_id = p.protocol_id
              )
            ORDER BY asm.asset_id
            LIMIT %s
        """
        candidate_params = (args.limit if args.limit > 0 else 10_000_000,)

    with get_connection(settings.database_url) as conn:
        ensure_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(candidate_sql, candidate_params)
            candidates = [dict(row) for row in cur.fetchall()]

    if not candidates:
        print(json.dumps({"status": "no_candidates"}, ensure_ascii=False))
        return 0

    print(f"待处理协议: {len(candidates)}")

    written = 0
    matched = 0
    skipped = 0
    preview_rows: list[dict] = []

    with get_connection(settings.database_url) as conn:
        for i, asset in enumerate(candidates):
            slug = asset["slug"]
            if i > 0:
                time.sleep(RATE_LIMIT_DELAY)

            protocol = _fetch_protocol(slug, args.timeout)
            if protocol is None:
                skipped += 1
                print(f"[{i + 1}/{len(candidates)}] {slug} 拉取失败", flush=True)
                continue

            rows = extract_raises(asset["asset_id"], asset["protocol_id"], protocol)

            # 成功拉取即标记该协议已检查（无论 raises 是否为空），保证断点续跑
            if not args.dry_run:
                conn.execute(mark_sql, (asset["protocol_id"],))

            if not rows:
                skipped += 1
                continue

            if args.dry_run:
                preview_rows.extend(rows)
            else:
                for row in rows:
                    fetch_one(conn, upsert_sql,
                        (row["asset_id"], row["defillama_id"], row["protocol_name"],
                         row["round"], row["raise_date"], row["amount"], row["chains"],
                         row["sector"], row["category"], row["lead_investors"],
                         row["other_investors"], row["valuation"], row["source"]))
                    written += 1
                conn.commit()

            matched += 1
            print(f"[{i + 1}/{len(candidates)}] {slug} +{len(rows)} 轮融资", flush=True)

    if args.dry_run:
        result = {
            "mode": "dry-run",
            "candidates": len(candidates),
            "matched": matched,
            "rows": len(preview_rows),
            "skipped": skipped,
            "first_row": preview_rows[0] if preview_rows else None,
        }
    else:
        result = {
            "status": "complete",
            "candidates": len(candidates),
            "matched": matched,
            "written": written,
            "skipped": skipped,
        }
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
