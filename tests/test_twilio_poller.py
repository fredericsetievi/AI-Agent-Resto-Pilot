"""Twilio Trial poller tests. All provider clients are local fakes."""

from types import SimpleNamespace

import pytest

from service.db import Store
from service.twilio_poller import TwilioTrialPoller, environment_config


TARGET = "whatsapp:+14155238886"
SENDER = "whatsapp:+628123456789"


def message(sid, body, *, direction="inbound", sender=SENDER, recipient=TARGET, media="0"):
    return SimpleNamespace(
        sid=sid,
        body=body,
        direction=direction,
        from_=sender,
        to=recipient,
        num_media=media,
    )


class FakeMessages:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.records)


class FakeClient:
    def __init__(self, records):
        self.messages = FakeMessages(records)


def counts(store):
    with store.transaction() as conn:
        return {
            "events": conn.execute("SELECT count(*) FROM whatsapp_events").fetchone()[0],
            "messages": conn.execute("SELECT count(*) FROM whatsapp_messages").fetchone()[0],
            "purchases": conn.execute("SELECT count(*) FROM purchases").fetchone()[0],
        }


def test_poll_filters_to_inbound_whatsapp_target_and_never_sends(tmp_path):
    records = [
        message("SMvalid", "beli cabai 3 kg 150rb"),
        message("SMwrongto", "beli beras 2 kg 50rb", recipient="whatsapp:+14155550123"),
        message("SMsms", "beli beras 2 kg 50rb", sender="+628123456789", recipient="+14155238886"),
        message("SMoutbound", "provider reply", direction="outbound"),
        message("SMinvalidmedia", "", media="11"),
    ]
    store = Store(tmp_path / "poll.sqlite3", seed=False)
    client = FakeClient(records)
    result = TwilioTrialPoller(store, client, recipient=TARGET, interval=2, batch_size=10).poll_once()

    assert result.scanned == 5
    assert result.processed == 1
    assert result.skipped == 4
    assert result.failed == 0
    assert client.messages.calls == [{"limit": 10, "to": TARGET}]
    assert counts(store) == {"events": 1, "messages": 2, "purchases": 0}
    with store.transaction() as conn:
        rows = conn.execute("SELECT direction, status, transport FROM whatsapp_messages ORDER BY rowid").fetchall()
        assert [tuple(row) for row in rows] == [
            ("inbound", "received", "twilio_whatsapp"),
            ("outbound", "not_delivered_trial_poll", "twilio_whatsapp"),
        ]


def test_poll_processes_oldest_first_and_persists_sid_deduplication_across_restart(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    # Twilio's list is newest first; YA must be applied after the older purchase note.
    records = [message("SMnewer", "ya"), message("SMolder", "beli cabai 3 kg 150rb")]
    first_store = Store(path, seed=False)
    first = TwilioTrialPoller(first_store, FakeClient(records), interval=2).poll_once()
    assert first.processed == 2 and first.failed == 0
    assert counts(first_store) == {"events": 2, "messages": 4, "purchases": 1}
    assert next(row for row in first_store.overview()["inventory"] if row["ingredient_id"] == "cabai")["quantity"] == "3"

    restarted = Store(path, seed=False)
    second = TwilioTrialPoller(restarted, FakeClient(records), interval=2).poll_once()
    assert second.processed == 2 and second.failed == 0
    assert counts(restarted) == {"events": 2, "messages": 4, "purchases": 1}


def test_poll_without_recipient_still_accepts_only_inbound_whatsapp(tmp_path):
    store = Store(tmp_path / "all-whatsapp.sqlite3", seed=False)
    client = FakeClient([message("SMone", "beli telur 2 kg 80rb")])
    result = TwilioTrialPoller(store, client, interval=2, batch_size=1).poll_once()
    assert result.processed == 1
    assert client.messages.calls == [{"limit": 1}]


def test_poller_bounds_and_environment_only_configuration():
    account_sid = "AC" + "a" * 32
    assert environment_config({
        "TWILIO_ACCOUNT_SID": account_sid,
        "TWILIO_AUTH_TOKEN": "synthetic-token",
        "TWILIO_WHATSAPP_TO": TARGET,
    }) == (account_sid, "synthetic-token", TARGET)
    with pytest.raises(ValueError, match="TWILIO_ACCOUNT_SID"):
        environment_config({"TWILIO_AUTH_TOKEN": "synthetic-token"})
    with pytest.raises(ValueError, match="TWILIO_AUTH_TOKEN"):
        environment_config({"TWILIO_ACCOUNT_SID": account_sid})
    with pytest.raises(ValueError, match="TWILIO_WHATSAPP_TO"):
        environment_config({
            "TWILIO_ACCOUNT_SID": account_sid,
            "TWILIO_AUTH_TOKEN": "synthetic-token",
            "TWILIO_WHATSAPP_TO": "+14155238886",
        })
    with pytest.raises(ValueError, match="interval"):
        TwilioTrialPoller(None, None, interval=1)
    with pytest.raises(ValueError, match="interval"):
        TwilioTrialPoller(None, None, interval=float("nan"))
    with pytest.raises(ValueError, match="batch size"):
        TwilioTrialPoller(None, None, batch_size=101)


def test_processing_error_does_not_log_exception_text_or_token(caplog):
    class FailingStore:
        def whatsapp_ingest(self, *args, **kwargs):
            raise RuntimeError("synthetic-token-must-not-appear")

    with caplog.at_level("ERROR"):
        result = TwilioTrialPoller(FailingStore(), FakeClient([message("SMfailure", "ya")]), interval=2).poll_once()
    assert result.failed == 1
    assert "synthetic-token-must-not-appear" not in caplog.text
    assert "RuntimeError" in caplog.text
