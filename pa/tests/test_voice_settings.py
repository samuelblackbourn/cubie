"""Tests for the layer between a slider in a browser and Cubie's voice.

The property that matters is not "the settings are correct" but "no setting can
make him silent". A wrong number here does not sound bad, it raises inside the
speech subprocess -- `ring_modulate` on a depth outside 0..1, `bit_crush` on
bits outside 2..16 -- and the CLI catches only FileNotFoundError and
SpeechError, so the exception exits non-zero and `live.py` logs "voice failed"
and drops the utterance. No audible symptom, no partial degradation, he just
does not answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voice_settings as vs  # noqa: E402


# --- the property the file exists for ---------------------------------------


def test_nothing_that_arrives_can_make_him_silent():
    """The one that matters. Every knob, driven far past both ends, must come
    out inside the range the audio code will accept -- because outside it the
    failure is not a bad noise, it is no noise."""
    hostile = {
        "pitch": -5.0, "variation": 99.0, "robot": 40.0,
        "robot_hz": -1.0, "crush": 0,
    }
    settings, notes = vs.coerce(hostile)
    assert notes, "clamping silently is how a person decides the robot ignores them"

    # The bounds the audio module actually enforces, restated from its raises,
    # plus variation -- which the first version drove past both ends and then
    # never asserted anything about.
    assert 0.0 <= settings.robot <= 1.0
    assert 2 <= settings.crush <= 16
    assert settings.robot_hz > 0
    assert settings.pitch > 0
    assert vs.LIMITS["variation"][0] <= settings.variation <= vs.LIMITS["variation"][1]

    for other in ({"robot": 1.0000001}, {"robot": -0.0001}, {"crush": 17}, {"crush": 1}):
        clamped, _ = vs.coerce(other)
        assert 0.0 <= (clamped.robot if clamped.robot is not None else 0.0) <= 1.0
        assert 2 <= (clamped.crush if clamped.crush is not None else 16) <= 16


def test_the_clamp_is_the_codes_limit_not_a_matter_of_taste():
    """The survey that preceded this recommended a hard stop at depth 0.45 --
    the documented point past which modulation starts eating syllables. That is
    good advice and it is not a limit: the shipped `machine` preset uses 0.6.

    A clamp that made a shipped value unreachable would be a bug wearing the
    costume of a safety feature, so the ceiling here is the one the code
    enforces and taste lives in the panel's marks."""
    import character

    for name, preset in character.CHARACTERS.items():
        settings, notes = vs.coerce({
            "pitch": preset.pitch, "robot": preset.robot,
            "robot_hz": preset.robot_hz, "crush": preset.crush,
            **({"variation": preset.variation} if preset.variation is not None else {}),
        })
        assert not notes, f"{name}'s own values were clamped: {notes}"


def test_a_missing_setting_leaves_the_preset_alone():
    """`None` is not zero. Passing 0 to `--robot` silences a preset's
    modulation; omitting the flag keeps it."""
    settings, _ = vs.coerce({"pitch": 1.2})
    assert settings.pitch == 1.2
    assert settings.robot is None
    assert "--robot" not in settings.flags()


def test_zero_is_a_value_and_is_sent():
    settings, _ = vs.coerce({"robot": 0})
    assert settings.robot == 0.0
    assert settings.flags() == ["--robot", "0.0"]


# --- what arrives is not to be trusted ---------------------------------------


def test_an_unknown_setting_is_ignored_rather_than_fatal():
    """The office may be newer than we are. Refusing the whole payload over one
    unknown field would make every future addition a breaking change."""
    settings, notes = vs.coerce({"pitch": 1.1, "reverb": 0.5})
    assert settings.pitch == 1.1
    assert any("reverb" in n for n in notes)


def test_a_string_where_a_number_belongs_is_dropped_not_coerced():
    settings, notes = vs.coerce({"pitch": "1.4"})
    assert settings.pitch is None
    assert any("not a number" in n for n in notes)


def test_a_boolean_is_not_a_number():
    """`True` is an int in Python, and `pitch: true` is not a pitch."""
    settings, notes = vs.coerce({"pitch": True})
    assert settings.pitch is None
    assert any("bool" in n for n in notes)


