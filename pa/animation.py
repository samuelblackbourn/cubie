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

from dataclasses import dataclass, field

import units
from chan import EYES, LEFT_EYE, MOUTH, RIGHT_EYE


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
SEQUENCES = {
    "happy": HAPPY,
    "robot": ROBOT,
    "panic": PANIC,
    "look-around": LOOK_AROUND,
}


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
