"""M5's keyframe engine and their four dances, ported.

`animation/animation.h` plus `modifiers/dance.h`. The engine is small and the
shape of it is the interesting part:

    Timeline is a STEP sequencer, not an interpolator.

Each keyframe is applied whole, then the timeline waits `duration_ms` and
applies the next one. Nothing is tweened. The smoothness comes from the servo's
own speed ramp -- `moveWithSpeed(angle, speed)` -- so the head glides while the
face snaps.

That is exactly why this ports cleanly to a robot at the end of a LAN. Six
keyframes over four seconds is six sets of calls, not sixty frames a second,
and the gateway's `move_head` carries the same speed parameter M5's servo does.
A tweening engine would have needed a frame rate we cannot afford.

--- An upstream bug, fixed here ---

`FeatureKeyframe`'s four-argument constructor -- the only one any dance uses --
does not initialise its `size` member:

    FeatureKeyframe(int x = 0, int y = 0, int rotation = 0, int weight = 0)
        : position(x, y), rotation(rotation), weight(weight) {}   // size: none

and `Keyframe::apply()` then calls `feat.setSize(kf.size)` unconditionally. So
every keyframe of every M5 dance applies an INDETERMINATE eye and mouth size.
`Feature::setSize` clamps to -100..100 so it cannot crash, which is why this
has survived: it shows up as eye size varying between builds rather than as a
fault. Here `size` defaults to 0, M5's documented "normal".

--- What the weights mean for us ---

The dances drive eye and mouth weight, which on this device belong to the
firmware's blink state machine and its audio-driven lip-sync. `DanceModifier`
therefore disables blink for the duration and restores it -- see modifiers.py.
Mouth weight is sent as the nearest `set_mouth` shape, which is lossy and
deliberately so: a dance's signature is its sequence, not one exact aperture.
"""

from __future__ import annotations

from dataclasses import dataclass

import units
from chan import LEFT_EYE, MOUTH, RIGHT_EYE


@dataclass(frozen=True)
class FeatureKeyframe:
    """One feature's target. M5's argument order, kept.

    `size` defaults to 0 rather than being left indeterminate -- see the module
    docstring for the upstream bug this fixes.
    """

    x: int = 0
    y: int = 0
    rotation: int = 0
    weight: int = 0
    size: int = 0


@dataclass(frozen=True)
class ServoKeyframe:
    """A servo target in M5's units: tenths of a degree, and their 0-1000 speed."""

    angle: int = 0
    speed: int = 0


@dataclass(frozen=True)
class Keyframe:
    """One step of an animation, applied whole.

    `duration_s` is M5's `durationMs` in seconds, because every clock in this
    package is seconds.
    """

    left_eye: FeatureKeyframe
    right_eye: FeatureKeyframe
    mouth: FeatureKeyframe
    yaw: ServoKeyframe
    pitch: ServoKeyframe
    duration_s: float


def _kf(
    eye: tuple[int, int, int, int],
    mouth: tuple[int, int, int, int],
    yaw: tuple[int, int],
    pitch: tuple[int, int],
    duration_ms: int,
) -> Keyframe:
    """Transcribe one M5 keyframe.

    Both eyes take the same tuple: every keyframe of all four M5 sequences sets
    leftEye and rightEye identically, which is also why `set_gaze` exists.
    """
    return Keyframe(
        left_eye=FeatureKeyframe(*eye),
        right_eye=FeatureKeyframe(*eye),
        mouth=FeatureKeyframe(*mouth),
        yaw=ServoKeyframe(*yaw),
        pitch=ServoKeyframe(*pitch),
        duration_s=duration_ms / 1000.0,
    )


#: Happy: swaying left and right, eyes squinting, mouth open.
HAPPY = (
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (0, 200), (0, 200), 500),
    _kf((-10, 0, 0, 50), (0, 0, 0, 50), (300, 200), (-100, 200), 800),
    _kf((10, 0, 0, 50), (0, 0, 0, 50), (-300, 200), (-100, 200), 800),
    _kf((-10, 0, 0, 50), (0, 0, 0, 50), (300, 200), (-100, 200), 800),
    _kf((10, 0, 0, 50), (0, 0, 0, 50), (-300, 200), (-100, 200), 800),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (0, 200), (0, 200), 500),
)

#: Robot: stiff, jerky, sharp angles, eyes fixed open.
ROBOT = (
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (0, 500), (0, 500), 500),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (450, 800), (0, 800), 400),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (450, 800), (200, 800), 400),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (-450, 800), (200, 800), 600),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (-450, 800), (0, 800), 400),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (0, 800), (0, 800), 400),
)

