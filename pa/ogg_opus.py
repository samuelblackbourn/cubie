"""Take an Ogg/Opus stream apart again.

The gateway captures device audio as raw Opus frames, packs them into an
Ogg container (RFC 3533 + RFC 7845), and POSTs the container to
`STACKCHAN_AUDIO_HOOK_URL`. Its own STT path never does that -- it decodes the
frames directly -- so the container exists only for the hook, and the receiver
has to undo it before anything can be transcribed.

--- Why not hand the container to ffmpeg ---

Decoding Opus needs `opuslib`, which the gateway's `[stt]` extra already
installs alongside `faster-whisper`; so the *codec* is free. What is not free
is a container parser: reaching for `av` or an `ffmpeg` subprocess would add a
dependency (or a process) to undo a transformation the gateway applied in the
same 200 lines of pure Python it documents as deliberate. Demuxing Ogg is the
symmetric half of that decision.

--- Why the CRC is checked rather than skipped ---

Every Ogg page carries a CRC32 over its own bytes, and the gateway computes it
correctly, so a mismatch here means the body was mangled between the two
processes. A mangled capture does not produce silence -- it produces plausible
noise, and whisper will cheerfully turn noise into words. A transcript that was
never said is worse than no transcript, because the brain then acts on it. So a
bad page is a refusal, not a best effort.

Note that Ogg's CRC is not `zlib.crc32`: same polynomial, but MSB-first with no
input/output reflection and no final XOR.
"""

from __future__ import annotations

import struct

#: Ogg page capture pattern (RFC 3533 s6).
MAGIC = b"OggS"

#: The two header packets RFC 7845 requires before any audio.
OPUS_HEAD_MAGIC = b"OpusHead"
OPUS_TAGS_MAGIC = b"OpusTags"

#: Fixed part of a page header: magic, version, flags, granule, serial,
#: sequence, CRC, segment count. The segment table follows, one byte each.
_HEADER_STRUCT = struct.Struct("<4sBBqIIIB")
HEADER_BYTES = _HEADER_STRUCT.size  # 27

#: header_type_flag bits (RFC 3533 s6).
FLAG_CONTINUED = 0x01
FLAG_BOS = 0x02
FLAG_EOS = 0x04


class OggError(ValueError):
    """The body is not a well-formed Ogg/Opus stream.

    A distinct type because the receiver answers it with a 400 rather than a
    500: a body it cannot parse is the sender's problem, and saying so lets
    the gateway's own log carry the reason.
    """


def _build_crc_table() -> tuple[int, ...]:
    poly = 0x04C11DB7
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _build_crc_table()


def crc32(data: bytes) -> int:
    """Ogg's CRC32 over `data` -- MSB-first, no reflection, no final XOR."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
    return crc


def opus_frames(body: bytes) -> list[bytes]:
    """Return the Opus packets in an Ogg/Opus stream, headers dropped.

    The result is exactly what the device sent and what
    `stackchan_mcp.stt.audio_utils.decode_opus_frames` expects: one raw Opus
    packet per element, 60 ms of 16 kHz mono each.

    Raises:
        OggError: the body is truncated, is not Ogg, is not Opus, or a page
            fails its own CRC. Every one of those is a refusal rather than a
            partial read -- see the module docstring.
    """
    if not body:
        raise OggError("empty body")
    packets = _packets(body)
    if not packets:
        raise OggError("no packets in stream")
    if not packets[0].startswith(OPUS_HEAD_MAGIC):
        raise OggError("first packet is not OpusHead: not an Opus stream")
    if len(packets) < 2 or not packets[1].startswith(OPUS_TAGS_MAGIC):
        raise OggError("second packet is not OpusTags")
    return [p for p in packets[2:] if p]


def _packets(body: bytes) -> list[bytes]:
    """Reassemble packets across pages and across the lacing table.

    Two kinds of continuation have to be handled and they are not the same
    thing. Within a page, a run of 255-byte segments means "this packet is not
    finished"; it ends at the first segment shorter than 255, which is why a
    packet whose length is an exact multiple of 255 needs a trailing
    zero-length segment. Across pages, a packet left open at the end of one
    page resumes in the next, which sets FLAG_CONTINUED.
    """
    packets: list[bytes] = []
    pending: list[bytes] = []       # segments of a packet still being read
    offset = 0
    expect_bos = True
    serial: int | None = None
    saw_eos = False

    while offset < len(body):
        page, offset = _page(body, offset)

        if expect_bos:
            if not page.flags & FLAG_BOS:
                raise OggError("first page is not marked beginning-of-stream")
            serial = page.serial
            expect_bos = False
        elif page.serial != serial:
            # One capture, one stream. A second serial means a multiplexed
            # file, which the gateway never produces and which we would
            # otherwise interleave into nonsense.
            raise OggError(
                f"multiplexed stream: page serial {page.serial} after {serial}"
            )
        if saw_eos:
            raise OggError("page after end-of-stream")
        saw_eos = bool(page.flags & FLAG_EOS)

        if page.flags & FLAG_CONTINUED:
            if not pending:
                raise OggError("page claims a continued packet, but none is open")
        elif pending:
            raise OggError("packet left open at a page boundary without a continuation flag")

        for segment in page.segments:
            pending.append(segment)
            if len(segment) < 255:
                packets.append(b"".join(pending))
                pending = []

    if pending:
        raise OggError("stream ends mid-packet")
    if not saw_eos:
        # The gateway always marks its last page EOS, so a stream without one
        # was cut short -- which means the tail of the capture is missing and
        # the transcript would silently lose the end of the sentence.
        raise OggError("stream has no end-of-stream page: truncated")
    return packets


class _Page:
    __slots__ = ("flags", "granule", "serial", "sequence", "segments")

    def __init__(self, flags, granule, serial, sequence, segments):
        self.flags = flags
        self.granule = granule
        self.serial = serial
        self.sequence = sequence
        self.segments = segments


def _page(body: bytes, offset: int) -> tuple[_Page, int]:
    # The magic is checked before the length so a body that is not Ogg at all
    # says so, rather than being reported as a truncated Ogg page.
    if body[offset:offset + 4] != MAGIC:
        raise OggError(f"bad capture pattern at offset {offset}: not an Ogg page")
    if len(body) - offset < HEADER_BYTES:
        raise OggError("truncated page header")
    _, version, flags, granule, serial, sequence, crc, count = _HEADER_STRUCT.unpack_from(
        body, offset
    )
    if version != 0:
        raise OggError(f"unsupported Ogg version {version}")

    table_at = offset + HEADER_BYTES
    if len(body) - table_at < count:
        raise OggError("truncated segment table")
    table = body[table_at:table_at + count]
    payload_at = table_at + count
    payload_len = sum(table)
    if len(body) - payload_at < payload_len:
        raise OggError("truncated page payload")

    page_bytes = body[offset:payload_at + payload_len]
    # The CRC is computed with its own field zeroed.
    zeroed = page_bytes[:22] + b"\x00\x00\x00\x00" + page_bytes[26:]
    if crc32(zeroed) != crc:
        raise OggError(f"page {sequence} fails its CRC: body was mangled in transit")

    segments = []
    at = payload_at
    for length in table:
        segments.append(body[at:at + length])
        at += length

    return _Page(flags, granule, serial, sequence, segments), payload_at + payload_len
