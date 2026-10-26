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
    PUBLIC_BASE_URL      Public origin of this deployment, for webhook signature
                         host matching (e.g. https://your-app.vercel.app).
    META_WHATSAPP_ACCESS_TOKEN
    META_APP_SECRET
    META_WEBHOOK_VERIFY_TOKEN
    META_WABA_ID
    META_PHONE_NUMBER_ID Enable the Meta WhatsApp Cloud API transport.
    META_GRAPH_API_VERSION
                         Pinned Graph version; defaults to v23.0.
    STEPFUN_API_KEY      Enables invoice-photo vision, voice-note ASR and the
                         TTS voice reply; without it photos and voice notes get
                         the safe text reply and "ringkas" replies with text.
    STEPFUN_VISION_MODEL
    STEPFUN_ASR_MODEL
    STEPFUN_TTS_MODEL
    STEPFUN_TTS_VOICE    StepFun model/voice overrides; service defaults apply.
    STEPFUN_BASE_URL     StepFun API base; defaults to api.stepfun.com/v1.
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
    public_base_url=os.environ.get("PUBLIC_BASE_URL"),
    meta_access_token=os.environ.get("META_WHATSAPP_ACCESS_TOKEN"),
    meta_app_secret=os.environ.get("META_APP_SECRET"),
    meta_verify_token=os.environ.get("META_WEBHOOK_VERIFY_TOKEN"),
    meta_waba_id=os.environ.get("META_WABA_ID"),
    meta_phone_number_id=os.environ.get("META_PHONE_NUMBER_ID"),
    meta_graph_api_version=os.environ.get("META_GRAPH_API_VERSION", "v23.0"),
    vision_api_key=os.environ.get("STEPFUN_API_KEY"),
    vision_model=os.environ.get("STEPFUN_VISION_MODEL"),
    vision_base_url=os.environ.get("STEPFUN_BASE_URL"),
    asr_model=os.environ.get("STEPFUN_ASR_MODEL"),
    tts_model=os.environ.get("STEPFUN_TTS_MODEL"),
    tts_voice=os.environ.get("STEPFUN_TTS_VOICE"),
)
