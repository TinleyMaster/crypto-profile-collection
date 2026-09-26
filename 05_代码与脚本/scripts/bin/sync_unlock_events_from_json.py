"""从 biz.asset_token_unlocks 的 JSON 解锁事件同步到 biz.asset_unlock_event 结构化表。

供 P1-1 解锁榜和其他信号模块消费。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
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


# 审计 P0-2：页面 Value 列与「比例 × 供应量 × 现价」自洽公式的偏差超过该倍数时，
# 才判定为源站口径错误并用重算值覆盖（PONS 2027-01-05 的 12% 解锁页面写 $123.46K、
# 实约 7,417 万，偏差 601x）。5x 以内的正常偏差保留页面值，避免全量同步误改。
_UNLOCK_OVERRIDE_RATIO = 5.0


def _severe_divergence(a: float | None, b: float | None,
                       ratio: float = _UNLOCK_OVERRIDE_RATIO) -> bool:
    """两个正数相差超过 ratio 倍时返回 True（用于判定源站值与重算值是否严重背离）。"""
    if not a or not b or a <= 0 or b <= 0:
        return False
    hi, lo = (a, b) if a >= b else (b, a)
    return hi / lo > ratio


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


# ── 解锁价值重算底座（审计 P0-2，2026-09-26）──────────────────────────
#
# 背景：unlock_value_usd 原先直接照抄 tokenomist 页面上的 Value 列，实测严重失真：
# PONS（asset_id=11114）2027-01-05 的 12% 解锁被写成 $123.46K，而按
# 「解锁比例 × 供应量 × 现价」应为 7,400 万量级（低估约 600 倍）；
# 同时 unlock_amount 恒为 NULL，事件表无法自证。
#
# 修复：不再信任页面 Value 列，改为用**自洽公式**重建：
#   unlock_amount    = ratio_total% × 供应量基数
#   unlock_value_usd = unlock_amount × 现价
# 供应量基数取「可信最大供应量」优先（max_supply 才是 total 口径的正确分母，
# tokenomist 的 total_amount 实测常被写成已释放量）；现价取 CMC 日线最新点。
#
# tokenomist 表格的「Release %」按源站表头语义 = 占**总供应量**的比例，
# 故 ratio_total 为首选口径；pct 与 value 会原样留在 raw_ref 里可追溯。
_SUPPLY_PRICE_SQL = """
    WITH tok AS (
        SELECT DISTINCT ON (asset_id)
               asset_id, max_supply, total_supply, circulating_supply, updated_at AS tok_at
        FROM biz.asset_tokenomics
        WHERE COALESCE(max_supply, total_supply) IS NOT NULL
        ORDER BY asset_id, updated_at DESC NULLS LAST
    ),
    mkt AS (
        SELECT DISTINCT ON (asset_id)
               asset_id, market_date, price_usd, circulating_supply, total_supply, market_cap
        FROM biz.asset_market_daily
        WHERE source_code = 'cmc' AND price_usd IS NOT NULL AND price_usd > 0
        ORDER BY asset_id, market_date DESC
    )
    SELECT t.asset_id,
           COALESCE(NULLIF(t.max_supply, 0), NULLIF(t.total_supply, 0),
                    NULLIF(m.total_supply, 0), NULLIF(m.circulating_supply, 0)) AS supply_basis,
           m.price_usd,
           m.market_cap,
           m.market_date,
           CASE WHEN NULLIF(t.max_supply, 0) IS NOT NULL THEN 'tokenomics.max_supply'
                WHEN NULLIF(t.total_supply, 0) IS NOT NULL THEN 'tokenomics.total_supply'
                WHEN NULLIF(m.total_supply, 0) IS NOT NULL THEN 'cmc.total_supply'
                ELSE 'cmc.circulating_supply' END AS supply_source
    FROM tok t
    FULL OUTER JOIN mkt m ON m.asset_id = t.asset_id
    WHERE COALESCE(t.asset_id, m.asset_id) = ANY(%s)
