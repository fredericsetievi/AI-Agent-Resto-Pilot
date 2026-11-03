"""Telegram webhook setup script: config resolution and Bot API calls."""

import httpx

from service.telegram_setup import main, run


BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"
BASE = "https://api.telegram.org"
# run() takes resolved settings; main() takes env-style overrides.
RUN_SETTINGS = {
    "token": BOT_TOKEN,
    "secret": "integration-secret",
    "public_base_url": "https://resto.example",
    "api_base": BASE,
}
ENV_OVERRIDES = {
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    "TELEGRAM_WEBHOOK_SECRET": "integration-secret",
    "PUBLIC_BASE_URL": "https://resto.example",
}


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeTelegramAPI:
    """Captures Bot API GETs; one queued response per call."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse({"ok": True, "result": {}})


# --- The happy paths -----------------------------------------------------------


def test_set_registers_the_webhook_with_url_and_secret():
    api = FakeTelegramAPI()
    report = run(RUN_SETTINGS, "set", client=api)
    assert "https://resto.example/webhooks/telegram" in report
    call = api.calls[0]
    assert call["url"] == BASE + f"/bot{BOT_TOKEN}/setWebhook"
    assert call["params"]["url"] == "https://resto.example/webhooks/telegram"
    assert call["params"]["secret_token"] == "integration-secret"
    assert "message" in call["params"]["allowed_updates"]


def test_info_reports_backlog_and_last_error():
    api = FakeTelegramAPI([FakeResponse({"ok": True, "result": {
        "url": "https://resto.example/webhooks/telegram", "pending_update_count": 3,
        "last_error_message": "Wrong response from the webhook", "last_error_date": 1730000000}})])
    report = run(RUN_SETTINGS, "info", client=api)
    assert "https://resto.example/webhooks/telegram" in report
    assert "3" in report and "Wrong response" in report


def test_delete_removes_the_webhook():
    api = FakeTelegramAPI()
    report = run(RUN_SETTINGS, "delete", client=api)
    assert "dihapus" in report
    assert api.calls[0]["url"].endswith("/deleteWebhook")


# --- Configuration errors -------------------------------------------------------


def test_info_without_a_token_reports_invalid_configuration(tmp_path):
    assert main(["--info"], client=FakeTelegramAPI(), overrides={}, project_root=tmp_path) == 2


def test_set_requires_secret_and_public_url(tmp_path):
    overrides = {"token": BOT_TOKEN, "secret": "integration-secret"}
    assert main([], client=FakeTelegramAPI(), overrides=overrides, project_root=tmp_path) == 2
    overrides.pop("secret")
    assert main([], client=FakeTelegramAPI(), overrides=overrides, project_root=tmp_path) == 2


def test_non_https_public_url_is_refused(tmp_path):
    overrides = dict(ENV_OVERRIDES, PUBLIC_BASE_URL="http://resto.example")
    assert main([], client=FakeTelegramAPI(), overrides=overrides, project_root=tmp_path) == 2


def test_malformed_token_is_refused(tmp_path):
    overrides = dict(ENV_OVERRIDES, TELEGRAM_BOT_TOKEN="bukan-token")
    assert main([], client=FakeTelegramAPI(), overrides=overrides, project_root=tmp_path) == 2


# --- Provider failures stay token-free -------------------------------------------


def test_telegram_rejection_surfaces_the_description_without_the_token(tmp_path, capsys):
    api = FakeTelegramAPI([FakeResponse({"ok": False, "description": "Unauthorized"}, status_code=401)])
    code = main([], client=api, overrides=ENV_OVERRIDES, project_root=tmp_path)
    captured = capsys.readouterr()
    assert code == 1
    assert "Unauthorized" in captured.err
    assert BOT_TOKEN not in captured.err and BOT_TOKEN not in captured.out


def test_network_failure_does_not_leak_the_token(tmp_path, capsys):
    class DeadNetwork:
        def get(self, url, params=None):
            raise httpx.ConnectError(f"cannot reach {url}")

    code = main([], client=DeadNetwork(), overrides=ENV_OVERRIDES, project_root=tmp_path)
    captured = capsys.readouterr()
    assert code == 1
    assert BOT_TOKEN not in captured.err
