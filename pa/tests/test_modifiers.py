"""Tests for the ported modifiers.

Seeded and clock-driven: every modifier owns its own timing, so the way to test
one is to step a clock past its interval and assert what it did to the shared
state -- never to sleep.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modifiers  # noqa: E402
from chan import Chan, RecordingEffector  # noqa: E402
from tracking import PITCH_MAX, PITCH_MIN, YAW_MAX, YAW_MIN  # noqa: E402


def fresh() -> Chan:
    return Chan(RecordingEffector())


def run(chan: Chan, until: float, step: float = 0.1) -> None:
    now = 0.0
    while now <= until:
        chan.update(now)
        now += step


# ------------------------------------------------------------------ breath --
def test_breath_moves_all_three_features_together():
    c = fresh()
    c.add(modifiers.BreathModifier())
    run(c, 2.0)
    assert c.face.left_eye.y == c.face.right_eye.y == c.face.mouth.y
    assert c.face.left_eye.y != 0


def test_breath_composes_with_a_gaze_rather_than_overwriting_it():
    """Their version applies a DELTA against the last offset it applied, which
    is the whole reason it tracks _last_applied_offset. An absolute version
    would erase whatever the idle-expression modifier had just set."""
    c = fresh()
    breath = modifiers.BreathModifier()
    c.add(breath)
    run(c, 1.0)
    drifted = c.face.left_eye.y

    c.face.set_gaze(-60, -70)            # something else moves the eyes
    run_from = -70
    c.update(1.5)
    # The breath delta was applied on top of the new gaze, not instead of it.
    assert c.face.left_eye.y != drifted
    assert abs(c.face.left_eye.y - run_from) <= breath.amplitude * 2


def test_breath_amplitude_is_in_m5s_position_units_not_pixels():
    """M5's docstring says pixels, but move_component adds the delta to
    getPosition(), which is the -100..100 normalised value. 16 units is about
    2.6 px of the +/-16 px travel -- a subtle wobble, which is correct."""
    c = fresh()
    c.add(modifiers.BreathModifier(amplitude=16))
    seen = set()
    now = 0.0
    while now < 14.0:
        c.update(now)
        seen.add(c.face.left_eye.y)
        now += 0.1
    assert max(seen) <= 16 and min(seen) >= -16


def test_breath_returns_the_face_to_centre_when_it_expires():
    c = fresh()
    c.add(modifiers.BreathModifier(duration_s=3.0))
    run(c, 5.0)
    assert c.face.left_eye.y == 0
    assert not c.modifiers


# -------------------------------------------------------- idle expression --
def test_idle_expression_drifts_the_gaze():
    c = fresh()
    c.add(modifiers.IdleExpressionModifier(rng=random.Random(7)))
    run(c, 30.0)
    assert (c.face.left_eye.x, c.face.left_eye.y) != (0, 0) or c.face.mouth.rotation != 0


def test_idle_expression_stays_inside_m5s_ranges():
    """Their gaze drift is deliberately small: +/-20 and +/-15, not the full
    +/-100. A face whose eyes swing to the corners looks unwell."""
    c = fresh()
    c.add(modifiers.IdleExpressionModifier(rng=random.Random(3)))
    now = 0.0
    while now < 300.0:
        c.update(now)
        assert -20 <= c.face.left_eye.x <= 20
        assert -15 <= c.face.left_eye.y <= 15
        assert 0 <= c.face.mouth.y <= 10
        assert 0 <= c.face.mouth.rotation <= 3600
        now += 0.25


def test_idle_expressions_reset_releases_weight_rather_than_zeroing_it():
    """M5 sets mouth weight and eye size to 0 in reset_to_neutral because they
    own those axes. We must RELEASE them, or every fifth idle beat would flatten
    the firmware's per-face resting mouth and the `surprised` eye size."""
    c = fresh()
    m = modifiers.IdleExpressionModifier(rng=random.Random(1))
    c.face.mouth.weight = 40
    c.face.left_eye.size = 100
    m._reset(c)
    assert c.face.mouth.weight is None
    assert c.face.left_eye.size is None


def test_idle_expression_waits_before_its_first_action():
    c = fresh()
    c.add(modifiers.IdleExpressionModifier(rng=random.Random(1)))
    c.update(0.0)
    c.update(0.4)
    assert (c.face.left_eye.x, c.face.left_eye.y) == (0, 0)


# ------------------------------------------------------------ idle motion --
def test_idle_motion_actually_moves_the_head():
    """The reported symptom: idle.py existed and nothing ran it."""
    c = fresh()
    c.add(modifiers.IdleMotionModifier(rng=random.Random(5)))
    c.effector.calls.clear()
    run(c, 20.0)
    assert len(c.effector.of("move_head")) >= 2


