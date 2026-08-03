from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg


SQL_ROOT = Path(__file__).resolve().parents[3] / "sql"


def load_sql(relative_path: str) -> str:
    return (SQL_ROOT / relative_path).read_text(encoding="utf-8")


def fetch_one(conn: "psycopg.Connection", sql_text: str, params: tuple[Any, ...]) -> dict[str, Any]:
    import psycopg

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql_text, params)
        row = cur.fetchone()
        return dict(row) if row else {}


def execute_many(conn: "psycopg.Connection", sql_text: str, rows: list[tuple[Any, ...]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(sql_text, rows)
