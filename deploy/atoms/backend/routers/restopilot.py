"""Mount the RestoPilot FastAPI app (feat/integration deployment copy) under Atoms.

Atoms Cloud proxies only ``/api/v1/*`` to this backend, so the original
RestoPilot routes (``/v1/...``, ``/health/...``, ``/webhooks/...``) are exposed
as ``/api/v1/restopilot/<original path>``. Requests are forwarded in-process to
the WHOLE original ASGI app built by ``service.api.create_app`` (middleware,
DomainError/validation handlers and routes unchanged).

The app runs in hosted mode: the existing Supabase Postgres is the only ledger,
startup performs a read-only schema check, and missing or unreachable database
configuration fails closed with a JSON ``storage_unavailable`` error (never a
SQLite fallback). Configuration comes only from server-side environment:
``SUPABASE_DB_PASSWORD`` (required) and optionally ``SUPABASE_DB_HOST`` /
``SUPABASE_DB_PORT`` / ``SUPABASE_DB_USER`` for the Supabase session pooler.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "restopilot_app"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

# Hosted mode: never read a .env file; the project URL is public, not a secret.
os.environ["RESTOPILOT_HOSTED"] = "1"
os.environ.setdefault("SUPABASE_URL", "https://dptyxkmomskjvwbrqpnx.supabase.co")

from service.api import create_app  # noqa: E402  (vendored deployment copy)

PREFIX = "/api/v1/restopilot"
_HOP_BY_HOP = {"content-length", "transfer-encoding", "connection", "content-encoding", "set-cookie"}
_REQUEST_DROP = {"host", "content-length"}
_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS", "HEAD"]

router = APIRouter(prefix=PREFIX, tags=["restopilot"])
_app = None


def _restopilot_app():
    """Build the original app once in hosted (Supabase, read-only startup) mode."""
    global _app
    if _app is None:
        env = os.environ.get
        _app = create_app(
            None,
            seed=False,
            hosted=True,
            public_demo=True,
            public_base_url=env("PUBLIC_BASE_URL"),
            meta_access_token=env("META_WHATSAPP_ACCESS_TOKEN"),
            meta_app_secret=env("META_APP_SECRET"),
            meta_verify_token=env("META_WEBHOOK_VERIFY_TOKEN"),
            meta_waba_id=env("META_WABA_ID"),
            meta_phone_number_id=env("META_PHONE_NUMBER_ID"),
            meta_graph_api_version=env("META_GRAPH_API_VERSION", "v23.0"),
            vision_api_key=env("STEPFUN_API_KEY"),
            vision_model=env("STEPFUN_VISION_MODEL"),
            vision_base_url=env("STEPFUN_BASE_URL"),
            asr_model=env("STEPFUN_ASR_MODEL"),
            tts_model=env("STEPFUN_TTS_MODEL"),
            tts_voice=env("STEPFUN_TTS_VOICE"),
        )
    return _app


@router.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
async def forward(path: str, request: Request) -> Response:
    """Forward the request verbatim to the original RestoPilot app."""
    target = "/" + path
    if request.url.query:
        target += "?" + request.url.query
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP]
    transport = httpx.ASGITransport(app=_restopilot_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://restopilot.internal") as client:
        upstream = await client.request(request.method, target, content=await request.body(), headers=headers)
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP},
    )
    # Every Set-Cookie header must survive (login sets, logout clears).
    for name, value in upstream.headers.multi_items():
        if name.lower() == "set-cookie":
            response.headers.append("set-cookie", value)
    return response
