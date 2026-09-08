"""Tests for the character driver -- which modifiers exist when.

This is the port of M5's own xiaozhi integration, so the properties worth
pinning are the ones their file exists to get right: he settles down when
spoken to, and he comes back to life when the conversation ends.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import driver  # noqa: E402
import modifiers  # noqa: E402
from chan import Chan, RecordingEffector  # noqa: E402


def fresh(level: int = 2) -> driver.CharacterDriver:
    return driver.CharacterDriver(
        Chan(RecordingEffector()), idle_motion_level=level, rng=random.Random(42)
    )


def test_the_standing_set_is_installed_once():
    """M5 installs breath, blink, head-pet and the IMU reaction at avatar
    creation and never removes them. Blink is the firmware's here."""
    d = fresh()
    kinds = {type(m) for m in d.chan.modifiers}
    assert modifiers.BreathModifier in kinds
    assert modifiers.HeadPetModifier in kinds
    assert modifiers.TiltReactionModifier in kinds


def test_blinking_is_asserted_on_startup():
    """It is runtime state on the device and unknown until we say so -- which
    is why it had been off."""
    d = fresh()
    d.update(0.0)
    assert {"enabled": True} in d.chan.effector.of("set_blink")


def test_standby_brings_idle_behaviour_to_life():
    d = fresh()
    d.set_status(driver.STANDBY)
    assert d.idle_motion is not None
    assert d.idle_expression is not None


def test_listening_stops_him_glancing_around():
    """M5's choice and worth keeping: a robot that keeps looking away while you
    talk to it reads as not listening."""
    d = fresh()
    d.set_status(driver.STANDBY)
    d.set_status(driver.LISTENING)
    assert d.idle_motion is None
    assert d.idle_expression is None


def test_speaking_adds_the_speaking_modifier_exactly_once():
    d = fresh()
    d.set_status(driver.SPEAKING)
    first = d.speaking
    d.set_status(driver.SPEAKING)
    assert d.speaking is first
    assert sum(isinstance(m, modifiers.SpeakingModifier) for m in d.chan.modifiers) == 1


def test_leaving_speaking_removes_it_and_releases_the_mouth():
    d = fresh()
    d.set_status(driver.SPEAKING)
    d.set_status(driver.LISTENING)
    assert d.speaking is None
    assert d.chan.face.mouth.weight is None


def test_the_full_conversation_cycle_returns_to_idle():
    d = fresh()
    for status in (driver.STANDBY, driver.LISTENING, driver.SPEAKING, driver.STANDBY):
        d.set_status(status)
    assert d.idle_motion is not None
    assert d.speaking is None


def test_an_unrecognised_status_goes_in_the_speech_bubble():
    """M5's fallback, and a genuinely useful one -- "Connecting..." ends up on
    his face rather than nowhere."""
    d = fresh()
    d.set_status("Connecting...")
    assert d.chan.face.speech == "Connecting..."


def test_a_recognised_status_clears_a_bubble_a_previous_one_left():
    d = fresh()
    d.set_status("Updating...")
    d.set_status(driver.STANDBY)
    assert d.chan.face.speech == ""


def test_the_status_leds_are_m5s():
    d = fresh()
    d.set_status(driver.LISTENING)
    assert d.chan.face.leds == (0, 50, 0)
    d.set_status(driver.SPEAKING)
    assert d.chan.face.leds == (0, 0, 50)
    d.set_status(driver.STANDBY)
    assert d.chan.face.leds == (0, 0, 0)


def test_idle_motion_level_zero_still_gives_him_a_living_face():
    """The levels are about how much the HEAD moves. A still head with a
    drifting gaze is a coherent thing to want."""
    d = fresh(level=0)
    d.set_status(driver.STANDBY)
    assert d.idle_motion is None
    assert d.idle_expression is not None


