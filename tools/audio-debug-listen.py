#!/usr/bin/env python3
"""Listen to what Cubie's microphone actually hears.

Every theory about the wake word eventually reduces to one question that no
amount of reading the firmware can answer: are there real samples in the buffer
that reaches the recogniser? `custom_wake_word.cc` was read line by line and is
correct -- it de-interleaves, it chunks, it calls `multinet_->detect()` at a
threshold the one observed hit cleared comfortably. The model loads. The state
machine reaches idle and enables detection. The feed asks for 16 kHz and gets
it. And nothing matches a human voice.

So this receives the audio itself.

--- Where the stream comes from ---

`audio_service.cc` ends `ReadAudioData` with

    #if CONFIG_USE_AUDIO_DEBUGGER
        audio_debugger_->Feed(data);
    #endif

which is the LAST thing it does before returning -- after the resample, and on
the same buffer `wake_word_->Feed(data)` is handed a few lines later. That
placement is the whole value of this tool: it is not a nearby tap or a similar
buffer, it is the identical samples. Whatever arrives here is exactly what
MultiNet was given.

--- Why it reports per-channel RMS ---

The codec runs the input as TDM with two slots (`total_slot: 2, slot_mask:
0x3`) -- the microphone and the playback reference AEC needs. So the stream is
interleaved, and "is the mic working" is really two questions: is there signal
at all, and is it in the slot the recogniser reads? `CustomWakeWord::Feed`
takes the even samples, channel 0. If the energy is all in channel 1, the
recogniser is reading the wrong slot and hears only what the speaker plays --
which would explain a device that fires on its own greeting and never on a
person.

RMS in dBFS answers both at a glance. Silence sits near -90; a voice at
conversational distance lands around -30 to -20. A channel pinned at -inf is
not connected to anything.

--- Why it writes two WAVs ---

Numbers can mislead in ways ears cannot. A channel can carry energy and still
be unusable: clipped, at the wrong rate, half-speed, DC-offset, or one word
smeared over three seconds. The files let you play the thing and know in a
second what a spectrum of statistics would take an evening to imply.

They are written per channel, mono, so each one is directly the signal one
consumer sees -- `cubie-mic-ch0.wav` is, sample for sample, what MultiNet was
asked to recognise.

--- Deliberately not clever ---

Stdlib only, one socket, no dependencies, no config file. It runs on
office-server with the gateway's interpreter or the system one, or on a laptop,
because a diagnostic that needs its own environment set up first is a
diagnostic nobody runs at the moment they need it.

It also does not attempt to decode, transcribe or judge. The transcription path
already exists elsewhere and has its own failure modes; mixing them in here
would mean a silent mic and a broken whisper produce the same output.
"""

from __future__ import annotations

import argparse
import math
import socket
import sys
import time
import wave

#: The device sends raw little-endian int16 PCM with no header of any kind --
#: no sequence numbers, no timestamps, nothing to resynchronise on. A dropped
#: UDP datagram therefore shortens the recording rather than corrupting it,
#: which is the right failure for this job: a gap is audible as a click and
#: changes no conclusion, whereas a resync protocol would be code to get wrong.
BYTES_PER_SAMPLE = 2

#: What `ReadAudioData` was asked for at the point the debugger tap sits. The
#: wake word feed requests 16000 explicitly (`ReadAudioData(data, 16000, ...)`)
#: and the resampler runs before the tap, so this is the rate of what arrives
#: -- not the codec's native 24000.
DEFAULT_RATE = 16000

#: Two, because the codec opens the input as TDM with a microphone slot and a
#: playback-reference slot. Override it if the board config ever changes; the
#: per-channel report will look obviously wrong if this is set incorrectly,
#: since the channels will be interleaved into each other.
DEFAULT_CHANNELS = 2


