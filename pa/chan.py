"""The modifier stack: M5's `StackChan` driver, ported.

M5's architecture is worth copying exactly, because it is what makes a dozen
small behaviours coexist without fighting. Three pieces (`stackchan.h`,
`modifiable.h`):

  - a **modifier** is a small object with one method, `update(chan)`, called
    every tick. It owns its own timing and asks to be destroyed when done.
  - a **pool** of them runs in order each tick, then reaps the finished ones.
  - the thing they modify is an in-memory **avatar and motion state**, not the
    hardware. A separate step pushes that state out.

That last point is the whole design. M5 gets it for free because their avatar
IS memory and LVGL draws it. We have a robot at the end of a LAN, so it has to
be deliberate:

  - **Modifiers never call the gateway.** They mutate `FaceState` /
    `MotionState`. So every ported behaviour is pure, seeded and testable with
    no robot and no network -- the same split as `tracking.py`, `idle.py` and
    `posture.ts`.
  - **`flush()` diffs and sends only what changed.** Breath updates the eye
    offset every 600 ms and blink every 200 ms; sending every axis every tick
    would be a flood, and sending nothing would be a robot that never moves.
    A diff is the only version that is both.

--- What the host can and cannot reach ---

Deliberately recorded here rather than discovered per modifier. Of M5's four
feature axes, all four are reachable since `set_feature` landed. What is NOT
reachable from the host, and therefore what the ports below cannot do:

  - **Decorators** (heart, dizzy, sweat, angry). Only `embarrassed`'s blush is
    wired, and it is wired in firmware to the face, not exposed as a tool. So
    head-pet loses its hearts and the tilt reaction loses its dizzy spiral.
  - **Element visibility.** `setVisible(false)` on the eyes, which the tilt
    reaction uses to replace them with the dizzy overlay.
  - **The device's IMU.** The gateway surfaces no shake event at all -- the
    only "IMU" in its tool surface is a host-fed pose stream. So the tilt
    reaction is ported but DORMANT: nothing can currently trigger it.

--- Two axes this device owns better than the host does ---

`SpeakingModifier` and `BlinkModifier` both drive weights, and on this hardware
the firmware already drives those from closer to the truth:

  - **Lip-sync** is firmware-driven off the `tts.start` transition, so it is
    synchronised to the audio rather than to a 180 ms host timer.
  - **Blink** is a firmware state machine, immune to LAN jitter.

So the ports below leave those alone by default and take the half M5's own
xiaozhi integration turned off instead -- the subtle head motion while speaking.
See `modifiers.py` for what that means per modifier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from tracking import REST_PITCH, REST_YAW, Pose

logger = logging.getLogger(__name__)

#: Our six faces, in the board's own index order -- which is also the order
#: `avatar_live.h` documents and `face-check.py` renders.
FACES = ("idle", "happy", "thinking", "sad", "surprised", "embarrassed")

#: The mouth shapes `set_mouth` accepts, and the weight each maps to in
#: firmware (`kMouthWeight` in RenderLiveAvatarLocked). Ported animations think
#: in M5 weights, so this is how a weight becomes a call we can actually make.
MOUTH_SHAPE_WEIGHTS = {"closed": 0, "half": 45, "open": 100, "e": 70, "u": 35}

#: The feature names `set_feature` accepts.
LEFT_EYE = "left_eye"
RIGHT_EYE = "right_eye"
MOUTH = "mouth"
EYES = "eyes"


def nearest_mouth_shape(weight: int) -> str:
    """The `set_mouth` shape closest to an M5 mouth weight.

    Needed because M5's animations set a mouth weight directly and our host
    surface names shapes. The mapping is lossy by construction and that is
    fine: the dance signature is the sequence, not one exact aperture.
    """
    return min(MOUTH_SHAPE_WEIGHTS, key=lambda s: abs(MOUTH_SHAPE_WEIGHTS[s] - weight))


@dataclass
class FeatureState:
    """One feature's four M5 axes.

    `weight` and `size` are None when we are not driving them, which is the
    default and which matters: `set_feature` leaves an axis to whatever already
    owns it unless asked, and blink owns eye weight while lip-sync owns the
    mouth's. A modifier that set them to 0 rather than None would be quietly
    fighting the firmware.
    """

    x: int = 0
    y: int = 0
    rotation: int = 0
    weight: int | None = None
    size: int | None = None


@dataclass
class FaceState:
    """Everything about the face the host can address."""

    face: str = "idle"
    left_eye: FeatureState = field(default_factory=FeatureState)
    right_eye: FeatureState = field(default_factory=FeatureState)
    mouth: FeatureState = field(default_factory=FeatureState)
    speech: str = ""
    blink_enabled: bool = True
    #: The status ring, as (r, g, b). M5 drives this from SetStatus -- green
    #: while listening, blue while speaking, dark while idle -- and it is the
    #: only part of their character stack that is not on the screen.
    leds: tuple[int, int, int] = (0, 0, 0)

    def feature(self, name: str) -> FeatureState:
        if name == LEFT_EYE:
            return self.left_eye
        if name == RIGHT_EYE:
            return self.right_eye
        if name == MOUTH:
            return self.mouth
        raise KeyError(name)

    def set_gaze(self, x: int, y: int) -> None:
        """Both eyes together. Every animation M5 ships moves them identically."""
        self.left_eye.x = self.right_eye.x = x
        self.left_eye.y = self.right_eye.y = y


@dataclass
class MotionState:
    """Where the head is going, and how fast.

    `moving_until` is how `is_moving()` is answered without asking the robot.
    M5 reads a live servo flag; a round trip per idle tick to learn the same
    thing would cost more than it tells us, so the travel time is estimated
    from the distance and the speed we asked for. It is an estimate and is
    named one -- it can only be wrong by being slightly early or late, and both
    are handled by M5's own "defer 500 ms and retry" rule.
    """

    pose: Pose = field(default_factory=lambda: Pose(REST_YAW, REST_PITCH))
    target: Pose = field(default_factory=lambda: Pose(REST_YAW, REST_PITCH))
    speed_dps: int = 60
    moving_until: float = 0.0
    locked: bool = False

    def is_moving(self, now: float) -> bool:
        return now < self.moving_until

    def adopt(self, pose: Pose) -> None:
        """Believe the head is HERE, without commanding it to move.

        M5's `Servo::init()` does exactly this and it is the better half of
        their boot sequence:

            _angle_anim.teleport(getCurrentAngle());

        They read the real angle and sync their state to it rather than
        driving the head somewhere. Their own `servo.h` names the reason --
        a mismatch between assumed and actual "may cause a snap".

        It matters here because three modifiers work RELATIVE to this pose:
        idle's small-observation adds an offset to it, speaking baselines on
        it, and head-pet records it to restore to. Starting from an assumption
        makes all three compute from the wrong place until the first absolute
        move happens to correct it.
        """
        self.pose = pose
        self.target = pose
        self.moving_until = 0.0

    def move_with_speed(self, yaw: float, pitch: float, speed_dps: int, now: float) -> None:
        """Ask for a pose. Ignored while motion is locked, as M5 has it."""
        if self.locked:
            return
        distance = max(abs(yaw - self.target.yaw), abs(pitch - self.target.pitch))
        self.target = Pose(yaw, pitch)
        self.speed_dps = max(1, speed_dps)
        self.moving_until = now + distance / self.speed_dps
        # The pose is assumed reached: nothing reads it mid-travel except the
        # relative idle actions, which want the commanded pose anyway -- M5's
        # own small-observation action reads getCurrentAngles() for exactly
        # that purpose and tolerates being a little behind.
        self.pose = self.target


class Effector(Protocol):
    """The robot, as the modifier stack needs it.

    A protocol so the whole stack runs in tests against a recorder. The real
    one speaks MCP to the gateway; nothing above this line knows that.
    """

    def set_avatar(self, face: str) -> None: ...
    def set_feature(
        self,
        feature: str,
        *,
        x: int | None = None,
        y: int | None = None,
        rotation: int | None = None,
        weight: int | None = None,
        size: int | None = None,
    ) -> None: ...
    def set_gaze(self, x: int, y: int) -> None: ...
    def set_mouth(self, shape: str) -> None: ...
    def set_speech(self, text: str) -> None: ...
    def set_blink(self, enabled: bool) -> None: ...
    def set_all_leds(self, r: int, g: int, b: int) -> None: ...
    def move_head(self, yaw: float, pitch: float, speed_dps: int) -> None: ...


class RecordingEffector:
    """An effector that records instead of moving. The test double, and the
    thing that makes every behaviour below assertable without hardware."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, **kwargs) -> None:
        self.calls.append((name, kwargs))

    def set_avatar(self, face: str) -> None:
        self._record("set_avatar", face=face)

    def set_feature(self, feature: str, **kwargs) -> None:
        self._record("set_feature", feature=feature, **kwargs)

    def set_gaze(self, x: int, y: int) -> None:
        self._record("set_gaze", x=x, y=y)

    def set_mouth(self, shape: str) -> None:
        self._record("set_mouth", shape=shape)

    def set_speech(self, text: str) -> None:
        self._record("set_speech", text=text)

    def set_blink(self, enabled: bool) -> None:
        self._record("set_blink", enabled=enabled)

    def set_all_leds(self, r: int, g: int, b: int) -> None:
        self._record("set_all_leds", r=r, g=g, b=b)

    def move_head(self, yaw: float, pitch: float, speed_dps: int) -> None:
        self._record("move_head", yaw=yaw, pitch=pitch, speed_dps=speed_dps)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def of(self, name: str) -> list[dict]:
        return [kwargs for n, kwargs in self.calls if n == name]


