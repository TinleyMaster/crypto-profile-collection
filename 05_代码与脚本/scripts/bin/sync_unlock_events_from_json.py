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
# HC-014：复用 build_allocation_mix 的单一词表与 recipients 解析（不另造一套分类规则）
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_allocation_mix import mixture_from_recipients, parse_recipients  # noqa: E402


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


# ── UNLOCK-SYS-002：tokenomist 模板占位行识别（2026-10-26 审计）──────
#
# 根因（实测证据，非推测）：
#   tokenomist 页面的「next unlock」卡片对**没有真实解锁计划**的资产会渲染一行占位符：
#       pct = 12.0, value_usd = 123460.0, value_str = "$123.46K",
#       status = "4 months left", allocations = 1, is_upcoming = True, recipients = None
#   该签名在母表 biz.asset_token_unlocks 的 1,383 个资产里**同时出现于 108 个资产**
#   （含 BTC 这种根本不存在「解锁」概念的币），日期集中在 2026-12-25 / 2027-01-20~21。
#   ⇒ 这是**模板占位行**，不是真实解锁事件。入库后表现为：
#       · ratio_total = 12.0（假的供应量释放比例）
#       · unlock_value_usd 被自洽公式重算成荒谬值（BEAM 5,371 万美元 / BNB 8.3 万亿美元）
#       · ratio_mcap 动辄数百至数千个百分点（BEAM 4654%、BNB 8524%）
#   ⚠️ **不能按 symbol 黑名单过滤**：命中面是 108 个资产且随页面变化，黑名单不可穷举，
#      且 pct=12.0 / $123.46K 在 SHIB/SOL/YFI 等真实事件中也存在（非 PoW 专属签名）。
#   ⇒ 正确解法是**按事件结构特征识别**。
#
# 🔴 源头治理 G3.1：判据已抽到公共模块 crypto_research.unlock_quality，
#    与采集端（phase_chain_token_unlocks.py）共用同一实现，避免判据漂移。
#    本文件保留 `_is_template_event` 薄封装以兼容既有调用方。
from crypto_research.unlock_quality import (  # noqa: E402
    TEMPLATE_PLACEHOLDER_PCT,
    TEMPLATE_PLACEHOLDER_VALUE_USD,
    TEMPLATE_STATUS_PATTERNS,
    is_template_event as _shared_is_template_event,
)


def _is_template_event(ev: dict) -> bool:
    """判定 tokenomist 模板占位行（跨资产重复的假解锁事件）。

    实现见 crypto_research.unlock_quality.is_template_event。
    宁可漏杀（漏判的行只是多一条噪声数据）也不可错杀（错杀会丢掉真实解锁事件）。
    """
    return _shared_is_template_event(ev)


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
#
# 🔴 UNLOCK-SYS-002（2026-10-26 审计）：供应量基数取数顺序必须 CMC 优先。
# 旧顺序 `tokenomics.max_supply → tokenomics.total_supply → cmc.*` 有两个致命缺陷：
#   ① biz.asset_tokenomics.max_supply 系统性污染（实测 2,047 行中 39 行 >1e12；
#      KAS max_supply=28,704,026,601 而真实总量仅 27,775,519,352 量级——差 10 倍量级错位）；
#   ② biz.asset_market_daily 存在同名多 asset_id 污染（BTC 9 个 / CAT 17 个 / PEPE 16 个），
#      其中错挂的「同名假币」行会带进量纲完全不同的 market_cap。
# 改为：**CMC 快照（与 price/mcap 同源同资产，量纲自洽）优先**，
# tokenomics.max_supply 降为末位兜底，并记录实际采用的口径供审计追溯。
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
    SELECT COALESCE(t.asset_id, m.asset_id) AS asset_id,
           -- 🔴 CMC 优先：cmc.total_supply → cmc.circulating_supply → tokenomics.total_supply
           --    → tokenomics.max_supply（污染源，末位兜底）
           COALESCE(NULLIF(m.total_supply, 0), NULLIF(m.circulating_supply, 0),
                    NULLIF(t.total_supply, 0), NULLIF(t.max_supply, 0)) AS supply_basis,
           m.price_usd,
           m.market_cap,
           m.circulating_supply,
           m.market_date,
           CASE WHEN NULLIF(m.total_supply, 0) IS NOT NULL THEN 'cmc.total_supply'
                WHEN NULLIF(m.circulating_supply, 0) IS NOT NULL THEN 'cmc.circulating_supply'
                WHEN NULLIF(t.total_supply, 0) IS NOT NULL THEN 'tokenomics.total_supply'
                WHEN NULLIF(t.max_supply, 0) IS NOT NULL THEN 'tokenomics.max_supply'
                ELSE NULL END AS supply_source
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
                "circulating_supply": _db_num(r["circulating_supply"]),
                "price_date": str(r["market_date"]) if r["market_date"] else None,
                "supply_source": r["supply_source"],
                "market_cap_source": "cmc.asset_market_daily" if r["market_cap"] is not None else None,
            }
            for r in cur.fetchall()
        }


