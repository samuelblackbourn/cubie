"""Tests for the keyframe engine and M5's four dances.

The load-bearing test here is the first one: `move_head` rejects out-of-range
poses rather than clamping them, so a dance whose arithmetic is wrong is not a
slightly-off dance, it is a robot that stands still while the face animates.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import animation  # noqa: E402
import units  # noqa: E402
from chan import Chan, RecordingEffector  # noqa: E402
from tracking import PITCH_MAX, PITCH_MIN, YAW_MAX, YAW_MIN  # noqa: E402


def test_every_keyframe_of_every_dance_produces_a_pose_the_firmware_accepts():
    """The one that matters. Rejection is silent at the animation level: the
    face would animate and the head would not move at all."""
    for name, sequence in animation.SEQUENCES.items():
        c = Chan(RecordingEffector())
        now = 0.0
        for keyframe in sequence:
            animation.apply_keyframe(c, keyframe, now)
            now += keyframe.duration_s
            assert YAW_MIN <= c.motion.target.yaw <= YAW_MAX, name
            assert PITCH_MIN <= c.motion.target.pitch <= PITCH_MAX, name
            assert (
                units.GATEWAY_SPEED_MIN_DPS
                <= c.motion.speed_dps
                <= units.GATEWAY_SPEED_MAX_DPS
            ), name


def test_all_four_of_m5s_dances_are_present():
    assert set(animation.SEQUENCES) == {"happy", "robot", "panic", "look-around"}


def test_keyframe_size_defaults_to_zero_rather_than_being_indeterminate():
    """M5's four-argument FeatureKeyframe constructor -- the only one any dance
    uses -- leaves `size` uninitialised, and Keyframe::apply() then calls
    setSize with it. Every dance in their tree applies a garbage eye size."""
    assert animation.FeatureKeyframe(1, 2, 3, 4).size == 0
    for sequence in animation.SEQUENCES.values():
        for keyframe in sequence:
            assert keyframe.left_eye.size == 0
            assert keyframe.mouth.size == 0


def test_both_eyes_are_identical_in_every_m5_keyframe():
    """Which is why `set_gaze` exists and why the flush can halve the traffic."""
    for sequence in animation.SEQUENCES.values():
        for keyframe in sequence:
            assert keyframe.left_eye == keyframe.right_eye


def test_yaw_and_pitch_speeds_match_in_every_keyframe():
    """We send one pose where M5 drives two servos independently. That is only
    lossless because their sequences never differ the two speeds."""
    for sequence in animation.SEQUENCES.values():
        for keyframe in sequence:
            assert keyframe.yaw.speed == keyframe.pitch.speed


def test_a_keyframe_sets_all_four_axes_on_all_three_features():
    c = Chan(RecordingEffector())
    keyframe = animation.Keyframe(
        left_eye=animation.FeatureKeyframe(-10, -20, 30, 40, 50),
        right_eye=animation.FeatureKeyframe(-10, -20, 30, 40, 50),
        mouth=animation.FeatureKeyframe(1, 2, 3, 4, 5),
        yaw=animation.ServoKeyframe(300, 200),
        pitch=animation.ServoKeyframe(-100, 200),
        duration_s=0.5,
    )
    animation.apply_keyframe(c, keyframe, 0.0)
    assert (c.face.left_eye.x, c.face.left_eye.y) == (-10, -20)
    assert c.face.left_eye.rotation == 30
    assert c.face.left_eye.weight == 40
    assert c.face.left_eye.size == 50
    assert c.face.mouth.weight == 4
    assert c.motion.target.yaw == units.m5_yaw_to_deg(300)
    assert c.motion.target.pitch == units.m5_pitch_to_deg(-100)


# --------------------------------------------------------------- timeline --
def test_the_timeline_applies_the_first_keyframe_immediately():
    seq = animation.SEQUENCES["robot"]
    t = animation.Timeline(seq)
    t.start(0.0)
    assert t.update(0.0) is seq[0]


def test_the_timeline_holds_a_keyframe_for_its_duration():
    seq = animation.SEQUENCES["robot"]
    t = animation.Timeline(seq)
    t.start(0.0)
    t.update(0.0)
    assert t.update(seq[0].duration_s - 0.01) is None
    assert t.update(seq[0].duration_s) is seq[1]


def test_a_late_tick_stretches_a_keyframe_rather_than_skipping_ahead():
    """M5 advances by ONE keyframe per update and resets its clock to now. A
    timeline that caught up would drop exactly the poses that make a dance
    recognisable."""
    seq = animation.SEQUENCES["panic"]
    t = animation.Timeline(seq)
    t.start(0.0)
    t.update(0.0)
    # Ten keyframes' worth of time in one tick.
    assert t.update(10.0) is seq[1]
    assert t.index == 1


def test_a_sequence_finishes_and_says_so():
    seq = animation.SEQUENCES["panic"]
    t = animation.Timeline(seq)
    t.start(0.0)
    now = 0.0
    for _ in range(len(seq) * 2 + 2):
        t.update(now)
        now += 1.0
    assert t.finished


def test_a_looping_sequence_never_finishes():
    seq = animation.SEQUENCES["panic"]
    t = animation.Timeline(seq, loop=True)
    t.start(0.0)
    now = 0.0
    for _ in range(len(seq) * 3):
        t.update(now)
        now += 1.0
    assert not t.finished


def test_pause_and_resume_preserve_the_elapsed_time():
    seq = animation.SEQUENCES["look-around"]
    t = animation.Timeline(seq)
    t.start(0.0)
    t.update(0.0)
    t.pause(0.5)
    assert t.update(100.0) is None       # paused: nothing advances
    t.resume(100.0)
    # 0.5 s was already spent, so the rest of a 1.0 s keyframe remains.
    assert t.update(100.4) is None
    assert t.update(100.6) is seq[1]


def test_an_empty_sequence_finishes_instead_of_hanging():
    t = animation.Timeline(())
    t.start(0.0)
    assert t.finished
    assert t.update(0.0) is None
