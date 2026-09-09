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


def run_for(d, seconds: float, step: float = 0.05) -> float:
    now = 0.0
    while now < seconds:
        d.update(now)
        now += step
    return now


# ------------------------------------------------------------- gestures --


def test_idle_motion_comes_back_when_a_sequence_ends():
    """The defect the gestures exposed. `dance` stood idle motion down and
    NOTHING ever put it back -- it returned only if something later happened to
    set the status to STANDBY, which a conversation does in its `finally`, so a
    dance during a turn recovered and this stayed hidden. A dance while idle
    left him still for good, and a one-second nod would have made the first
    nod the last time he ever looked around."""
    d = fresh()
    d.set_status(driver.STANDBY)
    assert d.idle_motion is not None
    d.gesture("nod")
    assert d.idle_motion is None, "idle should stand down for the gesture"
    run_for(d, 3.0)
    assert d.performance is None
    assert d.idle_motion is not None, "idle motion never came back"


def test_a_sequence_that_ends_while_he_is_talking_does_not_start_idle_motion():
    """Idle motion belongs to STANDBY. Restoring it because a gesture happened
    to finish mid-sentence would put it back exactly where `set_status` had
    just removed it."""
    d = fresh()
    d.set_status(driver.STANDBY)
    d.gesture("nod")
    d.set_status(driver.SPEAKING)
    run_for(d, 3.0)
    assert d.performance is None
    assert d.idle_motion is None


def test_a_status_change_mid_sequence_does_not_put_idle_underneath_it():
    """Two things steering the head, which is what standing idle down avoids in
    the first place."""
    d = fresh()
    d.gesture("glance")
    d.set_status(driver.STANDBY)
    assert d.idle_motion is None, "STANDBY restarted idle under a running gesture"
    run_for(d, 3.0)
    assert d.idle_motion is not None, "and it should be back once the gesture ends"


def test_a_second_gesture_replaces_the_first_rather_than_fighting_it():
    """Two timelines driving the same servos. The newer wins: a gesture reacts
    to something that just happened, so the older one is out of date."""
    d = fresh()
    first = d.gesture("nod")
    second = d.gesture("shake")
    assert first is not second
    assert first not in d.chan.modifiers
    assert second in d.chan.modifiers
    assert sum(isinstance(m, modifiers.DanceModifier) for m in d.chan.modifiers) == 1


def test_every_gesture_can_actually_be_played_by_name():
    """`gesture` goes through `animation.lookup`, so a name in the registry
    that the lookup cannot find would raise here rather than on hardware."""
    import animation

    for name in animation.GESTURES:
        d = fresh()
        assert d.gesture(name) is not None


def test_a_gesture_leaves_the_head_at_rest():
    """The last keyframe's job, asserted through the driver rather than the
    sequence, so the modifier's own teardown is in the picture too.

    Idle motion level 0 on purpose. At level 2 this passes or fails depending
    on when idle motion's next nudge lands, which would make it a test of the
    clock -- the first draft asserted after three seconds and caught idle
    having moved the head 6 degrees off rest, correctly."""
    from tracking import REST_PITCH, REST_YAW

    d = fresh(level=0)
    d.set_status(driver.STANDBY)
    d.gesture("shake")
    run_for(d, 3.0)
    assert d.performance is None, "the gesture should have finished by now"
    assert d.chan.motion.target.yaw == REST_YAW
    assert d.chan.motion.target.pitch == REST_PITCH


def test_a_gesture_gives_blinking_back():
    """A sequence drives eye weight, so blink is suspended for its duration.
    Leaving it off would be a robot that never blinks again after one nod.

    Note what this does NOT prove: `blink_enabled` is the host's flag, and the
    firmware can have blinking enabled while the blink is invisible, because a
    held eye-weight override is applied after it and wins. Re-enabling the flag
    is necessary and not sufficient -- the sufficient half is the face
    re-assert, covered by
    `test_a_finished_gesture_lets_the_device_have_its_face_back`. Both are
    asserted here so the pair cannot drift apart."""
    d = fresh()
    d.chan.face.blink_enabled = True
    d.gesture("laugh")
    run_for(d, 0.2)
    assert d.chan.face.blink_enabled is False
    run_for(d, 3.0)
    assert d.chan.face.blink_enabled is True
    assert "set_avatar" in d.chan.effector.names()[-6:], (
        "the flag came back but the override did not go, so blinking is "
        "enabled and invisible"
    )


# ----------------------------------------------------------------- wake --
def test_waking_adopts_the_real_pose_and_settles_from_it():
    """M5's Servo::init teleports its state to getCurrentAngle() rather than
    driving the head. The settle is then a move from where the head IS, at a
    speed we chose -- not a snap from an assumption."""
    from tracking import Pose

    d = fresh()
    d.wake(0.0, Pose(-60.0, 20.0))
    d.update(0.0)

    moves = d.chan.effector.of("move_head")
    assert len(moves) == 1
    assert moves[0]["yaw"] == 0.0 and moves[0]["pitch"] == 45.0
    assert moves[0]["speed_dps"] == d.WAKE_SPEED_DPS


def test_the_wake_speed_is_slower_than_every_idle_speed():
    """It is the one movement someone watches from cold. A robot that snaps to
    attention on power-up reads as a fault; one that settles reads as waking."""
    import units

    d = fresh()
    assert units.GATEWAY_SPEED_MIN_DPS <= d.WAKE_SPEED_DPS < 60


def test_the_travel_time_is_honest_after_adopting():
    """So idle motion defers instead of firing a competing target into the
    middle of the settle. 60 degrees at 40 dps is 1.5 s."""
    from tracking import Pose

    d = fresh()
    d.wake(0.0, Pose(-60.0, 45.0))
    assert d.chan.motion.is_moving(1.0)
    assert not d.chan.motion.is_moving(2.0)


