#!/usr/bin/env python3
"""把「解锁受益人」从资产级下沉到事件级（UNLOCK-REACTION 补齐 · 第三刀）。

背景
----
上一轮（build_allocation_mix.py）已把 `biz.asset_unlock_event.beneficiary_type` 从全空
补到**资产级推断**（用 biz.asset_tokenomics.allocation_json 的整条生命周期分配算 dominant
bucket）。但投研 SOP 真正要回答的是：**下一次解锁，到底是谁的筹码在解锁？**

数据源（2026-10-16 实测）
------------------------
✅ `https://app.tokenomics.com/tokenomics/{slug}/unlocks` —— 纯 HTTP（gzip）即可取，
   hero「Unlock Recipients」区块直接含**下一次解锁**的类别构成：
       Private Investors 35.0% / Insiders 53.8% / Foundation 11.2%
   另有 Next Unlock 日期 / USD 值 / 代币量，且页面带 ticker 可做身份校验。
❌ `tokenomist.ai` 被 Cloudflare 挑战拦截（headless Playwright 也过不去，实测标题
   仍为 "Just a moment..."）→ 其历史事件级类别暂不可得，故本脚本只覆盖**下一期**。
   历史事件继续沿用资产级推断（source='asset_level_inferred'），本脚本不去动它们。

✅ 对于源站是 tokenomist / tokenomist.ai 的币（其 slug 在 tokenomics.com 上 404），
   复用**既有主爬虫** `phase_chain_token_unlocks._search_tokenomist_slug` 的搜索 API
   反查 tokenomics.com 真实 slug（含项目名消歧），再抓 hero——不重复造轮子。
   旧版实现只覆盖 source_url=tokenomics.com 的 491 币；现覆盖全量有未来解锁的币。

产出
----
1. `biz.asset_unlock_recipient_mix`：完整构成（资产 × 解锁日 × 类别，幂等 upsert）。
2. 命中事件行的 `biz.asset_unlock_event` 回填：
   - `beneficiary_type`      = 本次解锁占比最大的类别归一化桶
   - `beneficiary_source`    = 'tokenomics_unlock_hero'（区别于 asset_level_inferred）
   - `raw_ref.tokenomics_recipients` = 完整构成快照
   ⚠️ build_allocation_mix.py 只覆盖 source IS NULL / 'unspecified' 的行，不会冲掉本回填。

桶词表与 build_allocation_mix.py 保持一致（team/investor/community/ecosystem/foundation/
public/liquidity/other）；tokenomics hero 的超级组里 'Insiders' 按官方口径（Insiders =
Team and Advisors）归入 `team`。

用法
----
    python bin/backfill_unlock_recipients.py --dry-run          # 只抓不算不写
    python bin/backfill_unlock_recipients.py --no-resolve       # 只抓 tokenomics 源的币
    python bin/backfill_unlock_recipients.py --limit 30         # 抓前 30（按市值）
    python bin/backfill_unlock_recipients.py                    # 全量（含 slug 反查）
    python bin/backfill_unlock_recipients.py --symbol ARB,ONDO  # 指定币
    python bin/backfill_unlock_recipients.py --json
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 复用既有的 8 桶词表（单一数据源，改词表只改 build_allocation_mix.py）
from build_allocation_mix import classify  # noqa: E402

SOURCE_CODE = "tokenomics_unlock_hero"
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept-Encoding": "gzip",
}
BASE = "https://app.tokenomics.com/tokenomics/{slug}/unlocks"

# tokenomics hero 的超级组 → 8 桶（未命中的交给 classify() 兜底）
SUPERGROUP_ALIAS = {
    "insiders": "team",          # 官方口径：Insiders = Team and Advisors
    "private investors": "investor",
    "foundation": "foundation",
    "community": "community",
    "ecosystem": "ecosystem",
    "public": "public",
    "liquidity": "liquidity",
}


def bucket_of(category: str) -> str:
    key = (category or "").strip().lower()
    if key in SUPERGROUP_ALIAS:
        return SUPERGROUP_ALIAS[key]
    b = classify(category)
    return b or "other"


# ── 解析 ────────────────────────────────────────────────────────────────
RE_RECIP_BLOCK = re.compile(r'aria-label="Unlock recipients"(.{0,4000}?)</section>', re.S)
RE_RECIP_ITEM = re.compile(
    r'>([A-Za-z][^<]{1,60}?)<span class="font-semibold tabular-nums text-content-1">([\d.]+)%</span>'
)
RE_NEXT_DATE = re.compile(r'Next Unlock[^<]{0,24}<!-- -->\s*<span>([^<]+)</span>')
RE_TICKER = re.compile(r'<h2 class="[^"]*">([^<]+)<span class="[^"]*">([^<]*)</span></h2>')


def fetch_html(url: str, timeout: int = 40, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 4xx 是确定性错误（404 = 该站没收录这个 slug），重试纯属浪费 4.5s 退避
            if 400 <= e.code < 500:
                raise
            last = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
        except Exception as e:  # 代理/SSL 抖动是常态（实测 ZRO 偶发 SSL EOF），退避重试
            last = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise last if last else RuntimeError("fetch failed")


def parse_hero(html: str) -> dict:
    """解析 hero 区块 → {ticker, name, next_unlock_date, recipients[]}"""
    out: dict = {"ticker": None, "name": None, "next_unlock_date": None, "recipients": []}
    m = RE_TICKER.search(html)
    if m:
        out["name"] = m.group(1).strip()
        out["ticker"] = m.group(2).strip()
    d = RE_NEXT_DATE.search(html)
    if d:
        out["next_unlock_date"] = d.group(1).strip()
    blk = RE_RECIP_BLOCK.search(html)
    if blk:
        for name, pct in RE_RECIP_ITEM.findall(blk.group(1)):
            out["recipients"].append({"category": name.strip(), "pct": float(pct)})
    return out


def parse_hero_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            continue
    return None


# ── 候选池 ──────────────────────────────────────────────────────────────
# 两类资产都要覆盖：
#   (a) 源站 = app.tokenomics.com  → slug 直接从 source_url 正则取出（实测 491 个）；
#   (b) 源站 = tokenomist / tokenomist.ai → 其 slug（bitcoin / solana …）在
#       tokenomics.com 上一律 404，slug 置 NULL，交给 resolve_slug() 用**既有主爬虫**
#       phase_chain_token_unlocks._search_tokenomist_slug 的搜索 API 反查真实 slug。
CAND_SQL = r"""
SELECT DISTINCT ON (e.asset_id)
       e.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
       a.market_cap_rank,
       CASE WHEN t.source_url LIKE 'https://app.tokenomics.com/tokenomics/%%'
            THEN regexp_replace(t.source_url,
                     '^https://app\.tokenomics\.com/tokenomics/([^/?#]+).*$', '\1')
            ELSE NULL END AS slug,
       t.source_url,
       e.unlock_date
