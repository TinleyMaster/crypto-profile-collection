"""
单币社交热度采集：社区规模 + 实时舆情 + 趋势新闻 + 市场热度，加权综合评分。

数据源（全部免费公开 API）：
  - 社区规模：CoinGecko /coins/{id}（community_data）
  - 实时舆情：Reddit 搜索 JSON + LLM 情绪分析
  - 趋势新闻：CoinGecko /search/trending + /coins/{id}/status_updates
  - 市场热度：CoinGecko /coins/{id}（market_data：成交量/涨跌幅/市值排名）

用法：
    python phase_c_social_heat.py --asset-id 1234
    python phase_c_social_heat.py --asset-id 1234 --save  # 写入数据库

输出约定：最终结果 print(json.dumps({"status": ...})) 单行输出到 stdout，
进度日志走 stderr。status 取值：
  - ok        采集成功（含综合评分与四维度明细）
  - not_found 无 CoinGecko 映射且各源均无数据
  - error     异常失败
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import quote

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

REQUEST_TIMEOUT = 8  # 单次请求超时（秒）

# 综合评分权重（缺失维度会自动剔除并重新归一化）
SCORE_WEIGHTS = {
    "community": 0.25,   # 社区规模
    "sentiment": 0.30,   # 实时舆情情绪
    "trend": 0.25,       # 趋势与新闻
    "market": 0.20,      # 市场热度
}

# 各指标的 log10 归一化上限（达到该值即得 100 分）
CAPS = {
    "twitter_followers": 1_000_000,
    "reddit_subscribers": 1_000_000,
    "telegram_channel_user_count": 100_000,
    "total_volume_usd": 1_000_000_000,
}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _f(v, default: float | None = None) -> float | None:
    """安全转 float，失败返回 default。"""
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="单币社交热度采集")
    p.add_argument("--asset-id", "--asset_id", type=int, dest="asset_id",
                   help="资产 ID（优先）")
    p.add_argument("--symbol", type=str, help="代币符号（未提供 asset-id 时使用）")
    p.add_argument("--save", action="store_true", help="写入数据库")
    return p


# ── 资产与 CoinGecko ID 解析 ──────────────────────────────

def resolve_asset(conn, asset_id: int | None, symbol: str | None) -> dict | None:
    """根据 asset_id 或 symbol 查找资产信息（含 CG coin_id）。"""
    query = """
        SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
               asm_cg.source_asset_key AS coingecko_id
        FROM core.asset a
        LEFT JOIN core.asset_source_map asm_cg
            ON asm_cg.asset_id = a.asset_id AND asm_cg.source_code = 'cg'
        WHERE {}
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if asset_id:
            cur.execute(query.format("a.asset_id = %s"), (asset_id,))
        elif symbol:
            cur.execute(query.format("UPPER(a.canonical_symbol) = UPPER(%s) LIMIT 1"), (symbol,))
        else:
            return None
        return cur.fetchone()


