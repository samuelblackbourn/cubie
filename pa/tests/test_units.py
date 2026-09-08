"""Tests for M5's units converted to ours.

The whole point of this module is that copying M5's numbers would be silently
wrong, so these tests pin the three ways it would have been wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import units  # noqa: E402
from tracking import PITCH_MAX, PITCH_MIN, REST_PITCH, YAW_MAX, YAW_MIN  # noqa: E402


def test_angles_are_tenths_of_a_degree():
    """Traced through hal_servo.cpp: angle * 16/5/10 steps at 0.3125 deg/step."""
    assert units.m5_yaw_to_deg(600) == 60.0
    assert units.m5_yaw_to_deg(-450) == -45.0
    assert units.m5_yaw_to_deg(0) == 0.0


def test_m5s_own_limits_convert_to_their_documented_degrees():
    """Their yaw limit is +/-1280 and pitch 30..870 -- i.e. +/-128 and 3..87."""
    assert 1280 * units.M5_DEGREES_PER_UNIT == 128.0
    lo, hi = units.M5_PITCH_LIMIT
    assert (lo * units.M5_DEGREES_PER_UNIT, hi * units.M5_DEGREES_PER_UNIT) == (3.0, 87.0)


def test_yaw_is_clamped_because_m5s_range_is_wider_than_ours():
    """move_head REJECTS out of range rather than clamping, so an unclamped
    conversion is a head that does not move at all."""
    assert units.m5_yaw_to_deg(1280) == YAW_MAX
    assert units.m5_yaw_to_deg(-1280) == YAW_MIN


def test_pitch_is_relative_to_our_rest_not_absolute():
    """The conversion that would otherwise break every ported animation.

    M5's pitch zero is their home and increasing pitch raises the head; ours is
    5..85 with about 45 level. Read as an offset, a nod of +200 is 20 degrees up
    from rest.
    """
    assert units.m5_pitch_to_deg(0) == REST_PITCH
    assert units.m5_pitch_to_deg(200) == REST_PITCH + 20.0
    assert units.m5_pitch_to_deg(-300) == REST_PITCH - 30.0


def test_negative_pitch_would_have_been_rejected_if_taken_literally():
    """Their dances use pitch 0 as the centre and go negative from there. Taken
    as an absolute value, every one of those is outside our 5..85 window."""
    literal_centre = 0.0
    literal_low = -300 * units.M5_DEGREES_PER_UNIT
    assert not PITCH_MIN <= literal_centre <= PITCH_MAX
    assert not PITCH_MIN <= literal_low <= PITCH_MAX
    # And rebased, both are comfortably inside it.
    assert PITCH_MIN <= units.m5_pitch_to_deg(0) <= PITCH_MAX
    assert PITCH_MIN <= units.m5_pitch_to_deg(-300) <= PITCH_MAX


def test_pitch_is_clamped_at_both_ends():
    assert units.m5_pitch_to_deg(10000) == PITCH_MAX
    assert units.m5_pitch_to_deg(-10000) == PITCH_MIN


def test_speed_is_monotonic_and_always_inside_the_gateways_band():
    """A calibration rather than a conversion, but it must never produce a
    speed the gateway refuses."""
    previous = -1
    for m5_speed in range(0, 1001, 10):
        dps = units.m5_speed_to_dps(m5_speed)
        assert units.GATEWAY_SPEED_MIN_DPS <= dps <= units.GATEWAY_SPEED_MAX_DPS
        assert dps >= previous
        previous = dps


def test_m5s_most_used_speed_band_lands_in_the_gateways_low_to_mid():
    """Their animations mostly use 100-400. That should read as unhurried."""
    assert units.m5_speed_to_dps(100) == 25
    assert units.m5_speed_to_dps(400) == 100
