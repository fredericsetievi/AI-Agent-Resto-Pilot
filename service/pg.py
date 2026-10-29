"""Shared PostgreSQL connection factory for the Supabase authority.

The application talks to Supabase's Postgres directly (psycopg2) instead of
the local SQLite file, using the same transaction discipline: one connection
per operation, explicit BEGIN, commit on success. Password and host come from
the environment (SUPABASE_DB_PASSWORD / SUPABASE_URL); nothing is hardcoded.
"""

from __future__ import annotations

import os
import threading
import time
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
    # TCP keepalives: the session pooler drops idle connections mid-flight,
    # which surfaces as "server closed the connection unexpectedly".
    return {"host": (env("SUPABASE_DB_HOST") or "").strip() or f"db.{ref}.supabase.co",
            "port": int(port_text), "dbname": "postgres",
            "user": (env("SUPABASE_DB_USER") or "").strip() or "postgres",
            "password": password,
            "keepalives": 1, "keepalives_idle": 30,
            "keepalives_interval": 10, "keepalives_count": 5}


def reset_pool():
    """Drop this thread's ambient connection and cached config (used after a
    failed hosted bootstrap and by tests). The ambient design has no global
    pool: each worker thread owns one long-lived connection."""
    db_config.cache_clear()
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None


# The session pooler is far from this machine (Tokyo), and on a mobile
# hotspot a fresh TLS connection costs 3-5s. A small pool does not help
# because pooler connections die between borrows, so every operation paid
# a fresh handshake. Instead, each worker thread keeps ONE long-lived
# connection; a cheap health check replaces it when the pooler drops it.
_local = threading.local()


def _ambient():
    """This thread's long-lived Postgres connection, replaced when dead."""
    import psycopg2

    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.rollback()
            return conn
        except psycopg2.OperationalError:
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None
    last_error = None
    for attempt in range(3):
        try:
            conn = psycopg2.connect(sslmode="require", connect_timeout=15, **db_config())
            _local.conn = conn
            return conn
        except psycopg2.OperationalError as error:
            last_error = error
            time.sleep(0.5 * (attempt + 1))
    raise last_error


class _AmbientPool:
    """Pool-like adapter over the per-thread ambient connection, so every
    borrower (and hosted-mode tests) goes through one uniform entry point."""

    def getconn(self):
        return _ambient()

    def putconn(self, conn, close=False):
        pass  # ambient connections stay bound to their thread


_AMBIENT_POOL = _AmbientPool()


def _get_pool():
    return _AMBIENT_POOL


@contextmanager
def pg_connection():
    """One live connection; callers commit or roll back explicitly."""
    conn = _get_pool().getconn()
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


@contextmanager
def pg_transaction():
    """BEGIN (write-leaning) + commit/rollback, mirroring Store.transaction()."""
    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
        yield conn
