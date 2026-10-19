"""Supabase PostgreSQL client for RestoPilot's cloud data path.

Reads SUPABASE_URL and SUPABASE_SERVICE_KEY from the process environment or a
local .env file (.env is git-ignored; see .env.example for placeholders).

The service-role key bypasses row level security, so this client is strictly
server-side: keep it out of browser bundles, logs, and any code path that
handles untrusted input. Direct client-side table mutations stay disabled
(row level security is enabled with no public policies in the migrations).

The local SQLite demo (service.db.Store) remains the default data path until
routes are explicitly wired to this client.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """Return the shared service-role client, or fail fast if unconfigured."""
    load_dotenv(ENV_PATH)
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "Supabase is not configured: set SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "in the environment or a local .env file (see .env.example)."
        )
    return create_client(url, key)
