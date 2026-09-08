"""Tests for voice selection.

The filtering is pure and testable; the downloading and the speaking are not,
and are exercised on office-server where the catalogue and the robot both
exist. The names below are a fixture, not a claim about what Piper publishes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audition import select, spoken_name  # noqa: E402
from character import CHARACTERS, resolve  # noqa: E402

CATALOGUE = [
    "de_DE-thorsten-medium",
    "en_GB-alan-low",
    "en_GB-alan-medium",
    "en_GB-alba-medium",
    "en_GB-cori-high",
    "en_US-amy-low",
    "en_US-ryan-high",
]


def test_lang_prefix_filters():
    assert select(CATALOGUE, lang="en_GB") == [
        "en_GB-alan-low",
        "en_GB-alan-medium",
        "en_GB-alba-medium",
        "en_GB-cori-high",
    ]


def test_a_broad_prefix_matches_both_englishes():
    assert len(select(CATALOGUE, lang="en")) == 6


def test_quality_filters_on_the_suffix_not_a_substring():
    """`low` must not match `en_US-amy-low` via the word appearing elsewhere,
    nor miss it. Suffix matching is what makes that reliable."""
    assert select(CATALOGUE, quality="low") == ["en_GB-alan-low", "en_US-amy-low"]


def test_lang_and_quality_combine():
    assert select(CATALOGUE, lang="en_GB", quality="low") == ["en_GB-alan-low"]


def test_grep_is_case_insensitive():
    assert select(CATALOGUE, grep="ALBA") == ["en_GB-alba-medium"]


def test_only_wins_and_is_not_validated_against_the_catalogue():
    """A voice can exist on disk without being published upstream; refusing
    to speak it would be unhelpful."""
    assert select(CATALOGUE, only=["something-local-medium"]) == [
        "something-local-medium"
    ]
    assert select([], only=["en_GB-alan-low"]) == ["en_GB-alan-low"]


def test_no_match_is_empty_not_an_error():
    assert select(CATALOGUE, lang="fr") == []


def test_spoken_name_is_worth_hearing_aloud():
    """Reading the raw identifier aloud is unpleasant and hard to follow."""
    assert spoken_name("en_GB-alan-low") == "This is alan, low quality."
    assert spoken_name("en_US-southern_english_female-medium") == (
        "This is southern english female, medium quality."
    )


def test_spoken_name_survives_an_unexpected_shape():
    assert spoken_name("weird") == "This is weird."


# --- named characters ---------------------------------------------------------

def test_every_character_is_resolvable_and_named_consistently():
    for key, character in CHARACTERS.items():
        assert resolve(key) is character
        assert character.name == key, "the key and the spoken name must agree"


def test_resolve_is_case_and_space_insensitive():
    assert resolve("  CUTE ").name == "cute"


def test_unknown_character_lists_the_known_ones():
    import pytest as _pytest

    with _pytest.raises(KeyError) as exc:
        resolve("walle")
    assert "cute" in str(exc.value)


def test_plain_really_is_plain():
    """It is the baseline everything else is judged against, so it must
    apply no processing at all."""
    plain = resolve("plain")
    assert plain.pitch == 1.0
    assert plain.robot == 0.0
    assert plain.crush == 16
    assert plain.variation is None


def test_pitches_are_sane_and_span_both_directions():
    pitches = {k: c.pitch for k, c in CHARACTERS.items()}
    assert all(0.5 <= p <= 2.0 for p in pitches.values())
    assert pitches["gruff"] < 1.0 < pitches["cute"]


def test_modulation_stays_intelligible_in_the_presets():
    """Depth near 1 is hard to follow, and this reads out approvals.
    Heavy settings are available explicitly, not by choosing a character."""
    assert all(c.robot <= 0.6 for c in CHARACTERS.values())


def test_each_character_introduces_itself_by_name():
    for character in CHARACTERS.values():
        assert character.name in character.spoken_intro()
