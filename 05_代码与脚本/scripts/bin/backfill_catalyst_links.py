"""
存量催化剂资产关联展开（linker 全量重算）。

问题：catalyst_asset_link 只有 legacy 单资产链接（12 条），
      多交易对公告未展开到 N:N 关联表。
修法：对所有催化剂重新跑 linker（related_pairs + 正文 cashtag 兜底），
      写入 catalyst_asset_link 表。

用法：
    python scripts/bin/backfill_catalyst_links.py [--dry-run] [--max-items 100]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


# ---------------------------------------------------------------------------
# linker 核心逻辑（内联自 catalyst/linker.py，避免依赖 workbench）
# ---------------------------------------------------------------------------

# symbol(大写) -> asset_id 缓存
_symbol_asset_cache: dict[str, int | None] = {}

# 非加密资产名称黑名单（SQL 正则，与 workbench/catalyst/linker.py 同步）。
# 命中即视为「美股 tokenized stock / 商品衍生」，不参与 crypto 催化剂信号映射。
_NON_CRYPTO_NAME_SQL = (
    r"tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ"
    r"|crude[[:space:]]+oil|brent"
    r"|robinhood[[:space:]]+token|backpack[[:space:]]+securities"
    r"|\mdinari\M|\mxstock\M"
)

# canonical 白名单（CAT-LINKER-DIRTY）：主流/高碰撞符号强制解析为真实主网币。
# 与 catalyst/linker.py 保持同一清单（内联副本需手动同步）。
_CANONICAL_TOP_SYMBOLS = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "PEPE", "SHIB", "ADA",
    "DOT", "AVAX", "LINK", "LTC", "UNI", "AAVE", "MATIC", "POL", "TRX",
    "XLM", "FIL", "NEAR", "ARB", "OP", "SUI", "TON", "APT", "INJ", "SEI",
    "WIF", "BONK", "FLOKI", "ENA", "PENDLE", "JUP", "RENDER", "FET",
})


def _resolve_canonical_top(conn, base: str) -> int | None:
    """白名单符号 → 真实主网资产（market_cap_rank 非空，取排名最小）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT asset_id
            FROM core.asset
            WHERE UPPER(canonical_symbol) = %s
              AND market_cap_rank IS NOT NULL
            ORDER BY market_cap_rank ASC, asset_id
            LIMIT 1
            """,
            (base,),
        )
        row = cur.fetchone()
    return row["asset_id"] if row else None

# 常见 quote 币种，用于拆分交易对
_QUOTE_ASSETS = (
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD",
    "BTC", "ETH", "BNB", "SOL", "XRP",
)


def _extract_base_symbol(pair: str) -> str | None:
    """从交易对中提取 base symbol（大写）。"""
    if not pair:
        return None
    pair = pair.upper().strip()
    for quote in _QUOTE_ASSETS:
        if pair.endswith(quote) and len(pair) > len(quote):
            base = pair[: -len(quote)]
            if len(base) >= 2 and any(c.isalpha() for c in base):
                return base
    return None


def extract_pairs_from_text(text: str) -> list[str]:
    """从正文中提取交易对（cashtag 兜底）。"""
    if not text:
        return []
    pairs: list[str] = []
    seen: set[str] = set()

    # 模式 1：$XXXUSDT 或 $XXX
    for m in re.finditer(r"\$([A-Z0-9]{2,20})(USDT|USDC|BTC|ETH|BNB)?\b", text):
        base = m.group(1)
        quote = m.group(2) or "USDT"
        pair = base + quote
        if pair not in seen and any(c.isalpha() for c in base):
            seen.add(pair)
            pairs.append(pair)

    # 模式 2：直接 XXXUSDT
    for m in re.finditer(r"\b([A-Z0-9]{2,20})USDT\b", text):
        base = m.group(1)
        if not any(c.isalpha() for c in base):
            continue
        if base in ("USD", "USDC", "BUSD", "TUSD", "USDP", "FDUSD"):
            continue
        pair = base + "USDT"
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    return pairs


def map_pairs_to_asset_ids(
    pairs: list[str],
    conn,
    source_hint: str = "binance",
) -> list[int]:
    """将交易对列表映射为 asset_id 列表（多资产）。"""
    if not pairs:
        return []

    asset_ids: list[int] = []
    seen: set[int] = set()

    for pair in pairs:
        base = _extract_base_symbol(pair)
        if not base:
            continue

        # 查缓存
        if base in _symbol_asset_cache:
            aid = _symbol_asset_cache[base]
            if aid is not None and aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 白名单优先（CAT-LINKER-DIRTY）：主流符号强制取真实主网币
        if base in _CANONICAL_TOP_SYMBOLS:
            aid = _resolve_canonical_top(conn, base)
            _symbol_asset_cache[base] = aid
            if aid is not None and aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 查库：优先 source_hint 来源的 source_asset_key
        # （同 key 多条时按 CMC 排名升序选最靠前的，防止脏映射）
        # 注意：精确 key 匹配也排除 tokenized stock / 商品衍生资产
        #       （CAT-CLEANUP v5.1 ③：美股快讯经 cashtag 会错连 xStock/Robinhood Token，
        #        已定性为脏数据，源头排除，与 workbench/catalyst/linker.py 同步）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                f"""
                SELECT a.asset_id
                FROM core.asset a
                JOIN core.asset_source_map m ON a.asset_id = m.asset_id
                WHERE m.source_code = %s
                  AND UPPER(m.source_asset_key) = %s
                  AND LOWER(COALESCE(a.canonical_name, '')) !~ '{_NON_CRYPTO_NAME_SQL}'
                ORDER BY a.market_cap_rank ASC NULLS LAST, a.asset_id
                LIMIT 1
                """,
                (source_hint, base),
            )
            row = cur.fetchone()
        if row:
            aid = row["asset_id"]
            _symbol_asset_cache[base] = aid
            if aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 退一步：asset 表的 canonical_symbol
        # （同 symbol 多行时避免取到脏行：
        #   1. 排除 tokenized/合成/商品衍生 等（CAT-CLEANUP v5.1 ③，源头排除）
        #   2. 排除真正仿盘标记（bridged/wrapped/intents/trophy/tomato/second chance/base coin）
        #      —— 不再用 meme 系（doge/shib/pepe/floki/inu/ai）无边界子串，避免误杀合法 meme 币（CAT-LINKER-NEG）
        #   3. 按 CMC 排名升序（rank 越小越主流），兜底用 asset_id
        # 线上审计 2026-09-15：BTC→Bitcoin Base/XRP→XRP AI/BNB→BNBTiger Inu
        # 部署复验 2026-09-15：SOL→Sol The Trophy Tomato / ETH→NEAR Intents Bridged ETH
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                f"""
                SELECT asset_id
                FROM core.asset
                WHERE UPPER(canonical_symbol) = %s
                  AND LOWER(COALESCE(canonical_name, '')) !~ '{_NON_CRYPTO_NAME_SQL}'
                  AND LOWER(COALESCE(canonical_name, '')) !~ 'bridged|wrapped|intents|trophy|tomato|second[[:space:]]+chance|base[[:space:]]+coin'
                ORDER BY
                    market_cap_rank ASC NULLS LAST,
                    CASE WHEN LOWER(COALESCE(canonical_name, '')) LIKE '%%' || LOWER(%s) || '%%' THEN 0
                         WHEN LOWER(COALESCE(canonical_name, '')) ~ 'gold|silver|oil|gas' THEN 2
                         ELSE 1 END,
                    asset_id
                LIMIT 1
                """,
                (base, base),
            )
            row = cur.fetchone()
        if row:
            aid = row["asset_id"]
            _symbol_asset_cache[base] = aid
            if aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 没找到，缓存 None
        _symbol_asset_cache[base] = None

    return asset_ids


# ---------------------------------------------------------------------------
# 业务逻辑
# ---------------------------------------------------------------------------


def fetch_all_catalysts(conn, limit: int = 500, offset: int = 0) -> list[dict]:
    """获取所有催化剂（分批）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT catalyst_id, source_code, title, body_text, related_pairs
            FROM biz.asset_catalyst
            ORDER BY catalyst_id
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()

    result = []
    for row in rows:
        d = dict(row)
        # related_pairs 可能是 list 或 str
        if isinstance(d["related_pairs"], str):
            try:
                d["related_pairs"] = json.loads(d["related_pairs"])
            except Exception:
                d["related_pairs"] = []
        elif d["related_pairs"] is None:
            d["related_pairs"] = []
        result.append(d)
    return result