def test_adopting_a_pose_does_not_command_a_move_to_it():
    """Otherwise syncing to reality would itself be a command, and the head
    would be told to go where it already is."""
    from tracking import Pose

    d = fresh()
    d.chan.adopt_pose(Pose(-60.0, 20.0))
    d.chan.flush()
    assert d.chan.effector.of("move_head") == []


def test_an_unreadable_pose_falls_back_to_rest_rather_than_inventing_one():
    """The firmware documents yaw/pitch null as a real outcome of a persistent
    ReadPos failure, so this path is reached in practice."""
    d = fresh()
    d.wake(0.0, None)
    d.update(0.0)
    moves = d.chan.effector.of("move_head")
    assert moves[-1]["yaw"] == 0.0 and moves[-1]["pitch"] == 45.0


def test_replacing_a_gesture_gives_back_what_it_took():
    """`Chan.remove` drops a modifier and runs no teardown -- which is why
    `_stop_speaking` resets the mouth by hand. Removing a RUNNING sequence was
    impossible until one gesture could replace another, and without
    `abandon()` blink would stay suspended and the eye weight stay pinned where
    the interrupted keyframe put it: one nod cut off by another and he never
    blinks again."""
    d = fresh()
    d.chan.face.blink_enabled = True
    d.gesture("laugh")
    run_for(d, 0.6)          # far enough in for the laugh to be driving weight
    assert d.chan.face.blink_enabled is False
    assert d.chan.face.left_eye.weight is not None

    d.gesture("nod")         # interrupts it
    assert d.chan.face.blink_enabled is True, "blink stayed suspended"
    assert d.chan.face.left_eye.weight is None, "eye weight stayed pinned"


def test_an_interrupted_gesture_is_not_left_in_the_pool():
    d = fresh()
    d.gesture("laugh")
    run_for(d, 0.3)
    d.gesture("shake")
    assert sum(isinstance(m, modifiers.DanceModifier) for m in d.chan.modifiers) == 1


def test_a_gesture_commands_rest_first_however_far_off_it_starts():
    """What the settle actually buys, asserted at the level that can see it.

    Two earlier versions of this test were vacuous and it is worth recording
    why, because the second one LOOKED rigorous. The first asserted the head
    ended at rest -- true with the settle deleted, since the last keyframe
    commands rest either way. The second watched the deepest pitch reached and
    asserted the nod dipped below rest -- also true with the settle deleted,
    because it reads `motion.target`, which jumps straight to whatever the
    keyframe says.

    The host does not model servo travel at all: `move_with_speed` sets `pose`
    and `target` together and `moving_until` is only an estimate. So no
    driver-level test can observe the head ARRIVING anywhere, and any test
    written as though it can is measuring nothing.

    What is observable is the first pose the gesture commands. With the settle
    that is rest; without it, it is the nod's dip -- which from a head parked
    low is a rise, and is the whole defect. That distinction this can see."""
    from tracking import PITCH_MIN, REST_PITCH, REST_YAW, YAW_MAX

    d = fresh(level=0)
    d.chan.motion.move_with_speed(YAW_MAX, PITCH_MIN, 240, 0.0)
    assert d.chan.motion.target.pitch == PITCH_MIN

    d.gesture("nod")
    d.update(0.0)          # one tick: the first keyframe and no more
    assert d.chan.motion.target.yaw == REST_YAW, "the gesture did not centre first"
    assert d.chan.motion.target.pitch == REST_PITCH, (
        f"the gesture's first command was pitch {d.chan.motion.target.pitch}, "
        f"not rest -- from a head at {PITCH_MIN} that is a rise, not a nod"
    )


def test_a_finished_gesture_lets_the_device_have_its_face_back():
    """`weight = None` stops US driving the axis; it does not tell the BOARD to
    let go. The board holds each override until an expression change drops
    them, and the flush only sends `set_avatar` when the name changed -- so a
    gesture ending on the same face left the eyelids pinned where its last
    keyframe put them, flattening every expression to one eyelid position until
    the face happened to change. That is the defect `fix-eye-weight.sh` already
    fixed once."""
    d = fresh()
    d.chan.flush()
    d.gesture("laugh")
    run_for(d, 3.0)
    names = d.chan.effector.names()
    assert "set_avatar" in names[-6:], (
        "the face was not re-asserted, so the overrides are still live "
        f"on the device (last calls: {names[-6:]})"
    )


def test_a_misspelled_sequence_name_does_not_freeze_him():
    """The first version stopped idle motion and THEN looked the name up, so a
    typo left him still: idle gone, `performance` still None, so update's
    recovery branch never fired and nothing brought it back. A frozen robot
    from a misspelling, which is the exact defect `_perform` exists to
    prevent."""
    d = fresh()
    d.set_status(driver.STANDBY)
    assert d.idle_motion is not None
    try:
        d.gesture("noddd")
    except KeyError:
        pass
    else:
        raise AssertionError("an unknown name must raise")
    assert d.idle_motion is not None, "idle motion was stood down for a gesture that never ran"
    assert d.performance is None


def test_a_sequence_ending_while_he_dozes_does_not_wake_the_head_up():
    """M5's sleepy stops idle motion deliberately -- he is dozing, not idling.
    A gesture finishing during it would put the looking-around back and undo
    that, which reads as a robot that cannot settle."""
    d = fresh()
    d.set_status(driver.STANDBY)
    d.gesture("nod")
    d.set_emotion("sleepy")
    assert d.sleeping is True
    run_for(d, 3.0)
    assert d.performance is None
    assert d.idle_motion is None, "idle motion came back on top of a doze"
