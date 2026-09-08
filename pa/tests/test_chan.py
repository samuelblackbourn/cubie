"""Tests for the modifier pool and, mostly, for the flush.

The flush is the part of this port that M5 did not have to write -- their
avatar is memory and LVGL draws it, ours is a robot at the end of a LAN. So it
is the part with no upstream to be faithful to, and the part worth testing
hardest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chan as chan_mod  # noqa: E402
from chan import LEFT_EYE, MOUTH, RIGHT_EYE, Chan, Modifier, RecordingEffector  # noqa: E402


def fresh() -> Chan:
    return Chan(RecordingEffector())


def test_the_first_flush_asserts_state_we_cannot_know():
    """Face, blink and pose are device state, unknown at startup. Starting the
    sent-record at the same defaults as the wanted state would mean the first
    flush said nothing at all -- including the blink assertion."""
    c = fresh()
    c.flush()
    assert "set_avatar" in c.effector.names()
    assert "set_blink" in c.effector.names()
    assert "move_head" in c.effector.names()


def test_a_second_flush_with_nothing_changed_sends_nothing():
    """Breath recomputes every 600 ms and blink every 200 ms. Without a diff
    this would be a flood; without a flush it would be a robot that never
    moves."""
    c = fresh()
    c.flush()
    c.effector.calls.clear()
    c.flush()
    assert c.effector.calls == []


def test_both_eyes_moving_together_uses_one_call_not_two():
    c = fresh()
    c.flush()
    c.effector.calls.clear()
    c.face.set_gaze(-60, -70)
    c.flush()
    assert c.effector.of("set_gaze") == [{"x": -60, "y": -70}]
    assert c.effector.of("set_feature") == []


def test_eyes_moving_independently_fall_back_to_per_feature():
    c = fresh()
    c.flush()
    c.effector.calls.clear()
    c.face.left_eye.x = 20
    c.face.right_eye.x = -20
    c.flush()
    assert c.effector.of("set_gaze") == []
    features = {call["feature"] for call in c.effector.of("set_feature")}
    assert features == {LEFT_EYE, RIGHT_EYE}


def test_position_is_always_sent_as_a_pair():
    """The firmware rejects exactly one of x/y, deliberately -- half a position
    is a silent jump to zero on the other axis. So we must never send half."""
    c = fresh()
    c.flush()
    c.effector.calls.clear()
    c.face.mouth.y = 10          # only y changed
    c.flush()
    call = c.effector.of("set_feature")[0]
    assert "x" in call and "y" in call


def test_weight_and_size_are_not_sent_while_we_are_not_driving_them():
    """None means 'leave it to whoever owns it' -- blink owns eye weight and
    lip-sync owns the mouth's. Sending 0 instead would fight the firmware."""
    c = fresh()
    c.flush()
    c.effector.calls.clear()
    c.face.set_gaze(5, 5)
    c.flush()
    for call in c.effector.of("set_feature"):
        assert "weight" not in call and "size" not in call


def test_a_face_change_makes_us_re_send_the_overrides():
    """set_avatar resets every override on the device to that face's resting
    values. If our record of what the device knows did not reset too, the diff
    would skip re-sending an override that had just been wiped."""
    c = fresh()
    c.face.set_gaze(-60, -70)
    c.flush()
    c.effector.calls.clear()

    c.face.face = "happy"
    c.flush()

    names = c.effector.names()
    assert names.index("set_avatar") < names.index("set_gaze")
    assert c.effector.of("set_gaze") == [{"x": -60, "y": -70}]


def test_the_face_is_always_sent_before_anything_it_would_wipe():
    c = fresh()
    c.flush()
    c.effector.calls.clear()
    c.face.face = "sad"
    c.face.set_gaze(0, 60)
    c.face.speech = "oh"
    c.flush()
    names = c.effector.names()
    assert names[0] == "set_avatar"


def test_leds_are_diffed_like_everything_else():
    c = fresh()
    c.flush()
    c.effector.calls.clear()
    c.face.leds = (0, 50, 0)
    c.flush()
    assert c.effector.of("set_all_leds") == [{"r": 0, "g": 50, "b": 0}]
    c.effector.calls.clear()
    c.flush()
    assert c.effector.of("set_all_leds") == []


# ------------------------------------------------------------------- pool --
class Counting(Modifier):
    def __init__(self, die_after: int | None = None) -> None:
        self.ticks = 0
        self.die_after = die_after

    def update(self, chan: Chan, now: float) -> None:
        self.ticks += 1
        if self.die_after is not None and self.ticks >= self.die_after:
            self.request_destroy()


def test_every_modifier_runs_each_tick_and_finished_ones_are_reaped():
    c = fresh()
    forever = c.add(Counting())
    once = c.add(Counting(die_after=1))
    c.update(0.0)
    assert once not in c.modifiers
    assert forever in c.modifiers
    c.update(1.0)
    assert forever.ticks == 2 and once.ticks == 1


class Spawning(Modifier):
    """Head-pet adds and removes modifiers from inside its own update, so the
    pool has to tolerate the list changing mid-iteration."""

    def update(self, chan: Chan, now: float) -> None:
        chan.add(Counting())
        self.request_destroy()


def test_a_modifier_may_add_another_during_the_tick():
    c = fresh()
    c.add(Spawning())
    c.update(0.0)  # must not raise
    assert any(isinstance(m, Counting) for m in c.modifiers)


def test_is_moving_is_estimated_from_distance_and_speed():
    """We do not ask the robot whether it is still moving -- a round trip per
    idle tick would cost more than it tells us. The estimate only has to be
    good enough for M5's defer-and-retry rule."""
    c = fresh()
    c.motion.move_with_speed(0.0, 45.0, 60, now=0.0)
    c.motion.move_with_speed(60.0, 45.0, 60, now=0.0)  # 60 deg at 60 dps = 1 s
    assert c.motion.is_moving(0.5)
    assert not c.motion.is_moving(1.5)


def test_a_locked_motion_ignores_move_requests():
    c = fresh()
    c.motion.locked = True
    c.motion.move_with_speed(80.0, 20.0, 100, now=0.0)
    assert c.motion.target.yaw == 0.0