FROM biz.asset_unlock_event e
JOIN core.asset a               ON a.asset_id = e.asset_id
JOIN biz.asset_token_unlocks t  ON t.asset_id = e.asset_id
WHERE e.unlock_date >= CURRENT_DATE
  AND (%(symbols)s::text[] IS NULL OR a.canonical_symbol = ANY(%(symbols)s::text[]))
  AND (%(asset_ids)s::bigint[] IS NULL OR e.asset_id = ANY(%(asset_ids)s::bigint[]))
ORDER BY e.asset_id, e.unlock_date ASC
"""


def _search_slug_loose(symbol: str, name: str) -> str | None:
    """宽松解析：搜索 API 中按 **ticker 精确匹配**取 slug（忽略项目名）。

    用于 tokenomics.com 项目名与本库名不一致的场景（实测 WLD：本库 'Worldcoin' vs
    该站 'World Network' → 主爬虫的严格名称消歧会误判为「歧义」而放弃）。
    同名 ticker 串项目的风险由主循环的**页面 ticker 校验**兜住（不符即 SKIP，不写库）。
    """
    try:
        import requests
        from phase_chain_token_unlocks import _parse_search_results
    except Exception:
        return None
    tgt = (symbol or "").strip().upper()
    if not tgt:
        return None
    for q in (symbol, name):
        if not q:
            continue
        try:
            resp = requests.get(
                "https://app.tokenomics.com/api/search/audits",
                params={"q": q, "limit": 50},
                headers={"Accept": "application/json"}, timeout=10,
            )
            if resp.status_code != 200:
                continue
            res = _parse_search_results(resp.json())
        except Exception:
            continue
        for x in res:
            if (x.get("ticker") or "").strip().upper() == tgt:
                slug = (x.get("project_slug") or x.get("slug") or "").strip()
                if slug:
                    return slug
    return None


def resolve_slug(symbol: str, name: str) -> str | None:
    """用**既有主爬虫**的 slug 解析器反查 tokenomics.com 的真实 slug。

    复用 phase_chain_token_unlocks._search_tokenomist_slug（搜索 API + 项目名消歧），
    不重复造轮子。延迟导入：该模块顶部会拉起 playwright，仅在真正需要解析时才导入。
    严格消歧失败时退回 **宽松 ticker 匹配**（覆盖项目名不一致的币，如 Worldcoin/WLD）。
    """
    try:
        from phase_chain_token_unlocks import _search_tokenomist_slug
    except Exception:
        _search_tokenomist_slug = None  # type: ignore
    if _search_tokenomist_slug is not None:
        try:
            s = _search_tokenomist_slug(symbol or "", name or "")
            if s:
                return s
        except Exception:
            pass
    return _search_slug_loose(symbol, name)


def _name_like(page_name: str, asset_name: str) -> bool:
    """页面项目名与本库资产名是否近似（归一化后互相包含）。

    用于「同 symbol 多义」时消歧（如 UP 在本库有 Unitas / Superform 等 10 行）。
    """
    def _n(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())
    a, b = _n(page_name), _n(asset_name)
    return bool(a and b and (a in b or b in a))


def load_symbol_counts(conn) -> dict:
    """{SYMBOL: 该 symbol 在 core.asset 的行数}——>1 表示 ticker 无法唯一定位资产。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT upper(canonical_symbol), count(*) FROM core.asset
                       WHERE canonical_symbol IS NOT NULL GROUP BY 1""")
        return {s: n for s, n in cur.fetchall()}


def load_candidates(conn, symbols: list[str] | None, asset_ids: list[int] | None,
                    limit: int | None) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(CAND_SQL, {"symbols": symbols, "asset_ids": asset_ids})
        rows = list(cur.fetchall())
    # 按市值排序（次要键 rank 升序 / NULL 最后），保证 --limit 先覆盖大币
    rows.sort(key=lambda r: (r["market_cap_rank"] is None, r["market_cap_rank"] or 0))
    if limit is not None and limit > 0:
        rows = rows[:limit]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="解锁受益人事件级下沉（tokenomics hero）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理 N 个资产（按市值优先）")
    ap.add_argument("--symbol", help="逗号分隔的币种符号，如 ARB,ONDO")
    ap.add_argument("--asset-id", help="逗号分隔的 asset_id")
    ap.add_argument("--sleep", type=float, default=0.4, help="每个请求间隔秒（默认 0.4）")
    ap.add_argument("--timeout", type=int, default=40)
    ap.add_argument("--retries", type=int, default=3, help="单页失败重试次数（默认 3）")
    ap.add_argument("--no-resolve", action="store_true",
                    help="不反查 slug：只抓源站已是 app.tokenomics.com 的币（跳过 tokenomist 源）")
    ap.add_argument("--dry-run", action="store_true", help="只抓不写库")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbol.split(",")] if args.symbol else None
    asset_ids = [int(x) for x in args.asset_id.split(",")] if args.asset_id else None

    url = get_settings(require_database=True).database_url
    with get_connection(url) as conn:
        cands = load_candidates(conn, symbols, asset_ids, args.limit)
        sym_counts = load_symbol_counts(conn)

    print(f"[候选] {len(cands)} 个资产（有未来解锁 + 有 slug）")
    if not cands:
        return 0

    fetched, skipped, failed = [], [], []
    t0 = time.time()
    n_resolved = 0
    for i, c in enumerate(cands, 1):
        slug = (c["slug"] or "").strip()
        loose = False  # True = slug 由宽松 ticker 匹配反查而来（需额外日期校验兜底）
        # 非 tokenomics 源（tokenomist / tokenomist.ai）没有可用 slug → 复用主爬虫解析器反查
        if not slug and not args.no_resolve:
            slug = resolve_slug(c["symbol"], c["name"]) or ""
            if slug:
                loose = True
                n_resolved += 1
                print(f"  [{i}/{len(cands)}] {c['symbol']:10} slug 反查 → {slug}")
            time.sleep(0.2)
        if not slug:
            skipped.append({**c, "reason": "slug_unresolved"})
            print(f"  [{i}/{len(cands)}] {c['symbol']:10} SKIP 无 slug（tokenomics 未收录 / 反查失败）")
            continue
        url_i = BASE.format(slug=slug)
        try:
            html = fetch_html(url_i, timeout=args.timeout, retries=args.retries)
            hero = parse_hero(html)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                skipped.append({**c, "reason": "not_on_tokenomics"})
                print(f"  [{i}/{len(cands)}] {c['symbol']:10} SKIP 404（slug={slug} 未收录）")
            else:
                failed.append({"asset_id": c["asset_id"], "symbol": c["symbol"], "reason": f"HTTP {e.code}"})
                print(f"  [{i}/{len(cands)}] {c['symbol']:10} ERR HTTP {e.code}")
            time.sleep(args.sleep)
            continue
        except Exception as e:
            failed.append({"asset_id": c["asset_id"], "symbol": c["symbol"], "reason": str(e)[:120]})
            print(f"  [{i}/{len(cands)}] {c['symbol']:10} ERR {str(e)[:80]}")
            time.sleep(args.sleep)
            continue

        tick = (hero["ticker"] or "").strip().upper()
        sym = (c["symbol"] or "").strip().upper()
        if not hero["recipients"]:
            skipped.append({**c, "reason": "no_recipients"})
            print(f"  [{i}/{len(cands)}] {sym:10} SKIP 无 recipients 区块（slug={slug}）")
        elif tick and sym and tick != sym:
            # 防同名 slug 串项目（历史踩坑：TUT 两个项目共用 slug）
            skipped.append({**c, "reason": f"ticker_mismatch:{tick}!={sym}"})
            print(f"  [{i}/{len(cands)}] {sym:10} SKIP ticker 不符：页面={tick}")
        else:
            hd = parse_hero_date(hero["next_unlock_date"])
            if loose:
                # 宽松 ticker 匹配：两道闸防串项目
                #   闸1 日期：hero 的 Next Unlock 必须 == 本库下一解锁日
                #   闸2 名称：该 symbol 在本库有多行（同名多义）时，还须页面项目名≈本库名
                #           （ticker 无法消歧：实测 UP 在本库有 Unitas/Superform 等 10 行）
                sym_n = sym_counts.get(sym, 0)
                name_ok = _name_like(hero.get("name"), c["name"])
                if hd != c["unlock_date"]:
                    skipped.append({**c, "reason": f"loose_date_mismatch:hero={hd}"})
                    print(f"  [{i}/{len(cands)}] {sym:10} SKIP 宽松解析日期不吻合"
                          f"（hero={hd} vs DB={c['unlock_date']}, slug={slug}）")
                    time.sleep(args.sleep)
                    continue
                if sym_n > 1 and not name_ok:
                    skipped.append({**c, "reason": f"loose_name_mismatch:page={hero.get('name')}"})
                    print(f"  [{i}/{len(cands)}] {sym:10} SKIP 宽松解析名称不符且同 symbol 多义"
                          f"（页面={hero.get('name')} vs 本库={c['name']}, slug={slug}）")
                    time.sleep(args.sleep)
                    continue
            recs = [{"category": r["category"], "bucket": bucket_of(r["category"]),
                     "pct": r["pct"]} for r in hero["recipients"]]
            total = sum(r["pct"] for r in recs) or 0.0
            dom = max(recs, key=lambda r: r["pct"]) if recs else None
            fetched.append({
                "asset_id": c["asset_id"], "symbol": sym, "slug": slug,
                "event_unlock_date": c["unlock_date"], "hero_date": hd,
                "hero_date_raw": hero["next_unlock_date"], "recipients": recs,
                "pct_sum": round(total, 3),
                "dominant_bucket": dom["bucket"] if dom else None,
                "date_match": (hd is not None and hd == c["unlock_date"]),
                "source_url": url_i,
            })
            print(f"  [{i}/{len(cands)}] {sym:10} OK {hero['next_unlock_date']} "
                  f"{len(recs)} 类 主导={dom['bucket'] if dom else '-'}"
                  f"{'' if (hd == c['unlock_date']) else '  ⚠日期与事件表不一致'}")
        time.sleep(args.sleep)

    elapsed = time.time() - t0
    n_ok = len(fetched)
    n_dated = sum(1 for r in fetched if r["date_match"])
    print(f"\n[抓取] 成功 {n_ok} / 跳过 {len(skipped)} / 失败 {len(failed)}"
          f"   用时 {elapsed:.0f}s ｜ slug 反查 {n_resolved}")

    written = {"mix_rows": 0, "event_rows": 0, "mix_only": 0}
    if not args.dry_run and fetched:
        now = datetime.now()
        mix_params, ev_params = [], []
        for r in fetched:
            d = r["hero_date"] or r["event_unlock_date"]
            for rec in r["recipients"]:
                mix_params.append((r["asset_id"], d, rec["category"], rec["bucket"],
                                   rec["pct"], SOURCE_CODE, r["source_url"], now))
            if r["date_match"] and r["dominant_bucket"]:
                patch = {
                    "tokenomics_recipients": r["recipients"],
                    "source_url": r["source_url"],
                    "scraped_at": now.isoformat(timespec="seconds"),
                }
                ev_params.append((r["dominant_bucket"], SOURCE_CODE,
                                  json.dumps(patch, ensure_ascii=False),
                                  r["asset_id"], r["event_unlock_date"]))
            else:
                written["mix_only"] += 1

        with get_connection(url) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO biz.asset_unlock_recipient_mix
                        (asset_id, unlock_date, category, bucket, pct,
                         source_code, source_url, scraped_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (asset_id, unlock_date, category) DO UPDATE SET
                        bucket = EXCLUDED.bucket,
                        pct = EXCLUDED.pct,
                        source_code = EXCLUDED.source_code,
                        source_url = EXCLUDED.source_url,
                        scraped_at = EXCLUDED.scraped_at
                    """,
                    mix_params,
                )
                written["mix_rows"] = len(mix_params)

                cur.executemany(
                    """
                    UPDATE biz.asset_unlock_event e
                    SET beneficiary_type = %s,
                        beneficiary_source = %s,
                        raw_ref = COALESCE(e.raw_ref, '{}'::jsonb) || %s::jsonb,
                        updated_at = NOW()
                    WHERE e.asset_id = %s AND e.unlock_date = %s
                    """,
                    ev_params,
                )
                cur.execute(
                    "SELECT count(*) FROM biz.asset_unlock_event WHERE beneficiary_source = %s",
                    (SOURCE_CODE,),
                )
                written["event_rows"] = int(cur.fetchone()[0])
        print(f"[落库] mix {written['mix_rows']} 行 ｜ 累计事件级受益人行 {written['event_rows']} "
              f"｜ 仅落 mix（日期不匹配）{written['mix_only']}")

    if args.json:
        print(json.dumps({
            "candidates": len(cands), "ok": n_ok, "skipped": skipped, "failed": failed,
            "date_matched": n_dated, "written": written,
            "sample": fetched[:10],
        }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