def test_a_payload_that_is_not_an_object_is_not_a_crash():
    for junk in ([1, 2], "pitch=2", 7):
        settings, notes = vs.coerce(junk)
        assert settings.is_empty()
        assert notes


def test_no_office_opinion_is_not_the_same_as_no_overrides():
    """None means the office said nothing about the voice. An office too old to
    know about the panel must not silently reset him."""
    settings, notes = vs.coerce(None)
    assert settings.is_empty()
    assert notes == []


# --- the flags, which are the actual interface -------------------------------


def test_the_flags_are_the_ones_the_speech_cli_accepts():
    """Read off the CLI rather than assumed: a flag that does not exist would
    make the subprocess exit 2 and drop the utterance, which is the same silent
    failure by another route."""
    import argparse
    import re

    source = (Path(vs.__file__).resolve().parent / "speech.py").read_text()
    accepted = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', source))
    settings, _ = vs.coerce({
        "pitch": 1.2, "variation": 0.4, "robot": 0.3, "robot_hz": 15, "crush": 12,
    })
    flags = [f for f in settings.flags() if f.startswith("--")]
    assert flags, "no flags produced"
    missing = [f for f in flags if f not in accepted]
    assert not missing, f"speech.py does not accept {missing}; it accepts {sorted(accepted)}"
    assert argparse  # the import documents what `accepted` is scraped from


def test_the_flags_are_pairs_and_nothing_else():
    """`flags()` builds argv by hand, and a stray or missing element shifts
    every following value onto the wrong flag.

    Replaces a test that asserted two identical settings produce identical
    argv -- which could not fail, because `flags()` iterates the dataclass's
    fields and the order is structural rather than insertion-dependent."""
    settings, _ = vs.coerce({
        "pitch": 1.2, "variation": 0.4, "robot": 0.3, "robot_hz": 15, "crush": 12,
    })
    flags = settings.flags()
    assert len(flags) == 10, flags
    for i in range(0, len(flags), 2):
        assert flags[i].startswith("--"), flags
        assert not flags[i + 1].startswith("--"), flags
        float(flags[i + 1])  # every value must parse as a number


def test_every_knob_has_a_limit():
    """A knob added to the dataclass without a limit would pass through
    unclamped, which is exactly the hole this file closes."""
    from dataclasses import fields

    assert {f.name for f in fields(vs.VoiceSettings)} == set(vs.LIMITS)


def test_a_non_finite_number_is_refused_rather_than_clamped():
    """NaN survived the clamp only by accident: `max(low, nan)` returns `low`
    because the comparison is False, so reversing those two arguments -- which
    nobody would think twice about while tidying -- would let NaN through to
    `--pitch nan`. argparse takes that as a float and Piper gets a NaN
    length_scale.

    Infinity does clamp correctly. It is refused alongside NaN anyway, because
    a slider cannot produce either and a payload carrying one is not a payload
    to trust the rest of."""
    for value in (float("nan"), float("inf"), float("-inf")):
        settings, notes = vs.coerce({"pitch": value, "robot": value})
        assert settings.is_empty(), f"{value} produced {settings}"
        assert len(notes) == 2
        assert all("not a finite number" in n for n in notes)


def test_every_value_that_survives_is_finite():
    """The property stated directly, over a payload that mixes the fatal with
    the merely extreme -- so something actually survives to be checked.

    The first version of this test passed a payload of nothing BUT non-finite
    values, which are all refused, so every field came out None and
    `math.isfinite` was never called on anything. It was the fourth vacuous
    test I wrote in a day, and it was the test guarding the fragility I had
    just found."""
    import math
    from dataclasses import fields

    mixed = {
        "pitch": float("nan"),      # refused
        "variation": 99.0,          # clamped, survives
        "robot": float("inf"),      # refused
        "robot_hz": -1.0,           # clamped, survives
        "crush": 40,                # clamped, survives
    }
    settings, notes = vs.coerce(mixed)

    survivors = [f.name for f in fields(settings) if getattr(settings, f.name) is not None]
    assert survivors == ["variation", "robot_hz", "crush"], survivors
    for name in survivors:
        value = getattr(settings, name)
        assert math.isfinite(value), f"{name} = {value}"
        low, high = vs.LIMITS[name]
        assert low <= value <= high
    assert len(notes) == 5, notes
