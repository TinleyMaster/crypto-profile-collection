"""
催化剂数据库操作
表：biz.asset_catalyst
"""
from __future__ import annotations

import os
import sys
import json
import logging
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# 兼容 Docker 环境和本地开发环境
_database_url: str | None = None


def _get_db_url() -> str:
    global _database_url
    if _database_url is None:
        _database_url = os.environ.get("DATABASE_URL", "")
        if not _database_url:
            # 本地结构: workbench/catalyst/db.py → workbench/../scripts/src
            # 扁平结构: catalyst/db.py → ../scripts/src
            candidates = [
                os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "src"),
                os.path.join(os.path.dirname(__file__), "..", "scripts", "src"),
            ]
            for candidate in candidates:
                if os.path.isdir(candidate):
                    try:
                        sys.path.insert(0, candidate)
                        from crypto_research.config import get_settings
                        _database_url = get_settings().database_url
                        break
                    except Exception:
                        continue
    if not _database_url:
        raise RuntimeError("DATABASE_URL not set")
    return _database_url


@contextmanager
def get_conn(max_retries: int = 5, retry_delay: float = 3.0):
    """获取数据库连接上下文管理器（自动提交/回滚/关闭）

    Args:
        max_retries: 连接失败重试次数
        retry_delay: 重试间隔（秒）
    """
    conn = None
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            conn = psycopg.connect(
                _get_db_url(),
                row_factory=dict_row,
                connect_timeout=30,
                # lock_timeout=30s：被其他事务持锁时快速失败并留痕，
                # 避免 catalyst 管道 UPDATE 无限等锁 → 90 分钟无日志被看护误杀（2026-09-15 P0）
                options="-c lock_timeout=30000",
                # TCP keepalive：pipeline 进程被 kill 后，PostgreSQL 能在 ~30s 内
                # 检测到连接断开并释放锁，避免僵尸连接持锁几小时（2026-09-15 P0）
                keepalives=1,
                keepalives_idle=15,
                keepalives_interval=5,
                keepalives_count=3,
            )
            break
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                import time
                time.sleep(retry_delay)
            continue
    if conn is None:
        raise last_error  # type: ignore[misc]

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---- 写入 ----

CATALYST_FIELDS = [
    "source_code",
    "source_article_id",
    "source_article_code",
    "asset_id",
    "title",
    "body_text",
    "body_html",
    "published_at",
    "event_category",
    "event_subcategory",
    "related_pairs",
    "source_url",
    "seo_keywords",
    "share_count",
    "raw_json",
]

_INSERT_SQL = None
_UPDATE_SET = None


def _get_insert_sql() -> tuple[str, str]:
    """缓存 INSERT SQL 模板"""
    global _INSERT_SQL, _UPDATE_SET
    if _INSERT_SQL is None:
        columns = ", ".join(CATALYST_FIELDS)
        placeholders = ", ".join([f"%({f})s" for f in CATALYST_FIELDS])
        update_set = ", ".join([
            f"{f} = EXCLUDED.{f}"
            for f in CATALYST_FIELDS
            if f not in ("source_code", "source_article_id")
        ]) + ", updated_at = NOW()"
        _INSERT_SQL = f"""
            INSERT INTO biz.asset_catalyst ({columns})
            VALUES ({placeholders})
            ON CONFLICT (source_code, source_article_id) DO UPDATE SET
                {update_set}
            RETURNING *
        """
        _UPDATE_SET = update_set
    return _INSERT_SQL, _UPDATE_SET


def _prepare_data(data: dict) -> dict:
    """统一数据预处理：时间戳转换、JSON 序列化"""
    data = dict(data)
    if data.get("published_at") is not None and isinstance(data["published_at"], (int, float)):
        data["published_at"] = datetime.fromtimestamp(data["published_at"])
    if data.get("raw_json") is not None and isinstance(data["raw_json"], (dict, list)):
        data["raw_json"] = json.dumps(data["raw_json"])
    return data


def upsert_catalyst(data: dict, conn=None) -> dict | None:
    """插入或更新一条催化剂记录

    Args:
        data: 催化剂数据 dict
        conn: 可选外部数据库连接（复用连接提升性能）

    唯一键：(source_code, source_article_id)
    """
    data = _prepare_data(data)
    sql, _ = _get_insert_sql()

    if conn is not None:
        row = conn.execute(sql, data).fetchone()
        return row

    with get_conn() as c:
        row = c.execute(sql, data).fetchone()
        return row


def batch_upsert_catalysts(articles: list[dict]) -> int:
    """批量 upsert（复用单连接），返回成功条数"""
    count = 0
    with get_conn() as conn:
        for art in articles:
            try:
                upsert_catalyst(art, conn=conn)
                count += 1
            except Exception as e:
                logger.error("upsert catalyst failed id=%s: %s", art.get("source_article_id"), e)
                conn.rollback()  # 单条失败不影响其他
    return count


# ---- 查询 ----

def get_latest_publish_time(source_code: str, conn=None) -> float | None:
    """获取某来源最新发布时间（秒级时间戳），用于增量抓取"""
    def _query(c):
        return c.execute(
            """
            SELECT MAX(published_at) as latest
            FROM biz.asset_catalyst
            WHERE source_code = %s
            """,
            (source_code,),
        ).fetchone()

    row = _query(conn) if conn is not None else _with_conn(_query)

    if not row or not row["latest"]:
        return None
    if isinstance(row["latest"], datetime):
        return row["latest"].timestamp()
    return float(row["latest"])


