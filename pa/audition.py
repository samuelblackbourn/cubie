"""Hear a range of voices and pick one.

Choosing Cubie's voice from a list of names is guesswork -- the only useful
test is hearing it come out of the robot, in the room, through that speaker.
So this downloads candidates and speaks a sample in each, announcing which
voice it is IN that voice, so you can tell them apart without counting.

    # what is available
    pa/.venv/bin/python pa/audition.py --list --lang en_GB

    # hear the low-quality British voices, which are the most synthetic
    pa/.venv/bin/python pa/audition.py --lang en_GB --quality low

    # hear one candidate with the robot effect at three depths
    pa/.venv/bin/python pa/audition.py --only en_GB-alan-low --robot-sweep

Voice names are `{lang}-{name}-{quality}`. Quality is a real trade for us:
LOW models are natively 16 kHz, which is the device's rate, so nothing is
resampled anywhere -- and they sound more synthetic, which is closer to what
a robot should sound like. Medium and high are 22050 Hz and smoother.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from character import CHARACTERS, Character, resolve
from speech import PiperSynthesizer, SpeechError, speak

logger = logging.getLogger(__name__)

CATALOGUE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
    "?download=true"
)

DEFAULT_DIR = Path("~/.local/share/piper-voices").expanduser()


def fetch_catalogue(url: str = CATALOGUE) -> list[str]:
    """Every voice name Piper publishes.

    Fetched rather than hardcoded: a list of names baked into this file would
    be wrong the moment upstream adds a voice, and wrong in a way that looks
    like the voice does not exist.
    """
    with urlopen(url, timeout=60) as response:
        return sorted(json.load(response).keys())


def select(
    voices: list[str],
    *,
    lang: str | None = None,
    quality: str | None = None,
    grep: str | None = None,
    only: list[str] | None = None,
) -> list[str]:
    """Filter the catalogue. Pure, so the filtering is testable offline."""
    if only:
        # An explicit list wins outright, and is NOT validated against the
        # catalogue: a voice can exist locally without being published, and
        # refusing to speak it would be unhelpful.
        return list(only)
    chosen = voices
    if lang:
        chosen = [v for v in chosen if v.startswith(lang)]
    if quality:
        chosen = [v for v in chosen if v.endswith(f"-{quality}")]
    if grep:
        chosen = [v for v in chosen if grep.lower() in v.lower()]
    return chosen


def spoken_name(voice: str) -> str:
    """Turn `en_GB-alan-low` into something worth hearing aloud.

    The name is spoken in its own voice so the recording identifies itself.
    Reading the raw identifier aloud ("en underscore G B dash alan") is
    unpleasant and hard to follow, so only the parts that matter are said.
    """
    parts = voice.split("-")
    if len(parts) >= 3:
        return f"This is {parts[1].replace('_', ' ')}, {parts[2]} quality."
    return f"This is {voice}."


def audition(
    voice: str,
    *,
    data_dir: Path,
    text: str | None,
    robot: float,
    robot_hz: float,
    crush: int,
    pitch: float = 1.0,
    variation: float | None = None,
    intro: str | None = None,
) -> bool:
    """Download if needed, then speak. True if it was heard."""
    from piper import download_voices

    model = data_dir / f"{voice}.onnx"
    if not model.exists():
        print(f"  downloading {voice} ...", flush=True)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            download_voices.download_voice(voice, data_dir)
        except Exception as exc:  # noqa: BLE001 - any failure is "skip it"
            # A bad name, a network blip, a voice withdrawn upstream: none of
            # them should end an audition of a dozen others.
            print(f"  SKIP {voice}: {type(exc).__name__}: {exc}")
            return False

    synth = PiperSynthesizer(
        voice_name=voice, data_dir=str(data_dir), pitch=pitch, variation=variation
    )
    line = text or intro or spoken_name(voice)
    try:
        rate = synth.sample_rate
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP {voice}: could not load ({exc})")
        return False

    native = " (16 kHz, no resampling)" if rate == 16000 else f" ({rate} Hz)"
    print(f"  {voice}{native}")
    try:
        speak(line, synth, robot=robot, robot_hz=robot_hz, crush_bits=crush)
    except SpeechError as exc:
        print(f"  FAILED {voice}: {exc}")
        return False
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="audition.py", description="Hear Piper voices on Cubie and pick one."
    )
    parser.add_argument("--list", action="store_true", help="print matches, say nothing")
    parser.add_argument("--lang", default="en", help="name prefix, e.g. en_GB")
    parser.add_argument("--quality", choices=["x_low", "low", "medium", "high"])
    parser.add_argument("--grep", help="substring match on the voice name")
    parser.add_argument("--only", nargs="+", help="audition exactly these voices")
    parser.add_argument("--say", help="text to speak (default: the voice names itself)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DIR))
    parser.add_argument("--robot", type=float, default=0.0, metavar="DEPTH")
    parser.add_argument("--robot-hz", type=float, default=60.0, metavar="HZ")
    parser.add_argument("--crush", type=int, default=16, metavar="BITS")
    parser.add_argument(
        "--robot-sweep",
        action="store_true",
        help="for each voice, speak it plain then at robot depth 0.4, 0.7 and 1.0",
    )
    parser.add_argument(
        "--characters",
        action="store_true",
        help="for each voice, speak every named character in turn",
    )
    parser.add_argument(
        "--limit", type=int, default=8,
        help="stop after this many voices, so a broad filter is not a long wait",
    )
    parser.add_argument(
        "--pause", type=float, default=1.0,
        help="seconds between voices, so they do not run together",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if args.only:
        voices = select([], only=args.only)
    else:
        try:
            catalogue = fetch_catalogue()
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not fetch the voice catalogue: {exc}", file=sys.stderr)
            print("Use --only to audition voices you already have.", file=sys.stderr)
            return 1
        voices = select(
            catalogue, lang=args.lang, quality=args.quality, grep=args.grep
        )

    if not voices:
        print("no voices matched", file=sys.stderr)
        return 1

    if args.list:
        for voice in voices:
            print(voice)
        print(f"\n{len(voices)} voices", file=sys.stderr)
        return 0

    shown = voices[: args.limit]
    if len(voices) > len(shown):
        print(f"{len(voices)} matched; auditioning the first {len(shown)} "
              f"(raise --limit for more)\n")

    data_dir = Path(args.data_dir).expanduser()
    heard = 0
    attempted = 0
    for voice in shown:
        if args.characters:
            settings = [resolve(k) for k in ("plain", "cute", "chirpy", "machine",
                                             "gruff")]
        elif args.robot_sweep:
            settings = [
                Character(
                    name=f"robot {d}", description="", robot=d,
                    robot_hz=args.robot_hz or 60.0,
                )
                for d in (0.0, 0.4, 0.7, 1.0)
            ]
        else:
            settings = [
                Character(
                    name="chosen", description="",
                    robot=args.robot, robot_hz=args.robot_hz or 60.0,
                    crush=args.crush,
                )
            ]

        for setting in settings:
            print(f"  -- {setting.name}")
            attempted += 1
            if audition(
                voice,
                data_dir=data_dir,
                text=args.say,
                robot=setting.robot,
                robot_hz=setting.robot_hz,
                crush=setting.crush,
                pitch=setting.pitch,
                variation=setting.variation,
                intro=setting.spoken_intro() if args.characters else None,
            ):
                heard += 1
            time.sleep(args.pause)

    print(f"\nheard {heard} of {attempted}")
    return 0 if heard else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
