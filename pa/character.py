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
    for key in ("plain", "cute", "chirpy", "machine", "gruff"):
        c = CHARACTERS[key]
        lines.append(f"{c.name:9s} pitch {c.pitch:<5} robot {c.robot:<5} {c.description}")
    return "\n".join(lines)
