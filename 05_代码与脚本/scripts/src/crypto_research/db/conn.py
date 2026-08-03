from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

import psycopg


@contextmanager
def get_connection(database_url: str) -> Iterator[psycopg.Connection]:
    last_exc: Exception | None = None
    conn: psycopg.Connection | None = None
    for attempt in range(5):
        try:
            conn = psycopg.connect(database_url, connect_timeout=30)
            break
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))

    if conn is None:
        raise last_exc or RuntimeError("Unable to establish database connection")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
