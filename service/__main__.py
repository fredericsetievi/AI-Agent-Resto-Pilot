"""Start the local, synthetic-data demo: python3 -m service."""
import argparse
import os
from pathlib import Path

import uvicorn

from service.api import create_app


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_local_env(path):
    """Read only supported literal settings from a small, non-executable .env file."""
    values = {}
    if not path.exists():
        return values
    if path.stat().st_size > 16_384:
        raise ValueError(f"Refusing oversized environment file: {path}")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in {
            "TWILIO_AUTH_TOKEN",
            "PUBLIC_BASE_URL",
            "DIALOG360_API_KEY",
            "DIALOG360_WEBHOOK_TOKEN",
        }:
            raise ValueError(f"Unsupported setting on {path.name} line {number}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def main():
    parser = argparse.ArgumentParser(description="RestoPilot local demo (not a production server)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "restopilot.sqlite3")
    parser.add_argument("--empty", action="store_true", help="Skip synthetic opening purchases on a new database")
    args = parser.parse_args()
    local_env = load_local_env(PROJECT_ROOT / ".env")
    twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN") or local_env.get("TWILIO_AUTH_TOKEN")
    public_base_url = os.environ.get("PUBLIC_BASE_URL") or local_env.get("PUBLIC_BASE_URL")
    dialog360_api_key = os.environ.get("DIALOG360_API_KEY") or local_env.get("DIALOG360_API_KEY")
    dialog360_webhook_token = os.environ.get("DIALOG360_WEBHOOK_TOKEN") or local_env.get("DIALOG360_WEBHOOK_TOKEN")
    print("RestoPilot demo: manual intake, WhatsApp simulator, rules extraction, SQLite, local demo identity.")
    print(f"Open http://127.0.0.1:{args.port} · Database: {args.db}")
    print("Twilio webhook: configured" if twilio_auth_token and public_base_url else "Twilio webhook: disabled (set TWILIO_AUTH_TOKEN and PUBLIC_BASE_URL to enable)")
    print("360dialog sandbox: configured" if dialog360_api_key and dialog360_webhook_token and public_base_url else "360dialog sandbox: disabled (set DIALOG360_API_KEY, DIALOG360_WEBHOOK_TOKEN and PUBLIC_BASE_URL to enable)")
    uvicorn.run(
        create_app(
            str(args.db),
            seed=not args.empty,
            twilio_auth_token=twilio_auth_token,
            public_base_url=public_base_url,
            dialog360_api_key=dialog360_api_key,
            dialog360_webhook_token=dialog360_webhook_token,
        ),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