#: Panic: fast shaking, eyes wide, mouth wide open.
PANIC = (
    _kf((0, 0, 0, 100), (0, 0, 0, 100), (0, 1000), (0, 1000), 100),
    _kf((0, 0, 0, 100), (0, 0, 0, 100), (200, 1000), (100, 1000), 100),
    _kf((0, 0, 0, 100), (0, 0, 0, 100), (-200, 1000), (-100, 1000), 100),
    _kf((0, 0, 0, 100), (0, 0, 0, 100), (200, 1000), (100, 1000), 100),
    _kf((0, 0, 0, 100), (0, 0, 0, 100), (-200, 1000), (-100, 1000), 100),
    _kf((0, 0, 0, 100), (0, 0, 0, 100), (200, 1000), (100, 1000), 100),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (0, 200), (0, 200), 500),
)

#: Look Around: a slow scan of the room.
LOOK_AROUND = (
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (0, 200), (0, 200), 1000),
    _kf((-20, 0, 0, 100), (0, 0, 0, 20), (600, 150), (0, 150), 2000),
    _kf((20, 0, 0, 100), (0, 0, 0, 20), (-600, 150), (0, 150), 2000),
    _kf((0, -20, 0, 100), (0, 0, 0, 40), (0, 150), (-300, 150), 1500),
    _kf((0, 0, 0, 100), (0, 0, 0, 0), (0, 200), (0, 200), 1000),
)

#: Every dance M5 ships, by the name a caller would ask for.
#:
#: Exactly M5's four, and a test asserts that. Ours live in `GESTURES` rather
#: than being added here: this dict's whole claim is "what the port carried
#: over", and a claim you keep appending to stops being checkable.
SEQUENCES = {
    "happy": HAPPY,
    "robot": ROBOT,
    "panic": PANIC,
    "look-around": LOOK_AROUND,
}


# --------------------------------------------------------------- gestures --
#
# Ours, not M5's. A dance is a performance you ask for; a gesture is
# punctuation -- a beat long, in the middle of something else. They share the
# keyframe machinery and nothing else.
#
# --- The envelope, derived rather than guessed ---
#
# `units.m5_pitch_to_deg` is `clamp(REST_PITCH + units/10, 5, 85)` with
# REST_PITCH 45, so pitch offsets stay honest between -400 and +400. Yaw is
# `clamp(units/10, -90, 90)`, so -900..+900. Past those the clamp does not
# reject the pose, it QUIETLY SHORTENS IT -- the gesture still plays and just
# stops looking like itself. Nothing below uses more than 15 of the 40 degrees
# of pitch offset available, or 15 of the 90 degrees of yaw -- glance is the
# widest at 15 degrees each way -- because a nod does not need 40.
#
# Higher pitch looks UP (M5's own "raise head" adds to pitch; our rest is 45 of
# 5..85), so a nod's dip is NEGATIVE.
#
# --- Speed has to be fast enough to arrive ---
#
# `move_with_speed` sends degrees per second and the device travels at that
# rate. A frame that asks for 30 degrees in 200 ms needs 150 dps; command less
# and the next keyframe interrupts the move part-way, so the gesture is
# silently shallower than it reads on the page. Every gesture here commands
# enough speed to complete its own travel, and a test checks the arithmetic.
#
# M5's PANIC deliberately fails that check -- its 40-degree reversals in 100 ms
# would need 400 dps against a 240 dps ceiling -- and that is WHY it reads as
# frantic: the head never arrives anywhere. So the test covers gestures only,
# and the exemption is the point rather than an oversight.
#
# --- Every gesture opens with a settle, and it is not decoration ---
#
# These keyframes are ABSOLUTE poses, and a gesture fires while idle motion has
# the head wherever it last looked. A nod's dip is "pitch 33"; from a head
# already lower than that, it is a RISE. The gesture does not merely start
# off-centre, it inverts.
#
# So the first keyframe of each sequence goes to rest, with the time and speed
# to get there from ANY pose the servos allow -- 90 degrees of yaw, since that
# is the worst case, needing 200 dps to cover it in 500 ms.
#
# The envelope is the SERVO RANGE, not some smaller idle envelope, and getting
# that wrong is how the first version of this came to be 30 degrees short. It
# claimed "idle.py reaches yaw +/-50 and pitch 25..55", which is what three of
# idle's four actions do in isolation -- but `_small_observation` is RELATIVE
# (`current.yaw + uniform(-15, 15)`), so the walk compounds and is bounded only
# by the clamp. Simulated: 300 seeds x 500 actions reaches |yaw| 87.5 and pitch
# 6.1..80.5. A settle sized for +/-50 arrives 37 degrees short of that, and a
# nod from pitch 6 still inverts -- the exact bug the settle was added to fix,
# surviving in the tail because the envelope was asserted rather than measured.
#
# The cost is 500 ms of settle before the gesture proper, which is also roughly
# what a person does before nodding. `_settle` builds it so the number lives in
# one place, and a test walks every gesture from the corners of the servo range.
#
# The settle is exempt from the reversal budget, and has to be: reaching centre
# from a stop IS a 90-degree move. It is also not the hazard -- that is about
# abrupt REVERSALS and about driving INTO a stop, and the settle is one move in
# one direction, away from the stops.

