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
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "src"))
                from crypto_research.config import get_settings
                _database_url = get_settings().database_url
            except Exception:
                pass
    if not _database_url:
        raise RuntimeError("DATABASE_URL not set")
    return _database_url


@contextmanager
def get_conn():
    """获取数据库连接上下文管理器（自动提交/回滚/关闭）"""
    conn = psycopg.connect(_get_db_url(), row_factory=dict_row, connect_timeout=30)
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
        # 优先：asset_source_map binance 来源的 source_asset_key
        row = c.execute(
            """
            SELECT a.asset_id
            FROM core.asset a
            JOIN core.asset_source_map m ON a.asset_id = m.asset_id
            WHERE m.source_code = 'binance'
              AND UPPER(m.source_asset_key) = %s
            LIMIT 1
            """,
            (base_symbol,),
        ).fetchone()
        if row:
            return row["asset_id"]
        # 退一步：asset 表的 canonical_symbol
        row = c.execute(
            """
            SELECT asset_id
            FROM core.asset
            WHERE UPPER(canonical_symbol) = %s
            LIMIT 1
            """,
            (base_symbol,),
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
