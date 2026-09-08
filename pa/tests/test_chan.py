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


# ------------------------------------------- the gateway's closed tool table --
def test_an_unknown_tool_answer_is_detected_despite_looking_successful():
    """The gateway answers a tool it does not know with an ORDINARY successful
    result whose text is `{"error": "Unknown tool: set_gaze"}` -- no isError,
    no exception. Trusting the flag made a real blocker invisible: firmware
    tools are unreachable until the gateway's hardcoded table knows them."""
    import json as _json
    import live

    def result(text):
        return type("R", (), {"content": [type("C", (), {"text": text})()]})()

    assert live.failure_reason(result(_json.dumps({"error": "Unknown tool: set_gaze"}))) == (
        "Unknown tool: set_gaze"
    )
    assert live.failure_reason(result(_json.dumps({"ok": False}))) is not None
    assert live.failure_reason(result(_json.dumps({"ok": True}))) is None
    assert live.failure_reason(result("not json at all")) is None
    assert live.failure_reason(result("")) is None


def test_calls_the_gateway_cannot_route_are_dropped_not_queued():
    """Once startup has said what is missing and why, queueing calls only to
    have them refused twice a second buries the diagnosis."""
    import live

    effector = live.McpEffector(session=None, loop=None, available={"move_head"})
    effector.set_gaze(1, 2)
    effector.move_head(0.0, 45.0, 60)
    assert effector._queue.qsize() == 1


def test_every_tool_the_effector_can_send_is_declared_as_required_or_optional():
    """So a tool added to the effector without being taught to the gateway is
    caught by the startup check rather than at 3 calls a second."""
    import live

    declared = set(live.REQUIRED_TOOLS) | set(live.OPTIONAL_TOOLS)
    sendable = {
        "set_avatar", "set_feature", "set_gaze", "set_mouth",
        "set_speech", "set_blink", "set_all_leds", "move_head",
    }
    assert sendable == declared


def test_the_stack_rides_out_a_gateway_restart_instead_of_exiting():
    """The gateway restarts for its own upgrades and every time this fleet's
    tools are re-patched into it, and it owns the device connection. Exiting on
    the first refused connection would make a routine restart look like a
    crash -- and with a start limit on the unit, a slow one could leave the
    character stack dead."""
    import asyncio
    import pathlib

    import live

    attempts = []

    async def refuse_then_succeed(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError("gateway is restarting")
        return 0

    original_run_once = live.run_once
    original_backoff = live.CONNECT_BACKOFF_S
    try:
        live.run_once = refuse_then_succeed
        live.CONNECT_BACKOFF_S = (0.0,)
        rc = asyncio.run(live.run("http://127.0.0.1:8767/mcp", pathlib.Path("/nope"), 2))
    finally:
        live.run_once = original_run_once
        live.CONNECT_BACKOFF_S = original_backoff

    assert rc == 0
    assert len(attempts) == 3


def test_a_missing_required_tool_is_not_retried():
    """Retrying cannot fix a configuration error, and spinning on one hides it.
    check_tools raises SystemExit, which must propagate."""
    import asyncio
    import pathlib

    import live

    async def config_error(*args, **kwargs):
        raise SystemExit("gateway is missing move_head")

    original = live.run_once
    try:
        live.run_once = config_error
        try:
            asyncio.run(live.run("x", pathlib.Path("/nope"), 2))
        except SystemExit as exc:
            assert "move_head" in str(exc)
        else:
            raise AssertionError("a config error should not be retried")
    finally:
        live.run_once = original


def test_giving_up_is_bounded_so_a_broken_config_still_surfaces():
    """Infinite retry would turn a permanently broken setup into a process that
    looks healthy forever."""
    import live

    assert live.CONNECT_TIMEOUT_S <= 600.0
    assert live.CONNECT_BACKOFF_S[-1] >= 5.0


def test_a_null_angle_reply_is_not_mistaken_for_a_pose():
    """`{"yaw": null, "pitch": null, "error": ...}` is what the firmware
    documents for a persistent ReadPos failure. Booleans are ints in Python,
    so those have to be rejected too."""
    import json as _json

    import live

    def result(payload):
        return type("R", (), {"content": [type("C", (), {"text": _json.dumps(payload)})()]})()

    import asyncio

    class Session:
        def __init__(self, payload):
            self.payload = payload

        async def call_tool(self, name, args):
            return result(self.payload)

    assert asyncio.run(live.read_head_pose(Session({"yaw": None, "pitch": None}))) is None
    assert asyncio.run(live.read_head_pose(Session({"yaw": True, "pitch": False}))) is None
    assert asyncio.run(live.read_head_pose(Session({}))) is None

    pose = asyncio.run(live.read_head_pose(Session({"yaw": -60, "pitch": 20})))
    assert (pose.yaw, pose.pitch) == (-60.0, 20.0)


def test_a_failed_angle_read_does_not_take_the_process_down():
    import asyncio

    import live

    class Broken:
        async def call_tool(self, name, args):
            raise ConnectionResetError("bus hang")

    assert asyncio.run(live.read_head_pose(Broken())) is None