#: How long the opening move gets, and how fast, to reach rest from anywhere
#: the idle system can leave the head. See the note above.
SETTLE_MS = 500
SETTLE_SPEED = 800

#: Eye weight for OPEN, and it is 100 rather than 0 -- the eyelid is a black
#: square that slides OFF the eye as weight rises, so 0 covers it completely.
#:
#: `firmware/fix-eye-weight.sh` exists because the first port had this
#: backwards and rendered both eyes shut, and the first draft of these gestures
#: made the same mistake for the same reason: 0 reads as "nothing set" and is
#: in fact "fully closed". No keyframe of any M5 dance goes BELOW 50, and
#: three of their four hold 100 throughout -- happy's squint at 50 is the only
#: departure, and it is deliberate. That was the tell. A nod with his eyes shut
#: is not a subtle failure, but nothing in the code would have said so.
EYES_OPEN = 100

#: Mouth weight for closed, where 0 genuinely does mean shut -- the mouth
#: opens as weight rises. Named only so the two zeros in a keyframe are not
#: mistaken for each other.
MOUTH_CLOSED = 0


def _settle() -> Keyframe:
    """The opening keyframe: get to rest, from wherever we actually are."""
    return _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED),
               (0, SETTLE_SPEED), (0, SETTLE_SPEED), SETTLE_MS)


#: Yes. Two dips of 12 degrees, because one reads as a twitch and three as
#: eagerness. 240 ms down and 260 ms back is the cadence of an actual nod --
#: slightly slower coming up than going down.
NOD = (
    _settle(),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (0, 600), (-120, 600), 240),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (0, 600), (0, 600), 260),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (0, 600), (-120, 600), 240),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (0, 600), (0, 600), 260),
)

#: No. Yaw only, 15 degrees each way, three crossings and back to centre. The
#: widest single move is 30 degrees, a quarter of upstream's +60-to-60 example
#: of a reversal that can hang the servo bus (their figure is an endpoint, so
#: the reversal is 120 degrees, not 60) -- and yaw is the
#: axis with NO stall protection in M5's HAL, so a wide fast reversal there
#: stalls against the mechanical stop undetected. See `MAX_GESTURE_TRAVEL_DEG`
#: in the tests for the mechanism; of every number in this file, this is the
#: one with a hardware consequence rather than an aesthetic one.
SHAKE = (
    _settle(),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (-150, 700), (0, 700), 200),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (150, 700), (0, 700), 220),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (-150, 700), (0, 700), 220),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (0, 700), (0, 700), 200),
)

#: Laughing. A bounce rather than a sway: small, fast, and on both axes at
#: once, with the mouth open and the eyes squeezed up. M5 has no laugh -- their
#: HAPPY is a slow sway of the whole body -- so this is built from their idiom
#: rather than ported from their code.
#:
#: It does not make a sound. Piper cannot fake a laugh and "ha. ha." read aloud
#: is not one, so this is the movement and the face; the noise is an open
#: question in PERSONALITY.md.
LAUGH = (
    _settle(),
    _kf((0, -10, 0, 60), (0, 0, 0, 100), (60, 800), (80, 800), 150),
    _kf((0, -10, 0, 60), (0, 0, 0, 80), (-60, 800), (-40, 800), 150),
    _kf((0, -10, 0, 60), (0, 0, 0, 100), (60, 800), (80, 800), 150),
    _kf((0, -10, 0, 60), (0, 0, 0, 80), (-60, 800), (-40, 800), 150),
    _kf((0, 0, 0, 60), (0, 0, 0, 100), (0, 800), (40, 800), 200),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (0, 400), (0, 400), 300),
)

