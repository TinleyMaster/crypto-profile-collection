"""从 biz.asset_token_unlocks 的 JSON 解锁事件同步到 biz.asset_unlock_event 结构化表。

供 P1-1 解锁榜和其他信号模块消费。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将 asset_token_unlocks 的 JSON 解锁事件同步到 asset_unlock_event 结构化表"
    )
    parser.add_argument(
        "--asset-id", type=int, default=None,
        help="只同步指定资产（默认全量）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅统计，不写入"
    )
    return parser


def _parse_date(val) -> datetime.date | None:
    """解析解锁日期，支持多种格式。"""
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    s = str(val).strip()
    # 清理 tokenomist 格式的后缀，如 "Sep 10, 2026Next" → "Sep 10, 2026"
    # 以及 "Apr 18, 2023TGE" → "Apr 18, 2023"
    for suffix in ("Next", "TGE", "Unlocks", "Unlock"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()

    # 先尝试完整匹配
    full_formats = (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y/%m/%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    )
    for fmt in full_formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # 再尝试 ISO 格式（处理各种时区后缀）
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        pass

    return None


def safe_float(x) -> float | None:
    """通用安全浮点转换：任何从网页解析的数值字段都走这里，绝不冒泡崩溃。"""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if not isinstance(x, str):
        return None
    x = x.strip().replace(",", "")
    if not x:
        return None
    try:
        return float(x)
    except (ValueError, AttributeError):
        return None


def _to_float(val) -> float | None:
    return safe_float(val)


# 锚定正则：必须以数字开头，"." / ".." / "abc" 等脏值直接不匹配
_AMOUNT_RE = re.compile(r'^([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMBTkmbt])?$')
_AMOUNT_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _parse_token_amount_str(s: str) -> float | None:
    """解析 '11.2M' / '1.2B' / '100K' 等代币数量字符串为数值。

    三道守卫：空值直接返回、正则 ^...$ 锚定拦截脏值、float 转换 try 包裹。
    """
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    m = _AMOUNT_RE.match(s)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except (ValueError, AttributeError):
        return None
    mult = _AMOUNT_MULT.get((m.group(2) or "").upper(), 1)
    return num * mult


def _infer_beneficiary_type(recipients: str) -> str | None:
    """从 recipients 文本推断受益人类别（Team / Investors / Public）。"""
    if not recipients:
        return None
    r = recipients.lower()
    if any(k in r for k in ("team", "founder", "core contributor", "contributor")):
        return "team"
    if any(k in r for k in ("investor", "seed", "private sale", "vc", "strategic", "advisor")):
        return "investors"
    if any(k in r for k in ("public", "community", "ecosystem", "treasury", "marketing", "liquidity")):
        return "public"
    return None


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 确保表存在
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.asset_unlock_event (
                    asset_id BIGINT NOT NULL,
                    unlock_date DATE NOT NULL,
                    unlock_type VARCHAR NOT NULL,
                    source_code VARCHAR NOT NULL,
                    unlock_amount NUMERIC,
                    unlock_ratio_total NUMERIC,
                    unlock_ratio_circulating NUMERIC,
                    unlock_ratio_mcap NUMERIC,
                    unlock_value_usd NUMERIC,
                    beneficiary_type VARCHAR,
                    remaining_locked NUMERIC,
                    risk_level VARCHAR,
                    raw_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (asset_id, unlock_date, unlock_type, source_code)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_unlock_event_date
                    ON biz.asset_unlock_event (unlock_date)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_unlock_event_asset
                    ON biz.asset_unlock_event (asset_id)
            """)
            # 兼容旧表：补充 unlock_ratio_mcap 列
            try:
                cur.execute("""
                    ALTER TABLE biz.asset_unlock_event
                    ADD COLUMN IF NOT EXISTS unlock_ratio_mcap NUMERIC
                """)
            except Exception:
                pass

        # 读取需要同步的资产
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if args.asset_id:
                cur.execute(
                    "SELECT asset_id, unlock_events_json, source_name "
                    "FROM biz.asset_token_unlocks WHERE asset_id = %s",
                    (args.asset_id,),
                )
            else:
                cur.execute(
                    "SELECT asset_id, unlock_events_json, source_name "
                    "FROM biz.asset_token_unlocks "
                    "WHERE unlock_events_json IS NOT NULL "
                    "  AND jsonb_array_length(unlock_events_json) > 0"
                )
            rows = cur.fetchall()

        print(f"找到 {len(rows)} 个有解锁数据的资产")
        if args.dry_run:
            total_events = sum(
                len(r.get("unlock_events_json") or []) for r in rows
            )
            print(f"共 {total_events} 条解锁事件（dry-run，不写入）")
            return 0

        inserted = 0
        skipped = 0
        now = datetime.now(timezone.utc)

        with conn.cursor() as cur:
            for row in rows:
                asset_id = row["asset_id"]
                events = row.get("unlock_events_json") or []
                source = row.get("source_name") or "unknown"

                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    unlock_date = _parse_date(ev.get("date"))
                    if not unlock_date:
                        continue

                    # P2-2: type/category 兜底；recipients 推断 beneficiary
                    unlock_type = str(ev.get("type") or ev.get("category") or "unspecified")[:50]
                    recipients_txt = str(ev.get("allocations") or ev.get("recipients") or "")[:500]
                    beneficiary = (
                        str(ev.get("beneficiary") or ev.get("holder") or "")[:100]
                        or _infer_beneficiary_type(recipients_txt)
                    )
                    source_code = str(source)[:50]

                    # P2-3: unlock_amount 从 amount_str / amount 解析
                    amount = _to_float(ev.get("amount") or ev.get("unlock_amount"))
                    if amount is None:
                        amount = _parse_token_amount_str(ev.get("amount_str"))

                    # P2-1: pct 语义 —— 主源(tokenomics.com)为 % of MCAP，写入 ratio_mcap
                    pct = _to_float(ev.get("pct") or ev.get("unlock_pct"))
                    ratio_mcap = pct if ev.get("ratio_mcap") else None
                    ratio_total = None if ev.get("ratio_mcap") else pct
                    unlock_ratio_circulating = _to_float(ev.get("pct_of_circulating"))
                    unlock_value_usd = _to_float(ev.get("value_usd"))
                    risk_level = str(ev.get("risk_level") or "")[:20] or None

                    try:
                        cur.execute("""
                            INSERT INTO biz.asset_unlock_event
                                (asset_id, unlock_date, unlock_type, source_code,
                                 unlock_amount, unlock_ratio_total, unlock_ratio_circulating,
                                 unlock_ratio_mcap, unlock_value_usd, beneficiary_type,
                                 risk_level, raw_ref)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (asset_id, unlock_date, unlock_type, source_code)
                            DO UPDATE SET
                                unlock_amount = EXCLUDED.unlock_amount,
                                unlock_ratio_total = EXCLUDED.unlock_ratio_total,
                                unlock_ratio_circulating = EXCLUDED.unlock_ratio_circulating,
                                unlock_ratio_mcap = EXCLUDED.unlock_ratio_mcap,
                                unlock_value_usd = EXCLUDED.unlock_value_usd,
                                beneficiary_type = EXCLUDED.beneficiary_type,
                                risk_level = EXCLUDED.risk_level,
                                raw_ref = EXCLUDED.raw_ref,
                                updated_at = NOW()
                        """, (
                            asset_id, unlock_date, unlock_type, source_code,
                            amount, ratio_total, unlock_ratio_circulating,
                            ratio_mcap, unlock_value_usd, beneficiary,
                            risk_level,
                            psycopg.types.json.Jsonb(ev),
                        ))
                        inserted += 1
                    except Exception as e:
                        skipped += 1
                        print(f"  [skip] asset={asset_id} date={unlock_date} type={unlock_type}: {e}")

        conn.commit()
        print(f"同步完成：写入 {inserted} 条，跳过 {skipped} 条")

    return 0


if __name__ == "__main__":
    sys.exit(main())
