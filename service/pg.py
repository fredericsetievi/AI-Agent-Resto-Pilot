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


def hosted_mode() -> bool:
    """Hosted deployments read configuration from server env only (no .env)."""
    return os.environ.get("RESTOPILOT_HOSTED", "").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def db_config():
    """Resolve the connection settings once; fail fast when unset.

    Defaults to the direct host ``db.<ref>.supabase.co:5432`` as user
    ``postgres``. Runtimes without IPv6 can point at the Supabase session
    pooler instead via SUPABASE_DB_HOST / SUPABASE_DB_PORT / SUPABASE_DB_USER
    (e.g. ``aws-0-<region>.pooler.supabase.com``, ``5432``, ``postgres.<ref>``).
    TLS is always required.
    """
    if not hosted_mode():
        load_dotenv(ENV_PATH)
    env = os.environ.get
    url = (env("SUPABASE_URL") or "").strip()
    password = (env("SUPABASE_DB_PASSWORD") or "").strip()
    if not url or not password:
        raise RuntimeError(
            "Postgres authority is not configured: set SUPABASE_URL and "
            "SUPABASE_DB_PASSWORD in the server environment."
        )
    ref = url.split("//")[-1].split(".")[0]
    port_text = (env("SUPABASE_DB_PORT") or "5432").strip()
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise RuntimeError("SUPABASE_DB_PORT must be a TCP port number.")
    return {"host": (env("SUPABASE_DB_HOST") or "").strip() or f"db.{ref}.supabase.co",
            "port": int(port_text), "dbname": "postgres",
            "user": (env("SUPABASE_DB_USER") or "").strip() or "postgres",
            "password": password}


def reset_pool():
    """Drop cached config and pool (used after a failed hosted bootstrap and by tests)."""
    global _pool
    db_config.cache_clear()
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
        _pool = None


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
