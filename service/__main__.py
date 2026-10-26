"""Start the local, synthetic-data demo: python3 -m service."""
import argparse
import os
from pathlib import Path

import uvicorn

from service.api import create_app


PROJECT_ROOT = Path(__file__).resolve().parent.parent


SUPPORTED_ENV_KEYS = {
    "PUBLIC_BASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "SUPABASE_MIRROR",
    "META_WHATSAPP_ACCESS_TOKEN",
    "META_APP_SECRET",
    "META_WEBHOOK_VERIFY_TOKEN",
    "META_WABA_ID",
    "META_PHONE_NUMBER_ID",
    "META_GRAPH_API_VERSION",
    "STEPFUN_API_KEY",
    "STEPFUN_VISION_MODEL",
    "STEPFUN_ASR_MODEL",
    "STEPFUN_TTS_MODEL",
    "STEPFUN_TTS_VOICE",
    "STEPFUN_BASE_URL",
}


def load_local_env(path):
    """Read supported literal settings from a small, non-executable .env file."""
    values = {}
    retired = {"TWILIO_AUTH_TOKEN", "DIALOG360_API_KEY", "DIALOG360_WEBHOOK_TOKEN"}
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
        if separator and key in retired:
            continue
        if not separator or key not in SUPPORTED_ENV_KEYS:
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

    def setting(name, default=None):
        return os.environ.get(name) or local_env.get(name) or default

    public_base_url = os.environ.get("PUBLIC_BASE_URL") or local_env.get("PUBLIC_BASE_URL")
    meta_settings = {
        "meta_access_token": setting("META_WHATSAPP_ACCESS_TOKEN"),
        "meta_app_secret": setting("META_APP_SECRET"),
        "meta_verify_token": setting("META_WEBHOOK_VERIFY_TOKEN"),
        "meta_waba_id": setting("META_WABA_ID"),
        "meta_phone_number_id": setting("META_PHONE_NUMBER_ID"),
        "meta_graph_api_version": setting("META_GRAPH_API_VERSION", "v23.0"),
    }
    meta_configured = public_base_url and all(meta_settings.values())
    vision_api_key = setting("STEPFUN_API_KEY")
    print("RestoPilot demo: manual intake, WhatsApp simulator, rules extraction, SQLite, local demo identity.")
    print(f"Open http://127.0.0.1:{args.port} · Database: {args.db}")
    print("Meta WhatsApp Cloud API: configured" if meta_configured else "Meta WhatsApp Cloud API: disabled (see .env.example)")
    print("StepFun Vision: configured" if vision_api_key else "StepFun Vision: disabled (set STEPFUN_API_KEY)")
    uvicorn.run(
        create_app(
            str(args.db),
            seed=not args.empty,
            public_base_url=public_base_url,
            vision_api_key=vision_api_key,
            vision_model=setting("STEPFUN_VISION_MODEL"),
            vision_base_url=setting("STEPFUN_BASE_URL"),
            asr_model=setting("STEPFUN_ASR_MODEL"),
            tts_model=setting("STEPFUN_TTS_MODEL"),
            tts_voice=setting("STEPFUN_TTS_VOICE"),
            **meta_settings,
        ),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
