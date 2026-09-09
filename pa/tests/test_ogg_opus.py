"""Tests for taking an Ogg/Opus capture apart.

The strongest test here is not written in this file. `pack_opus_frames_to_ogg`
in the gateway's own `audio_input_hook` is the *producer* of every body this
ever sees, so a round-trip against it tests the demuxer against the thing it
has to agree with rather than against my reading of RFC 3533. That round-trip
runs whenever the gateway is importable -- on office-server always, in `pa/.venv`
never -- and is skipped with a reason rather than silently passing.

It was run against stackchan-mcp 0.17.0 while writing this, and the awkward
cases came back exact: a 255-byte frame (which needs a trailing zero-length
lacing segment), a 510-byte frame (an exact multiple, so two full segments plus
the terminator), a 300-byte frame (255 + 45), and frames big enough to force
the packer's mid-batch page flush.

The hand-built pages below cover what the round-trip cannot: a producer that
splits one packet ACROSS pages. The gateway never does -- it always flushes on
a frame boundary -- so FLAG_CONTINUED would otherwise be dead, untested code
in a parser that claims to implement the format.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ogg_opus  # noqa: E402


def page(*, flags: int, granule: int, serial: int, sequence: int, segments: list[bytes]) -> bytes:
    """Build one Ogg page, CRC and all. The inverse of `ogg_opus._page`."""
    table = bytes(len(s) for s in segments)
    head = struct.pack(
        "<4sBBqII", ogg_opus.MAGIC, 0, flags, granule, serial, sequence
    )
    body = head + b"\x00\x00\x00\x00" + bytes([len(segments)]) + table + b"".join(segments)
    crc = ogg_opus.crc32(body)
    return body[:22] + struct.pack("<I", crc) + body[26:]


def head_packet() -> bytes:
    return struct.pack("<8sBBHIhB", ogg_opus.OPUS_HEAD_MAGIC, 1, 1, 0, 16000, 0, 0)


def tags_packet() -> bytes:
    vendor = b"test"
    return ogg_opus.OPUS_TAGS_MAGIC + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)


def stream(frames: list[bytes], *, serial: int = 1) -> bytes:
    """A minimal well-formed stream: BOS, tags, one audio page marked EOS."""
    segments: list[bytes] = []
    for frame in frames:
        pos = 0
        while pos < len(frame):
            segments.append(frame[pos:pos + 255])
            pos += 255
        if len(frame) % 255 == 0:
            segments.append(b"")
    return (
        page(flags=ogg_opus.FLAG_BOS, granule=0, serial=serial, sequence=0,
             segments=[head_packet()])
        + page(flags=0, granule=0, serial=serial, sequence=1, segments=[tags_packet()])
        + page(flags=ogg_opus.FLAG_EOS, granule=2880 * len(frames), serial=serial,
               sequence=2, segments=segments or [b""])
    )


# --- the shape of the thing -------------------------------------------------


def test_it_returns_the_frames_and_drops_the_two_headers():
    frames = [b"one", b"two", b"three"]
    assert ogg_opus.opus_frames(stream(frames)) == frames


def test_a_frame_that_ends_on_a_255_byte_boundary_survives():
    """The zero-length terminating segment is not a frame of its own. Reading
    it as one would append an empty packet the decoder then has to skip, and
    the frame count is what the granule and the timing are derived from."""
    frames = [b"x" * 255, b"tail"]
    assert ogg_opus.opus_frames(stream(frames)) == frames


def test_a_long_frame_split_across_lacing_segments_is_rejoined():
    frames = [b"y" * 700]
    assert ogg_opus.opus_frames(stream(frames)) == frames


def test_a_packet_split_across_two_pages_is_rejoined():
    """FLAG_CONTINUED. The gateway never emits it -- it flushes on frame
    boundaries -- so this is the only thing that exercises the path."""
    frame = b"z" * 300
    body = (
        page(flags=ogg_opus.FLAG_BOS, granule=0, serial=1, sequence=0,
             segments=[head_packet()])
        + page(flags=0, granule=0, serial=1, sequence=1, segments=[tags_packet()])
        # First page ends mid-packet: one full 255-byte segment and no shorter
        # one to terminate it.
        + page(flags=0, granule=0, serial=1, sequence=2, segments=[frame[:255]])
        + page(flags=ogg_opus.FLAG_CONTINUED | ogg_opus.FLAG_EOS, granule=2880,
               serial=1, sequence=3, segments=[frame[255:]])
    )
    assert ogg_opus.opus_frames(body) == [frame]


# --- the refusals -----------------------------------------------------------


def test_an_empty_body_says_so():
    with pytest.raises(ogg_opus.OggError, match="empty body"):
        ogg_opus.opus_frames(b"")


def test_something_that_is_not_ogg_is_named_as_such():
    """Not reported as a truncated Ogg page, which is what a length check
    before a magic check would have said."""
    with pytest.raises(ogg_opus.OggError, match="not an Ogg page"):
        ogg_opus.opus_frames(b"RIFF....WAVEfmt " + b"\x00" * 64)


def test_a_flipped_byte_is_refused_rather_than_transcribed():
    """The reason the CRC is checked at all. Whisper turns noise into words
    happily, and the brain then acts on a sentence nobody said."""
    body = bytearray(stream([b"a" * 60] * 30))
    body[-10] ^= 0x40
    with pytest.raises(ogg_opus.OggError, match="CRC"):
        ogg_opus.opus_frames(bytes(body))


def test_a_truncated_stream_is_refused():
    body = stream([b"a" * 60] * 30)
    with pytest.raises(ogg_opus.OggError):
        ogg_opus.opus_frames(body[:len(body) // 2])


def test_a_stream_with_no_end_of_stream_page_is_refused():
    """A capture whose last page never arrived is missing the end of the
    sentence, and a transcript that quietly stops early is worse than none."""
    body = stream([b"a" * 60])
    # Clear the EOS bit on the last page and repair its CRC, so this tests the
    # EOS rule rather than the CRC check.
    last = body.rfind(b"OggS")
    patched = bytearray(body)
    patched[last + 5] &= ~ogg_opus.FLAG_EOS
    zeroed = bytes(patched[last:last + 22]) + b"\x00\x00\x00\x00" + bytes(patched[last + 26:])
    patched[last + 22:last + 26] = struct.pack("<I", ogg_opus.crc32(zeroed))
    with pytest.raises(ogg_opus.OggError, match="truncated"):
        ogg_opus.opus_frames(bytes(patched))


def test_a_stream_that_is_not_opus_is_refused():
    body = (
        page(flags=ogg_opus.FLAG_BOS, granule=0, serial=1, sequence=0,
             segments=[b"VorbisSomethingElse"])
        + page(flags=ogg_opus.FLAG_EOS, granule=0, serial=1, sequence=1,
               segments=[b"payload"])
    )
    with pytest.raises(ogg_opus.OggError, match="not an Opus stream"):
        ogg_opus.opus_frames(body)


def test_a_multiplexed_stream_is_refused():
    """One capture is one stream. Interleaving two serials would splice two
    speakers into one transcript."""
    body = (
        page(flags=ogg_opus.FLAG_BOS, granule=0, serial=1, sequence=0,
             segments=[head_packet()])
        + page(flags=0, granule=0, serial=1, sequence=1, segments=[tags_packet()])
        + page(flags=ogg_opus.FLAG_EOS, granule=2880, serial=99, sequence=2,
               segments=[b"frame"])
    )
    with pytest.raises(ogg_opus.OggError, match="multiplexed"):
        ogg_opus.opus_frames(body)


def test_a_first_page_not_marked_beginning_of_stream_is_refused():
    body = (
        page(flags=0, granule=0, serial=1, sequence=0, segments=[head_packet()])
        + page(flags=ogg_opus.FLAG_EOS, granule=0, serial=1, sequence=1,
               segments=[tags_packet()])
    )
    with pytest.raises(ogg_opus.OggError, match="beginning-of-stream"):
        ogg_opus.opus_frames(body)


# --- against the real producer ----------------------------------------------


def _real_packer():
    """The gateway's own packer, or None where the gateway is not installed."""
    try:
        from stackchan_mcp.audio_input_hook import pack_opus_frames_to_ogg
    except Exception:  # noqa: BLE001 - ImportError, or aiohttp missing
        return None
    return pack_opus_frames_to_ogg


@pytest.mark.parametrize(
    "sizes",
    [
        [3],
        [60] * 250,          # a typical 15 s auto-stopped capture
        [60] * 50,           # exactly one page
        [60] * 51,           # one frame into a second page
        [255],               # needs the zero-length terminator
        [510],               # exact multiple: two full segments + terminator
        [300],               # splits 255 + 45
        [1200] * 40,         # forces the packer's mid-batch page flush
    ],
    ids=[
        "one-tiny-frame", "typical-15s", "one-page", "one-frame-over",
        "255-bytes", "510-bytes", "300-bytes", "mid-batch-flush",
    ],
)
def test_round_trip_against_the_gateways_own_packer(sizes):
    pack = _real_packer()
    if pack is None:
        pytest.skip(
            "stackchan_mcp is not importable here (pa/.venv has no gateway); "
            "this round-trip runs on office-server"
        )
    frames = [bytes((i + n) % 256 for n in range(size)) for i, size in enumerate(sizes)]
    assert ogg_opus.opus_frames(pack(frames)) == frames