def link_catalyst(cat: dict, conn) -> tuple[list[int], list[int]]:
    """对单条催化剂做资产关联。

    Returns:
        (trading_pairs_asset_ids, cashtag_asset_ids)
    """
    related_pairs = cat.get("related_pairs") or []
    body_text = cat.get("body_text") or ""
    title = cat.get("title") or ""

    # 第一路：related_pairs 官方标签（置信度高）
    pair_asset_ids = []
    if related_pairs:
        pair_asset_ids = map_pairs_to_asset_ids(
            related_pairs, conn, source_hint="binance"
        )

    # 第二路：正文 cashtag 兜底（置信度低）
    cashtag_asset_ids = []
    text_pairs = extract_pairs_from_text(title + "\n" + body_text)
    # 过滤掉已经通过 related_pairs 匹配到的
    if text_pairs:
        all_ids = map_pairs_to_asset_ids(text_pairs, conn, source_hint="binance")
        pair_set = set(pair_asset_ids)
        cashtag_asset_ids = [aid for aid in all_ids if aid not in pair_set]

    return pair_asset_ids, cashtag_asset_ids


def insert_links(catalyst_id: int, asset_ids: list[int], link_source: str,
                 confidence: float, conn) -> int:
    """写入关联表，返回新增条数。"""
    if not asset_ids:
        return 0

    added = 0
    with conn.cursor() as cur:
        for aid in asset_ids:
            try:
                cur.execute(
                    """
                    INSERT INTO biz.catalyst_asset_link
                        (catalyst_id, asset_id, link_source, confidence)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (catalyst_id, asset_id) DO NOTHING
                    """,
                    (catalyst_id, aid, link_source, confidence),
                )
                if cur.rowcount > 0:
                    added += 1
            except Exception as e:
                print(f"    写入关联失败 catalyst_id={catalyst_id}, asset_id={aid}: {e}")
    return added


