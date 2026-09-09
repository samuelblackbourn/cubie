"""Turn a delivered capture into words, using the gateway's own recogniser.

Nothing here recognises speech. It borrows the engine the `listen` tool
already uses -- `stackchan_mcp.stt`'s registry, `faster-whisper` by default --
which is the whole point: one model, one cache, one set of settings, and a
transcript from the hook path that matches a transcript from tap-to-talk
instead of merely resembling it. `STACKCHAN_FASTER_WHISPER_MODEL` keeps working
because it is the engine's own environment variable, not ours.

That is also why this is a separate file from `hook.py`. The receiver is
stdlib-only and therefore importable and testable in `pa/.venv`; the engine
lives in the *gateway's* virtualenv and cannot be imported there at all. So
every gateway import in this module is lazy and behind a named failure, and
both halves of the pipeline take injected collaborators -- which is what lets
the tests drive the whole path with no model, no opuslib and no robot.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Sequence

import ogg_opus

logger = logging.getLogger("cubie.transcribe")

#: The gateway's own default, mirrored rather than re-decided.
DEFAULT_ENGINE = "faster-whisper"

#: The device speaks one language and so does the office.
DEFAULT_LANGUAGE = "en"


class TranscriberUnavailable(RuntimeError):
    """The recogniser could not be reached, and the reason is in the message.

    Distinct from "heard nothing": this means the capture was never given a
    chance, which is a configuration fault to fix rather than a quiet turn.
    """


def load_engine(name: str = DEFAULT_ENGINE) -> Any:
    """Return the gateway's registered STT engine called `name`.

    Raises:
        TranscriberUnavailable: the gateway package is not importable (wrong
            interpreter -- `live.py` documents that it must run on the
            gateway's), or the engine did not register because the `[stt]`
            extra is missing. Both are named, because "no transcript" with no
            reason is the failure this whole path exists to remove.
    """
    try:
        from stackchan_mcp.stt import get_registry
    except ImportError as exc:
        raise TranscriberUnavailable(
            "stackchan_mcp.stt is not importable -- run this with the "
            f"gateway's interpreter, not pa/.venv ({exc})"
        ) from exc

    registry = get_registry()
    engine = registry.get(name)
    if engine is None:
        available = ", ".join(registry.names()) or "none"
        raise TranscriberUnavailable(
            f"no STT engine called {name!r} is registered (available: {available}); "
            "the gateway's [stt] extra installs faster-whisper"
        )
    return engine


def load_decoder() -> Callable[[Sequence[bytes]], bytes]:
    """Return the gateway's Opus frame decoder.

    Separate from `load_engine` so a missing codec and a missing recogniser
    say different things -- they are installed by the same extra, but a
    partially built virtualenv is exactly when a precise message pays.
    """
    try:
        from stackchan_mcp.stt.audio_utils import decode_opus_frames
    except ImportError as exc:
        raise TranscriberUnavailable(
            f"stackchan_mcp.stt.audio_utils is not importable ({exc})"
        ) from exc
    return decode_opus_frames


async def transcribe_capture(
    body: bytes,
    *,
    engine: Any,
    decode: Callable[[Sequence[bytes]], bytes],
    language: str | None = DEFAULT_LANGUAGE,
) -> str | None:
    """Ogg/Opus bytes in, a transcript or None out.

    None means the capture held no words -- an empty transcript from the
    recogniser, which is what a wake word in an empty room produces. It is a
    quiet turn, not an error.

    Raises:
        ogg_opus.OggError: the body is not a well-formed Ogg/Opus stream.
            Propagated rather than swallowed: a mangled body is worth a loud
            log, because transcribing it would invent words nobody said.
    """
    frames = ogg_opus.opus_frames(body)
    if not frames:
        logger.info("capture held no audio frames")
        return None

    # Both of these are CPU work in a C library. The engine already runs its
    # model in a worker thread; the decoder does not, and a 15 second capture
    # is 250 frames, so it goes to a thread too rather than stalling the tick
    # that drives breathing and blink.
    pcm = await asyncio.to_thread(decode, frames)
    if not pcm:
        logger.warning("decoded %d frames to no PCM at all", len(frames))
        return None

    result = await engine.transcribe(pcm, language=language)
    text = (result or {}).get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        logger.info(
            "transcribed %d frames (%d PCM bytes) to nothing", len(frames), len(pcm)
        )
        return None

    logger.info("transcript: %s", text.strip())
    return text.strip()