#: Noticing something. This is what `attention` wanted and could not have while
#: the office's mood was a held pose: a person's eye is caught by MOVEMENT, so
#: he looks up and off to one side and then comes back, rather than sitting
#: there with his chin up.
#:
#: Slower than a nod on purpose -- 400 ms out, 500 ms back. A quick version of
#: this reads as a flinch.
GLANCE = (
    _settle(),
    _kf((-20, -20, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (110, 300), (150, 300), 400),
    _kf((-20, -20, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (110, 300), (150, 300), 500),
    _kf((0, 0, 0, EYES_OPEN), (0, 0, 0, MOUTH_CLOSED), (0, 300), (0, 300), 500),
)

#: Ours, by the name a caller asks for.
GESTURES = {
    "nod": NOD,
    "shake": SHAKE,
    "laugh": LAUGH,
    "glance": GLANCE,
}


def lookup(name: str) -> tuple[Keyframe, ...]:
    """A dance or a gesture, by name. Raises rather than doing nothing quietly.

    One entry point over both dicts, so a caller does not have to know which
    kind a name is and there is one error message listing everything available.
    A name in both would silently shadow, so a test forbids it.
    """
    if name in SEQUENCES:
        return SEQUENCES[name]
    if name in GESTURES:
        return GESTURES[name]
    raise KeyError(
        f"unknown sequence {name!r}; "
        f"dances {sorted(SEQUENCES)}, gestures {sorted(GESTURES)}"
    )


def duration_of(sequence: tuple[Keyframe, ...]) -> float:
    """Total seconds a sequence takes. Used to size a blink suspension."""
    return sum(kf.duration_s for kf in sequence)


class Timeline:
    """M5's `Timeline`, verbatim in behaviour.

    Deliberately NOT catching up when a tick arrives late: M5 advances by one
    keyframe per update and resets its clock to `now`, so a late tick stretches
    that keyframe rather than skipping ahead. A dance that dropped steps to
    stay on schedule would lose exactly the poses that make it recognisable.
    """

    def __init__(self, sequence: tuple[Keyframe, ...], loop: bool = False) -> None:
        self._sequence = sequence
        self._loop = loop
        self._index = 0
        self._started_at = 0.0
        self._playing = False
        self._finished = False
        self._paused_elapsed = 0.0
        self._pending: Keyframe | None = None

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def index(self) -> int:
        return self._index

    def start(self, now: float) -> None:
        if not self._sequence:
            self._finished = True
            return
        self._index = 0
        self._playing = True
        self._finished = False
        self._started_at = now
        self._pending = self._sequence[0]

    def stop(self) -> None:
        self._playing = False
        self._index = 0
        self._pending = None

    def pause(self, now: float) -> None:
        if self._playing:
            self._playing = False
            self._paused_elapsed = now - self._started_at

    def resume(self, now: float) -> None:
        if not self._playing and not self._finished:
            self._playing = True
            self._started_at = now - self._paused_elapsed

    def update(self, now: float) -> Keyframe | None:
        """Return the keyframe to apply this tick, or None.

        Returning it rather than applying it keeps this class free of any
        knowledge of the robot -- the caller decides what a keyframe means.
        """
        if self._pending is not None:
            keyframe, self._pending = self._pending, None
            return keyframe
        if not self._playing or not self._sequence:
            return None
        if now - self._started_at < self._sequence[self._index].duration_s:
            return None
        self._index += 1
        if self._index >= len(self._sequence):
            if not self._loop:
                self._playing = False
                self._finished = True
                return None
            self._index = 0
        self._started_at = now
        return self._sequence[self._index]


def apply_keyframe(chan, keyframe: Keyframe, now: float) -> None:
    """M5's `Keyframe::apply()`, against our state rather than their objects.

    The neon-light fields of their `Keyframe` are dropped: none of the four
    sequences sets them (they all use the six-argument constructor), and this
    board's LEDs are a separate tool rather than an avatar element.
    """
    for name, kf in (
        (LEFT_EYE, keyframe.left_eye),
        (RIGHT_EYE, keyframe.right_eye),
        (MOUTH, keyframe.mouth),
    ):
        feature = chan.face.feature(name)
        feature.x = kf.x
        feature.y = kf.y
        feature.rotation = kf.rotation
        feature.weight = kf.weight
        feature.size = kf.size

    # Both servos come from one keyframe, so one move rather than two: our
    # `move_head` takes a pose, where M5 drives each servo independently.
    # The speeds in every M5 sequence are equal across the two axes, so
    # nothing is lost -- asserted in the tests.
    chan.motion.move_with_speed(
        units.m5_yaw_to_deg(keyframe.yaw.angle),
        units.m5_pitch_to_deg(keyframe.pitch.angle),
        units.m5_speed_to_dps(max(keyframe.yaw.speed, keyframe.pitch.speed)),
        now,
    )
