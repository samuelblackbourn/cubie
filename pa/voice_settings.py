"""The voice knobs, as something safe to receive from somewhere else.

`pa/character.py` holds the presets and `pa/speech.py` applies them. This is the
layer between those and a value that arrived over a network: it takes whatever
turned up, coerces it into something the pipeline cannot choke on, and says what
it had to change.

--- Why clamping is the whole point ---

A wrong number here does not make Cubie sound bad. It makes him SILENT.
`ring_modulate` raises on a depth outside 0..1 and `bit_crush` on bits outside
2..16 (`pa/audio.py`), the speech CLI catches only `FileNotFoundError` and
`SpeechError`, so the exception leaves the subprocess non-zero and `live.py`
logs "voice failed" and drops the utterance. There is no audible symptom to
notice and no partial degradation -- he just does not answer.

That was tolerable while these numbers were module constants a person edited
with a test suite in front of them. It is not tolerable once a slider in a
browser can set them, which is why this file exists and why it clamps rather
than raising: a panel that mutes the robot is worse than one that quietly
refuses to go past 1.0.

--- Where the limits come from, and where they do NOT ---

The bounds below are the CODE's, read off `pa/audio.py`, not anyone's taste:

    robot     0.0 .. 1.0    audio.py:131 raises outside this
    crush     2 .. 16       audio.py:160 raises outside this
    robot_hz  > 0           audio.py:134 raises otherwise
    pitch     > 0           speech.py needs a positive length_scale

Taste is deliberately NOT encoded here. The survey that preceded this file
recommended a hard stop at depth 0.45 -- the documented point past which the
modulation "starts eating syllables" -- and that is good advice for a robot who
reads out approvals, but it is advice: the shipped `machine` preset uses 0.6,
and a clamp that made a shipped value unreachable would be a bug wearing the
costume of a safety feature. So the ceiling here is the one the code enforces,
and the useful bands (fan versus tone, the syllable-eating cliff) belong in the
panel's marks where a person can knowingly cross them.

The outer bounds on `pitch`, `variation` and `robot_hz` are wider than any
preset uses, for the same reason: they exist to stop a nonsense value, not to
express an opinion. `variation` has no validation anywhere in the stack -- it
goes straight to Piper's `noise_w_scale` -- so this is the only bound it will
ever have.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any

#: (low, high) per knob, and why that bound and not another.
LIMITS: dict[str, tuple[float, float]] = {
    # Positive, and beyond this range the voice is not slow-and-deep or
    # fast-and-high, it is unusable. Shipped presets span 0.85..1.50.
    "pitch": (0.5, 2.0),
    # Piper's own default is about 0.8 and shipped presets span 0.15..0.50.
    # Nothing else validates this at all.
    "variation": (0.0, 1.5),
    # The code's limit exactly: audio.py raises outside it.
    "robot": (0.0, 1.0),
    # Positive is the code's only requirement. 8 is below the ~12 Hz where
    # modulation stops reading as timbre; 200 is well past where it fuses into
    # a tone. Both ends are reachable on purpose.
    "robot_hz": (8.0, 200.0),
    # The code's limit exactly. 16 is identity.
    "crush": (2, 16),
}

#: Which knobs are whole numbers. `crush` is bits.
INTEGRAL = frozenset({"crush"})


@dataclass(frozen=True)
class VoiceSettings:
    """Overrides for a preset. `None` means "leave the preset's value alone".

    Not a `Character`: a `Character` is a complete voice with a name and a
    description, and these are a partial override of one. Keeping them separate
    is what lets the panel adjust the deployed `retro` without editing it --
    `retro`'s numbers are pinned by tests as the record of what was tuned by
    ear, and a UI that rewrote them would turn the suite red on every save.
    """

    pitch: float | None = None
    variation: float | None = None
    robot: float | None = None
    robot_hz: float | None = None
    crush: int | None = None

    def flags(self) -> list[str]:
        """The `pa/speech.py` flags for these overrides, in a stable order.

        The CLI's own defaults are all `None` precisely so an explicit flag
        beats the preset -- including passing 0, which is how you silence a
        preset's modulation rather than merely reducing it.
        """
        out: list[str] = []
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None:
                continue
            out.append("--" + field.name.replace("_", "-"))
            out.append(str(value))
        return out

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))


def coerce(raw: Any) -> tuple[VoiceSettings, list[str]]:
    """Whatever arrived, as settings that cannot mute him, plus what changed.

    Returns the settings and a list of human-readable complaints. The
    complaints are for the log and for the panel to echo back: silently
    clamping a slider is how a person comes to believe the robot ignores them.

    Anything unrecognised, missing or the wrong type is dropped rather than
    guessed at. A key we do not know is not an error -- the office may be newer
    than we are, and refusing the whole payload over one unknown field would
    make every future addition a breaking change.
    """
    if not isinstance(raw, dict):
        return VoiceSettings(), [] if raw is None else [f"ignored: not an object ({type(raw).__name__})"]

    values: dict[str, Any] = {}
    notes: list[str] = []
    known = {f.name for f in fields(VoiceSettings)}

    for key, value in raw.items():
        if key not in known:
            notes.append(f"ignored unknown setting {key!r}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # bool is an int in Python and `pitch: true` is not a pitch.
            notes.append(f"ignored {key}: not a number ({type(value).__name__})")
            continue

        if not math.isfinite(value):
            # Rejected explicitly rather than clamped, because the clamp only
            # survives a NaN by accident: `max(low, nan)` returns `low` because
            # the comparison is False, and reversing those arguments -- which
            # nobody would think twice about -- lets `nan` through to
            # `--pitch nan`. argparse accepts that as a float and Piper gets a
            # NaN length_scale. Infinity does clamp correctly, and is refused
            # alongside NaN because a slider cannot produce either and a
            # payload containing one is not a payload to trust the rest of.
            notes.append(f"ignored {key}: {value} is not a finite number")
            continue

        low, high = LIMITS[key]
        clamped = min(high, max(low, float(value)))
        if key in INTEGRAL:
            clamped = int(round(clamped))
        if clamped != value:
            notes.append(f"clamped {key} {value} -> {clamped}")
        values[key] = clamped

    return VoiceSettings(**values), notes
