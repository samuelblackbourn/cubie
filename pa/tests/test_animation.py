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


# ------------------------------------------------------------- gestures --
#
# Ours rather than M5's, so there is no upstream to be faithful to and the
# tests carry the whole argument. Two of them check things that fail SILENTLY:
# a gesture can be quietly shortened by a clamp, or quietly shallow because it
# was not given the speed to arrive. Neither raises; both just stop looking
# like the gesture.


#: The corners of where the idle system can leave the head when a gesture
#: fires. From idle.py: `_quick_glance` reaches yaw +/-50, and every action's
#: pitch band together spans 25..55. A gesture starts from one of these, NOT
#: from rest -- the first draft of these tests walked from rest and so proved
#: nothing about the case that actually happens.
IDLE_CORNERS = ((50.0, 25.0), (-50.0, 55.0), (50.0, 55.0), (-50.0, 25.0))


def travel_of(sequence, start=None):
    """Per keyframe: (degrees the head must move, seconds it has, dps asked for).

    `start` is the pose the head is in when the sequence begins. Defaults to
    rest; pass an IDLE_CORNERS entry for the case that matters.
    """
    from tracking import REST_PITCH, REST_YAW

    yaw, pitch = start if start is not None else (REST_YAW, REST_PITCH)
    out = []
    for keyframe in sequence:
        next_yaw = units.m5_yaw_to_deg(keyframe.yaw.angle)
        next_pitch = units.m5_pitch_to_deg(keyframe.pitch.angle)
        out.append((
            max(abs(next_yaw - yaw), abs(next_pitch - pitch)),
            keyframe.duration_s,
            units.m5_speed_to_dps(keyframe.yaw.speed),
        ))
        yaw, pitch = next_yaw, next_pitch
    return out


def test_a_gesture_never_asks_the_device_for_a_pose_it_would_reject():
    """Weaker than it looks, and named so nobody mistakes it for the guard.

    `apply_keyframe` goes through the converters, which CLAMP -- so
    `motion.target` is in range by construction and this can only fail if a
    converter breaks. It is worth keeping as exactly that: a check on the
    converters, not on the sequences. The real guard on the sequences is
    `test_no_gesture_is_quietly_shortened_by_the_clamp`, which reads the raw M5
    numbers, because after conversion a clamped value looks perfectly legal."""
    for name, sequence in animation.GESTURES.items():
        c = Chan(RecordingEffector())
        now = 0.0
        for keyframe in sequence:
            animation.apply_keyframe(c, keyframe, now)
            now += keyframe.duration_s
            assert YAW_MIN <= c.motion.target.yaw <= YAW_MAX, name
            assert PITCH_MIN <= c.motion.target.pitch <= PITCH_MAX, name


def test_no_gesture_nods_with_his_eyes_shut():
    """The eyelid is a black square that slides OFF the eye as weight rises, so
    weight 0 is fully SHUT and 100 is open. `firmware/fix-eye-weight.sh` exists
    because the first port had it backwards, and the first draft of these
    gestures made the identical mistake for the identical reason: 0 reads as
    "nothing set" and means "closed".

    Every one of M5's own keyframes uses 100, which is the tell nobody read.
    A nod with his eyes shut is not a subtle failure -- and nothing in the code
    would have said a word about it."""
    for name, sequence in animation.GESTURES.items():
        for i, keyframe in enumerate(sequence):
            for side, feature in (("left", keyframe.left_eye), ("right", keyframe.right_eye)):
                assert feature.weight > 0, (
                    f"{name} keyframe {i} sets the {side} eye weight to "
                    f"{feature.weight}, which is fully shut"
                )


def test_a_gesture_leaves_the_eyes_open():
    """`_finish` sets weight to None, which stops DRIVING the axis -- the device
    keeps the last value it was sent until the firmware's blink machine moves it
    again. So a sequence ending on a squint leaves him squinting."""
    for name, sequence in animation.GESTURES.items():
        assert sequence[-1].left_eye.weight == animation.EYES_OPEN, name
        assert sequence[-1].right_eye.weight == animation.EYES_OPEN, name
        assert sequence[-1].mouth.weight == animation.MOUTH_CLOSED, name


def test_m5s_own_dances_agree_about_which_way_eye_weight_runs():
    """Not our code, and that is the point: their keyframes are the evidence
    for the convention, so if this ever fails the vendored pin moved under us
    and EYES_OPEN needs re-deriving rather than assuming."""
    weights = {kf.left_eye.weight for seq in animation.SEQUENCES.values() for kf in seq}
    assert min(weights) > 0, f"an M5 keyframe closes the eyes: {sorted(weights)}"
    assert animation.EYES_OPEN == max(weights)