"""


def _db_num(v) -> float | None:
    """库内 numeric 值（psycopg 返回 Decimal）→ float。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_supply_price_basis(conn, asset_ids: list[int]) -> dict[int, dict]:
    """批量取「供应量基数 + 现价」，供解锁价值重算使用。"""
    if not asset_ids:
        return {}
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(_SUPPLY_PRICE_SQL, (asset_ids,))
        return {
            r["asset_id"]: {
                "supply_basis": _db_num(r["supply_basis"]),
                "price_usd": _db_num(r["price_usd"]),
                "market_cap": _db_num(r["market_cap"]),
                "price_date": str(r["market_date"]) if r["market_date"] else None,
                "supply_source": r["supply_source"],
            }
            for r in cur.fetchall()
        }


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

        # DDL 收尾即提交：ALTER TABLE 会取 ACCESS EXCLUSIVE 锁（即使 IF NOT EXISTS 也取），
        # 若拖到全量同步结束后才 commit，整轮（数百资产 / 数千事件，实测 >15 分钟）都会
        # 阻塞线上对 asset_unlock_event 的一切读（解锁榜、投研页解锁卡），曾实测导致
        # 页面查询排队。此处先提交释放重锁，再做逐行 INSERT（RowExclusive 不与读冲突）。
        conn.commit()

        # 读取需要同步的资产
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if args.asset_id:
                cur.execute(
                    "SELECT asset_id, unlock_events_json, source_name, "
                    "       unlock_ratio_mcap, input_snapshot_json "
                    "FROM biz.asset_token_unlocks WHERE asset_id = %s",
                    (args.asset_id,),
                )
            else:
                cur.execute(
                    "SELECT asset_id, unlock_events_json, source_name, "
                    "       unlock_ratio_mcap, input_snapshot_json "
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

        # 供应量基数 + 现价（审计 P0-2：解锁价值必须可自证）
        basis_map = _load_supply_price_basis(conn, [r["asset_id"] for r in rows])
        print(f"供应量/现价基准命中 {len(basis_map)}/{len(rows)} 个资产")

        with conn.cursor() as cur:
            for row in rows:
                asset_id = row["asset_id"]
                events = row.get("unlock_events_json") or []
                source = row.get("source_name") or "unknown"

                # P0-1: 母表已有市值基准时，为缺 ratio_mcap 的事件补算 unlock_value_usd / market_cap
                snapshot = row.get("input_snapshot_json") or {}
                snap_overview = snapshot.get("overview") or {} if isinstance(snapshot, dict) else {}
                mcap_fallback = None
                _mc = snap_overview.get("market_cap") or snap_overview.get("market_cap_usd")
                try:
                    mcap_fallback = float(_mc) if _mc not in (None, "") else None
                except (TypeError, ValueError):
                    mcap_fallback = None

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

                    # P2-1: pct 语义 —— 源站表头「Release %」= 占总供应量比例，写入 ratio_total；
                    # 若事件显式标注 ratio_mcap 才走市值口径
                    pct = _to_float(ev.get("pct") or ev.get("unlock_pct"))
                    ratio_mcap = pct if ev.get("ratio_mcap") else None
                    ratio_total = None if ev.get("ratio_mcap") else pct
                    unlock_ratio_circulating = _to_float(ev.get("pct_of_circulating"))
                    scraped_value = _to_float(ev.get("value_usd"))

                    # 审计 P0-2：不再照抄页面 Value 列，改用自洽公式重建
                    #   amount = ratio_total% × 供应量基数；value = amount × 现价
                    _b = basis_map.get(asset_id) or {}
                    supply_basis = _b.get("supply_basis")
                    price_usd = _b.get("price_usd")
                    mcap_basis = _b.get("market_cap") or mcap_fallback
                    # 比例越界（>100%）说明源站该列语义与「占总量比例」不符，
                    # 不据此反推金额（否则会算出超过最大供应量的解锁量），仅在 raw_ref 标记
                    ratio_implausible = ratio_total is not None and ratio_total > 100

                    derived_amount = None
                    derived_value = None
                    if (not ratio_implausible and ratio_total is not None and ratio_total > 0
                            and supply_basis and supply_basis > 0):
                        derived_amount = ratio_total / 100.0 * supply_basis
                        if price_usd and price_usd > 0:
                            derived_value = derived_amount * price_usd
                    elif (ratio_mcap is not None and 0 < ratio_mcap <= 100 and mcap_basis):
                        # 源站给的是「占市值比例」时按市值折算
                        derived_value = ratio_mcap / 100.0 * mcap_basis

                    # 覆盖策略：只有「页面值与自洽公式严重背离（>5x）」才让重算值取代页面值；
                    # 页面字段缺失（unlock_amount 实测普遍为 NULL）时直接落重算值。
                    scraped_amount = amount
                    amount_overridden = False
                    if derived_amount is not None:
                        if scraped_amount in (None, 0):
                            amount = derived_amount
                        elif _severe_divergence(scraped_amount, derived_amount):
                            amount = derived_amount
                            amount_overridden = True
                        else:
                            amount = scraped_amount
                    value_overridden = False
                    if derived_value is not None:
                        if scraped_value in (None, 0):
                            unlock_value_usd = derived_value
                        elif _severe_divergence(scraped_value, derived_value):
                            unlock_value_usd = derived_value
                            value_overridden = True
                        else:
                            unlock_value_usd = scraped_value
                    else:
                        unlock_value_usd = scraped_value
                    # ratio_mcap：有市值基准时统一补算（供解锁榜/压力分消费）
                    if mcap_basis and unlock_value_usd and mcap_basis > 0:
                        ratio_mcap = round(unlock_value_usd / mcap_basis * 100.0, 4)
                    risk_level = str(ev.get("risk_level") or "")[:20] or None

                    raw_ref = dict(ev)
                    raw_ref["_derived"] = {
                        "scraped_value_usd": scraped_value,
                        "derived_value_usd": derived_value,
                        "value_overridden": value_overridden,
                        "scraped_amount": scraped_amount,
                        "derived_amount": derived_amount,
                        "amount_overridden": amount_overridden,
                        "supply_basis": supply_basis,
                        "supply_source": _b.get("supply_source"),
                        "price_usd": price_usd,
                        "price_date": _b.get("price_date"),
                        "ratio_implausible": ratio_implausible,
                    }

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
                            psycopg.types.json.Jsonb(raw_ref),
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