# ------------------------------------------------------------- emotions --
def test_every_emotion_maps_to_a_face_the_board_actually_has():
    from chan import FACES

    for emotion, face in driver.EMOTION_FACES.items():
        assert face in FACES, emotion


def test_m5s_own_emotion_table_is_carried_over():
    assert driver.EMOTION_FACES["neutral"] == "idle"
    assert driver.EMOTION_FACES["laughing"] == "happy"
    assert driver.EMOTION_FACES["crying"] == "sad"
    assert driver.EMOTION_FACES["doubtful"] == "thinking"


def test_angry_lands_on_sad_because_this_board_has_no_angry_face():
    """Documented rather than silently wrong: M5's renderer has Emotion::Angry
    but this board's face list never mapped anything to it."""
    assert driver.EMOTION_FACES["angry"] == "sad"


def test_an_unknown_emotion_falls_back_to_idle_rather_than_raising():
    d = fresh()
    assert d.set_emotion("smug") == "idle"


def test_sleepy_is_a_whole_state_not_just_a_face():
    d = fresh()
    d.set_status(driver.STANDBY)
    d.set_emotion("sleepy")
    assert d.sleeping
    assert d.chan.face.speech.startswith("Zzz")
    assert d.idle_motion is None      # he should not doze off mid-glance


def test_the_next_status_wakes_him_out_of_sleep():
    d = fresh()
    d.set_emotion("sleepy")
    d.set_status(driver.LISTENING)
    assert not d.sleeping
    assert d.chan.face.speech == ""


# ------------------------------------------------------------- messages --
def test_a_message_gets_a_bubble_and_some_mouth_movement():
    d = fresh()
    d.on_message("Sam", "the build is green")
    kinds = [type(m) for m in d.chan.modifiers]
    assert modifiers.TimedSpeechModifier in kinds
    assert modifiers.SpeakingModifier in kinds


def test_being_greeted_makes_him_happy():
    d = fresh()
    d.on_message("Sam", "hello there")
    assert any(
        isinstance(m, modifiers.TimedFaceModifier) and m.face == "happy"
        for m in d.chan.modifiers
    )


def test_the_keyword_match_is_whole_word():
    """A substring match would make "think" contain "hi" and greet the office
    every time somebody thought about something."""
    assert driver.contains_word("hello there", ("hello", "hi"))
    assert driver.contains_word("hi!", ("hello", "hi"))
    assert not driver.contains_word("let me think", ("hello", "hi"))
    assert not driver.contains_word("this is history", ("hello", "hi"))


def test_only_one_keyword_reaction_fires_per_message():
    d = fresh()
    d.on_message("Sam", "hello, I love this")
    reactions = [m for m in d.chan.modifiers if isinstance(m, modifiers.TimedFaceModifier)]
    assert len(reactions) == 1


# ---------------------------------------------------------------- touch --
def test_a_stroke_event_reaches_the_head_pet_reaction():
    d = fresh()
    d.update(0.0)
    d.on_touch("stroke")
    d.update(0.1)
    assert d.chan.face.face == "happy"


def test_an_unrelated_event_subtype_is_ignored():
    d = fresh()
    d.update(0.0)
    d.chan.face.face = "idle"
    d.on_touch("swipe-left")
    d.update(0.1)
    assert d.chan.face.face == "idle"


# ---------------------------------------------------------------- dance --
def test_starting_a_dance_stands_idle_motion_down():
    """They would be two things steering the head at once."""
    d = fresh()
    d.set_status(driver.STANDBY)
    d.dance("happy")
    assert d.idle_motion is None
    assert any(isinstance(m, modifiers.DanceModifier) for m in d.chan.modifiers)


def test_a_dance_finishes_and_removes_itself():
    d = fresh()
    d.dance("panic")
    now = 0.0
    while now < 6.0:
        d.update(now)
        now += 0.1
    assert not any(isinstance(m, modifiers.DanceModifier) for m in d.chan.modifiers)