def test_no_sequence_is_quietly_shortened_by_the_clamp():
    """`m5_pitch_to_deg` and `m5_yaw_to_deg` CLAMP rather than reject, so a
    keyframe outside the envelope still plays -- it just stops being the
    gesture that was written. The failure is invisible at every level, which is
    why the envelope is asserted rather than trusted to be remembered.

    Checked against the raw M5 numbers, not the converted ones: after
    conversion a clamped value looks perfectly legal."""
    from tracking import PITCH_MAX, PITCH_MIN, REST_PITCH

    pitch_floor = round((PITCH_MIN - REST_PITCH) / units.M5_DEGREES_PER_UNIT)
    pitch_ceiling = round((PITCH_MAX - REST_PITCH) / units.M5_DEGREES_PER_UNIT)
    yaw_limit = round(YAW_MAX / units.M5_DEGREES_PER_UNIT)

    # Every sequence, not just ours. The clamp does not care who wrote the
    # keyframe, and a check that covered only GESTURES left anything added
    # anywhere else unguarded -- which was the state of it a moment ago.
    for name, sequence in {**animation.SEQUENCES, **animation.GESTURES}.items():
        for i, keyframe in enumerate(sequence):
            assert pitch_floor <= keyframe.pitch.angle <= pitch_ceiling, (
                f"{name} keyframe {i} pitch {keyframe.pitch.angle} is outside "
                f"{pitch_floor}..{pitch_ceiling} and would be clamped"
            )
            assert -yaw_limit <= keyframe.yaw.angle <= yaw_limit, (
                f"{name} keyframe {i} yaw {keyframe.yaw.angle} would be clamped"
            )


def test_every_gesture_arrives_from_wherever_idle_motion_left_the_head():
    """The one that caught a real defect. A frame asking for 30 degrees in
    200 ms needs 150 dps; command less and the next keyframe interrupts the
    move part-way, so the gesture is shallower than it reads on the page and
    nothing anywhere reports it.

    Checked from every corner of the idle envelope rather than from rest,
    because that is where a gesture actually starts. All four gestures failed
    this when it was first written: their opening frame had 100-120 ms to cover
    up to 50 degrees, wanting 400-500 dps against a 240 ceiling. A nod's dip is
    the ABSOLUTE pose 'pitch 33', so from a head idle had left at pitch 25 the
    nod did not merely start off-centre -- it inverted into a rise. Hence the
    400 ms settle frame that now opens every sequence.

    M5's PANIC deliberately fails this same check and that is WHY it reads as
    frantic, so this covers gestures only; the exemption is asserted below."""
    for name, sequence in animation.GESTURES.items():
        for start in IDLE_CORNERS:
            for i, (travel, seconds, dps) in enumerate(travel_of(sequence, start)):
                needed = travel / seconds
                assert dps >= needed, (
                    f"{name} from {start}: keyframe {i} must travel "
                    f"{travel:.1f} deg in {seconds * 1000:.0f} ms, needing "
                    f"{needed:.0f} dps, but asks for {dps}"
                )


def test_the_settle_frame_is_what_makes_that_true():
    """Named rather than incidental: if someone shortens SETTLE_MS or slows
    SETTLE_SPEED to make a gesture snappier, the test above is what stops it,
    and this is what explains why. The worst travel is yaw 50 -> 0."""
    worst_travel = max(
        max(abs(0.0 - y), abs(45.0 - p)) for y, p in IDLE_CORNERS
    )
    reach = units.m5_speed_to_dps(animation.SETTLE_SPEED) * (animation.SETTLE_MS / 1000)
    assert reach >= worst_travel, (
        f"the settle reaches {reach:.0f} deg but may need {worst_travel:.0f}"
    )
    for name, sequence in animation.GESTURES.items():
        assert sequence[0] == animation._settle(), f"{name} does not open with a settle"


def test_m5s_panic_is_the_exception_that_proves_that_rule_is_ours():
    """Not a defect in the port -- their sequence is unreachable on purpose, and
    the test above would be wrong to apply here. Asserted so that the exemption
    stays a fact about their dance rather than a sentence in a comment."""
    unreachable = [
        (i, travel, seconds, dps)
        for i, (travel, seconds, dps) in enumerate(travel_of(animation.PANIC))
        if dps < travel / seconds
    ]
    assert unreachable, "PANIC now arrives everywhere; the exemption is stale"


