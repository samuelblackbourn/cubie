"""Cubie's voice: local synthesis, streamed to the device speaker.

Nothing leaves the LAN. Piper runs here on office-server, and the PCM goes
straight to the gateway's ``POST /pcm`` endpoint, which encodes Opus and
pushes frames over the WebSocket the device already holds open.

--- Why not the gateway's own `say` tool ---

`say` synthesises with a registered TTS engine: VOICEVOX, Edge TTS, ElevenLabs
or Irodori. Of those only VOICEVOX is local, and it is built for Japanese --
it would satisfy "nothing leaves the network" while sounding wrong in English.
Adding a Piper engine to the registry would mean forking the gateway, which
costs the property that it is a pinned PyPI release we run unmodified.

`POST /pcm` avoids the whole question: it accepts finished audio from an
external producer. So we keep the pinned gateway, get a natural English voice,
and stay on the LAN. The one thing `say` does that this does not is map
expression emoji onto the avatar's face -- and we would rather choose the
expression deliberately in the brain than infer it from a regex.

Lip-sync still works: it is driven by the FIRMWARE off the `tts.start` state
transition, and `send_pcm_stream` sends that transition. It is not something
the gateway times per-syllable, so it does not care where the audio came from.

--- Why streaming ---

Piper yields audio chunks as it synthesises. Sending them as they arrive means
the device starts speaking while the rest of the sentence is still being made,
which for a long reply is the difference between a natural answer and a pause
that reads as the robot having crashed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, Iterator, Protocol

import httpx

logger = logging.getLogger(__name__)

#: The gateway's capture server. Loopback because the PA service runs on the
#: same host as the gateway -- the same reason the MCP control port does.
DEFAULT_PCM_URL = "http://127.0.0.1:8766/pcm"

#: Piper ships several English voices. `alba` is a Scottish-accented medium
#: model; `alan` and `cori` are the other reasonable British options. Medium
#: rather than high: on office-server's CPU, high roughly triples synthesis
#: time for a difference that is inaudible through a 1 W robot speaker.
DEFAULT_VOICE = "en_GB-alba-medium"


class Synthesizer(Protocol):
    """What `speak` needs from a voice.

    A Protocol rather than a concrete class so tests can supply a stub: the
    interesting behaviour here is the streaming and the HTTP contract, and
    downloading a 60 MB voice model to assert that a POST has the right
    headers would test the wrong thing.
    """

    @property
    def sample_rate(self) -> int:
        """Output rate in Hz. Fixed per voice, known before synthesis."""

    def stream(self, text: str) -> Iterator[bytes]:
        """Yield signed 16-bit little-endian mono PCM as it is produced."""


@dataclass
class PiperSynthesizer:
    """Piper, loaded once and reused.

    Model load is the expensive part (hundreds of milliseconds and tens of
    megabytes); synthesis afterwards is fast. A long-lived PA service pays it
    once at startup, which is why this is a held object rather than a function.
    """

    voice_name: str = DEFAULT_VOICE
    data_dir: str = os.path.expanduser("~/.local/share/piper-voices")
    _voice: object | None = None

    def _load(self):
        if self._voice is None:
            from piper import PiperVoice  # imported late: heavy, and optional

            path = os.path.join(self.data_dir, f"{self.voice_name}.onnx")
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"Piper voice not found at {path}. Download it once with:\n"
                    f"  python -m piper.download_voices {self.voice_name} "
                    f"--data-dir {self.data_dir}"
                )
            logger.info("loading Piper voice %s", self.voice_name)
            self._voice = PiperVoice.load(path)
        return self._voice

    @property
    def sample_rate(self) -> int:
        return int(self._load().config.sample_rate)

    def stream(self, text: str) -> Iterator[bytes]:
        for chunk in self._load().synthesize(text):
            # Piper's AudioChunk exposes the raw bytes under a couple of names
            # across versions. Prefer the explicit int16 accessor and fall back
            # rather than pinning to one spelling -- the same lesson as the MCP
            # SDK's isError/is_error, where guessing one silently misbehaved.
            data = getattr(chunk, "audio_int16_bytes", None)
            if data is None:
                data = getattr(chunk, "audio_int16_array", None)
                if data is not None:
                    data = data.tobytes()
            if data is None:
                raise RuntimeError(
                    "Piper AudioChunk exposed neither audio_int16_bytes nor "
                    "audio_int16_array -- unsupported piper-tts version"
                )
            yield data


class SpeechError(RuntimeError):
    """The device did not play the audio, and the reason is in the message."""


def speak(
    text: str,
    synthesizer: Synthesizer,
    *,
    url: str | None = None,
    token: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Synthesise `text` and stream it to the device speaker.

    Returns the gateway's JSON summary (frame count, duration).

    Raises SpeechError with a message naming the actual problem: a 401 means
    the token, a 503 means the robot is not connected, and those want
    different things done about them.
    """
    if not text or not text.strip():
        raise ValueError("text must be non-empty")

    url = url or os.environ.get("CUBIE_PCM_URL", DEFAULT_PCM_URL)
    token = token if token is not None else os.environ.get("STACKCHAN_TOKEN", "")

    headers = {
        # Sent once, before any audio: the rate is a property of the voice,
        # not of a chunk, so it is known up front and cannot change mid-stream.
        "X-Sample-Rate": str(synthesizer.sample_rate),
        "X-Channels": "1",
        "Content-Type": "application/octet-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def body() -> Iterable[bytes]:
        total = 0
        for chunk in synthesizer.stream(text):
            total += len(chunk)
            yield chunk
        logger.info(
            "streamed %d bytes of PCM (%.1f s at %d Hz)",
            total,
            total / 2 / max(synthesizer.sample_rate, 1),
            synthesizer.sample_rate,
        )

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, content=body())
    except httpx.HTTPError as exc:
        raise SpeechError(f"could not reach the gateway at {url}: {exc}") from exc

    if response.status_code == 401:
        raise SpeechError(
            "gateway rejected the bearer token for /pcm. It reads "
            "STACKCHAN_TOKEN (or BEARER_TOKEN) from its EnvironmentFile."
        )
    if response.status_code == 503:
        raise SpeechError("no device connected -- Cubie is not on the gateway")
    if response.status_code >= 400:
        raise SpeechError(
            f"gateway returned {response.status_code}: {response.text[:200]}"
        )

    try:
        return response.json()
    except ValueError:
        # A 200 with an unparseable body still played the audio. Report the
        # oddity rather than turning a success into a failure.
        logger.warning("gateway returned 200 with a non-JSON body")
        return {"ok": True, "body": response.text[:200]}
