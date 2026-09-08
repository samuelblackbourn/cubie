"""Idle behaviour: the small movements that make him seem awake.

Ported from M5's `modifiers/idle_motion.h`, which is the version that
demonstrably feels right on this hardware. Their STRUCTURE is copied exactly,
because it is the part that matters:

  - a random 4-8 second interval between actions, not a fixed tick
  - four weighted actions, so the behaviour is varied rather than a loop
  - if the head is still moving, defer 500 ms rather than queue another
    command -- which is what stops idle motion fighting itself

Their RANGES are adapted, not copied, and that distinction is deliberate.
M5 works in tenths of a degree with `lookAtNormalized` coordinates whose
mapping depends on their servo mounting and their own normalisation; our
`move_head` takes degrees with pitch 5..85, and speed in degrees per second
(theirs is an opaque 100-400 scale). Copying their numbers into different
units would look faithful and behave wrong. So the weights and cadence are
theirs; the angles are ours, anchored on the resting pose the bridge already
uses.

Nothing here talks to the robot. Choosing the next move is pure and testable;
sending it is the caller's job, the same split as tracking.py and posture.ts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from tracking import PITCH_MAX, PITCH_MIN, REST_PITCH, YAW_MAX, YAW_MIN, Pose, clamp

#: M5's interval, verbatim: a random gap rather than a fixed one, so the
#: movement does not read as a metronome.
INTERVAL_MIN_S = 4.0
INTERVAL_MAX_S = 8.0

#: M5 defers by this much when the previous move has not finished. Without it,
#: commands pile up and the head jerks between queued targets.
BUSY_RETRY_S = 0.5

#: M5 waits a second after start before the first action. A robot that lurches
#: the instant it powers on reads as a fault, not as life.
FIRST_ACTION_DELAY_S = 1.0


@dataclass(frozen=True)
class Move:
    """Where to look next, and how fast to get there."""

    yaw: float
    pitch: float
    speed_dps: int
    kind: str


def _look_around(rng: random.Random, current: Pose) -> Move:
    """Half of all idle actions: an unhurried look somewhere else.

    M5's normalised x range is +/-0.4 -- deliberately not the full sweep. A
    robot that regularly turns its head 80 degrees looks like it is scanning
    for threats; one that glances within a comfortable arc looks like it is
    thinking.
    """
    return Move(
        yaw=rng.uniform(-35.0, 35.0),
        pitch=rng.uniform(30.0, 55.0),
        speed_dps=rng.randint(30, 60),
        kind="look-around",
    )


def _small_observation(rng: random.Random, current: Pose) -> Move:
    """Slightly adjust from wherever the head already is.

    Relative rather than absolute, as M5 has it: this is the action that reads
    as attention rather than as a decision, and it needs to start from the
    current pose to do that.
    """
    return Move(
        yaw=current.yaw + rng.uniform(-15.0, 15.0),
        pitch=current.pitch + rng.uniform(-8.0, 8.0),
        speed_dps=rng.randint(25, 45),
        kind="small-observation",
    )


def _quick_glance(rng: random.Random, current: Pose) -> Move:
    """A fast look further afield. The only fast idle action, at 10%.

    Rare and quick on purpose. Frequent sudden movement is unnerving to sit
    next to; occasional sudden movement is the thing that makes a machine seem
    to have noticed something.
    """
    return Move(
        yaw=rng.uniform(-50.0, 50.0),
        pitch=rng.uniform(25.0, 50.0),
        speed_dps=rng.randint(150, 240),
        kind="quick-glance",
    )


def _recentre(rng: random.Random, current: Pose) -> Move:
    """Return yaw to zero. M5's "go home".

    Needed because the other three actions random-walk: without something
    pulling yaw back, he drifts to one side over a few minutes and stays
    there, looking at a wall.
    """
    return Move(
        yaw=0.0,
        pitch=rng.uniform(35.0, 50.0),
        speed_dps=rng.randint(40, 80),
        kind="recentre",
    )


#: M5's weights: 50 / 30 / 10 / 10. Kept exactly -- the feel of the idle
#: behaviour is mostly this distribution, not the individual ranges.
ACTIONS = (
    (50, _look_around),
    (30, _small_observation),
    (10, _quick_glance),
    (10, _recentre),
)


def next_move(current: Pose, rng: random.Random | None = None) -> Move:
    """Choose the next idle move from the current pose.

    Always returns something inside the servo limits: `move_head` REJECTS
    out-of-range values rather than clamping, so an unclamped target is not a
    slightly-wrong pose but a head that does not move at all.
    """
    rng = rng or random.Random()
    roll = rng.randint(0, 99)
    cumulative = 0
    chosen = _look_around
    for weight, action in ACTIONS:
        cumulative += weight
        if roll < cumulative:
            chosen = action
            break

    move = chosen(rng, current)
    return Move(
        yaw=clamp(move.yaw, YAW_MIN, YAW_MAX),
        pitch=clamp(move.pitch, PITCH_MIN, PITCH_MAX),
        speed_dps=move.speed_dps,
        kind=move.kind,
    )


def next_interval(rng: random.Random | None = None) -> float:
    """Seconds until the next idle action."""
    rng = rng or random.Random()
    return rng.uniform(INTERVAL_MIN_S, INTERVAL_MAX_S)


def resting() -> Pose:
    """Where he sits when nothing is happening."""
    return Pose(yaw=0.0, pitch=REST_PITCH)
