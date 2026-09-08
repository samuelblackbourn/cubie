"""PCM shaping: resampling, and making him sound like a robot.

--- Why we resample here rather than letting the gateway do it ---

`POST /pcm` accepts any sample rate and resamples to the device's 16 kHz. But
it reads the request body in 8192-byte chunks and resamples EACH CHUNK
INDEPENDENTLY -- its own docstring says so, and calls the error "negligible
for speech-rate inputs".

It is not negligible at 22050 Hz. The ratio 22050/16000 is 1.378, so chunk
boundaries do not fall on whole output samples, and an independent linear
resample restarts its interpolation at each boundary. An 8192-byte chunk is
186 ms of 22050 Hz mono, so a 4.6 s sentence crosses about 25 boundaries and
picks up a discontinuity at every one. Heard on the robot, that is a voice
that is "a bit broken up".

So: resample once, here, with the interpolator's state carried ACROSS chunks,
and send 16 kHz. The gateway then finds the input already at the device rate
and its resample step is a documented no-op. Fewer bytes on the wire, too.

--- Robot voice ---

We own the PCM before it leaves the machine, so the effects are ours to apply.
A ring modulator -- multiplying the waveform by a sine -- is the classic
robot-voice trick and what most sci-fi robots actually are. Bit-crushing
(quantising to fewer levels) adds the cheap-hardware edge on top.

Both are deliberately simple and dependency-free: numpy would be a heavier
install for arithmetic this basic, and keeping it in the standard library
means the PA service's dependency list stays short enough to read.
"""

from __future__ import annotations

import array
import math

#: What the device wants. Matching it exactly is what makes the gateway's
#: per-chunk resampling a no-op instead of a source of artefacts.
DEVICE_RATE = 16000


class Resampler:
    """Linear resampler that keeps its place between calls.

    Stateful on purpose. The whole point is that feeding it a stream in pieces
    gives the same output as feeding it the stream whole -- which is exactly
    what per-chunk independent resampling fails to do.
    """

    def __init__(self, source_rate: int, target_rate: int = DEVICE_RATE):
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._ratio = source_rate / target_rate
        # Fractional read position within the source stream, carried across
        # calls. This is the state that removes the boundary discontinuity.
        self._position = 0.0
        # Last sample of the previous chunk, so interpolation can span the
        # join rather than starting afresh inside the new chunk.
        self._previous: int | None = None

    @property
    def passthrough(self) -> bool:
        """True when no conversion is needed, so callers can skip the work."""
        return self.source_rate == self.target_rate

    def feed(self, pcm: bytes) -> bytes:
        """Resample one chunk, continuing from where the last call ended."""
        if self.passthrough:
            return pcm
        if not pcm:
            return b""

        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not samples:
            return b""

        # Prepend the previous chunk's final sample so the first output of
        # this chunk interpolates across the join.
        if self._previous is not None:
            joined = array.array("h", [self._previous])
            joined.extend(samples)
            samples = joined

        out = array.array("h")
        # No offset for the prepended sample: `_position` was already stored
        # relative to it at the end of the previous call. Adding one here
        # double-counted it, which skipped a sample per chunk and diverged
        # from whole-stream output by thousands -- caught by
        # test_chunked_matches_whole before it reached the robot.
        position = self._position
        limit = len(samples) - 1
        while position < limit:
            index = int(position)
            frac = position - index
            a = samples[index]
            b = samples[index + 1]
            out.append(int(a + (b - a) * frac))
            position += self._ratio

        # Carry the leftover fraction and the boundary sample forward.
        self._position = position - limit
        self._previous = samples[-1]
        return out.tobytes()

    def flush(self) -> bytes:
        """Nothing is buffered beyond one sample, so there is no tail.

        Present so callers can end a stream uniformly rather than having to
        know that this particular resampler happens not to need it.
        """
        return b""


def ring_modulate(pcm: bytes, rate: int, frequency: float = 60.0, depth: float = 0.6,
                  phase: float = 0.0) -> tuple[bytes, float]:
    """Multiply the waveform by a sine. The classic robot voice.

    Returns the processed audio and the carrier phase to pass into the next
    call, so a chunked stream does not click at every boundary -- the same
    continuity problem as resampling, and the same fix.

    `depth` 0 leaves the voice untouched; 1 is full modulation, which is
    strongly robotic and starts to cost intelligibility. Around 0.6 reads as
    "machine" while staying easy to understand, which matters for something
    that reads out approvals.
    """
    if not 0.0 <= depth <= 1.0:
        raise ValueError("depth must be between 0 and 1")
    if frequency <= 0:
        raise ValueError("frequency must be positive")

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    step = 2.0 * math.pi * frequency / rate

    out = array.array("h")
    for sample in samples:
        carrier = 1.0 - depth + depth * math.sin(phase)
        value = int(sample * carrier)
        # Clamp rather than let int16 wrap: a wrap turns a loud syllable into
        # a burst of noise, which is far worse than a clipped one.
        out.append(max(-32768, min(32767, value)))
        phase += step
        if phase > 2.0 * math.pi:
            phase -= 2.0 * math.pi
    return out.tobytes(), phase


def bit_crush(pcm: bytes, bits: int = 8) -> bytes:
    """Quantise to fewer levels, for a cheap-hardware edge.

    8 bits is audibly gritty without being unintelligible. Below about 6 it
    stops being a voice.
    """
    if not 2 <= bits <= 16:
        raise ValueError("bits must be between 2 and 16")
    if bits == 16:
        return pcm

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    shift = 16 - bits
    out = array.array("h")
    for sample in samples:
        # Shift down and back up: the low bits are discarded, which is what
        # quantisation is.
        out.append((sample >> shift) << shift)
    return out.tobytes()
