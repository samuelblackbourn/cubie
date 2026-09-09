"""Tests for turning a delivered capture into words.

The recogniser itself is the gateway's, and deliberately not tested here --
`faster-whisper` has its own tests and its own model, and running it would
turn a 2-second bar into a 30-second one. What is tested is everything around
it, because that is where this file can be wrong: the empty transcript that is
a quiet turn rather than an error, the mangled body that must not be
transcribed at all, and the two lazy imports whose failures have to say which
one failed.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ogg_opus  # noqa: E402
import transcribe as transcribe_mod  # noqa: E402
from test_ogg_opus import stream  # noqa: E402


class Engine:
    """Stands in for the gateway's faster-whisper engine."""

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[bytes, dict]] = []

    async def transcribe(self, pcm, **opts):
        self.calls.append((pcm, opts))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def decode_to(pcm: bytes):
    def decode(frames):
        decode.frames = list(frames)
        return pcm
    decode.frames = []
    return decode


def run(coro):
    return asyncio.run(coro)


def test_a_capture_becomes_a_transcript():
    engine = Engine({"text": " what is waiting ", "language": "en"})
    decode = decode_to(b"\x00\x01" * 100)
    text = run(transcribe_mod.transcribe_capture(
        stream([b"frame-one", b"frame-two"]), engine=engine, decode=decode
    ))
    assert text == "what is waiting"
    # The frames handed to the decoder are the device's, headers dropped.
    assert decode.frames == [b"frame-one", b"frame-two"]


def test_the_language_is_passed_through():
    """The office speaks English and so does he. Autodetect on a three-word
    utterance is how you get a confident answer in Welsh."""
    engine = Engine({"text": "hello", "language": "en"})
    run(transcribe_mod.transcribe_capture(
        stream([b"f"]), engine=engine, decode=decode_to(b"\x00\x01")
    ))
    assert engine.calls[0][1]["language"] == "en"


def test_an_empty_transcript_is_a_quiet_turn_not_an_error():
    """A wake word in an empty room. The auto-stop's heard-speech guard makes
    it rare, not impossible, and None is what `Conversation` already knows how
    to answer."""
    engine = Engine({"text": "   ", "language": "en"})
    assert run(transcribe_mod.transcribe_capture(
        stream([b"f"]), engine=engine, decode=decode_to(b"\x00\x01")
    )) is None


def test_a_missing_text_key_is_a_quiet_turn():
    engine = Engine({"language": "en"})
    assert run(transcribe_mod.transcribe_capture(
        stream([b"f"]), engine=engine, decode=decode_to(b"\x00\x01")
    )) is None


def test_a_non_dict_result_is_a_quiet_turn_rather_than_a_crash():
    engine = Engine("just a string")
    assert run(transcribe_mod.transcribe_capture(
        stream([b"f"]), engine=engine, decode=decode_to(b"\x00\x01")
    )) is None


def test_a_capture_that_decodes_to_no_pcm_is_a_quiet_turn():
    engine = Engine({"text": "should not be reached"})
    assert run(transcribe_mod.transcribe_capture(
        stream([b"f"]), engine=engine, decode=decode_to(b"")
    )) is None
    assert engine.calls == []


def test_a_mangled_body_never_reaches_the_recogniser():
    """The whole reason the CRC is checked. Whisper would turn the noise into
    words and the brain would act on them."""
    body = bytearray(stream([b"a" * 60] * 20))
    body[-8] ^= 0x20
    engine = Engine({"text": "words nobody said"})
    with pytest.raises(ogg_opus.OggError):
        run(transcribe_mod.transcribe_capture(
            bytes(body), engine=engine, decode=decode_to(b"\x00\x01")
        ))
    assert engine.calls == []


def test_a_header_only_stream_holds_no_frames():
    """Two header packets and no audio. Well-formed, and nothing to say."""
    def head():
        return struct.pack("<8sBBHIhB", ogg_opus.OPUS_HEAD_MAGIC, 1, 1, 0, 16000, 0, 0)

    from test_ogg_opus import page, tags_packet
    body = (
        page(flags=ogg_opus.FLAG_BOS, granule=0, serial=1, sequence=0, segments=[head()])
        + page(flags=ogg_opus.FLAG_EOS, granule=0, serial=1, sequence=1,
               segments=[tags_packet()])
    )
    engine = Engine({"text": "should not be reached"})
    assert run(transcribe_mod.transcribe_capture(
        body, engine=engine, decode=decode_to(b"\x00\x01")
    )) is None
    assert engine.calls == []


# --- the lazy imports -------------------------------------------------------


def test_a_missing_gateway_names_the_interpreter():
    """`pa/.venv` cannot import the gateway at all, and this is the message a
    misconfigured service would get -- so it has to point at the cause rather
    than just failing."""
    import importlib.util

    # `sys.modules` is the wrong question: nothing has imported the gateway
    # yet, so it is absent there even on the box where it is installed -- and
    # this test would then fail on office-server, which is the one place it
    # matters that the suite is green.
    if importlib.util.find_spec("stackchan_mcp") is not None:
        pytest.skip("the gateway is importable here, so this cannot be provoked")
    with pytest.raises(transcribe_mod.TranscriberUnavailable) as caught:
        transcribe_mod.load_engine()
    assert "gateway's interpreter" in str(caught.value)


def test_an_unknown_engine_name_lists_what_is_registered(monkeypatch):
    """Two different faults -- no gateway, or a gateway without the [stt]
    extra -- and they need two different messages."""
    class Registry:
        def get(self, name):
            return None

        def names(self):
            return ["openai-whisper"]

    import types
    module = types.ModuleType("stackchan_mcp.stt")
    module.get_registry = lambda: Registry()
    parent = types.ModuleType("stackchan_mcp")
    monkeypatch.setitem(sys.modules, "stackchan_mcp", parent)
    monkeypatch.setitem(sys.modules, "stackchan_mcp.stt", module)

    with pytest.raises(transcribe_mod.TranscriberUnavailable) as caught:
        transcribe_mod.load_engine()
    message = str(caught.value)
    assert "faster-whisper" in message
    assert "openai-whisper" in message


def test_the_default_engine_matches_the_gateways_own_default():
    """Read off the gateway rather than restated, where the gateway is here.

    Its `listen` tool defaults to this engine. Two defaults that drift would
    mean the wake word and a tap used different models, and the only symptom
    would be different transcripts for the same words. Comparing our constant
    to a copy of itself would not notice, so this compares it to theirs and
    skips where theirs is not installed.
    """
    try:
        from stackchan_mcp.stt.orchestrator import DEFAULT_ENGINE
    except Exception:  # noqa: BLE001 - ImportError, or a missing extra
        pytest.skip(
            "stackchan_mcp is not importable here (pa/.venv has no gateway); "
            "this comparison runs on office-server"
        )
    assert transcribe_mod.DEFAULT_ENGINE == DEFAULT_ENGINE