def _infer_beneficiary_type(ev: dict) -> str | None:
    """从事件的结构化 beneficiaries 推断主导桶（HC-014 重写）。

    🔴 旧实现为何必然失效（2026-10-27 取证）：
       旧签名 `_infer_beneficiary_type(recipients: str)`，调用处传
           `str(ev.get("allocations") or ev.get("recipients") or "")`
       而 tokenomics 源里 `allocations` 实际是**类别计数**（'1' / '2' / '4'），
       不是类别文本 → `_infer("4")` 恒返回 None。同时 `Insiders` 因词表缺
       'insider' 会被 classify 判成 other（第二个根因，已在 build_allocation_mix
       词表修复）。两者叠加 = 即使源端给了 Insiders 42%，库里也是 NULL。

    现口径：优先读带 pct 的结构化数组，按 pct 加权取主导桶；退化到文本时才扫关键词。
    """
    for key in ("recipients", "tokenomics_recipients", "beneficiaries"):
        items = parse_recipients(ev.get(key))
        if items:
            mix = mixture_from_recipients(items)
            if mix:
                return mix["dominant_bucket"]

    # 退化路径：只有一段人类可读文本时扫关键词（可靠性低，但仍好过直接丢弃）
    txt = ev.get("allocations_text") or ""
    if isinstance(txt, str) and txt.strip():
        r = txt.lower()
        if any(k in r for k in ("insider", "team", "founder", "contributor", "advisor")):
            return "team"
        if any(k in r for k in ("investor", "seed", "private sale", "vc", "strategic")):
            return "investor"
        if any(k in r for k in ("community", "ecosystem", "treasury", "marketing", "liquidity")):
            return "community"
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
            # 兼容旧表：补索引。🔴 UNLOCK-SYS-001（2026-10-23）：CREATE INDEX IF NOT EXISTS
            # 即使索引已存在也校验表 owner 权限，navicat 等非 owner 角色会被
            # InsufficientPrivilege 硬中断（连 --dry-run 都无法执行）。索引缺失不阻塞
            # 逐行 INSERT，故降级为告警；真缺索引由建表迁移负责，此处不阻断同步。
            for _idx_sql in (
                """
                CREATE INDEX IF NOT EXISTS idx_unlock_event_date
                    ON biz.asset_unlock_event (unlock_date)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_unlock_event_asset
                    ON biz.asset_unlock_event (asset_id)
                """,
            ):
                try:
                    cur.execute(_idx_sql)
                except Exception as e:
                    # 及时回滚：否则事务 abort 后后续语句连锁报 InFailedSqlTransaction
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    print(f"[warn] 索引自愈跳过（不影响同步）: {type(e).__name__}: {e}")
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
        # 🔴 UNLOCK-SYS-002 计数器
        skipped_template = 0
        mcap_contaminated = 0
        ratio_mcap_out_of_range = 0

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
                    # 🔴 UNLOCK-SYS-002：模板占位行直接丢弃（不入库、不参与任何下游计算）
                    # 详见 _is_template_event 的说明：源站对无真实解锁计划的资产渲染
                    # pct=12.0 / value_usd=123460.0 的假行，跨 108 个资产重复出现。
                    if _is_template_event(ev):
                        skipped_template += 1
                        continue
                    unlock_date = _parse_date(ev.get("date"))
                    if not unlock_date:
                        continue

                    # P2-2: type/category 兜底；beneficiary 走结构化 recipients（HC-014）
                    #   旧实现直接把 ev.beneficiary 的原始文本当 beneficiary_type 存，
                    #   会写进 "Insiders" / "Private Investors" 这类非 8 桶取值，下游
                    #   `IN ('team','investor')` 永远匹配不到。现统一归一化到 8 桶。
                    unlock_type = str(ev.get("type") or ev.get("category") or "unspecified")[:50]
                    beneficiary = _infer_beneficiary_type(ev)
                    source_code = str(source)[:50]

                    # P2-3: unlock_amount 从 amount_str / amount 解析
                    amount = _to_float(ev.get("amount") or ev.get("unlock_amount"))
                    if amount is None:
                        amount = _parse_token_amount_str(ev.get("amount_str"))

                    # P2-1: pct 语义 —— 源站表头「Release %」= 占总供应量比例，恒写入 ratio_total。
                    # 🔴 UNLOCK-SYS-001（2026-10-23 审计）：旧逻辑
                    #   `ratio_mcap = pct if ev.get("ratio_mcap") else None`
                    # 把「占供应量比例」错位写进「占市值比例」列，库内 3,033 行被污染
                    # （已数据修复置 NULL）。今后 pct 只进 ratio_total；
                    # ratio_mcap 仅接受下方「价值/市值」自洽重算值。
                    pct = _to_float(ev.get("pct") or ev.get("unlock_pct"))
                    ratio_total = pct
                    ratio_mcap = None
                    # 页面显式给出市值口径时单独取用（拒绝 bool 标志位被 safe_float 强转成 1.0）
                    _mcap_pct_raw = ev.get("ratio_mcap")
                    mcap_pct = _to_float(_mcap_pct_raw) if not isinstance(_mcap_pct_raw, bool) else None
                    unlock_ratio_circulating = _to_float(ev.get("pct_of_circulating"))
                    scraped_value = _to_float(ev.get("value_usd"))

                    # 审计 P0-2：不再照抄页面 Value 列，改用自洽公式重建
                    #   amount = ratio_total% × 供应量基数；value = amount × 现价
                    _b = basis_map.get(asset_id) or {}
                    supply_basis = _b.get("supply_basis")
                    price_usd = _b.get("price_usd")
                    mcap_basis = _b.get("market_cap") or mcap_fallback

                    # 🔴 UNLOCK-SYS-002：量纲自检 —— market_cap 必须与 price×circ 同量级。
                    # biz.asset_market_daily 存在同名多 asset_id 污染（BTC 9 个 / CAT 17 个 /
                    # PEPE 16 个），其中错挂的「同名假币」行 market_cap 量纲完全错误
                    # （实测 ZEC 9133「Zcash AI」mcap=3,873.67 而真 ZEC(1273)=206 亿）。
                    # 用 price×circulating_supply 交叉验市值：偏差 >10 倍即判定基准污染，
                    # 该资产的 mcap_basis 一律拒用（ratio_mcap 留 NULL，绝不写错值）。
                    mcap_basis_valid = True
                    _circ = _b.get("circulating_supply")
                    if mcap_basis and price_usd and _circ and _circ > 0:
                        _implied = price_usd * _circ
                        _r = (_implied / mcap_basis) if mcap_basis > 0 else 0
                        if _r > 10 or _r < 0.1:
                            mcap_basis_valid = False
                            mcap_contaminated += 1
                    elif mcap_basis and price_usd and _circ is None and supply_basis:
                        # 无流通量时退而用总量做弱校验（允许 1/50 ~ 50 倍的宽松区间）
                        _implied = price_usd * supply_basis
                        _r = (_implied / mcap_basis) if mcap_basis > 0 else 0
                        if _r > 50 or _r < 0.02:
                            mcap_basis_valid = False
                            mcap_contaminated += 1
                    if not mcap_basis_valid:
                        mcap_basis = None

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
                    elif (mcap_pct is not None and 0 < mcap_pct <= 100 and mcap_basis):
                        # 源站显式给出「占市值比例」时按市值折算（与供应量口径互斥）
                        derived_value = mcap_pct / 100.0 * mcap_basis

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
                    # 🔴 合理性护栏（UNLOCK-SYS-001 + SYS-002）：单次解锁占市值 >100% 不可能成立
                    # （实测为 PoW 年发射量误抓 / supply_basis 污染 / 模板占位行所致），超限不写入
                    ratio_mcap_rejected = False
                    if mcap_basis and unlock_value_usd and mcap_basis > 0:
                        _rm = unlock_value_usd / mcap_basis * 100.0
                        if 0 < _rm <= 100:
                            ratio_mcap = round(_rm, 4)
                        else:
                            ratio_mcap = None
                            ratio_mcap_rejected = True
                            ratio_mcap_out_of_range += 1
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
                        # 🔴 UNLOCK-SYS-002 审计溯源
                        "mcap_basis_valid": mcap_basis_valid,
                        "mcap_basis_source": _b.get("market_cap_source"),
                        "ratio_mcap_rejected": ratio_mcap_rejected,
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
                                -- 🔴 HC-014（2026-10-27）：旧写法 `= EXCLUDED.beneficiary_type`
                                --    会用 NULL 覆盖 backfill_unlock_recipients / 词表回填
                                --    已经算出来的精准值，而 beneficiary_source 又不在 SET
                                --    子句里 → 留下「source 说已标注、type 却是 NULL」的矛盾行
                                --    （实测 2,974 行）。与 HC-012 的 scraped_at 是同一类病：
                                --    UPSERT 的 SET 语义与字段生命周期不匹配。
                                --    现改为「NULL 不覆盖」，并在确有值时同步推进 source，
                                --    避免 source 与 type 语义脱节。
                                beneficiary_type = COALESCE(EXCLUDED.beneficiary_type,
                                                            biz.asset_unlock_event.beneficiary_type),
                                beneficiary_source = CASE
                                    -- 只在「库里原本没有 type」时才认领来源，
                                    -- 否则保留更早的精确来源标签（event_recipients /
                                    -- label / tokenomics_unlock_hero），避免每次同步
                                    -- 都把审计线索改写成 sync_recipients。
                                    WHEN EXCLUDED.beneficiary_type IS NOT NULL
                                         AND biz.asset_unlock_event.beneficiary_type IS NULL
                                        THEN 'sync_recipients'
                                    ELSE biz.asset_unlock_event.beneficiary_source
                                END,
                                risk_level = EXCLUDED.risk_level,
                                -- 🔴 HC-014：原为 `raw_ref = EXCLUDED.raw_ref`（整体替换），
                                --    会把 backfill_unlock_recipients 写进 raw_ref 的
                                --    tokenomics_recipients 一并冲掉 —— 每次 sync 都在
                                --    静默销毁事件级受益人的唯一原始证据。
                                --    jsonb `||` 为键级合并（右侧覆盖同名键），既保历史
                                --    也接受新抓值。
                                raw_ref = biz.asset_unlock_event.raw_ref || EXCLUDED.raw_ref,
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
        # 🔴 UNLOCK-SYS-002 审计摘要：三项防线各自拦截量，缺一不可见
        print(f"  [SYS-002] 模板占位行丢弃: {skipped_template} 条"
              f" | 市值基准污染拒用: {mcap_contaminated} 次"
              f" | ratio_mcap 越界拒写: {ratio_mcap_out_of_range} 条")
        # ⚠️ SKILL-HC-010：本步只写 biz.asset_unlock_event，链路还差两步。
        #    biz.mv_asset_screen 的 u_n_events 取自 biz.asset_unlock_reaction
        #    的**最新 as_of 快照**（ORDER BY as_of DESC LIMIT 1），本脚本既不重算
        #    快照也不刷新视图 —— 只跑到这里就去查 hc 漏斗，会看到「数据明明补了、
        #    u_n_events 却纹丝不动」的假故障（SKILL-HC-009 的真实踩坑）。
        print()
        print("⚠️  链路未完成（本步只更新 event 表）：")
        print("    ③ 重算解锁反应快照  python bin/asset_unlock_reaction.py "
              f"{'--asset-id %d ' % args.asset_id if args.asset_id else '--batch '}--persist")
        print("    ④ 刷新物化视图      python bin/refresh_asset_screen_mv.py")
        print("    或一条命令跑完 ③④   python bin/refresh_unlock_chain.py "
              f"{'--asset-id %d' % args.asset_id if args.asset_id else '--from-file <targets.tsv>'} "
              "--skip-crawl --skip-event")

    return 0


if __name__ == "__main__":
    sys.exit(main())
