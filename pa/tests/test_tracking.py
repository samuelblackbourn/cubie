"""Tests for the tracking geometry.

No camera, no servos. Everything here is a fact about the maths, which is
where the bugs that reach the robot actually are: a sign error makes him turn
away from you, a missing clamp makes `move_head` reject the command outright,
and a missing deadband makes him twitch at a stationary face.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tracking as tr  # noqa: E402


def test_centred_face_has_zero_offset():
    box = tr.Box(x=140, y=100, width=40, height=40)
    assert tr.offset(box, 320, 240) == (0.0, 0.0)


def test_offset_signs_follow_image_coordinates():
    """Positive x is right of centre; positive y is BELOW centre."""
    right = tr.Box(x=280, y=100, width=40, height=40)
    dx, _ = tr.offset(right, 320, 240)
    assert dx > 0

    low = tr.Box(x=140, y=200, width=40, height=40)
    _, dy = tr.offset(low, 320, 240)
    assert dy > 0


def test_offset_is_bounded_even_for_a_box_outside_the_frame():
    """A detector can return a box that overhangs the edge."""
    box = tr.Box(x=400, y=-50, width=40, height=40)
    dx, dy = tr.offset(box, 320, 240)
    assert -1.0 <= dx <= 1.0 and -1.0 <= dy <= 1.0


def test_zero_frame_size_is_rejected():
    with pytest.raises(ValueError):
        tr.offset(tr.Box(0, 0, 10, 10), 0, 240)


def test_deadband_holds_the_head_still():
    """The whole point: a jittering box must not become a twitching head."""
    current = tr.Pose(yaw=10.0, pitch=40.0)
    nudged = tr.step_toward(current, 0.05, 0.05)
    assert nudged == current


def test_outside_the_deadband_it_moves():
    current = tr.Pose(yaw=0.0, pitch=45.0)
    moved = tr.step_toward(current, 0.5, 0.5)
    assert moved != current


def test_it_turns_toward_the_face_under_either_sign(monkeypatch):
    """Flipping YAW_SIGN must flip the direction and nothing else.

    The sign is an empirical fact about one robot's assembly, so the test
    pins the RELATIONSHIP rather than a direction: whichever way is
    configured, the two signs must move opposite ways by the same amount.
    """
    current = tr.Pose(yaw=0.0, pitch=45.0)

    monkeypatch.setattr(tr, "YAW_SIGN", 1.0)
    positive = tr.step_toward(current, 0.6, 0.0)

    monkeypatch.setattr(tr, "YAW_SIGN", -1.0)
    negative = tr.step_toward(current, 0.6, 0.0)

    assert positive.yaw == pytest.approx(-negative.yaw)
    assert positive.yaw != 0.0


def test_pitch_never_leaves_the_firmware_range():
    """move_head REJECTS pitch outside 5..85 rather than clamping it.

    So an out-of-range value is not a slightly wrong head position, it is a
    failed call and a head that does not move at all.
    """
    for start in (tr.PITCH_MIN, tr.PITCH_MAX, 6.0, 84.0, 45.0):
        for dy in (-1.0, -0.5, 0.0, 0.5, 1.0):
            pose = tr.step_toward(tr.Pose(yaw=0.0, pitch=start), 0.0, dy)
            assert tr.PITCH_MIN <= pose.pitch <= tr.PITCH_MAX


def test_yaw_never_leaves_the_mechanical_range():
    for start in (tr.YAW_MIN, tr.YAW_MAX, -89.0, 89.0, 0.0):
        for dx in (-1.0, -0.5, 0.0, 0.5, 1.0):
            pose = tr.step_toward(tr.Pose(yaw=start, pitch=45.0), dx, 0.0)
            assert tr.YAW_MIN <= pose.yaw <= tr.YAW_MAX


def test_no_single_step_exceeds_the_cap():
    """A snapping head is unpleasant, and the servo bus has hung on big
    abrupt reversals before."""
    for dx in (-1.0, 1.0):
        for dy in (-1.0, 1.0):
            current = tr.Pose(yaw=0.0, pitch=45.0)
            pose = tr.step_toward(current, dx, dy, gain=1.0)
            assert abs(pose.yaw - current.yaw) <= tr.MAX_STEP_DEG + 1e-9
            assert abs(pose.pitch - current.pitch) <= tr.MAX_STEP_DEG + 1e-9


def test_the_loop_converges_rather_than_oscillating():
    """Feed back a shrinking error, as a real camera would, and the head
    should settle rather than hunt."""
    pose = tr.Pose(yaw=0.0, pitch=45.0)
    dx = 0.9
    seen = []
    for _ in range(25):
        pose = tr.step_toward(pose, dx, 0.0)
        seen.append(pose.yaw)
        # The face appears to move toward centre as the head turns to it.
        dx *= 0.5
    # Settled: the last few steps barely move.
    assert abs(seen[-1] - seen[-2]) < 0.5
    # And it actually went somewhere.
    assert abs(seen[-1]) > 1.0


def test_rest_drifts_back_and_arrives():
    pose = tr.Pose(yaw=60.0, pitch=80.0)
    for _ in range(50):
        pose = tr.step_to_rest(pose)
    assert pose.yaw == pytest.approx(tr.REST_YAW)
    assert pose.pitch == pytest.approx(tr.REST_PITCH)


def test_rest_is_slower_than_tracking():
    """He should linger when you look away, not whip back."""
    far = tr.Pose(yaw=90.0, pitch=45.0)
    rest_step = abs(tr.step_to_rest(far).yaw - far.yaw)
    assert rest_step <= tr.MAX_STEP_DEG / 2.0 + 1e-9


def test_rest_pose_is_itself_legal():
    """A resting pitch outside 5..85 would be rejected by the firmware."""
    assert tr.PITCH_MIN <= tr.REST_PITCH <= tr.PITCH_MAX
    assert tr.YAW_MIN <= tr.REST_YAW <= tr.YAW_MAX


def test_picks_the_nearest_face_not_the_first():
    small = tr.Box(x=0, y=0, width=20, height=20)
    big = tr.Box(x=100, y=100, width=60, height=60)
    assert tr.largest([small, big]) is big
    assert tr.largest([big, small]) is big


def test_no_faces_is_none_not_an_error():
    assert tr.largest([]) is None
