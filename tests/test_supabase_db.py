import pytest

from service import supabase_db


@pytest.fixture(autouse=True)
def clean_client_cache():
    supabase_db.get_supabase_client.cache_clear()
    yield
    supabase_db.get_supabase_client.cache_clear()


def test_missing_configuration_fails_fast(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setattr(supabase_db, "ENV_PATH", "/nonexistent/.env")
    with pytest.raises(RuntimeError, match="Supabase is not configured"):
        supabase_db.get_supabase_client()


def test_env_file_configuration_creates_cached_client(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SUPABASE_URL=https://demo.supabase.co\nSUPABASE_SERVICE_KEY=service-key\n", encoding="utf-8")
    monkeypatch.setattr(supabase_db, "ENV_PATH", env)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    client = supabase_db.get_supabase_client()
    assert client is supabase_db.get_supabase_client()
