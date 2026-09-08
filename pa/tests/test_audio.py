"""Tests for PCM shaping.

The load-bearing test is `test_chunked_matches_whole`: it asserts the exact
property the gateway's per-chunk resampling fails, which is why the voice
came out "a bit broken up" on the first real listen.
"""

from __future__ import annotations

import array
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio import DEVICE_RATE, Resampler, bit_crush, ring_modulate  # noqa: E402


def tone(rate: int, seconds: float, hz: float = 440.0) -> bytes:
    """A clean sine. Discontinuities in a sine are obvious in the numbers."""
    n = int(rate * seconds)
    samples = array.array(
        "h", [int(20000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)]
    )
    return samples.tobytes()


def to_list(pcm: bytes) -> list[int]:
    out = array.array("h")
    out.frombytes(pcm)
    return list(out)


def test_passthrough_when_rates_match():
    """The point of sending 16 kHz: the gateway's resample becomes a no-op,
    and so does ours."""
    r = Resampler(DEVICE_RATE, DEVICE_RATE)
    assert r.passthrough is True
    data = tone(DEVICE_RATE, 0.05)
    assert r.feed(data) == data


def test_downsamples_to_roughly_the_right_length():
    r = Resampler(22050, 16000)
    out = r.feed(tone(22050, 1.0))
    # 1 s in, ~1 s out at the target rate. Allow a couple of samples for the
    # interpolator's edge behaviour.
    assert abs(len(to_list(out)) - 16000) <= 4


def test_chunked_matches_whole():
    """Feeding a stream in pieces must equal feeding it whole.

    This is exactly what the gateway does NOT do -- it resamples each 8192-byte
    body chunk independently, restarting interpolation ~25 times in a 4.6 s
    sentence. Each restart is a discontinuity, and that is what was audible.
    """
    source = tone(22050, 0.5)

    whole = Resampler(22050, 16000).feed(source)

    chunked_parts = []
    chunked = Resampler(22050, 16000)
    # 8192 bytes is the gateway's own chunk size, so this reproduces the
    # exact framing that caused the problem.
    for start in range(0, len(source), 8192):
        chunked_parts.append(chunked.feed(source[start : start + 8192]))
    chunked_parts.append(chunked.flush())
    chunked_out = b"".join(chunked_parts)

    a, b = to_list(whole), to_list(chunked_out)
    assert abs(len(a) - len(b)) <= 2
    shared = min(len(a), len(b))
    # Identical to within rounding, not merely similar.
    worst = max(abs(a[i] - b[i]) for i in range(shared))
    assert worst <= 1, f"chunked output diverges by {worst}"


def test_independent_per_chunk_resampling_would_fail_this_test():
    """Guard the guard: prove the test can detect the bug it exists for.

    A test that passes for both the right and wrong implementation is
    decoration. This resamples each chunk with a FRESH resampler -- the
    gateway's behaviour -- and asserts the result is measurably worse.
    """
    source = tone(22050, 0.5)
    whole = to_list(Resampler(22050, 16000).feed(source))

    naive = b"".join(
        Resampler(22050, 16000).feed(source[start : start + 8192])
        for start in range(0, len(source), 8192)
    )
    naive_list = to_list(naive)

    shared = min(len(whole), len(naive_list))
    worst = max(abs(whole[i] - naive_list[i]) for i in range(shared))
    assert worst > 1, "the naive version should diverge, or this test proves nothing"


def test_odd_byte_counts_do_not_crash():
    """A chunk can split a 16-bit sample across a boundary."""
    r = Resampler(22050, 16000)
    assert isinstance(r.feed(b"\x01"), bytes)
    assert r.feed(b"") == b""


def test_zero_or_negative_rates_are_rejected():
    for bad in ((0, 16000), (22050, 0), (-1, 16000)):
        with pytest.raises(ValueError):
            Resampler(*bad)


def test_ring_modulation_preserves_length_and_range():
    data = tone(16000, 0.1)
    out, _ = ring_modulate(data, 16000)
    values = to_list(out)
    assert len(values) == len(to_list(data))
    assert all(-32768 <= v <= 32767 for v in values)


def test_ring_modulation_at_zero_depth_changes_nothing():
    """A knob that does nothing at zero is a knob you can trust."""
    data = tone(16000, 0.05)
    out, _ = ring_modulate(data, 16000, depth=0.0)
    a, b = to_list(data), to_list(out)
    assert max(abs(a[i] - b[i]) for i in range(len(a))) <= 1


def test_ring_modulation_actually_modulates_at_full_depth():
    data = tone(16000, 0.05)
    out, _ = ring_modulate(data, 16000, depth=1.0)
    assert to_list(out) != to_list(data)


def test_carrier_phase_continues_across_chunks():
    """Returned phase must make chunked processing match whole processing,
    or every chunk boundary clicks -- the same failure as resampling."""
    data = tone(16000, 0.2)
    whole, _ = ring_modulate(data, 16000, depth=0.8)

    parts = []
    phase = 0.0
    for start in range(0, len(data), 4096):
        piece, phase = ring_modulate(data[start : start + 4096], 16000, depth=0.8,
                                     phase=phase)
        parts.append(piece)
    chunked = b"".join(parts)

    a, b = to_list(whole), to_list(chunked)
    assert len(a) == len(b)
    assert max(abs(a[i] - b[i]) for i in range(len(a))) <= 2


def test_bad_modulation_parameters_are_rejected():
    data = tone(16000, 0.01)
    with pytest.raises(ValueError):
        ring_modulate(data, 16000, depth=1.5)
    with pytest.raises(ValueError):
        ring_modulate(data, 16000, frequency=0)


def test_bit_crush_reduces_distinct_values():
    data = tone(16000, 0.1)
    crushed = bit_crush(data, bits=6)
    assert len(set(to_list(crushed))) < len(set(to_list(data)))


def test_bit_crush_at_16_bits_is_identity():
    data = tone(16000, 0.05)
    assert bit_crush(data, bits=16) == data


def test_bit_crush_rejects_silly_depths():
    for bad in (1, 17, 0):
        with pytest.raises(ValueError):
            bit_crush(b"\x00\x00", bits=bad)
