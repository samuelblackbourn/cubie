"""Named voice characters.

Four numbers -- pitch, variation, ring-modulation depth and carrier frequency
-- interact in ways nobody can predict from the values, so tuning them one at
a time by ear is slow and the good combinations are not obvious. These are
starting points to audition and then adjust.

They are DESCRIPTIONS OF INTENT, not impressions of any particular film
robot. Those voices were built by sound designers from processed recordings,
not synthesised from text, and no amount of ring modulation gets there. What
these do capture is the part that actually reads as "small friendly machine":
a raised pitch and a flatter delivery.

The single most effective knob is pitch. A voice 30-40% up sounds small, which
is most of what makes a robot endearing rather than menacing. Ring modulation
adds the machine edge but costs intelligibility as it rises, and this thing
has to read out approvals -- so the presets keep it modest and leave the
heavy settings to anyone who wants them explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Character:
    """A complete voice setting."""

    name: str
    description: str
    #: The Piper model this character wants, when it wants a particular one.
    #: None means "whatever the default is" -- most of these presets are
    #: processing settings that work on any voice, but `retro` is built around
    #: a specific model and sounds wrong on another.
    voice: str | None = None
    pitch: float = 1.0
    variation: float | None = None
    robot: float = 0.0
    robot_hz: float = 60.0
    crush: int = 16

    def spoken_intro(self) -> str:
        return f"This is the {self.name} voice."


CHARACTERS: dict[str, Character] = {
    "plain": Character(
        name="plain",
        description="The voice as the model made it. The baseline to judge against.",
    ),
    "cute": Character(
        name="cute",
        description=(
            "Small and friendly. Pitch well up, delivery flattened, only a "
            "trace of modulation. Closest to the small-helpful-robot idea, "
            "and the most intelligible of the character voices."
        ),
        pitch=1.35,
        variation=0.4,
        robot=0.2,
        robot_hz=90.0,
    ),
    "chirpy": Character(
        name="chirpy",
        description=(
            "Higher and brighter than cute, with a faster modulation that "
            "reads as electronic chatter. Charming in short replies; tiring "
            "over a long one."
        ),
        pitch=1.5,
        variation=0.3,
        robot=0.35,
        robot_hz=140.0,
    ),
    "machine": Character(
        name="machine",
        description=(
            "Unmistakably synthetic: flat delivery, strong modulation and "
            "quantised to 10 bits. Least natural, and the hardest to follow "
            "for anything longer than a sentence."
        ),
        pitch=1.15,
        variation=0.15,
        robot=0.6,
        robot_hz=70.0,
        crush=10,
    ),
    "retro": Character(
        name="retro",
        description=(
            "The 1970s robot: a young voice chopped by a slow carrier, so it "
            "sounds like it is being spoken through a desk fan. Built on "
            "en_US-ryan-high specifically -- a clear male voice with the "
            "headroom to be pitched up without going thin. Tuned on the robot "
            "rather than by ear at a desk: both the pitch and the chop are a "
            "step gentler than the measured maxima."
        ),
        voice="en_US-ryan-high",
        # 1.25, not 1.30. Tuned DOWN after hearing it on the robot: 1.30 is
        # +4.54 semitones over the model and read as slightly too young, where
        # 1.25 is +3.86 -- a drop of 0.68 of a semitone, which is under the
        # ~1 semitone that reads as a nudge rather than a different voice.
        #
        # `pitch` also sets Piper's length_scale by the same factor, so the
        # utterance keeps its duration; this is a pitch and formant shift, not
        # a speed-up.
        pitch=1.25,
        variation=0.35,
        # 0.35, tuned DOWN from 0.45 after hearing it, and 0.40 is the reason
        # the step is this big rather than smaller.
        #
        # `ring_modulate` applies gain = 1 - depth + depth*sin, so the gain
        # sweeps from 1-2*depth up to 1. There is a cliff between 0.45 and
        # 0.40, because that is where the trough stops being near-silent:
        #
        #     depth 0.45   gain +0.10..1.00   swing 10.0:1   ~10 ms near-silent
        #     depth 0.40   gain +0.20..1.00   swing  5.0:1   no gap at all
        #     depth 0.35   gain +0.30..1.00   swing  3.3:1   no gap at all
        #     depth 0.55   gain -0.10..1.00   ~16 ms, and phase inverts
        #
        # 0.45 was the deepest chop available before the gate starts eating
        # syllables -- correct as a ceiling, and more than wanted in practice.
        # 0.35 clears the cliff and softens the throb as well: measured
        # through a flat tone, the trough rises from 11% of peak to 31% while
        # the chop rate stays 22 a second. The fan is the FREQUENCY, not the
        # depth, so backing the depth off does not cost the effect.
        robot=0.35,
        # The number that makes this a fan rather than a buzz. Every other
        # character modulates at 45-140 Hz, fast enough to fuse into a tone
        # and read as electronic. A fan chops at its blade-pass rate -- tens
        # of hertz -- and at 22 Hz the ear hears the individual chops instead.
        # Verified as 22 chops per second through a flat test tone, not
        # assumed from the parameter.
        robot_hz=22.0,
    ),
    "gruff": Character(
        name="gruff",
        description=(
            "Pitched DOWN and slowly modulated -- a big machine rather than a "
            "small one. Included mostly so the others have something to be "
            "compared against."
        ),
        pitch=0.85,
        variation=0.5,
        robot=0.45,
        robot_hz=45.0,
    ),
}


def resolve(name: str) -> Character:
    """Look up a character by name, case-insensitively."""
    key = name.strip().lower()
    if key not in CHARACTERS:
        known = ", ".join(sorted(CHARACTERS))
        raise KeyError(f"unknown character {name!r}. Known: {known}")
    return CHARACTERS[key]


def describe_all() -> str:
    lines = []
    for key in ("plain", "cute", "chirpy", "retro", "machine", "gruff"):
        c = CHARACTERS[key]
        voice = c.voice or "(default)"
        lines.append(
            f"{c.name:9s} pitch {c.pitch:<5} robot {c.robot:<5} "
            f"{c.robot_hz:>5.0f}Hz  {voice:20s} {c.description}"
        )
    return "\n".join(lines)
