from pathlib import Path

import pytest

from service.__main__ import load_local_env


def test_local_env_reads_only_supported_literal_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# local only\nTWILIO_AUTH_TOKEN='secret'\nPUBLIC_BASE_URL=https://demo.example\nDIALOG360_API_KEY='sandbox-key'\nDIALOG360_WEBHOOK_TOKEN=abcdefghijklmnopqrstuvwxyz012345\n", encoding="utf-8")
    assert load_local_env(path) == {
        "TWILIO_AUTH_TOKEN": "secret",
        "PUBLIC_BASE_URL": "https://demo.example",
        "DIALOG360_API_KEY": "sandbox-key",
        "DIALOG360_WEBHOOK_TOKEN": "abcdefghijklmnopqrstuvwxyz012345",
    }


def test_local_env_does_not_execute_or_accept_unknown_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("PATH=$(touch /tmp/should-not-run)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported setting"):
        load_local_env(path)


def test_missing_local_env_is_empty(tmp_path):
    assert load_local_env(tmp_path / ".env") == {}
