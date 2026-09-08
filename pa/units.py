"""M5's units, converted to ours. One place, because getting it wrong is silent.

M5's modifiers and dances are full of numbers like `moveWithSpeed(600, 150)` and
`pitch += 250`. Copying those into our calls would look faithful and behave
wrong, in three separate ways.

--- 1. Angles are TENTHS of a degree ---

Not degrees, and not a servo-specific unit. Traced through their HAL rather
than assumed (``hal/hal_servo.cpp``):

    int mapped_angle = _zero_pos + angle * 16 / 5 / 10;  // one step = 0.3125 deg

``angle * 16/50`` steps at ``0.3125`` degrees a step is ``angle * 0.1`` degrees.
So their yaw limit of +/-1280 is +/-128 degrees, and their pitch limit of
30..870 is 3..87 degrees. Their dance yaw of 600 is 60 degrees, which fits our
+/-90 comfortably.

--- 2. Pitch is measured from a different zero ---

This is the one that would break every call rather than merely look odd. Our
gateway's pitch is 5..85 with about 45 being level, and it **rejects**
out-of-range values rather than clamping -- so a rejected pose is not a
slightly-wrong head, it is a head that does not move at all.

M5's pitch zero is not level, it is their home position, and increasing pitch
raises the head. Their own code says so: ``head_pet.h``'s "raise head" case is
``target_pitch += 150..250``, and its neighbouring "tilt head" case subtracts.
Their dances then use pitch 0 as the centre of the gesture and go negative from
there, which against a 3..87 servo limit would clamp to the floor.

So M5 pitch is treated here as an offset from OUR resting pitch. A nod of
``+200`` becomes 45 + 20 = 65 degrees: the same gesture, in our frame. That
preserves what the animation means, which copying the number would not.

--- 3. Speed is not degrees per second ---

``moveWithSpeed(angle, speed)`` documents speed as 0-1000 and feeds it to
``map_speed_to_spring_options`` -- a spring stiffness/damping pair, not a rate.
There is no conversion to derive, so the mapping below is a CALIBRATION: it is
monotonic, it puts M5's most-used band (100-400) onto the gateway's own
low-to-mid band (~30-120 dps), and it is the knob to turn if ported animations
feel too fast or too slow. It is not claimed to be equivalent.
"""

from __future__ import annotations

from tracking import PITCH_MAX, PITCH_MIN, REST_PITCH, YAW_MAX, YAW_MIN, clamp

#: Degrees per M5 angle unit. Proved from hal_servo.cpp, not assumed.
M5_DEGREES_PER_UNIT = 0.1

#: M5's own servo limits, in their units, for reference and for tests:
#: yaw +/-1280 (+/-128 deg), pitch 30..870 (3..87 deg).
M5_YAW_LIMIT = 1280
M5_PITCH_LIMIT = (30, 870)

#: The gateway's speed band, in degrees per second. `low` and `high` are the
#: named speeds the gateway documents; the mapping below lands inside them.
GATEWAY_SPEED_MIN_DPS = 15
GATEWAY_SPEED_MAX_DPS = 240

#: Degrees per second per M5 speed unit. See the docstring: a calibration.
#: M5 100 -> 25 dps, 400 -> 100 dps, 1000 -> 250 dps (clamped to 240).
DPS_PER_M5_SPEED = 0.25


def m5_yaw_to_deg(units: int) -> float:
    """M5 yaw, in tenths of a degree, as our yaw in degrees.

    Absolute on both sides: their yaw zero and ours are both straight ahead,
    so this is only a unit change. Clamped, because `move_head` rejects rather
    than clamps and their range is wider than ours.
    """
    return clamp(units * M5_DEGREES_PER_UNIT, YAW_MIN, YAW_MAX)


def m5_pitch_to_deg(units: int, rest: float = REST_PITCH) -> float:
    """M5 pitch, in tenths of a degree, as our pitch in degrees.

    RELATIVE, not absolute -- see the docstring. `units` is read as an offset
    from M5's home, and applied as the same offset from our resting pitch.
    """
    return clamp(rest + units * M5_DEGREES_PER_UNIT, PITCH_MIN, PITCH_MAX)


def m5_speed_to_dps(speed: int) -> int:
    """M5's opaque 0-1000 spring speed as degrees per second.

    A calibration, not a conversion. Monotonic and clamped into the gateway's
    band so no ported animation can ask for a speed it refuses.
    """
    dps = round(speed * DPS_PER_M5_SPEED)
    return int(clamp(float(dps), float(GATEWAY_SPEED_MIN_DPS), float(GATEWAY_SPEED_MAX_DPS)))