def dbfs(samples: list[int]) -> float:
    """RMS of one channel in dBFS. -inf for digital silence.

    dBFS rather than raw RMS because the useful comparison is against a fixed
    scale -- full scale is 0, and a voice is 60 dB above silence -- not against
    the other channel, which may itself be broken.
    """
    if not samples:
        return float("-inf")
    total = sum(float(sample) * float(sample) for sample in samples)
    rms = math.sqrt(total / len(samples))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms / 32768.0)


def split_channels(payload: bytes, channels: int) -> list[list[int]]:
    """De-interleave, exactly the way `CustomWakeWord::Feed` does.

    Channel 0 here is the same `data[i]` for even `i` that the recogniser
    buffers, so a comparison between these two lists is a comparison between
    what MultiNet reads and what it ignores.
    """
    frame = channels * BYTES_PER_SAMPLE
    usable = len(payload) - (len(payload) % frame)
    out: list[list[int]] = [[] for _ in range(channels)]
    for offset in range(0, usable, BYTES_PER_SAMPLE):
        value = int.from_bytes(payload[offset : offset + BYTES_PER_SAMPLE], "little", signed=True)
        out[(offset // BYTES_PER_SAMPLE) % channels].append(value)
    return out


def write_wav(path: str, samples: list[int], rate: int) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(BYTES_PER_SAMPLE)
        handle.setframerate(rate)
        handle.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Receive Cubie's raw microphone stream and report what is in it."
    )
    parser.add_argument("--port", type=int, default=8098, help="UDP port to listen on")
    parser.add_argument("--seconds", type=float, default=10.0, help="how long to record")
    parser.add_argument("--rate", type=int, default=DEFAULT_RATE)
    parser.add_argument("--channels", type=int, default=DEFAULT_CHANNELS)
    parser.add_argument("--prefix", default="cubie-mic", help="output file prefix")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Bind to every interface: the device sends to whatever address was baked
    # into the image, and on a host with both a wired and a wireless address
    # binding the wrong one looks identical to a device that never sent.
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)

    print(f"listening on udp/{args.port} for {args.seconds:g}s -- talk to him now")

    payload = bytearray()
    started = time.monotonic()
    first_packet_at: float | None = None
    packets = 0

    while time.monotonic() - started < args.seconds:
        try:
            chunk, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if first_packet_at is None:
            first_packet_at = time.monotonic()
        packets += 1
        payload.extend(chunk)

    sock.close()

    if not packets:
        # Distinguished from a silent microphone on purpose. Nothing arriving
        # is a wiring question -- wrong host, wrong port, firewall, debugger
        # not compiled in -- and none of those say anything about the mic.
        print(
            "NOTHING ARRIVED.\n"
            "  That is not a verdict on the microphone -- no audio reached this\n"
            "  host at all. Check that the image was built with the audio\n"
            "  debugger enabled, that its UDP target is this machine's address\n"
            f"  and port {args.port}, and that the device is on the network.",
            file=sys.stderr,
        )
        return 1

    channels = split_channels(bytes(payload), args.channels)
    duration = len(channels[0]) / args.rate if channels[0] else 0.0

    print(f"\n{packets} packets, {len(payload)} bytes, {duration:.1f}s of audio\n")
    for index, samples in enumerate(channels):
        level = dbfs(samples)
        peak = max((abs(s) for s in samples), default=0)
        role = "  <- what the wake word reads" if index == 0 else ""
        shown = "-inf" if level == float("-inf") else f"{level:6.1f}"
        print(f"  channel {index}: RMS {shown} dBFS   peak {peak:6d}{role}")

    for index, samples in enumerate(channels):
        path = f"{args.prefix}-ch{index}.wav"
        write_wav(path, samples, args.rate)
        print(f"  wrote {path}")

    print(
        "\nRoughly: near -90 dBFS is silence, -30 to -20 is a voice at desk\n"
        "distance. If channel 0 is silent and channel 1 is not, the recogniser\n"
        "is reading the wrong slot. If both are silent, the microphone is not\n"
        "capturing. Play the files before concluding either -- the numbers\n"
        "cannot tell you that speech arrived at the wrong speed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
