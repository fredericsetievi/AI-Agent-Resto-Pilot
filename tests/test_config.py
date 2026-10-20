from pathlib import Path

import pytest

from service.__main__ import load_local_env


def test_local_env_reads_only_supported_literal_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# local only\nPUBLIC_BASE_URL=https://demo.example\nMETA_WHATSAPP_ACCESS_TOKEN='access-token'\nMETA_APP_SECRET=app-secret\nMETA_WEBHOOK_VERIFY_TOKEN=abcdefghijklmnopqrstuvwxyz012345\nMETA_WABA_ID=2096089503992178\nMETA_PHONE_NUMBER_ID=123456789012345\nMETA_GRAPH_API_VERSION=v23.0\n", encoding="utf-8")
    assert load_local_env(path) == {
        "PUBLIC_BASE_URL": "https://demo.example",
        "META_WHATSAPP_ACCESS_TOKEN": "access-token",
        "META_APP_SECRET": "app-secret",
        "META_WEBHOOK_VERIFY_TOKEN": "abcdefghijklmnopqrstuvwxyz012345",
        "META_WABA_ID": "2096089503992178",
        "META_PHONE_NUMBER_ID": "123456789012345",
        "META_GRAPH_API_VERSION": "v23.0",
    }


def test_local_env_does_not_execute_or_accept_unknown_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("PATH=$(touch /tmp/should-not-run)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported setting"):
        load_local_env(path)


def test_local_env_ignores_retired_provider_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TWILIO_AUTH_TOKEN=old\nDIALOG360_API_KEY=old\nDIALOG360_WEBHOOK_TOKEN=old\n", encoding="utf-8")
    assert load_local_env(path) == {}


def test_missing_local_env_is_empty(tmp_path):
    assert load_local_env(tmp_path / ".env") == {}