def get_unprocessed_count(conn=None) -> int:
    """获取未 AI 处理的催化剂数量"""
    def _query(c):
        return c.execute(
            "SELECT count(*) as cnt FROM biz.asset_catalyst WHERE NOT ai_processed"
        ).fetchone()

    row = _query(conn) if conn is not None else _with_conn(_query)
    return row["cnt"] if row else 0


# ---- 资产关联 ----

# symbol(大写) -> asset_id 缓存，避免重复查库
_symbol_asset_cache: dict[str, int | None] = {}

# 非加密资产名称黑名单（SQL 正则，与 linker.py / asset_filter.py 同步）。
# 命中即视为「美股 tokenized stock / 商品衍生」，不参与 crypto 催化剂信号映射。
_NON_CRYPTO_NAME_SQL = (
    r"tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ"
    r"|crude[[:space:]]+oil|brent"
    r"|robinhood[[:space:]]+token|backpack[[:space:]]+securities"
    r"|\mdinari\M|\mxstock\M"
)

# canonical 白名单（CAT-LINKER-DIRTY）：主流/高碰撞符号强制解析为真实主网币。
# 命中白名单即返回（要求有 CMC 排名），跳过 source_map / 脏词兜底。
# 与 linker.py 保持同一清单，避免两处口径漂移。
_CANONICAL_TOP_SYMBOLS = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "PEPE", "SHIB", "ADA",
    "DOT", "AVAX", "LINK", "LTC", "UNI", "AAVE", "MATIC", "POL", "TRX",
    "XLM", "FIL", "NEAR", "ARB", "OP", "SUI", "TON", "APT", "INJ", "SEI",
    "WIF", "BONK", "FLOKI", "ENA", "PENDLE", "JUP", "RENDER", "FET",
})


def _resolve_canonical_top(c, base: str) -> int | None:
    """白名单符号 → 真实主网资产（market_cap_rank 非空，取排名最小）。"""
    row = c.execute(
        """
        SELECT asset_id
        FROM core.asset
        WHERE UPPER(canonical_symbol) = %s
          AND market_cap_rank IS NOT NULL
        ORDER BY market_cap_rank ASC, asset_id
        LIMIT 1
        """,
        (base,),
    ).fetchone()
    return row["asset_id"] if row else None


def map_pairs_to_asset_id(pairs: list[str], conn=None) -> int | None:
    """将交易对数组映射到 core.asset.asset_id

    策略：取第一个 USDT 交易对的 base symbol，查 asset_source_map（带缓存）
    """
    if not pairs:
        return None

    # 提取 base symbol
    base_symbol = None
    for p in pairs:
        if p.endswith("USDT"):
            base_symbol = p[:-4].upper()
            break
    if not base_symbol and pairs:
        p = pairs[0]
        for quote in ("USDT", "USDC", "BTC", "ETH", "BNB"):
            if p.endswith(quote):
                base_symbol = p[: -len(quote)].upper()
                break
        if not base_symbol:
            return None

    # 查缓存
    if base_symbol in _symbol_asset_cache:
        return _symbol_asset_cache[base_symbol]

    def _query(c):
        # 白名单优先（CAT-LINKER-DIRTY）：主流符号强制取真实主网币
        if base_symbol in _CANONICAL_TOP_SYMBOLS:
            aid = _resolve_canonical_top(c, base_symbol)
            if aid is not None:
                return aid
        # 优先：asset_source_map binance 来源的 source_asset_key
        # （同 key 多条时按 CMC 排名升序选最靠前的，防止脏映射）
        # 注意：精确 key 匹配也排除 tokenized stock / 商品衍生资产
        #       （CAT-CLEANUP v5.1 ③：美股快讯经 cashtag 会错连 xStock/Robinhood Token，
        #        已定性为脏数据，源头排除，与 linker.py 同步）
        row = c.execute(
            f"""
            SELECT a.asset_id
            FROM core.asset a
            JOIN core.asset_source_map m ON a.asset_id = m.asset_id
            WHERE m.source_code = 'binance'
              AND UPPER(m.source_asset_key) = %s
              AND LOWER(COALESCE(a.canonical_name, '')) !~ '{_NON_CRYPTO_NAME_SQL}'
            ORDER BY a.market_cap_rank ASC NULLS LAST, a.asset_id
            LIMIT 1
            """,
            (base_symbol,),
        ).fetchone()
        if row:
            return row["asset_id"]
        # 退一步：asset 表的 canonical_symbol
        # （同 symbol 多行时避免取到脏行：
        #   1. 排除 tokenized/合成/商品衍生 等（CAT-CLEANUP v5.1 ③，源头排除）
        #   2. 排除真正仿盘标记（bridged/wrapped/intents/trophy/tomato/second chance/base coin）
        #      —— 不再用 meme 系（doge/shib/pepe/floki/inu/ai）无边界子串，避免误杀合法 meme 币（CAT-LINKER-NEG）
        #   3. 按 CMC 排名升序（rank 越小越主流），兜底用 asset_id
        # 线上审计 2026-09-15：BTC→Bitcoin Base/XRP→XRP AI/BNB→BNBTiger Inu
        # 部署复验 2026-09-15：SOL→Sol The Trophy Tomato / ETH→NEAR Intents Bridged ETH
        row = c.execute(
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
            (base_symbol, base_symbol),
        ).fetchone()
        return row["asset_id"] if row else None

    asset_id = _query(conn) if conn is not None else _with_conn(_query)
    _symbol_asset_cache[base_symbol] = asset_id
    return asset_id


# ---- 工具 ----

def _with_conn(func):
    """用 get_conn 执行一个查询函数"""
    with get_conn() as conn:
        return func(conn)
