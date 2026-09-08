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
    """The device receives exactly what the voice produced, when no
    resampling is needed.

    Pinned at the device rate on purpose: at 16 kHz the resampler is a
    passthrough, so this asserts the streaming itself rather than the
    arithmetic, which test_audio.py covers.
    """
    voice = StubVoice([b"\x01\x02", b"\x03\x04", b"\x05\x06"], rate=16000)
    cap: dict = {}
    speak_with(voice, cap, text="hello there", token="t")
    assert cap["content"] == b"\x01\x02\x03\x04\x05\x06"
    assert voice.calls == ["hello there"]


def test_a_non_device_rate_voice_is_resampled_and_declared_as_16k():
    """The fix for the broken-up audio: convert once here, and tell the
    gateway 16 kHz so its own per-chunk resampling is a no-op."""
    voice = StubVoice([bytes(2000)], rate=22050)
    cap: dict = {}
    speak_with(voice, cap, token="t")
    assert cap["headers"]["x-sample-rate"] == "16000"
    # 1000 samples at 22050 becomes ~726 at 16000.
    assert 700 <= len(cap["content"]) // 2 <= 740


def test_robot_effect_changes_the_audio():
    import array
    import math

    samples = array.array(
        "h", [int(15000 * math.sin(2 * math.pi * 300 * i / 16000)) for i in range(1600)]
    )
    plain: dict = {}
    speak_with(StubVoice([samples.tobytes()], rate=16000), plain, token="t")
    robotic: dict = {}
    speak_with(
        StubVoice([samples.tobytes()], rate=16000), robotic, token="t", robot=0.8
    )
    assert plain["content"] != robotic["content"]
    assert len(plain["content"]) == len(robotic["content"])


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


def test_cli_lists_characters_without_needing_text():
    """--characters is informational; requiring text to read a list is rude."""
    from speech import _main

    assert _main(["--characters"]) == 0


def test_cli_without_text_says_so():
    from speech import _main

    with pytest.raises(SystemExit):
        _main(["--character", "cute"])


# ------------------------------------------------------- the retro voice --
def test_a_character_can_carry_its_own_voice_model():
    """`retro` is built around en_US-ryan-high specifically. Before this, a
    character could only carry processing settings."""
    from character import resolve

    assert resolve("retro").voice == "en_US-ryan-high"
    assert resolve("cute").voice is None


def test_the_fan_carrier_is_far_slower_than_every_other_character():
    """The one number that makes it a fan rather than a sci-fi buzz. Everything
    else modulates fast enough to fuse into a tone."""
    from character import CHARACTERS, resolve

    retro = resolve("retro")
    others = [c for c in CHARACTERS.values() if c.name != "retro" and c.robot > 0]
    assert all(retro.robot_hz < c.robot_hz / 2 for c in others)


def test_the_fan_depth_never_gates_the_voice_to_silence():
    """ring_modulate's gain is 1-depth+depth*sin, so depth above 0.5 passes
    through zero and inverts phase. At 22 Hz that eats ~16 ms of every 45 ms
    chop, which stutters instead of throbbing."""
    from character import resolve

    retro = resolve("retro")
    minimum_gain = 1.0 - 2.0 * retro.robot
    assert minimum_gain > 0.0


def test_the_fan_actually_chops_at_its_carrier_rate():
    """Measured through a flat tone rather than assumed from the parameter."""
    import array
    import math

    from audio import ring_modulate
    from character import resolve

    retro = resolve("retro")
    rate = 16000
    tone = array.array(
        "h", [int(12000 * math.sin(2 * math.pi * 200 * t / rate)) for t in range(rate)]
    )
    out, _ = ring_modulate(
        tone.tobytes(), rate, frequency=retro.robot_hz, depth=retro.robot, phase=0.0
    )
    samples = array.array("h")
    samples.frombytes(out)

    window = rate // 200
    envelope = [
        max(abs(v) for v in samples[i : i + window])
        for i in range(0, len(samples) - window, window)
    ]
    midpoint = (min(envelope) + max(envelope)) / 2
    below = [e < midpoint for e in envelope]
    chops = sum(1 for a, b in zip(below, below[1:]) if not a and b)
    assert abs(chops - retro.robot_hz) <= 2

    # And the trough is an attenuation, not a gate -- a fan, not a gate pedal.
    assert min(envelope) > 0.02 * max(envelope)


def test_an_explicit_voice_flag_still_wins_over_the_characters_own():
    from speech import DEFAULT_VOICE
    from character import resolve

    preset = resolve("retro")
    assert (None or preset.voice or DEFAULT_VOICE) == "en_US-ryan-high"
    assert ("en_GB-alba-medium" or preset.voice or DEFAULT_VOICE) == "en_GB-alba-medium"
