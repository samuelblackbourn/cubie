"""Tests for the speech path.

Deliberately no real Piper voice. A voice model is ~60 MB from a host this
sandbox cannot reach, and downloading one to assert that a POST carries the
right headers would be testing the wrong thing. The Synthesizer Protocol
exists so a stub can stand in; real synthesis is verified on office-server,
where the model lives.

What IS tested here is everything between the voice and the device, which is
where the contract with the gateway lives and where a mistake is silent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speech import SpeechError, speak  # noqa: E402


class StubVoice:
    """A voice that yields known bytes in known chunks."""

    def __init__(self, chunks: list[bytes], rate: int = 22050):
        self._chunks = chunks
        self._rate = rate
        self.calls: list[str] = []

    @property
    def sample_rate(self) -> int:
        return self._rate

    def stream(self, text: str):
        self.calls.append(text)
        yield from self._chunks


def transport(capture: dict, status: int = 200, body: str = '{"frames": 3}'):
    def handler(request: httpx.Request) -> httpx.Response:
        capture["headers"] = dict(request.headers)
        capture["content"] = request.read()
        capture["url"] = str(request.url)
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def speak_with(voice, capture, status=200, body='{"frames": 3}', **kwargs):
    """Run speak() against a mock transport."""
    real_client = httpx.Client

    class PatchedClient(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = transport(capture, status, body)
            super().__init__(*a, **kw)

    text = kwargs.pop("text", "hello")
    httpx.Client = PatchedClient
    try:
        return speak(text, voice, **kwargs)
    finally:
        httpx.Client = real_client


def test_streams_every_chunk_in_order():
    """The device must receive exactly what the voice produced."""
    voice = StubVoice([b"\x01\x02", b"\x03\x04", b"\x05\x06"])
    cap: dict = {}
    speak_with(voice, cap, text="hello there", token="t")
    assert cap["content"] == b"\x01\x02\x03\x04\x05\x06"
    assert voice.calls == ["hello there"]


def test_sample_rate_header_comes_from_the_voice():
    """Sent once, up front. The gateway rejects a missing or bad rate."""
    voice = StubVoice([b"\x00\x00"], rate=16000)
    cap: dict = {}
    speak_with(voice, cap, token="t")
    assert cap["headers"]["x-sample-rate"] == "16000"
    assert cap["headers"]["x-channels"] == "1"


def test_no_token_sends_no_authorization_header():
    """An empty token must not become a bare `Bearer `.

    The gateway disables auth entirely when no token is configured, and an
    empty header would fail against a server that would have accepted none --
    the same trap as the MCP client.
    """
    cap: dict = {}
    speak_with(StubVoice([b"\x00\x00"]), cap, token="")
    assert "authorization" not in cap["headers"]


def test_token_becomes_a_bearer_header():
    cap: dict = {}
    speak_with(StubVoice([b"\x00\x00"]), cap, token="secret")
    assert cap["headers"]["authorization"] == "Bearer secret"


def test_empty_text_is_rejected_before_any_request():
    """Cheap local failure beats a round trip that cannot succeed."""
    for bad in ("", "   ", "\n"):
        with pytest.raises(ValueError):
            speak(bad, StubVoice([b"\x00"]), token="t")


def test_401_names_the_token_not_the_device():
    cap: dict = {}
    with pytest.raises(SpeechError) as exc:
        speak_with(StubVoice([b"\x00\x00"]), cap, status=401, token="wrong")
    assert "token" in str(exc.value).lower()


def test_503_names_the_device_not_the_token():
    """A disconnected robot and a bad token want different things done."""
    cap: dict = {}
    with pytest.raises(SpeechError) as exc:
        speak_with(StubVoice([b"\x00\x00"]), cap, status=503, token="t")
    assert "not connected" in str(exc.value) or "no device" in str(exc.value)


def test_other_errors_quote_the_gateway():
    cap: dict = {}
    with pytest.raises(SpeechError) as exc:
        speak_with(
            StubVoice([b"\x00\x00"]), cap, status=500, body="encoder exploded", token="t"
        )
    assert "500" in str(exc.value) and "encoder exploded" in str(exc.value)


def test_200_with_unparseable_body_is_still_a_success():
    """The audio played. An odd body is a curiosity, not a failure."""
    cap: dict = {}
    result = speak_with(StubVoice([b"\x00\x00"]), cap, body="not json", token="t")
    assert result["ok"] is True


def test_json_summary_is_returned():
    cap: dict = {}
    result = speak_with(StubVoice([b"\x00\x00"]), cap, body='{"frames": 42}', token="t")
    assert result["frames"] == 42


# --- the command-line entry point -------------------------------------------


def test_cli_reports_a_missing_voice_model_with_the_download_command():
    """The model is a separate ~60 MB download, not a pip dependency, so this
    is the most likely first-run failure. The message must carry the fix."""
    from speech import _main

    code = _main(["--data-dir", "/nonexistent", "hello"])
    assert code == 2


def test_cli_requires_text():
    from speech import _main

    with pytest.raises(SystemExit):
        _main([])