def test_idle_motion_never_leaves_the_servo_window():
    c = fresh()
    c.add(modifiers.IdleMotionModifier(0.1, 0.2, rng=random.Random(11)))
    now = 0.0
    while now < 200.0:
        c.update(now)
        assert YAW_MIN <= c.motion.target.yaw <= YAW_MAX
        assert PITCH_MIN <= c.motion.target.pitch <= PITCH_MAX
        now += 0.05


def test_idle_motion_defers_instead_of_queueing_while_the_head_is_moving():
    """M5's rule, and the reason idle motion does not fight itself."""
    c = fresh()
    m = modifiers.IdleMotionModifier(0.1, 0.1, rng=random.Random(2))
    c.add(m)
    c.motion.move_with_speed(80.0, 45.0, 5, now=0.0)   # a long, slow move
    c.update(0.0)                 # flush it, so the startup assertion is spent
    c.effector.calls.clear()
    now = 1.2
    while now < 3.0:
        c.update(now)
        now += 0.1
    assert c.effector.of("move_head") == []


def test_m5s_four_idle_levels_are_carried_over():
    assert modifiers.IdleMotionModifier.at_level(0) is None
    assert modifiers.IdleMotionModifier.at_level(1).interval_min_s == 8.0
    assert modifiers.IdleMotionModifier.at_level(2).interval_min_s == 4.0
    assert modifiers.IdleMotionModifier.at_level(3).interval_min_s == 2.0


def test_idle_motion_respects_a_motion_lock():
    c = fresh()
    c.add(modifiers.IdleMotionModifier(0.1, 0.1, rng=random.Random(4)))
    c.motion.locked = True
    c.update(0.0)                 # the first flush always asserts the pose
    c.effector.calls.clear()
    run(c, 5.0)
    assert c.effector.of("move_head") == []


# ---------------------------------------------------------------- speaking --
def test_speaking_nudges_the_head_but_leaves_the_mouth_alone_by_default():
    """The opposite of M5's own xiaozhi integration, on purpose: our lip-sync
    is firmware-driven off tts.start, so the mouth is already handled and a
    180 ms host flap would fight it."""
    c = fresh()
    c.add(modifiers.SpeakingModifier(rng=random.Random(9)))
    c.effector.calls.clear()
    run(c, 12.0)
    assert len(c.effector.of("move_head")) >= 1
    assert c.face.mouth.weight is None


def test_speaking_can_drive_the_mouth_when_asked():
    c = fresh()
    c.add(modifiers.SpeakingModifier(drive_mouth=True, rng=random.Random(9)))
    run(c, 2.0)
    assert c.face.mouth.weight is not None


def test_speaking_releases_the_mouth_when_it_expires():
    c = fresh()
    c.add(modifiers.SpeakingModifier(1.0, drive_mouth=True, rng=random.Random(9)))
    run(c, 3.0)
    assert c.face.mouth.weight is None
    assert not c.modifiers


def test_speaking_motion_stays_inside_the_servo_window():
    c = fresh()
    c.add(modifiers.SpeakingModifier(rng=random.Random(6)))
    now = 0.0
    while now < 120.0:
        c.update(now)
        assert YAW_MIN <= c.motion.target.yaw <= YAW_MAX
        assert PITCH_MIN <= c.motion.target.pitch <= PITCH_MAX
        now += 0.2


# ---------------------------------------------------------------- head pet --
def test_a_stroke_makes_him_happy_and_moves_his_head():
    c = fresh()
    pet = c.add(modifiers.HeadPetModifier(rng=random.Random(8)))
    c.face.face = "idle"
    c.update(0.0)
    c.effector.calls.clear()
    pet.on_swipe()
    c.update(0.1)
    assert c.face.face == "happy"
    assert c.effector.of("move_head")


def test_he_goes_back_to_the_face_he_had_after_the_settle_delay():
    c = fresh()
    pet = c.add(modifiers.HeadPetModifier(restore_delay_s=3.0, rng=random.Random(8)))
    c.face.face = "thinking"
    c.update(0.0)
    pet.on_swipe()
    c.update(0.1)
    pet.on_release()
    c.update(0.2)
    assert c.face.face == "happy"        # still happy, settling
    run(c, 5.0)
    assert c.face.face == "thinking"


