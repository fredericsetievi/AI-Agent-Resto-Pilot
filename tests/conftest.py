"""Shared fixtures: keep the suite hermetic against a developer's local .env."""
import pytest

from service import supabase_db


@pytest.fixture(autouse=True)
def isolate_cloud_environment(monkeypatch):
    """No test may talk to a real Supabase project via the local .env.

    mirror_enabled() loads the repo .env into os.environ at call time, so
    point it at a nonexistent path and strip Supabase variables for every
    test. Tests that exercise the mirror stub mirror_enabled/mirror_purchase
    explicitly (tests/test_supabase_db.py).
    """
    monkeypatch.setattr(supabase_db, "ENV_PATH", "/nonexistent/.env")
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_MIRROR"):
        monkeypatch.delenv(key, raising=False)
