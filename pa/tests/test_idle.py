"""Tests for idle behaviour.

Pure, seeded, no robot. The properties worth pinning are the ones that would
be unpleasant to discover on a desk: a pose the firmware rejects outright, a
head that random-walks into a corner and stays there, or sudden fast movement
often enough to be unnerving.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import idle  # noqa: E402
from tracking import PITCH_MAX, PITCH_MIN, YAW_MAX, YAW_MIN, Pose  # noqa: E402


def test_every_move_is_inside_the_servo_limits():
    """move_head REJECTS out-of-range values rather than clamping, so an
    unclamped target is a head that does not move at all."""
    rng = random.Random(1)
    extremes = [
        Pose(YAW_MIN, PITCH_MIN),
        Pose(YAW_MAX, PITCH_MAX),
        Pose(0.0, 45.0),
        Pose(YAW_MAX, PITCH_MIN),
        Pose(YAW_MIN, PITCH_MAX),
    ]
    for start in extremes:
        for _ in range(400):
            move = idle.next_move(start, rng)
            assert YAW_MIN <= move.yaw <= YAW_MAX
            assert PITCH_MIN <= move.pitch <= PITCH_MAX


def test_the_weights_are_M5s():
    """The feel of idle behaviour is mostly this distribution, so it is worth
    asserting rather than trusting."""
    rng = random.Random(7)
    seen = Counter(idle.next_move(Pose(0.0, 45.0), rng).kind for _ in range(4000))
    assert 0.44 < seen["look-around"] / 4000 < 0.56
    assert 0.25 < seen["small-observation"] / 4000 < 0.35
    assert 0.06 < seen["quick-glance"] / 4000 < 0.14
    assert 0.06 < seen["recentre"] / 4000 < 0.14


def test_only_the_glance_is_fast():
    """Frequent sudden movement is unnerving to sit next to; occasional
    sudden movement is what makes a machine seem to have noticed something."""
    rng = random.Random(3)
    for _ in range(2000):
        move = idle.next_move(Pose(0.0, 45.0), rng)
        if move.kind == "quick-glance":
            assert move.speed_dps >= 150
        else:
            assert move.speed_dps <= 80


def test_speeds_are_always_positive():
    rng = random.Random(11)
    for _ in range(500):
        assert idle.next_move(Pose(0.0, 45.0), rng).speed_dps > 0


def test_small_observation_stays_near_the_current_pose():
    """It reads as attention rather than a decision, which needs it to start
    from where the head already is."""
    rng = random.Random(5)
    start = Pose(40.0, 60.0)
    found = 0
    for _ in range(2000):
        move = idle.next_move(start, rng)
        if move.kind == "small-observation":
            found += 1
            assert abs(move.yaw - start.yaw) <= 15.001
            assert abs(move.pitch - start.pitch) <= 8.001
    assert found > 100, "the sample should contain plenty of these"


def test_recentre_always_returns_yaw_to_zero():
    rng = random.Random(13)
    found = 0
    for _ in range(3000):
        move = idle.next_move(Pose(70.0, 45.0), rng)
        if move.kind == "recentre":
            found += 1
            assert move.yaw == 0.0
    assert found > 100


def test_yaw_does_not_drift_into_a_corner_over_time():
    """The real failure this guards: three of the four actions random-walk,
    and without the recentre action he ends up facing a wall and staying
    there. Simulating a long idle period is the only way to see it.
    """
    rng = random.Random(17)
    pose = idle.resting()
    extremes = 0
    for _ in range(3000):
        move = idle.next_move(pose, rng)
        pose = Pose(move.yaw, move.pitch)
        if abs(pose.yaw) > 80.0:
            extremes += 1
    # He should spend almost no time pinned near the mechanical limit.
    assert extremes < 60, f"spent {extremes}/3000 steps near the yaw limit"
    # And should not have ended up parked there.
    assert abs(pose.yaw) < 80.0


def test_pitch_stays_in_a_comfortable_band_not_just_a_legal_one():
    """Legal is 5..85, but a head pointing at the ceiling or the desk for
    minutes looks broken rather than idle."""
    rng = random.Random(19)
    pose = idle.resting()
    for _ in range(2000):
        move = idle.next_move(pose, rng)
        pose = Pose(move.yaw, move.pitch)
        assert 15.0 <= pose.pitch <= 70.0


def test_interval_matches_M5s_range():
    rng = random.Random(23)
    for _ in range(500):
        seconds = idle.next_interval(rng)
        assert idle.INTERVAL_MIN_S <= seconds <= idle.INTERVAL_MAX_S


def test_interval_is_actually_random_not_fixed():
    """A fixed tick reads as a metronome, which is the thing M5's random
    interval avoids."""
    rng = random.Random(29)
    values = {round(idle.next_interval(rng), 3) for _ in range(50)}
    assert len(values) > 40


def test_resting_pose_is_legal():
    pose = idle.resting()
    assert PITCH_MIN <= pose.pitch <= PITCH_MAX
    assert YAW_MIN <= pose.yaw <= YAW_MAX


def test_no_rng_supplied_still_works():
    move = idle.next_move(Pose(0.0, 45.0))
    assert YAW_MIN <= move.yaw <= YAW_MAX
