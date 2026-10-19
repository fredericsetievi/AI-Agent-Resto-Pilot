"""Vercel serverless entry point for the hosted RestoPilot demo.

Local development still uses `python -m service` (see service/__main__.py);
this module only adapts the same ASGI app to Vercel's Python runtime.

Required environment variable:
    PUBLIC_DEMO=1        Opt in to the hosted-demo boundary (no auth; public URL).

Optional environment variables:
    DEMO_DB_PATH         SQLite file location. Defaults to Vercel's /tmp, which
                         is ephemeral: state resets when the function recycles.
    DEMO_SEED=0          Start with an empty database instead of the synthetic
                         opening purchases.
    TWILIO_AUTH_TOKEN    Enables the signed Twilio webhook route.
    PUBLIC_BASE_URL      Public origin of this deployment, for webhook signature
                         host matching (e.g. https://your-app.vercel.app).
    DIALOG360_API_KEY    Enables 360dialog sandbox API replies.
    DIALOG360_WEBHOOK_TOKEN
                         URL-safe secret embedded in the sandbox callback path.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.api import create_app

app = create_app(
    os.environ.get("DEMO_DB_PATH", "/tmp/restopilot.sqlite3"),
    seed=os.environ.get("DEMO_SEED", "1") != "0",
    public_demo=os.environ.get("PUBLIC_DEMO") == "1",
    twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN"),
    public_base_url=os.environ.get("PUBLIC_BASE_URL"),
    dialog360_api_key=os.environ.get("DIALOG360_API_KEY"),
    dialog360_webhook_token=os.environ.get("DIALOG360_WEBHOOK_TOKEN"),
)
