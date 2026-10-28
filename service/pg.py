"""Shared PostgreSQL connection factory for the Supabase authority.

The application talks to Supabase's Postgres directly (psycopg2) instead of
the local SQLite file, using the same transaction discipline: one connection
per operation, explicit BEGIN, commit on success. Password and host come from
the environment (SUPABASE_DB_PASSWORD / SUPABASE_URL); nothing is hardcoded.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


@lru_cache(maxsize=1)
def db_config():
    """Resolve the direct-connection settings once; fail fast when unset."""
    load_dotenv(ENV_PATH)
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    password = (os.environ.get("SUPABASE_DB_PASSWORD") or "").strip()
    if not url or not password:
        raise RuntimeError(
            "Postgres authority is not configured: set SUPABASE_URL and "
            "SUPABASE_DB_PASSWORD in the environment or a local .env (see .env.example)."
        )
    ref = url.split("//")[-1].split(".")[0]
    return {"host": f"db.{ref}.supabase.co", "port": 5432, "dbname": "postgres",
            "user": "postgres", "password": password}


# A small persistent pool: opening a fresh TLS connection to Supabase per
# query costs a full handshake, which made multi-query reads time out.
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg2.pool import SimpleConnectionPool

            _pool = SimpleConnectionPool(1, 8, sslmode="require", connect_timeout=15, **db_config())
        return _pool


def _borrow():
    """Take a live connection from the pool, replacing stale ones once."""
    import psycopg2

    last_error = None
    for _attempt in range(2):
        conn = _get_pool().getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.rollback()
            return conn
        except psycopg2.OperationalError as error:
            last_error = error
            _get_pool().putconn(conn, close=True)
    raise last_error


@contextmanager
def pg_connection():
    """One pooled connection; callers commit or roll back explicitly."""
    conn = _borrow()
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _get_pool().putconn(conn)


@contextmanager
def pg_transaction():
    """BEGIN (write-leaning) + commit/rollback, mirroring Store.transaction()."""
    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
        yield conn