def test_every_gesture_ends_where_it_started():
    """`DanceModifier._finish` releases weight and size and NOTHING else, so
    x, y and rotation stay wherever the last keyframe put them. A gesture that
    ended mid-swing would leave his eyes skewed until the next face change."""
    from tracking import REST_PITCH, REST_YAW

    for name, sequence in animation.GESTURES.items():
        last = sequence[-1]
        assert units.m5_yaw_to_deg(last.yaw.angle) == REST_YAW, name
        assert units.m5_pitch_to_deg(last.pitch.angle) == REST_PITCH, name
        for feature in (last.left_eye, last.right_eye, last.mouth):
            assert (feature.x, feature.y, feature.rotation) == (0, 0, 0), name


def test_a_gesture_is_short_enough_to_be_punctuation():
    """The distinction from a dance is length, and it is the whole reason these
    are a separate registry. M5's shortest dance is 2.6 s; a gesture that ran
    that long would stop being a beat in a conversation."""
    for name, sequence in animation.GESTURES.items():
        assert animation.duration_of(sequence) <= 2.0, (
            f"{name} runs {animation.duration_of(sequence):.1f}s"
        )


def test_the_gestures_are_the_four_that_were_asked_for():
    assert set(animation.GESTURES) == {"nod", "shake", "laugh", "glance"}


def test_no_name_is_both_a_dance_and_a_gesture():
    """`lookup` checks dances first, so a collision would silently shadow the
    gesture and there would be no error to notice."""
    assert not (set(animation.SEQUENCES) & set(animation.GESTURES))


def test_lookup_finds_both_kinds_and_names_everything_when_it_cannot():
    assert animation.lookup("happy") is animation.HAPPY
    assert animation.lookup("nod") is animation.NOD
    try:
        animation.lookup("moonwalk")
    except KeyError as exc:
        assert "nod" in str(exc) and "happy" in str(exc)
    else:
        raise AssertionError("an unknown name must raise, not return silently")


#: The bus-hang bound, in degrees of travel in a single commanded move.
#:
#: Upstream's known-issues list warns the servo bus can hang on a large abrupt
#: reversal, their example being +60 to -60. The deleted `bridge/src/posture.ts`
#: held that as MAX_YAW_TRAVEL = 60, bounding the DELTA rather than the
#: magnitude; with the bridge gone this and `tracking.MAX_STEP_DEG` are the only
#: surviving records, and `MAX_STEP_DEG` is enforced solely inside
#: `step_toward` / `step_to_rest`, which have no production callers. So nothing
#: in the live path bounds a reversal but this.
#:
#: 30 is half of upstream's example: a budget, not a measurement. But the
#: MECHANISM behind it was read out of M5's servo HAL at the pinned commit
#: rather than taken on trust, and it is worse than "the bus can hang":
#:
#:   - **Yaw has no stall protection at all.** `hal_servo.cpp` sets
#:     `enablePwmMode` and never `enableStallProtection`, and the stall check
#:     returns immediately for that axis. A wide fast reversal, or a pose past
#:     the physical range, stalls the servo against its stop with no detection
#:     and no back-off. This is the one path in the whole animation system that
#:     can actually cook hardware.
#:   - **Pitch has stall protection, and tripping it is not free either.**
#:     `handle_stall` permanently SHRINKS that servo's angle limit for the rest
#:     of the session, reset only by `init()`. One gesture that stalls pitch
#:     quietly reduces the range of every gesture afterwards until reboot, and
#:     says so only in a `tagWarn`.
#:
#: Which is why this is asserted for every gesture from every starting corner
#: rather than trusted to good sense: the failure is silent, and on the yaw axis
#: it is mechanical.
MAX_GESTURE_TRAVEL_DEG = 30.0


def test_no_gesture_asks_for_a_reversal_the_servo_bus_might_hang_on():
    """Every gesture, from every starting corner -- not SHAKE by name.

    The first draft bounded `animation.SHAKE` specifically, so a new gesture
    called anything else could have commanded a 120-degree reversal and passed.
    A guard that names one instance of the thing it guards is not a guard."""
    for name, sequence in animation.GESTURES.items():
        for start in (None,) + IDLE_CORNERS:
            # The settle frame is exempt and has to be: reaching rest from the
            # far corner of the idle envelope IS a 50-degree move. It is also
            # not the hazard -- the warning is about an abrupt REVERSAL at
            # speed, and the settle is one move in one direction at 150 dps
            # from wherever the head already was. Everything after it starts
            # from rest, so the budget applies with no exemptions.
            for i, (travel, _, _) in enumerate(travel_of(sequence, start)):
                if i == 0:
                    continue
                assert travel <= MAX_GESTURE_TRAVEL_DEG, (
                    f"{name} keyframe {i} from {start} travels {travel:.0f} deg, "
                    f"over the {MAX_GESTURE_TRAVEL_DEG:.0f} budget"
                )
