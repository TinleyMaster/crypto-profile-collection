#!/usr/bin/env python3
"""催化剂事件因子化：规则推导 event_type → 定向市场影响（零 LLM）。

P0-A: 把 ai_sentiment 转成对具体资产的定向影响（direction + strength + horizon）。

用法：
    python build_catalyst_impact.py --backfill          # 一次性回填所有未推导的 catalyst
    python build_catalyst_impact.py --incremental       # 增量：仅处理新 catalyst
    python build_catalyst_impact.py --catalyst-id 123   # 单条推导
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
_project = SCRIPT_DIR.parent / "src"
if str(_project) not in sys.path:
    sys.path.insert(0, str(_project))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# event_type → (direction, strength, horizon_days)
# 2026-10-02 数据校准版（来源：scan_catalyst_impact_from_moves.py 前瞻口径，近 90 天
# top300 资产，催化剂发布后 2 天内平均|异动|）：
#   delisting 5.03% / burn 4.55% / partnership 4.03% / tech_upgrade 3.76% /
#   listing 3.59% / funding 3.09% / regulation 2.84% / market_update(播报) ≈2%
# 据此校准：
#   · partnership 由 medium 上调 strong（实测 4.03%，方向对齐 +2.90%）
#   · listing 由 strong 下调 medium（实测 3.59%，低于 partnership/tech_upgrade）
#   · regulation 由 strong 大幅下调 weak（实测 2.84% 且方向对齐仅 +0.25%，中性事件
#     对价格几乎没有方向性影响，原 strong 严重高估）
#   · market_update 维持 weak（行情播报，前瞻力≈0，且不参与 mcap 升档）
RULE = {
    "listing":      ("bullish", "medium", 7),
    "delisting":    ("bearish", "strong", 0),
    "burn":         ("bullish", "strong", 30),
    "partnership":  ("bullish", "strong", 14),
    "tech_upgrade": ("bullish", "medium", 14),
    "funding":      ("bullish", "medium", 14),
    "regulation":   ("neutral", "weak", 0),
    "security":     ("bearish", "medium", 0),   # 2026-10-07 新增：安全事件/漏洞 → 利空
    "unlock":       ("bearish", "medium", 0),   # 2026-10-07 新增：代币解锁 → 利空
    "mint":         ("bearish", "medium", 0),   # 2026-10-07 新增：增发稀释 → 利空
    "airdrop":      ("bullish", "weak", 7),     # 2026-10-07 对齐：空投 → 利多
    "market_update":("neutral", "weak", 0),
}
DEFAULT_RULE = ("neutral", "weak", 0)

# 行情播报/非事件类：不参与 mcap 升档（前瞻力≈0，避免小市值播报被误标 strong）
NON_EVENT_TYPES = {"market_update", "other"}

# 审计 2026-10-02：impact_strength 原为纯 event_type 静态查表（同类型事件强度永远一样），
# 与真实影响无关——用 outcome 实测 72h 超额反查：strong 档均值 5.32% 反而被 weak 档 4.94%
# 追平（n=208），而市值分层是唯一强判别因子（<1亿 6.93% vs ≥50亿 1.80%，3.8 倍差距）。
# 修法：strength 改为「event_type 基础档 × 关联资产最小市值」联合判定（与 grade._mcap_score
# 同分档口径），mcap 仅作上调、永不降档——保留事件类型语义，同时让小市值事件真实反映
# 更高的预期影响幅度。
STRENGTH_ORDER = ["weak", "medium", "strong"]  # 档位索引越大影响越强
MCAP_BOOST = [  # (上限, 升档数)，命中首个即停
    (1e8, 2),   # <1 亿：小市值是影响首要因子（实测 6.93%），升两档
    (1e9, 1),   # <10 亿：升一档
    (None, 0),  # 其余保持
]


def _mcap_boost(min_mcap: float | None) -> int:
    """关联资产最小市值 → strength 升档数。无市值数据保守不升档。"""
    if min_mcap is None or min_mcap <= 0:
        return 0
    for cap, boost in MCAP_BOOST:
        if cap is None or min_mcap < cap:
            return boost
    return 0


def _resolve_strength(base_strength: str, min_mcap: float | None,
                      event_type: str = "") -> str:
    """event_type 基础档 + mcap 升档 → 最终 strength。

    mcap 只上调不降档：例如 funding(medium)+<1亿(升2) → strong；
    listing(strong)+任意 mcap → strong（保持，避免过度膨胀）。
    行情播报/非事件类（market_update/other）固定基础档、不随市值升档。
    """
    if event_type in NON_EVENT_TYPES:
        return base_strength
    try:
        idx = STRENGTH_ORDER.index(base_strength)
    except ValueError:
        idx = STRENGTH_ORDER.index("weak")
    idx = min(len(STRENGTH_ORDER) - 1, idx + _mcap_boost(min_mcap))
    return STRENGTH_ORDER[idx]


def _load_sql(relative: str) -> str:
    sql_dir = SCRIPT_DIR.parent / "sql"
    return (sql_dir / relative).read_text(encoding="utf-8")


def build_for_catalyst(cur, catalyst_id: int, event_type: str,
                       ai_sentiment: str | None = None) -> int:
    """为单条 catalyst 推导 impact 并 upsert，返回写入行数。

    方向推导优先级：
    1. ai_sentiment（AI 已分析过情绪，最准确）
    2. RULE[event_type]（事件类型规则，作为兜底）
    3. DEFAULT_RULE（neutral/weak/0）

    强度与时间窗口继续沿用 event_type 规则（情绪只决定多空方向，
    强度/周期由事件性质决定更合理）。
    """
    import re as _re

    # 方向：优先用 ai_sentiment
    if ai_sentiment and ai_sentiment.lower() in ("bullish", "bearish", "neutral"):
        direction = ai_sentiment.lower()
    else:
        direction = RULE.get(event_type, DEFAULT_RULE)[0]

    # 基础强度 & 周期：沿用 event_type 规则；mcap 升档在拿到关联资产后计算
    _, base_strength, horizon = RULE.get(event_type, DEFAULT_RULE)

    # 1) 优先：catalyst_asset_link（已建立的链接关系）
    cur.execute(
        "SELECT asset_id FROM biz.catalyst_asset_link WHERE catalyst_id = %s",
        (catalyst_id,),
    )
    links = cur.fetchall()

    # 2) 兜底：从 title 提取 token，匹配 core.asset.canonical_symbol
    if not links:
        cur.execute(
            "SELECT title FROM biz.asset_catalyst WHERE catalyst_id = %s",
            (catalyst_id,),
        )
        row = cur.fetchone()
        title = (row[0] or "") if row else ""
        if title:
            # P0-2: token 长度下限 3（过滤 2 位介词/缩写噪声）+ 扩充 skip_words
            tokens = _re.findall(r'\b([A-Z]{3,6})\b', title)
            skip_words = {
                "THE", "FOR", "AND", "NOT", "HAS", "ITS", "ARE", "WAS", "CEO",
                "CFO", "SEC", "FDA", "GDP", "ETF", "IPO", "OTC", "ALL", "NEW",
                "HIP", "API", "USD", "COIN", "BTC", "ETH", "SOL", "BNB",
            }
            tokens = [t for t in tokens if t not in skip_words]
            if tokens:
                # P0-1: 同 symbol 只取 1 个资产（按市值降序取 canonical）
                # 避免 "ETH Staking" 扩散到 15 个 symbol=ETH 的脏资产
                cur.execute(
                    "SELECT DISTINCT ON (UPPER(k.canonical_symbol)) "
                    "  k.asset_id, UPPER(k.canonical_symbol) "
                    "FROM core.asset k "
                    "WHERE UPPER(k.canonical_symbol) = ANY(%s) "
                    "ORDER BY UPPER(k.canonical_symbol), "
                    "         COALESCE(k.market_cap, 0) DESC NULLS LAST",
                    (tokens,),
                )
                matched = cur.fetchall()
                links = [(r[0],) for r in matched]

    # P1-3: delisting 类事件额外尝试混合大小写匹配（覆盖 HyENA/Lakala 等）
    if not links and event_type == "delisting" and title:
        # 提取 3-8 位混合大小写 token（非全大写，也非全小写）
        mixed_tokens = _re.findall(r'\b([A-Za-z]{3,8})\b', title)
        # 过滤纯小写（介词）和 skip_words
        mixed_tokens = [
            t.upper() for t in mixed_tokens
            if t.upper() not in skip_words
            and t != t.lower()  # 排除全小写
            and t != t.upper()  # 排除全大写（已处理过）
            and not t[0].isdigit()  # 排除数字开头
        ]
        if mixed_tokens:
            cur.execute(
                "SELECT DISTINCT ON (UPPER(k.canonical_symbol)) "
                "  k.asset_id, UPPER(k.canonical_symbol) "
                "FROM core.asset k "
                "WHERE UPPER(k.canonical_symbol) = ANY(%s) "
                "ORDER BY UPPER(k.canonical_symbol), "
                "         COALESCE(k.market_cap, 0) DESC NULLS LAST",
                (mixed_tokens,),
            )
            matched = cur.fetchall()
            links = [(r[0],) for r in matched]

    if not links:
        return 0

    # 关联资产最小市值 → strength 升档（审计 2026-10-02：市值是实测唯一强判别因子，
    # 纯 event_type 静态分档与真实影响无关）。无市值数据保守保持基础档。
    min_mcap = None
    asset_ids = [link[0] for link in links]
    cur.execute(
        "SELECT min(market_cap) FROM core.asset WHERE asset_id = ANY(%s) AND market_cap > 0",
        (asset_ids,),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        min_mcap = float(row[0])
    strength = _resolve_strength(base_strength, min_mcap, event_type)

    sql = _load_sql("biz/upsert_catalyst_impact.sql")
    params = [
        (catalyst_id, link[0], direction, strength, horizon, "rule")
        for link in links
    ]
    cur.executemany(sql, params)
    return len(params)


def backfill_all(cur) -> int:
    """回填所有已 AI 处理但未推导的 catalyst。"""
    cur.execute("""
        SELECT ac.catalyst_id, ac.ai_event_type, ac.ai_sentiment
        FROM biz.asset_catalyst ac
        WHERE ac.ai_processed = true
          AND NOT EXISTS (
              SELECT 1 FROM biz.catalyst_impact ci
              WHERE ci.catalyst_id = ac.catalyst_id
          )
    """)
    rows = cur.fetchall()
    total = 0
    for cat_id, event_type, ai_sentiment in rows:
        total += build_for_catalyst(cur, cat_id, event_type or "other", ai_sentiment)
    return total


def incremental(cur) -> int:
    """增量：仅处理新 catalyst（与 backfill 逻辑相同，因 backfill 已排除已推导的）。"""
    return backfill_all(cur)


def restrength_all(cur, limit: int | None = None) -> int:
    """存量 impact 全量重算 strength（审计 2026-10-02 第一步改造）。

    背景：本次把 impact_strength 从纯 event_type 静态查表改为
    「event_type 基础档 × 关联资产最小市值升档」。backfill_all 只补
    「无 impact 记录」的新 catalyst（NOT EXISTS），存量 1 万+ 条 impact
    不会自动按新口径重算。本函数遍历全部既有 impact，重新读取 event_type
    与关联资产市值，按新逻辑重写 strength（ON CONFLICT DO UPDATE 幂等；
    mcap 只上调不降档，不会把旧 strong 降回去）。
    """
    # 分批取 catalyst_id，避免一次性载入过多
    cur.execute("SELECT DISTINCT catalyst_id FROM biz.catalyst_impact ORDER BY catalyst_id")
    cat_ids = [r[0] for r in cur.fetchall()]
    if limit:
        cat_ids = cat_ids[:limit]

    done = 0
    for cid in cat_ids:
        cur.execute(
            "SELECT COALESCE(ai_event_type, rule_event_type, 'other'), ai_sentiment "
            "FROM biz.asset_catalyst WHERE catalyst_id = %s",
            (cid,),
        )
        row = cur.fetchone()
        if not row:
            continue
        done += build_for_catalyst(cur, cid, row[0] or "other", row[1])
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂事件因子化（规则推导）")
    parser.add_argument("--backfill", action="store_true", help="一次性回填所有未推导的 catalyst")
    parser.add_argument("--incremental", action="store_true", help="增量：仅处理新 catalyst")
    parser.add_argument("--restrength", action="store_true",
                        help="存量 impact 全量重算 strength（2026-10-02 mcap 并入后刷新历史）")
    parser.add_argument("--catalyst-id", type=int, help="单条 catalyst ID 推导")
    args = parser.parse_args()

    if not args.backfill and not args.incremental and not args.catalyst_id and not args.restrength:
        parser.print_help()
        return 1

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            if args.catalyst_id:
                cur.execute(
                    "SELECT ai_event_type, ai_sentiment FROM biz.asset_catalyst WHERE catalyst_id = %s",
                    (args.catalyst_id,),
                )
                row = cur.fetchone()
                if not row:
                    print(f"catalyst {args.catalyst_id}: not found")
                    return 1
                event_type = row[0] or "other"
                ai_sentiment = row[1]
                n = build_for_catalyst(cur, args.catalyst_id, event_type, ai_sentiment)
                conn.commit()
                print(f"catalyst {args.catalyst_id} ({event_type}, sentiment={ai_sentiment}): {n} impacts upserted")
                return 0

            if args.backfill:
                n = backfill_all(cur)
                conn.commit()
                print(f"backfill done: {n} impacts upserted")
                return 0

            if args.incremental:
                n = incremental(cur)
                conn.commit()
                print(f"incremental done: {n} impacts upserted")
                return 0

            if args.restrength:
                n = restrength_all(cur)
                conn.commit()
                print(f"restrength done: {n} impacts upserted")
                return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
