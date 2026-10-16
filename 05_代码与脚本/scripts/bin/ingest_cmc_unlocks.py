#!/usr/bin/env python3
"""抓取 CMC token-unlocks 页 → 落 biz.asset_unlock_next_cmc（CM-UNLOCK-001）。

为什么用它
----------
1. **覆盖更广**：CMC 收录 1326 币的解锁排期（其中 931 币有「下一次解锁」），
   远超事件级受益人现有源（app.tokenomics.com hero，~349 资产）。
2. **类别构成是事件级**：每币 `nextUnlockedDetail[]` 直接给
   `allocationName`（Team / Investors / Ecosystem / Treasury / Advisors / Community …）
   —— 即「下一次解锁是谁在解锁」，正是 tokenomics hero 给的同一层信息。
3. **两个新信号**（此前完全没有）：
   - `next_unlock_usd`          下次解锁的美元金额
   - `next_unlock_pct_supply`   下次解锁量占流通供应比（**解锁压力定量化**）

数据是 SSR 静态内嵌（`<script id="__NEXT_DATA__">` 里 react-query 缓存的
queryKey `token-unlocks-page-table` 查询结果），**纯 HTTP 即可，无需浏览器**。
分页：`?page=N`（每页 ~100 行，1326 行 ≈ 14 页）。API `/data-api/v3/token-unlock/listing`
的 `start` 参数在当前版本不可用，故走网页分页。

映射：`cmc cryptoId` → `biz.coin_basic.cmc_id` → `asset_id`。
**必须用 cmc_id 而非 symbol**：同名多 asset_id 极常见（如 ARB 在 core.asset 有 3 行，
靠 cmc_id=11841 才能精确命中真正那个）。

分桶：复用 `bin/build_allocation_mix.py` 的 `classify()` / `BUCKET_RULES`（单一数据源）。

质控：源站偶发荒谬的「下一次解锁日」（实测 JUP=2123-01-01、P2PS=2035-12-25、
      RLC=2035-04-01、ULTIMA=2033-09-01、WOO=2032-10-31）。**超过 today+5 年**的
      一律判无效 → 该资产的解锁字段（日期/金额/占供应比/类别构成）**整条置空落库**，
      用 NULL 覆盖历史污染，下游按「无有效下次解锁」处理；币的行情字段（供应/排名/
      价格）保留，`raw` 留源站原始行备查。统计见 summary 的 `invalid_date_blanked`。

用法
----
    python bin/ingest_cmc_unlocks.py --dry-run          # 只抓不写，打印统计
    python bin/ingest_cmc_unlocks.py --pages 3          # 只抓前 3 页
    python bin/ingest_cmc_unlocks.py                    # 全量（~14 页）
    python bin/ingest_cmc_unlocks.py --json             # 输出 JSON 摘要
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from build_allocation_mix import classify  # noqa: E402  ← 单一数据源：8 桶词表

PAGE_URL = "https://coinmarketcap.com/token-unlocks/?page={page}"
LIST_URL = "https://coinmarketcap.com/token-unlocks/"
SOURCE = "cmc_token_unlocks"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 直连（绕开环境里的 HTTP(S)_PROXY：CMC 为全球 CDN，实测直连可达且更稳）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ── 源站脏值防护 ──────────────────────────────────────────────────────────
# 实测源站会给出荒谬的「下一次解锁日」：JUP = 2123-01-01（却带 $505M）、
# P2PS 2035-12-25、RLC 2035-04-01、ULTIMA 2033-09-01、WOO 2032-10-31。
# 超过 today + MAX_FUTURE_YEARS 的一律视为无效 → 该资产的解锁相关字段
# **整条置空落库**（覆盖旧值），确保下游（MV / 报告 / reaction 兜底）不会
# 读到假压力；币的基本行情字段（供应/排名/价格）仍保留，且 raw 留原始 JSON 备查。
MAX_FUTURE_YEARS = 5

_UPSERT = """
INSERT INTO biz.asset_unlock_next_cmc (
    asset_id, cmc_id, symbol, slug, name, cmc_rank,
    next_unlock_date, next_unlock_amount, next_unlock_usd, next_unlock_pct_supply,
    total_unlocked_pct, circulating_supply, total_supply, max_supply,
    allocations, vesting_types, bucket_pct, dominant_bucket, insider_pct,
    price_usd, market_cap_usd, self_reported,
    as_of, source, source_url, raw, updated_at
) VALUES (
    %(asset_id)s, %(cmc_id)s, %(symbol)s, %(slug)s, %(name)s, %(cmc_rank)s,
    %(next_unlock_date)s, %(next_unlock_amount)s, %(next_unlock_usd)s, %(next_unlock_pct_supply)s,
    %(total_unlocked_pct)s, %(circulating_supply)s, %(total_supply)s, %(max_supply)s,
    %(allocations)s, %(vesting_types)s, %(bucket_pct)s, %(dominant_bucket)s, %(insider_pct)s,
    %(price_usd)s, %(market_cap_usd)s, %(self_reported)s,
    %(as_of)s, %(source)s, %(source_url)s, %(raw)s, NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    cmc_id = EXCLUDED.cmc_id, symbol = EXCLUDED.symbol, slug = EXCLUDED.slug,
    name = EXCLUDED.name, cmc_rank = EXCLUDED.cmc_rank,
    next_unlock_date = EXCLUDED.next_unlock_date,
    next_unlock_amount = EXCLUDED.next_unlock_amount,
    next_unlock_usd = EXCLUDED.next_unlock_usd,
    next_unlock_pct_supply = EXCLUDED.next_unlock_pct_supply,
    total_unlocked_pct = EXCLUDED.total_unlocked_pct,
    circulating_supply = EXCLUDED.circulating_supply,
    total_supply = EXCLUDED.total_supply, max_supply = EXCLUDED.max_supply,
    allocations = EXCLUDED.allocations, vesting_types = EXCLUDED.vesting_types,
    bucket_pct = EXCLUDED.bucket_pct, dominant_bucket = EXCLUDED.dominant_bucket,
    insider_pct = EXCLUDED.insider_pct,
    price_usd = EXCLUDED.price_usd, market_cap_usd = EXCLUDED.market_cap_usd,
    self_reported = EXCLUDED.self_reported,
    as_of = EXCLUDED.as_of, source = EXCLUDED.source, source_url = EXCLUDED.source_url,
    raw = EXCLUDED.raw, updated_at = NOW()
"""


# ── 抓取 ────────────────────────────────────────────────────────────────
def fetch(url: str, timeout: int = 40, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
            raw = _OPENER.open(req, timeout=timeout).read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return raw.decode("utf-8", "replace")
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise last if last else RuntimeError("fetch failed")


def parse_page(html: str) -> dict:
    """从 __NEXT_DATA__ 取出 token-unlocks-page-table 查询结果。"""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("__NEXT_DATA__ not found (页面结构可能变化)")
    data = json.loads(m.group(1))
    queries = (data.get("props", {}).get("dehydratedState", {}) or {}).get("queries", []) or []
    hit = [q for q in queries if "token-unlocks" in json.dumps(q.get("queryKey", []))]
    if not hit:
        raise RuntimeError("token-unlocks-page-table query not found")
    return hit[0].get("state", {}).get("data") or {}


def scrape(pages: int | None, sleep: float) -> tuple[list[dict], int]:
    """翻页抓取全部行；返回 (rows, total_count)。"""
    rows: list[dict] = []
    seen: set = set()
    total = 0
    page = 1
    while True:
        D = parse_page(fetch(PAGE_URL.format(page=page)))
        total = D.get("totalCount") or total
        L = D.get("tokenUnlockList") or []
        new = [r for r in L if r.get("cryptoId") not in seen]
        for r in new:
            seen.add(r.get("cryptoId"))
        rows.extend(new)
        print(f"  page {page:2}: rows={len(L):3} new={len(new):3} cum={len(rows):4} / total={total}")
        last = (not L) or (pages is not None and page >= pages) or (total and page * 100 >= total) or page >= 40
        if last:
            break
        page += 1
        time.sleep(sleep)
    return rows, total


# ── 归桶 ────────────────────────────────────────────────────────────────
def bucket_breakdown(details: list[dict]) -> tuple[dict | None, dict | None, str | None, float | None, list[str]]:
    """nextUnlockedDetail → (原始明细, 桶占比, 主导桶, 内部人占比, vesting_types)。"""
    if not details:
        return None, None, None, None, []
    agg: dict[str, float] = {}
    weight_sum = 0.0
    vesting: list[str] = []
    for d in details:
        name = (d.get("allocationName") or "").strip()
        b = classify(name)
        w = d.get("tokenAmountUsd")
        if w is None or w <= 0:
            w = d.get("tokenAmount") or 0
        try:
            w = float(w)
        except (TypeError, ValueError):
            w = 0.0
        agg[b] = agg.get(b, 0.0) + w
        weight_sum += w
        vt = d.get("vestingType")
        if vt and vt not in vesting:
            vesting.append(vt)
    if weight_sum <= 0:  # 权重全缺失 → 等权
        agg = {b: 1.0 for b in agg}
        weight_sum = float(len(agg))
    pct = {b: round(w / weight_sum, 6) for b, w in sorted(agg.items(), key=lambda kv: -kv[1])}
    dominant = max(pct.items(), key=lambda kv: kv[1])[0] if pct else None
    insider = round(pct.get("team", 0.0) + pct.get("investor", 0.0), 6)
    return details, pct, dominant, insider, vesting


def to_date(ms) -> date | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def build_record(row: dict, asset_id: int, as_of: date,
                 max_future: date) -> tuple[dict | None, str]:
    """构造 upsert 记录；返回 (record, flag)，flag ∈ {'ok','no_next','invalid_date'}。

    - `no_next`      ：源站无下一次解锁 → 无快照意义，调用方跳过（不落库）。
    - `invalid_date` ：解锁日超出 `max_future`（源站脏值，见 MAX_FUTURE_YEARS 注释）
                       → **解锁相关字段全部置空但整行照常落库**，用 NULL 覆盖旧值，
                       既清掉历史污染，也向「无有效下次解锁」对齐。
    """
    nu = row.get("nextUnlocked") or {}
    nd = to_date(nu.get("date"))
    if nd is None:
        return None, "no_next"  # 「无下一次解锁」= 无快照意义，跳过
    bad_date = nd > max_future
    if bad_date:
        alloc, pct, dominant, insider, vesting = None, None, None, None, []
    else:
        alloc, pct, dominant, insider, vesting = bucket_breakdown(row.get("nextUnlockedDetail") or [])
    q = (row.get("quotes") or [{}])[0]
    rec = {
        "asset_id": asset_id,
        "cmc_id": row.get("cryptoId"),
        "symbol": (row.get("symbol") or "")[:32],
        "slug": (row.get("slug") or "")[:96],
        "name": (row.get("name") or "")[:160],
        "cmc_rank": row.get("cmcRank"),
        "next_unlock_date": None if bad_date else nd,
        "next_unlock_amount": None if bad_date else nu.get("tokenAmount"),
        "next_unlock_usd": None if bad_date else nu.get("tokenAmountUsd"),
        "next_unlock_pct_supply": None if bad_date else nu.get("tokenAmountPercentage"),
        "total_unlocked_pct": row.get("totalUnlockedPercentage"),
        "circulating_supply": row.get("circulatingSupply"),
        "total_supply": row.get("totalSupply"),
        "max_supply": row.get("maxSupply"),
        "allocations": json.dumps(alloc, ensure_ascii=False) if alloc else None,
        "vesting_types": vesting or None,
        "bucket_pct": json.dumps(pct, ensure_ascii=False) if pct else None,
        "dominant_bucket": dominant,
        "insider_pct": insider,
        "price_usd": q.get("price"),
        "market_cap_usd": q.get("marketCap"),
        "self_reported": row.get("selfReported"),
        "as_of": as_of,
        "source": SOURCE,
        "source_url": LIST_URL,
        # raw 始终保留源站原始行（脏值也留档，便于事后追溯）
        "raw": json.dumps({**row, "_qc_invalid_next_unlock_date": nd.isoformat()} if bad_date else row,
                          ensure_ascii=False),
    }
    return rec, ("invalid_date" if bad_date else "ok")


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 CMC token-unlocks → biz.asset_unlock_next_cmc")
    ap.add_argument("--pages", type=int, default=None, help="只抓前 N 页（默认全量）")
    ap.add_argument("--sleep", type=float, default=1.0, help="翻页间隔秒（默认 1.0，礼貌爬取）")
    ap.add_argument("--dry-run", action="store_true", help="只抓不算不写")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args()

    t0 = time.time()
    rows, total = scrape(args.pages, args.sleep)
    print(f"\n[抓取] {len(rows)} 行 / totalCount={total}   用时 {time.time() - t0:.0f}s")

    # 映射 cmc_id → asset_id
    url = get_settings(require_database=True).database_url
    with get_connection(url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cmc_id, asset_id FROM biz.coin_basic
                WHERE cmc_id IS NOT NULL AND asset_id IS NOT NULL
            """)
            cmap = {r[0]: r[1] for r in cur.fetchall()}
        print(f"[映射] coin_basic cmc_id→asset_id 字典 {len(cmap)} 条")

        as_of = date.today()
        max_future = as_of + timedelta(days=365 * MAX_FUTURE_YEARS + 2)
        recs, unmapped, no_next, invalid = [], 0, 0, 0
        for row in rows:
            aid = cmap.get(row.get("cryptoId"))
            if aid is None:
                unmapped += 1
                continue
            rec, flag = build_record(row, aid, as_of, max_future)
            if flag == "no_next":
                no_next += 1
                continue
            if flag == "invalid_date":
                invalid += 1
            recs.append(rec)

        print(f"[构造] 可落库 {len(recs)} ｜ 无下一次解锁跳过 {no_next} ｜ cmc_id 未映射 {unmapped}")
        if invalid:
            print(f"[质控] 解锁日超出 today+{MAX_FUTURE_YEARS} 年 → 解锁字段置空（源站脏值）{invalid} 条")

        written = 0
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.executemany(_UPSERT, recs)
            written = len(recs)

    # 统计
    dom: dict[str, int] = {}
    for r in recs:
        if r["dominant_bucket"]:
            dom[r["dominant_bucket"]] = dom.get(r["dominant_bucket"], 0) + 1
    dates = sorted(r["next_unlock_date"] for r in recs if r["next_unlock_date"] is not None)
    summary = {
        "fetched": len(rows),
        "total_count": total,
        "mapped": len(recs),
        "unmapped_cmc_id": unmapped,
        "skipped_no_next_unlock": no_next,
        "invalid_date_blanked": invalid,
        "written": written,
        "dry_run": args.dry_run,
        "dominant_bucket": dict(sorted(dom.items(), key=lambda kv: -kv[1])),
        "next_unlock_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print(f"\n[落库] 写入 {written} 行（dry-run={args.dry_run}）")
        print("[主导桶分布]", json.dumps(summary["dominant_bucket"], ensure_ascii=False))
        if dates:
            print(f"[解锁日期] {dates[0]} → {dates[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
