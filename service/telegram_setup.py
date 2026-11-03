"""One-command Telegram webhook setup, straight from the local .env.

Usage:
  python -m service.telegram_setup             # register the webhook
  python -m service.telegram_setup --info      # Telegram's view: URL, backlog, last error
  python -m service.telegram_setup --delete    # remove it (e.g. before polling mode)

Reads TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and PUBLIC_BASE_URL from
the environment or the project .env — the same values the server uses — so
a tunnel change is one command instead of a hand-built URL. The bot token
never appears in any printed message or error.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from .__main__ import PROJECT_ROOT, load_local_env
from .telegram import (
    DEFAULT_API_BASE,
    TELEGRAM_WEBHOOK_PATH,
    TelegramError,
    validate_bot_token,
    validate_secret_token,
)
from .whatsapp import public_origin


def _resolve(name, overrides, local_env):
    """Setting order: explicit overrides (tests), environment, then the .env file."""
    return (overrides or {}).get(name) or os.environ.get(name) or local_env.get(name)


def api_call(token, method, params, *, api_base, client):
    """One Bot API GET; returns the result object or raises a safe TelegramError."""
    try:
        response = client.get(api_base + f"/bot{token}/{method}", params=params or {})
    except httpx.HTTPError:
        # httpx error text can embed the full request URL, which carries the
        # token; report the failure without it.
        raise TelegramError("Tidak dapat menghubungi Telegram Bot API.")
    description = None
    body = {}
    try:
        body = response.json()
        if not body.get("ok"):
            description = body.get("description")
    except ValueError:
        pass
    if response.status_code != 200:
        raise TelegramError(f"Telegram menolak permintaan (HTTP {response.status_code}){': ' + description if description else ''}.")
    if description or not body.get("ok"):
        raise TelegramError(f"Telegram: {description or 'permintaan gagal'}.")
    return body.get("result")


def run(settings, action, *, client):
    """Perform one action and return the human-readable report.

    settings: {"token", "secret", "public_base_url", "api_base"}; raises
    SystemExit for configuration problems and TelegramError for provider
    failures, so main() can turn them into exit codes.
    """
    token = validate_bot_token(settings.get("token"))
    api_base = settings.get("api_base") or DEFAULT_API_BASE
    if action == "set":
        secret = validate_secret_token(settings.get("secret"))
        base_url = public_origin(settings.get("public_base_url"))
        webhook_url = base_url + TELEGRAM_WEBHOOK_PATH
        api_call(token, "setWebhook", {
            "url": webhook_url,
            "secret_token": secret,
            # Only fresh messages; edits and callbacks are ignored anyway.
            "allowed_updates": json_dumps(["message"]),
        }, api_base=api_base, client=client)
        return f"✅ Webhook terdaftar: {webhook_url}\nSekarang kirim pesan apa pun ke bot untuk mencoba."
    if action == "info":
        info = api_call(token, "getWebhookInfo", {}, api_base=api_base, client=client) or {}
        lines = []
        lines.append(f"URL webhook: {info.get('url') or '(belum diset)'}")
        lines.append(f"Update menunggu diproses: {info.get('pending_update_count', 0)}")
        if info.get("last_error_message"):
            lines.append(f"Error terakhir Telegram: {info['last_error_message']} ({info.get('last_error_date', '?')})")
        return "\n".join(lines)
    if action == "delete":
        api_call(token, "deleteWebhook", {}, api_base=api_base, client=client)
        return "✅ Webhook dihapus. Telegram tidak lagi mengirim update ke URL mana pun."
    raise SystemExit(f"Aksi tidak dikenal: {action}")


def json_dumps(value):
    import json

    return json.dumps(value)


def main(argv=None, *, client=None, overrides=None, project_root=None):
    parser = argparse.ArgumentParser(description="Atur webhook Telegram RestoPilot dari .env.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--info", action="store_true", help="Tampilkan status webhook menurut Telegram")
    group.add_argument("--delete", action="store_true", help="Hapus webhook (misal sebelum mode polling)")
    args = parser.parse_args(argv)
    action = "info" if args.info else "delete" if args.delete else "set"

    root = project_root or PROJECT_ROOT
    local_env = load_local_env(root / ".env")
    settings = {
        "token": _resolve("TELEGRAM_BOT_TOKEN", overrides, local_env),
        "secret": _resolve("TELEGRAM_WEBHOOK_SECRET", overrides, local_env),
        "public_base_url": _resolve("PUBLIC_BASE_URL", overrides, local_env),
        "api_base": _resolve("TELEGRAM_API_BASE", overrides, local_env),
    }
    try:
        config_error = None
        if action == "set" and not settings.get("token"):
            config_error = "TELEGRAM_BOT_TOKEN kosong — isi dulu di .env (lihat .env.example)."
        elif action == "set" and not settings.get("secret"):
            config_error = "TELEGRAM_WEBHOOK_SECRET kosong — isi dulu di .env (lihat .env.example)."
        elif action == "set" and not settings.get("public_base_url"):
            config_error = "PUBLIC_BASE_URL kosong — webhook butuh URL HTTPS publik (tunnel), lihat docs/TUNNEL.md."
        if config_error:
            print(config_error, file=sys.stderr)
            return 2
        print(run(settings, action, client=client or httpx.Client(timeout=30.0)))
    except ValueError as error:
        print(f"Konfigurasi tidak valid: {error}", file=sys.stderr)
        return 2
    except TelegramError as error:
        print(f"Gagal: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
