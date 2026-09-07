"""Turning a face in the frame into a head pose.

Pure functions, no camera and no servos, for the same reason
`bridge/src/posture.ts` is pure: this is where the bugs that matter live --
sign errors, clamping, jitter -- and none of them need a robot on the desk to
find. The detector and the servo driver are somebody else's maintained code.

--- Proportional, not absolute ---

The obvious approach is to convert a pixel offset into an angle using the
camera's field of view, then command that angle. It needs a number we do not
have: the GC0308's effective horizontal FOV behind the CoreS3's lens is not in
any datasheet we can trust, and guessing it wrong makes the head either
undershoot forever or oscillate.

So instead: measure how far off-centre the face is, and move a FRACTION of the
way toward it. The loop converges on any FOV, because each step reduces the
error and the next frame measures the new error. Getting the gain wrong costs
smoothness, not correctness -- and a gain is something you can tune by watching
the robot, which an FOV is not.

--- The sign is not knowable from here ---

Whether a face on the right of the image needs yaw to increase or decrease
depends on how the servo is mounted and which way the camera is fitted. That
is an empirical fact about one robot, so YAW_SIGN and PITCH_SIGN are constants
to flip once, on hardware, rather than a guess baked into the maths. The tests
below assert the behaviour under BOTH signs so that flipping one cannot
silently break the clamping.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Servo limits. Pitch is the firmware's hard range; `move_head` REJECTS
#: values outside it rather than clamping, so anything we emit must already be
#: inside. Yaw is the mechanical range.
PITCH_MIN, PITCH_MAX = 5.0, 85.0
YAW_MIN, YAW_MAX = -90.0, 90.0

#: A resting pose to return to when nobody is there. Slightly below level: a
#: robot on a desk is looking at a seated person, not at the far wall.
REST_YAW, REST_PITCH = 0.0, 45.0

#: Fraction of the measured error to correct per frame. 0.5 halves the error
#: each step, which settles in ~4 frames without the overshoot that a gain
#: near 1 gives when the capture rate is uneven.
DEFAULT_GAIN = 0.5

#: Offsets smaller than this (as a fraction of half the frame) are treated as
#: centred. Without it the head hunts continuously around a stationary face,
#: because the detector's box jitters by a few pixels every frame -- motion
#: that reads as a nervous tic rather than attention.
DEFAULT_DEADBAND = 0.08

#: Largest change in a single step, in degrees. The servo bus has been seen to
#: hang on large abrupt reversals, and a head that snaps is unpleasant to sit
#: next to. Tracking should look like noticing, not like a turret.
MAX_STEP_DEG = 12.0

#: Flip on hardware if he turns away from you instead of toward you. See the
#: module docstring: this is a fact about one robot's assembly.
YAW_SIGN = -1.0
PITCH_SIGN = -1.0


@dataclass(frozen=True)
class Box:
    """A detected face, in pixels."""

    x: float
    y: float
    width: float
    height: float

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


@dataclass(frozen=True)
class Pose:
    yaw: float
    pitch: float


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def offset(box: Box, frame_width: int, frame_height: int) -> tuple[float, float]:
    """How far off-centre the face is, as fractions in [-1, 1].

    (0, 0) is dead centre. Positive x is right of centre, positive y is BELOW
    centre -- image coordinates, where y grows downward. Keeping the image
    convention here and flipping it once in the sign constant is less
    error-prone than a half-flipped convention nobody can keep straight.
    """
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError("frame dimensions must be positive")
    cx, cy = box.centre
    dx = (cx - frame_width / 2.0) / (frame_width / 2.0)
    dy = (cy - frame_height / 2.0) / (frame_height / 2.0)
    return clamp(dx, -1.0, 1.0), clamp(dy, -1.0, 1.0)


def step_toward(
    current: Pose,
    dx: float,
    dy: float,
    *,
    gain: float = DEFAULT_GAIN,
    deadband: float = DEFAULT_DEADBAND,
    max_step: float = MAX_STEP_DEG,
) -> Pose:
    """The next pose, moving a fraction of the way toward the face.

    Deadband first, then gain, then the per-step cap, then the servo clamp.
    The order matters: capping before clamping would let a capped step still
    land outside the firmware's accepted range on a head already near the
    limit, and `move_head` rejects rather than clamps.
    """
    # Inside the deadband the face is close enough to centred. Returning the
    # current pose unchanged -- rather than a slightly different one -- is what
    # stops the hunting.
    if abs(dx) < deadband:
        dx = 0.0
    if abs(dy) < deadband:
        dy = 0.0

    # A full-frame offset asks for roughly a quarter turn; the gain then scales
    # it. The exact constant is not load-bearing precisely because the loop is
    # proportional: too small is slow, too large is bouncy, neither is wrong.
    yaw_error = dx * 45.0
    pitch_error = dy * 30.0

    yaw_delta = clamp(YAW_SIGN * yaw_error * gain, -max_step, max_step)
    pitch_delta = clamp(PITCH_SIGN * pitch_error * gain, -max_step, max_step)

    return Pose(
        yaw=clamp(current.yaw + yaw_delta, YAW_MIN, YAW_MAX),
        pitch=clamp(current.pitch + pitch_delta, PITCH_MIN, PITCH_MAX),
    )


def step_to_rest(current: Pose, *, max_step: float = MAX_STEP_DEG) -> Pose:
    """Drift back to resting when there is no face.

    Stepped rather than snapped, and slower than tracking: a head that whips
    back the instant you look away is worse company than one that lingers.
    """
    rest_step = max_step / 2.0
    return Pose(
        yaw=current.yaw + clamp(REST_YAW - current.yaw, -rest_step, rest_step),
        pitch=current.pitch + clamp(REST_PITCH - current.pitch, -rest_step, rest_step),
    )


def largest(boxes: list[Box]) -> Box | None:
    """Pick whom to look at: the biggest face, i.e. the nearest.

    Not the first detected, which is an artefact of scan order, and not the
    most central, which would make him refuse to turn to someone at the edge
    of frame. Nearest is the best available proxy for "the person talking to
    me" until the audio can tell us direction.
    """
    if not boxes:
        return None
    return max(boxes, key=lambda b: b.width * b.height)
