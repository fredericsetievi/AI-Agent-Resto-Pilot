"""StepFun speech recognition and synthesis for WhatsApp voice notes.

A transcript is only proposed purchase text: it re-enters the same rule-based
extraction and owner review as typed messages, so nothing the model hears is
trusted until the owner approves it. The summary voice reply is an optional
enhancement; every failure falls back to the plain text summary.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .whatsapp import AUDIO_MEDIA_TYPES


DEFAULT_BASE_URL = "https://api.stepfun.com/v1"
DEFAULT_ASR_MODEL = "step-asr-1.1"
DEFAULT_TTS_MODEL = "step-tts-2"
DEFAULT_TTS_VOICE = "linjiameimei"
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 10_000
MAX_TTS_CHARS = 4_000
_MEDIA_FILENAMES = {
    "audio/ogg": "voice.ogg",
    "audio/mpeg": "voice.mp3",
    "audio/mp4": "voice.m4a",
    "audio/aac": "voice.aac",
    "audio/amr": "voice.amr",
}


class StepFunAudioError(Exception):
    """Safe provider error that never includes the API key or response bodies."""

    def __init__(self, message, status=502):
        self.status = status
        super().__init__(message)


def validate_base_url(value):
    value = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(value)
    parsed.port
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("STEPFUN_BASE_URL must be an HTTPS URL without credentials, query, or fragment.")
    return value


def validate_asr_model(value):
    value = (value or DEFAULT_ASR_MODEL).strip()
    if not value or len(value) > 100 or any(char.isspace() for char in value):
        raise ValueError("STEPFUN_ASR_MODEL must be a single model name such as step-asr-1.1.")
    return value


def validate_tts_model(value):
    value = (value or DEFAULT_TTS_MODEL).strip()
    if not value or len(value) > 100 or any(char.isspace() for char in value):
        raise ValueError("STEPFUN_TTS_MODEL must be a single model name such as step-tts-2.")
    return value


def _check_audio(raw, mime):
    if mime not in AUDIO_MEDIA_TYPES:
        raise StepFunAudioError("Use an OGG, MP3, MP4, AAC, or AMR voice note.", 415)
    if not raw:
        raise StepFunAudioError("The voice note is empty.", 400)
    if len(raw) > MAX_AUDIO_BYTES:
        raise StepFunAudioError("The voice note exceeds 5 MiB.", 413)
    return raw


async def transcribe_audio(data, mime, *, api_key, model=DEFAULT_ASR_MODEL, base_url=DEFAULT_BASE_URL, language=None, client=None):
    """Send one WhatsApp voice note to StepFun ASR and return its transcript."""
    if not api_key or not api_key.strip():
        raise StepFunAudioError("StepFun speech recognition is not configured.", 503)
    base_url = validate_base_url(base_url)
    model = validate_asr_model(model)
    language = (language or "").strip().lower() or None
    data = _check_audio(data, mime)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
    try:
        try:
            response = await client.post(
                base_url + "/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (_MEDIA_FILENAMES.get(mime, "voice"), data, mime)},
                data={"model": model, "response_format": "json", **({"language": language} if language else {})},
            )
        except httpx.HTTPError:
            raise StepFunAudioError("StepFun speech recognition could not be reached; try again.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunAudioError(f"StepFun speech recognition rejected the voice note (HTTP {response.status_code}).", status)
    try:
        text = response.json()["text"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFunAudioError("StepFun speech recognition returned an unexpected response.")
    if not isinstance(text, str) or not text.strip():
        raise StepFunAudioError("StepFun speech recognition returned an empty transcript.")
    return text.strip()[:MAX_TRANSCRIPT_CHARS]


async def synthesize_speech(text, *, api_key, model=DEFAULT_TTS_MODEL, voice=DEFAULT_TTS_VOICE, base_url=DEFAULT_BASE_URL, response_format="mp3", client=None):
    """Send one short text to StepFun TTS and return the synthesized audio bytes."""
    if not api_key or not api_key.strip():
        raise StepFunAudioError("StepFun speech synthesis is not configured.", 503)
    base_url = validate_base_url(base_url)
    model = validate_tts_model(model)
    if not isinstance(text, str) or not text.strip():
        raise StepFunAudioError("No text was provided to speak.", 400)
    if len(text) > MAX_TTS_CHARS:
        raise StepFunAudioError("The text to speak exceeds 4000 characters.", 413)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
    try:
        try:
            response = await client.post(
                base_url + "/audio/speech",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "input": text, "voice": voice, "response_format": response_format},
            )
        except httpx.HTTPError:
            raise StepFunAudioError("StepFun speech synthesis could not be reached; try again.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunAudioError(f"StepFun speech synthesis rejected the text (HTTP {response.status_code}).", status)
    if not response.headers.get("content-type", "").startswith("audio/"):
        raise StepFunAudioError("StepFun speech synthesis returned a non-audio response.")
    if not response.content:
        raise StepFunAudioError("StepFun speech synthesis returned no audio.")
    return response.content
