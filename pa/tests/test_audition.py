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