class Modifier:
    """M5's `Modifier`: one `update` per tick, and it asks to be destroyed.

    Named `update` rather than M5's `_update` because the leading underscore in
    C++ was signalling "called by the pool, not by you", and in Python that
    reads as private-by-convention on a method the pool must call.
    """

    #: Set by `request_destroy`; the pool reaps it after the tick.
    done: bool = False

    #: Name used in logs and in tests. Defaults to the class name.
    @property
    def name(self) -> str:
        return type(self).__name__

    def request_destroy(self) -> None:
        self.done = True

    def update(self, chan: "Chan", now: float) -> None:
        """Called every tick. `now` is a monotonic clock in SECONDS.

        M5 works in integer milliseconds off `GetHAL().millis()`. Seconds as a
        float is Python's natural clock (`time.monotonic`), and carrying
        milliseconds would mean converting at every call site. Every interval
        below is therefore M5's number divided by 1000, and named as such.
        """


class Chan:
    """M5's `StackChan`: the modifier pool, the state, and the flush.

    `update()` is their `update()` -- run every modifier, reap the finished --
    plus the diff-and-send step their LVGL renderer gave them for free.
    """

    def __init__(self, effector: Effector) -> None:
        self.effector = effector
        self.face = FaceState()
        self.motion = MotionState()
        self._modifiers: list[Modifier] = []
        # What the robot was last told. The diff is against this, not against
        # the state, so a modifier that sets a value back to what it already
        # was costs nothing.
        #
        # Every scalar starts as None, meaning "we have not told it anything".
        # Starting them at the same defaults as `face` would mean the first
        # flush sent nothing at all -- including the blink assertion, which is
        # runtime state on the device and genuinely unknown until we set it.
        self._sent_face = FaceState()
        self._sent_face_name: str | None = None
        self._sent_blink: bool | None = None
        self._sent_speech: str | None = None
        self._sent_leds: tuple[int, int, int] | None = None
        self._sent_pose: Pose | None = None
        self._sent_speed: int | None = None

    def adopt_pose(self, pose: Pose) -> None:
        """Adopt the head's real pose, and record it as already known.

        Both halves matter. Without the state change the relative modifiers
        compute from an assumption; without marking it sent, the very next
        flush would command the head to where it already is.
        """
        self.motion.adopt(pose)
        self._sent_pose = pose
        self._sent_speed = self.motion.speed_dps

    # ---------------------------------------------------------- the pool --
    def add(self, modifier: Modifier) -> Modifier:
        """Add a modifier. Returns it, so callers can keep a handle -- M5
        returns an id into a pool; a reference is the Python equivalent and
        cannot go stale."""
        self._modifiers.append(modifier)
        return modifier

    def remove(self, modifier: Modifier | None) -> bool:
        if modifier is None or modifier not in self._modifiers:
            return False
        self._modifiers.remove(modifier)
        return True

    def clear(self) -> None:
        self._modifiers.clear()

    @property
    def modifiers(self) -> tuple[Modifier, ...]:
        return tuple(self._modifiers)

    def has(self, cls: type) -> bool:
        return any(isinstance(m, cls) for m in self._modifiers)

    # --------------------------------------------------------- the update --
    def update(self, now: float) -> None:
        """One tick: every modifier, then reap, then send what changed.

        Iterates a copy: M5's pool tolerates a modifier adding or removing
        another mid-iteration (head-pet does exactly that), and a live list
        would not.
        """
        for modifier in list(self._modifiers):
            if modifier.done:
                continue
            modifier.update(self, now)
        self._modifiers = [m for m in self._modifiers if not m.done]
        self.flush()

    # ---------------------------------------------------------- the flush --
    def flush(self) -> None:
        """Send only what changed since the last flush."""
        face = self.face
        sent = self._sent_face

        # Face first: a face change clears the firmware's feature overrides, so
        # anything sent before it would be discarded. This ordering is not
        # cosmetic.
        if face.face != self._sent_face_name:
            self.effector.set_avatar(face.face)
            self._sent_face_name = face.face
            # The device has just reset every override to that face's resting
            # values, so our record of what it knows must reset too, or the
            # diff below would skip re-sending an override that is now gone.
            sent.left_eye = FeatureState()
            sent.right_eye = FeatureState()
            sent.mouth = FeatureState()

        if face.blink_enabled != self._sent_blink:
            self.effector.set_blink(face.blink_enabled)
            self._sent_blink = face.blink_enabled

        # Both eyes moving identically is the common case and has its own tool,
        # which halves the traffic and reads better in the gateway's log.
        eyes_together = (
            face.left_eye.x == face.right_eye.x
            and face.left_eye.y == face.right_eye.y
            and (face.left_eye.x, face.left_eye.y) != (sent.left_eye.x, sent.left_eye.y)
        )
        if eyes_together:
            self.effector.set_gaze(face.left_eye.x, face.left_eye.y)
            for target in (sent.left_eye, sent.right_eye):
                target.x, target.y = face.left_eye.x, face.left_eye.y

        for name in (LEFT_EYE, RIGHT_EYE, MOUTH):
            self._flush_feature(name, face.feature(name), sent.feature(name))

        if face.speech != self._sent_speech:
            self.effector.set_speech(face.speech)
            self._sent_speech = face.speech

        if face.leds != self._sent_leds:
            self.effector.set_all_leds(*face.leds)
            self._sent_leds = face.leds

        target = self.motion.target
        if (
            self._sent_pose is None
            or target != self._sent_pose
            or self.motion.speed_dps != self._sent_speed
        ):
            self.effector.move_head(target.yaw, target.pitch, self.motion.speed_dps)
            self._sent_pose = target
            self._sent_speed = self.motion.speed_dps

    def _flush_feature(self, name: str, want: FeatureState, sent: FeatureState) -> None:
        """One `set_feature` per feature, carrying only the changed axes.

        Position goes as a pair or not at all: the firmware rejects exactly one
        of x/y, deliberately, because half a position is a jump to zero on the
        other axis.
        """
        kwargs: dict[str, int] = {}
        if (want.x, want.y) != (sent.x, sent.y):
            kwargs["x"], kwargs["y"] = want.x, want.y
        if want.rotation != sent.rotation:
            kwargs["rotation"] = want.rotation
        if want.weight is not None and want.weight != sent.weight:
            kwargs["weight"] = want.weight
        if want.size is not None and want.size != sent.size:
            kwargs["size"] = want.size
        if not kwargs:
            return
        self.effector.set_feature(name, **kwargs)
        for axis, value in kwargs.items():
            setattr(sent, axis, value)
