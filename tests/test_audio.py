"""StepFun speech recognition and synthesis; the provider is a local fake."""

import asyncio

import pytest

from service.audio import (
    StepFunAudioError,
    synthesize_speech,
    transcribe_audio,
    validate_asr_model,
    validate_tts_model,
)

OGG = b"OggS" + b"\x00" * 64
ASR_URL = "https://api.stepfun.com/v1/audio/transcriptions"
TTS_URL = "https://api.stepfun.com/v1/audio/speech"


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeAudioClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers=None, json=None, files=None, data=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "files": files, "data": data})
        return self.responses.pop(0)


def run_transcribe(data=OGG, mime="audio/ogg", client=None, **kwargs):
    return asyncio.run(transcribe_audio(
        data,
        mime,
        api_key=kwargs.pop("api_key", "synthetic-stepfun-key"),
        client=client,
        **kwargs,
    ))


def run_speak(text="Ringkasan 7 hari: 1 pembelian.", client=None, **kwargs):
    return asyncio.run(synthesize_speech(
        text,
        api_key=kwargs.pop("api_key", "synthetic-stepfun-key"),
        client=client,
        **kwargs,
    ))


def test_transcribe_posts_multipart_and_returns_the_text():
    client = FakeAudioClient()
    client.responses.append(FakeResponse(body={"text": "  Cabai 3 kg 150rb  "}))
    assert run_transcribe(client=client) == "Cabai 3 kg 150rb"

    call = client.calls[0]
    assert call["url"] == ASR_URL
    assert call["headers"]["Authorization"] == "Bearer synthetic-stepfun-key"
    assert "Content-Type" not in call["headers"]
    assert call["data"] == {"model": "step-asr-1.1", "response_format": "json"}
    assert call["files"]["file"] == ("voice.ogg", OGG, "audio/ogg")


def test_transcribe_rejects_a_provider_error_without_leaking_the_key():
    client = FakeAudioClient()
    client.responses.append(FakeResponse(status_code=401, body={"error": {"message": "bad key"}}))
    with pytest.raises(StepFunAudioError) as error:
        run_transcribe(client=client)
    assert error.value.status == 502
    assert "synthetic-stepfun-key" not in str(error.value)
    assert "bad key" not in str(error.value)


def test_transcribe_requires_a_key():
    with pytest.raises(StepFunAudioError) as error:
        run_transcribe(client=FakeAudioClient(), api_key="")
    assert error.value.status == 503


def test_transcribe_rejects_an_oversize_voice_note():
    client = FakeAudioClient()
    with pytest.raises(StepFunAudioError) as error:
        run_transcribe(data=b"x" * (5 * 1024 * 1024 + 1), client=client)
    assert error.value.status == 413
    assert client.calls == []


def test_transcribe_rejects_an_unsupported_type_and_empty_audio():
    client = FakeAudioClient()
    with pytest.raises(StepFunAudioError) as error:
        run_transcribe(mime="video/mp4", client=client)
    assert error.value.status == 415
    with pytest.raises(StepFunAudioError) as error:
        run_transcribe(data=b"", client=client)
    assert error.value.status == 400
    assert client.calls == []


def test_transcribe_rejects_an_empty_or_unreadable_transcript():
    client = FakeAudioClient()
    client.responses.append(FakeResponse(body={"text": "   "}))
    with pytest.raises(StepFunAudioError) as error:
        run_transcribe(client=client)
    assert error.value.status == 502
    client.responses.append(FakeResponse(body={"detail": "no text field"}))
    with pytest.raises(StepFunAudioError) as error:
        run_transcribe(client=client)
    assert error.value.status == 502


def test_speak_returns_audio_bytes():
    client = FakeAudioClient()
    client.responses.append(FakeResponse(content=b"ID3mp3-bytes", headers={"content-type": "audio/mpeg"}))
    assert run_speak(client=client) == b"ID3mp3-bytes"

    call = client.calls[0]
    assert call["url"] == TTS_URL
    assert call["headers"]["Authorization"] == "Bearer synthetic-stepfun-key"
    assert call["json"] == {
        "model": "step-tts-2",
        "input": "Ringkasan 7 hari: 1 pembelian.",
        "voice": "linjiameimei",
        "response_format": "mp3",
    }


def test_speak_rejects_an_error_json_response():
    client = FakeAudioClient()
    client.responses.append(FakeResponse(status_code=500, body={"error": {"message": "boom"}}))
    with pytest.raises(StepFunAudioError) as error:
        run_speak(client=client)
    assert error.value.status == 502


def test_speak_rejects_a_non_audio_response():
    client = FakeAudioClient()
    client.responses.append(FakeResponse(body={"text": "not audio"}))
    with pytest.raises(StepFunAudioError) as error:
        run_speak(client=client)
    assert error.value.status == 502


def test_speak_requires_a_key_and_bounded_text():
    with pytest.raises(StepFunAudioError) as error:
        run_speak(client=FakeAudioClient(), api_key="")
    assert error.value.status == 503
    client = FakeAudioClient()
    with pytest.raises(StepFunAudioError) as error:
        run_speak("a" * 4001, client=client)
    assert error.value.status == 413
    assert client.calls == []


def test_model_and_base_url_validation():
    assert validate_asr_model("") == "step-asr-1.1"
    assert validate_asr_model(" step-asr-1.1 ") == "step-asr-1.1"
    assert validate_tts_model(None) == "step-tts-2"
    with pytest.raises(ValueError, match="STEPFUN_ASR_MODEL"):
        validate_asr_model("two models")
    with pytest.raises(ValueError, match="STEPFUN_TTS_MODEL"):
        validate_tts_model("x" * 101)