def test_stroking_again_postpones_the_settle():
    """M5: "只要在摸，就推迟恢复时间" -- as long as you are stroking, push the
    restore back."""
    c = fresh()
    pet = c.add(modifiers.HeadPetModifier(restore_delay_s=1.0, rng=random.Random(8)))
    c.face.face = "idle"
    c.update(0.0)
    pet.on_swipe()
    c.update(0.1)
    pet.on_release()
    c.update(0.2)
    pet.on_swipe()                        # stroked again before the settle
    c.update(0.3)
    c.update(1.5)
    assert c.face.face == "happy"


def test_pet_motion_stays_inside_the_servo_window():
    for seed in range(20):
        c = fresh()
        pet = c.add(modifiers.HeadPetModifier(rng=random.Random(seed)))
        for i in range(20):
            pet.on_swipe()
            c.update(i * 2.0)
            assert YAW_MIN <= c.motion.target.yaw <= YAW_MAX
            assert PITCH_MIN <= c.motion.target.pitch <= PITCH_MAX


# ----------------------------------------------------------- tilt reaction --
def test_the_tilt_reaction_wobbles_the_mouth_and_locks_the_head():
    c = fresh()
    tilt = c.add(modifiers.TiltReactionModifier())
    tilt.on_shake()
    c.update(0.0)
    assert tilt.reacting
    assert c.face.mouth.weight == 65
    assert c.motion.locked


def test_the_tilt_wobble_stays_a_legal_rotation():
    """M5 passes -25 straight to setRotation, which clamps to 0..3600 -- so
    their negative half is silently clamped to 0 and only one side of the
    wobble ever shows. Expressed here as 3600-25, which is what they meant."""
    c = fresh()
    tilt = c.add(modifiers.TiltReactionModifier())
    tilt.on_shake()
    seen = set()
    now = 0.0
    while now < 3.0:
        c.update(now)
        seen.add(c.face.mouth.rotation)
        now += 0.1
    assert {25, 3575} <= seen
    assert all(0 <= r <= 3600 for r in seen)


def test_the_tilt_reaction_releases_the_head_when_it_ends():
    c = fresh()
    tilt = c.add(modifiers.TiltReactionModifier(reaction_duration_s=1.0))
    tilt.on_shake()
    run(c, 3.0)
    assert not tilt.reacting
    assert not c.motion.locked
    assert c.face.mouth.weight is None


# ------------------------------------------------------------------ timers --
def test_a_timed_face_reverts_to_what_was_there_before():
    c = fresh()
    c.face.face = "sad"
    c.add(modifiers.TimedFaceModifier("happy", 2.0))
    c.update(0.0)
    assert c.face.face == "happy"
    run(c, 3.0)
    assert c.face.face == "sad"


def test_a_timed_speech_clears_its_own_bubble():
    c = fresh()
    c.add(modifiers.TimedSpeechModifier("Sam says: hello", 2.0))
    c.update(0.0)
    assert c.face.speech == "Sam says: hello"
    run(c, 3.0)
    assert c.face.speech == ""


def test_a_zero_duration_timer_starts_and_ends_in_one_tick():
    """M5's behaviour, and how a one-shot is expressed."""
    c = fresh()
    c.face.face = "idle"
    c.add(modifiers.TimedFaceModifier("surprised", 0.0))
    c.update(0.0)
    assert c.face.face == "idle"
    assert not c.modifiers


# ------------------------------------------------------------------- dance --
def test_a_dance_suspends_blink_and_puts_it_back():
    """The dances drive eye weight, which on this device belongs to the
    firmware's blink state machine. Without this the eyes would blink over the
    squint and the dance would flicker."""
    c = fresh()
    c.face.blink_enabled = True
    c.add(modifiers.DanceModifier.named("happy"))
    c.update(0.0)
    assert c.face.blink_enabled is False
    run(c, 10.0)
    assert c.face.blink_enabled is True


def test_a_dance_releases_the_axes_it_was_driving():
    c = fresh()
    c.add(modifiers.DanceModifier.named("happy"))
    run(c, 10.0)
    assert c.face.left_eye.weight is None
    assert c.face.mouth.weight is None
    assert not c.modifiers


def test_a_dance_reaches_every_keyframe():
    c = fresh()
    c.add(modifiers.DanceModifier.named("look-around"))
    poses = []
    now = 0.0
    while now < 10.0:
        c.update(now)
        poses.append((round(c.motion.target.yaw, 1), round(c.motion.target.pitch, 1)))
        now += 0.1
    distinct = set(poses)
    assert len(distinct) >= 4


def test_an_unknown_dance_raises_rather_than_doing_nothing():
    try:
        modifiers.DanceModifier.named("floss")
    except KeyError as exc:
        assert "floss" in str(exc)
    else:
        raise AssertionError("expected KeyError")