def _cg_get(settings, path: str, params: dict | None = None) -> dict | None:
    """带 key 与重试的 CoinGecko GET 请求。"""
    params = dict(params or {})
    headers = {"Accept": "application/json", "User-Agent": "crypto-research-ingest/1.0"}
    for key in settings.get_coingecko_keys():
        h = dict(headers)
        if key:
            h["x-cg-demo-api-key"] = key
        try:
            resp = requests.get(f"{settings.coingecko_base_url}{path}",
                                params=params, headers=h, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            _log(f"  [CG] {path} 请求失败: {e}")
    return None


def resolve_coin_id(conn, asset_id: int, symbol: str, name: str, settings) -> str | None:
    """优先用 asset_source_map 的 CG 映射，无则按 symbol 搜索。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT source_asset_key FROM core.asset_source_map "
            "WHERE asset_id = %s AND source_code = 'cg'",
            (asset_id,),
        )
        row = cur.fetchone()
    if row and row.get("source_asset_key"):
        return row["source_asset_key"]

    if not symbol:
        return None
    data = _cg_get(settings, "/search", params={"query": symbol.lower()})
    coins = (data or {}).get("coins") or []
    if not coins:
        return None
    exact = [c for c in coins if (c.get("symbol") or "").lower() == symbol.lower()]
    return (exact[0] if exact else coins[0]).get("id")


# ── 各维度数据拉取 ────────────────────────────────────────

def fetch_community_market(settings, coin_id: str) -> dict:
    """拉取社区规模 + 市场热度原始数据。"""
    out = {"community": {}, "market": {}}
    data = _cg_get(settings, f"/coins/{coin_id}", params={
        "localization": "false", "tickers": "false", "market_data": "true",
        "community_data": "true", "developer_data": "true", "sparkline": "false",
    })
    if not data:
        return out

    cd = data.get("community_data") or {}
    out["community"] = {
        "twitter_followers": _f(cd.get("twitter_followers")),
        "reddit_subscribers": _f(cd.get("reddit_subscribers")),
        "reddit_average_posts_48h": _f(cd.get("reddit_average_posts_48h")),
        "reddit_average_comments_48h": _f(cd.get("reddit_average_comments_48h")),
        "reddit_accounts_active_48h": _f(cd.get("reddit_accounts_active_48h")),
        "telegram_channel_user_count": _f(cd.get("telegram_channel_user_count")),
    }
    dev = data.get("developer_data") or {}
    out["community"]["github_stars"] = _f(dev.get("stars"))
    out["community"]["github_forks"] = _f(dev.get("forks"))

    md = data.get("market_data") or {}
    price = (md.get("current_price") or {}).get("usd")
    cap = (md.get("market_cap") or {}).get("usd")
    vol = (md.get("total_volume") or {}).get("usd")
    out["market"] = {
        "price_usd": _f(price),
        "market_cap_usd": _f(cap),
        "market_cap_rank": _f(md.get("market_cap_rank")),
        "total_volume_usd": _f(vol),
        "price_change_24h": _f(md.get("price_change_percentage_24h")),
        "price_change_7d": _f(md.get("price_change_percentage_7d")),
    }
    return out


def fetch_trending_rank(settings, coin_id: str) -> int | None:
    """返回该币在 CoinGecko 全球热搜榜的位次（1 起），未上榜返回 None。"""
    data = _cg_get(settings, "/search/trending")
    coins = (data or {}).get("coins") or []
    for idx, c in enumerate(coins):
        item = c.get("item") or {}
        if (item.get("id") or "").lower() == coin_id.lower():
            return idx + 1
    return None


def fetch_status_updates(settings, coin_id: str) -> list[dict]:
    """拉取项目动态（新闻/公告），返回 [{text, created_at}]。"""
    data = _cg_get(settings, f"/coins/{coin_id}/status_updates",
                   params={"per_page": "10", "page": "1"})
    updates = (data or {}).get("status_updates") or []
    out = []
    for u in updates:
        desc = (u.get("description") or "").strip()
        if desc:
            out.append({
                "text": desc[:500],
                "created_at": u.get("created_at", ""),
            })
    return out


def fetch_reddit_posts(symbol: str, name: str) -> list[dict]:
    """从 Reddit 搜索最近相关帖子，返回 [{title, text, subreddit, score, num_comments}]。"""
    query = symbol
    if len(symbol) < 3 and name:
        query = f"{symbol} {name}"
    url = "https://www.reddit.com/search.json"
    params = {"q": query, "sort": "new", "t": "month", "limit": "25", "raw_json": "1"}
    headers = {"User-Agent": "crypto-research/1.0 (research bot)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            _log(f"  [Reddit] 搜索失败 HTTP {resp.status_code}")
            return []
        data = resp.json()
    except Exception as e:
        _log(f"  [Reddit] 搜索失败: {e}")
        return []

    posts = []
    for child in (data.get("data", {}).get("children") or []):
        d = child.get("data") or {}
        title = (d.get("title") or "").strip()
        text = (d.get("selftext") or "").strip()
        if not title and not text:
            continue
        posts.append({
            "title": title[:300],
            "text": text[:800],
            "subreddit": d.get("subreddit", ""),
            "score": _f(d.get("score")),
            "num_comments": _f(d.get("num_comments")),
        })
    return posts[:20]


# ── LLM 情绪分析 ──────────────────────────────────────────

SENTIMENT_PROMPT = """你是一个加密货币舆情分析专家。根据给定的社交帖子与项目动态文本，判断该代币当前的市场情绪。

请只返回 JSON，格式：
{
  "sentiment": "positive|neutral|negative",
  "sentiment_score": 0-100 的数值（0=极度负面，50=中性，100=极度正面），
  "bullish_ratio": 0-100 的数值（看涨观点占比估计），
  "bearish_ratio": 0-100 的数值（看跌观点占比估计），
  "key_topics": ["讨论最多的主题", "..."],
  "summary": "一句话概括当前舆情"
}

规则：
1. 综合所有文本判断整体倾向，注意区分「讨论热度」与「看涨/看跌」。
2. key_topics 最多 5 个。
3. 若文本不足或无法判断，sentiment 返回 neutral，sentiment_score 返回 50。
4. bullish_ratio + bearish_ratio 可不为 100（其余视为中性）。"""


def analyze_sentiment(settings, symbol: str, name: str,
                      posts: list[dict], updates: list[dict]) -> dict | None:
    """用 LLM 对 Reddit 帖 + 项目动态做情绪分析。无文本时返回 None。"""
    from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

    text_parts = []
    for p in posts:
        text_parts.append(f"- [Reddit r/{p.get('subreddit', '?')}] {p.get('title', '')} {p.get('text', '')}")
    for u in updates:
        text_parts.append(f"- [动态] {u.get('text', '')}")
    if not text_parts:
        return None

    llm = LLMClient(settings)
    if not llm.is_available():
        _log("  [LLM] 未配置，跳过情绪分析")
        return None

    user_prompt = (
        f"代币: {symbol} ({name})\n\n"
        f"以下是从 Reddit 和项目动态收集到的 {len(text_parts)} 条文本：\n\n"
        + "\n".join(text_parts[:40])
    )
    try:
        raw = llm.chat(SENTIMENT_PROMPT, user_prompt, temperature=0.1, max_tokens=1024)
        data = extract_json_from_llm_response(raw)
        return {
            "sentiment": str(data.get("sentiment", "neutral")),
            "sentiment_score": _f(data.get("sentiment_score"), 50.0),
            "bullish_ratio": _f(data.get("bullish_ratio")),
            "bearish_ratio": _f(data.get("bearish_ratio")),
            "key_topics": data.get("key_topics") or [],
            "summary": str(data.get("summary", "")),
        }
    except Exception as e:
        _log(f"  [LLM] 情绪分析失败: {e}")
        return None


# ── 评分 ──────────────────────────────────────────────────

def _log_score(value: float | None, cap: float) -> float | None:
    """log10 归一化：达到 cap 得 100，<=1 得 0。"""
    if value is None or value <= 0:
        return None
    if value >= cap:
        return 100.0
    return round(max(0.0, min(100.0, (math.log10(value) / math.log10(cap)) * 100)), 1)


def _mean(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def compute_scores(community: dict, market: dict, trending_rank: int | None,
                   updates: list[dict], sentiment: dict | None) -> dict:
    """计算四维度评分与综合评分。"""
    # 社区规模：twitter / reddit / telegram 的 log 归一化均值
    community_score = _mean([
        _log_score(community.get("twitter_followers"), CAPS["twitter_followers"]),
        _log_score(community.get("reddit_subscribers"), CAPS["reddit_subscribers"]),
        _log_score(community.get("telegram_channel_user_count"), CAPS["telegram_channel_user_count"]),
    ])

    # 舆情情绪：LLM 直接给 0-100
    sentiment_score = _f(sentiment.get("sentiment_score")) if sentiment else None

    # 趋势新闻：热搜位次 + 项目动态数量
    trending_score = None
    if trending_rank:
        trending_score = max(0.0, 100 - (trending_rank - 1) * 12)
    news_score = min(50.0, len(updates) * 12.0) if updates else None
    trend_score = _mean([v for v in (trending_score, news_score) if v is not None])

    # 市场热度：成交量 + 涨跌幅绝对值 + 市值排名
    volume_score = _log_score(market.get("total_volume_usd"), CAPS["total_volume_usd"])
    change_score = None
    ch = market.get("price_change_24h")
    if ch is not None:
        change_score = round(max(0.0, min(100.0, abs(ch) * 10)), 1)
    rank_score = None
    rk = market.get("market_cap_rank")
    if rk is not None:
        rank_score = round(max(0.0, min(100.0, 100 - rk / 10)), 1)
    market_score = _mean([volume_score, change_score, rank_score])

    dim_scores = {
        "community": community_score,
        "sentiment": sentiment_score,
        "trend": trend_score,
        "market": market_score,
    }
    available = {k: v for k, v in dim_scores.items() if v is not None}

    # 综合评分：按可用维度加权，重新归一化
    score = None
    if available:
        total_w = sum(SCORE_WEIGHTS[k] for k in available)
        score = round(sum(v * SCORE_WEIGHTS[k] for k, v in available.items()) / total_w, 1)

    # 置信度：可用维度数
    n = len(available)
    confidence = "high" if n >= 4 else ("medium" if n >= 3 else "low")

    return {
        "score": score,
        "confidence": confidence,
        "score_detail": {
            "community": community_score,
            "sentiment": sentiment_score,
            "trend": trend_score,
            "market": market_score,
            "sub": {
                "trending": trending_score,
                "news": news_score,
                "volume": volume_score,
                "change": change_score,
                "rank": rank_score,
            },
        },
        "missing": [k for k, v in dim_scores.items() if v is None],
    }


# ── 存入数据库 ────────────────────────────────────────────

def ensure_table(conn) -> None:
    """确保 biz.asset_social_heat 表存在。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.asset_social_heat (
                asset_id INTEGER PRIMARY KEY REFERENCES core.asset(asset_id),
                symbol TEXT,
                score NUMERIC(6,1),
                confidence TEXT,
                community_json JSONB,
                sentiment_json JSONB,
                trend_json JSONB,
                market_json JSONB,
                score_detail_json JSONB,
                methodology_json JSONB,
                input_snapshot_json JSONB,
                fetched_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


def save_to_db(conn, asset_id: int, data: dict) -> None:
    """写入或更新 biz.asset_social_heat。"""
    sql = """
        INSERT INTO biz.asset_social_heat (
            asset_id, symbol, score, confidence,
            community_json, sentiment_json, trend_json, market_json,
            score_detail_json, methodology_json, input_snapshot_json,
            fetched_at, updated_at
        ) VALUES (
            %(asset_id)s, %(symbol)s, %(score)s, %(confidence)s,
            %(community_json)s, %(sentiment_json)s, %(trend_json)s, %(market_json)s,
            %(score_detail_json)s, %(methodology_json)s, %(input_snapshot_json)s,
            NOW(), NOW()
        )
        ON CONFLICT (asset_id) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            score = EXCLUDED.score,
            confidence = EXCLUDED.confidence,
            community_json = EXCLUDED.community_json,
            sentiment_json = EXCLUDED.sentiment_json,
            trend_json = EXCLUDED.trend_json,
            market_json = EXCLUDED.market_json,
            score_detail_json = EXCLUDED.score_detail_json,
            methodology_json = EXCLUDED.methodology_json,
            input_snapshot_json = EXCLUDED.input_snapshot_json,
            updated_at = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, {
            "asset_id": asset_id,
            "symbol": data.get("symbol"),
            "score": data.get("score"),
            "confidence": data.get("confidence"),
            "community_json": json.dumps(data.get("community", {}), ensure_ascii=False),
            "sentiment_json": json.dumps(data.get("sentiment"), ensure_ascii=False),
            "trend_json": json.dumps(data.get("trend", {}), ensure_ascii=False),
            "market_json": json.dumps(data.get("market", {}), ensure_ascii=False),
            "score_detail_json": json.dumps(data.get("score_detail", {}), ensure_ascii=False),
            "methodology_json": json.dumps(data.get("methodology", {}), ensure_ascii=False),
            "input_snapshot_json": json.dumps(data.get("input_snapshot", {}), ensure_ascii=False),
        })
    conn.commit()
    _log("  已写入数据库")


# ── 主流程 ────────────────────────────────────────────────

def main() -> int:
    try:
        return _main()
    except Exception as e:
        _log(f"[FATAL] {e}")
        _log(traceback.format_exc())
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        return 2


def _main() -> int:
    args = build_parser().parse_args()
    if not args.asset_id and not args.symbol:
        print(json.dumps({"status": "error", "message": "需要 --asset-id 或 --symbol"},
                         ensure_ascii=False))
        return 1

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        asset = resolve_asset(conn, args.asset_id, args.symbol)
        if not asset:
            print(json.dumps({"status": "error", "message": "资产未找到"}, ensure_ascii=False))
            return 1

        asset_id = asset["asset_id"]
        symbol = (asset.get("symbol") or "").strip()
        name = (asset.get("name") or "").strip()
        _log(f"资产: {symbol} ({name}), asset_id={asset_id}")

        coin_id = asset.get("coingecko_id") or resolve_coin_id(conn, asset_id, symbol, name, settings)
        if coin_id:
            _log(f"  CoinGecko ID: {coin_id}")
        else:
            _log("  [WARN] 无 CoinGecko 映射，社区/趋势/市场维度可能缺失")

        # 各维度独立拉取，互不影响
        community, market = {}, {}
        if coin_id:
            cm = fetch_community_market(settings, coin_id)
            community, market = cm.get("community", {}), cm.get("market", {})
            _log(f"  社区规模: {json.dumps(community, ensure_ascii=False)}")
            _log(f"  市场数据: {json.dumps(market, ensure_ascii=False)}")

        trending_rank = fetch_trending_rank(settings, coin_id) if coin_id else None
        _log(f"  趋势排名: {trending_rank if trending_rank else '未上榜'}")

        updates = fetch_status_updates(settings, coin_id) if coin_id else []
        _log(f"  项目动态: {len(updates)} 条")

        posts = fetch_reddit_posts(symbol, name)
        _log(f"  Reddit 帖子: {len(posts)} 条")

        sentiment = analyze_sentiment(settings, symbol, name, posts, updates)
        if sentiment:
            _log(f"  情绪: {sentiment.get('sentiment')} ({sentiment.get('sentiment_score')})")
        else:
            _log("  情绪: 无数据（跳过）")

        # 全部维度都无数据 → not_found
        if not community and not market and trending_rank is None and not updates and not posts:
            print(json.dumps({
                "status": "not_found",
                "message": "未获取到任何社交热度数据（无 CoinGecko 映射或各源均无数据）",
                "asset_id": asset_id,
                "symbol": symbol,
                "name": name,
            }, ensure_ascii=False))
            return 0

        scores = compute_scores(community, market, trending_rank, updates, sentiment)

        data_sources = ["CoinGecko community_data", "CoinGecko market_data"]
        if trending_rank or updates:
            data_sources.append("CoinGecko trending/status_updates")
        if posts:
            data_sources.append("Reddit search")
        if sentiment:
            data_sources.append("LLM 情绪分析 (DeepSeek/豆包)")

        methodology = {
            "data_sources": data_sources,
            "key_assumptions": {
                "log_caps": CAPS,
                "weights": SCORE_WEIGHTS,
                "trending_rank": trending_rank,
                "reddit_posts": len(posts),
                "status_updates": len(updates),
            },
            "calculation_steps": [
                "社区规模：twitter/reddit/telegram 用户数 log10 归一化取均值",
                "舆情情绪：LLM 对 Reddit 帖 + 项目动态做情绪分析，输出 0-100 情绪分",
                "趋势新闻：热搜位次 + 项目动态数量合成",
                "市场热度：成交量 + 24h 涨跌幅绝对值 + 市值排名合成",
                "综合评分：可用维度按权重加权并重新归一化",
            ],
            "confidence": scores["confidence"],
        }

        input_snapshot = {
            "coin_id": coin_id,
            "community": community,
            "market": market,
            "trending_rank": trending_rank,
            "status_updates_count": len(updates),
            "reddit_posts_count": len(posts),
            "sentiment_raw": sentiment,
        }

        note_parts = []
        if not coin_id:
            note_parts.append("无 CoinGecko 映射")
        if scores["missing"]:
            note_parts.append("缺失维度: " + ", ".join(scores["missing"]))

        data = {
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "score": scores["score"],
            "confidence": scores["confidence"],
            "community": community,
            "sentiment": sentiment,
            "trend": {"trending_rank": trending_rank, "updates": updates},
            "market": market,
            "score_detail": scores["score_detail"],
            "methodology": methodology,
            "input_snapshot": input_snapshot,
            "note": "；".join(note_parts) if note_parts else "",
        }

        if args.save:
            ensure_table(conn)
            save_to_db(conn, asset_id, data)

        print(json.dumps({"status": "ok", **data}, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    sys.exit(main())