def main():
    parser = argparse.ArgumentParser(description="存量催化剂资产关联展开")
    parser.add_argument("--dry-run", action="store_true", help="只预览不修改")
    parser.add_argument("--max-items", type=int, default=0, help="最多处理条数（0=全部）")
    parser.add_argument("--batch-size", type=int, default=500, help="每批查询数量")
    args = parser.parse_args()

    settings = get_settings(require_database=not args.dry_run)

    total = 0
    total_pair_links = 0
    total_cashtag_links = 0
    total_no_link = 0
    offset = 0

    with get_connection(settings.database_url) as conn:
        while True:
            batch = fetch_all_catalysts(conn, args.batch_size, offset)
            if not batch:
                break

            print(f"\n处理第 {offset+1}-{offset+len(batch)} 条（共处理 {total} 条已完成）")

            for cat in batch:
                if args.max_items and total >= args.max_items:
                    break

                cid = cat["catalyst_id"]
                pair_ids, cashtag_ids = link_catalyst(cat, conn)

                n_pair = 0
                n_cash = 0
                if not args.dry_run:
                    n_pair = insert_links(cid, pair_ids, "trading_pairs", 0.95, conn)
                    n_cash = insert_links(cid, cashtag_ids, "cashtag", 0.6, conn)
                else:
                    n_pair = len(pair_ids)
                    n_cash = len(cashtag_ids)

                total_pair_links += n_pair
                total_cashtag_links += n_cash

                if not pair_ids and not cashtag_ids:
                    total_no_link += 1
                    status = "无关联"
                else:
                    parts = []
                    if pair_ids:
                        parts.append(f"pairs:{len(pair_ids)}")
                    if cashtag_ids:
                        parts.append(f"cashtag:{len(cashtag_ids)}")
                    status = "+".join(parts)

                title_preview = (cat.get("title") or "")[:50]
                mode = "DRY" if args.dry_run else "OK"
                print(f"  [{cid}] {mode} {status} | {title_preview}")

                total += 1

            if args.max_items and total >= args.max_items:
                break

            if len(batch) < args.batch_size:
                break

            offset += len(batch)

    print(f"\n{'='*60}")
    print(f"处理催化剂总数: {total}")
    print(f"  trading_pairs 关联新增: {total_pair_links}")
    print(f"  cashtag 关联新增:      {total_cashtag_links}")
    print(f"  无任何关联:            {total_no_link}")
    if args.dry_run:
        print("（dry-run 模式，未实际写入）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
