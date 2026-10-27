"""Shared PostgreSQL connection factory for the Supabase authority.

The application talks to Supabase's Postgres directly (psycopg2) instead of
the local SQLite file, using the same transaction discipline: one connection
per operation, explicit BEGIN, commit on success. Password and host come from
the environment (SUPABASE_DB_PASSWORD / SUPABASE_URL); nothing is hardcoded.
"""

from __future__ import annotations

import os
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


@contextmanager
def pg_connection():
    """One autocommit-off connection; callers commit or roll back explicitly."""
    import psycopg2

    conn = psycopg2.connect(sslmode="require", connect_timeout=15, **db_config())
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def pg_transaction():
    """BEGIN (write-leaning) + commit/rollback, mirroring Store.transaction()."""
    with pg_connection() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("BEGIN")
        yield conn
