"""What a gateway event does when it reaches the character stack.

`run_once` composes an MCP session, a hook receiver, an office poll and the
event tail, and has no test -- so the routing inside its closure could not be
asserted at all. That is not an academic gap: the touch routing was wrong for
the whole life of the project and only a person stroking a robot found it.

`route_event` is module-level for that reason, and these are the assertions
that could not previously exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402


# --- what a touch does --------------------------------------------------
#
# This routing was wrong for the whole life of the project and nothing could
# assert it, because it lived inside `run_once`'s closure. It is a module-level
# function now precisely so these can exist.


class RecordingCharacter:
    def __init__(self):
        self.touched = []

    def on_touch(self, subtype):
        self.touched.append(subtype)


def test_a_tap_is_affection_not_a_wake_word():
    """The wake word starts a conversation. Touching him does not.

    A tap used to open a five-second microphone, which meant `on_touch` never
    ran and head-pet had never fired on the robot.
    """
    character = RecordingCharacter()
    live.route_event({"event_type": "touch", "subtype": "tap"}, character)
    assert character.touched == ["tap"]


def test_a_stroke_is_affection_too():
    character = RecordingCharacter()
    live.route_event({"event_type": "touch", "subtype": "stroke"}, character)
    assert character.touched == ["stroke"]


def test_every_subtype_reaches_the_character():
    """No subtype is special. `apply-m5-touch.sh` makes TAP the default outcome
    of any release that did not swipe, so a one-pad stroke arrives as a tap."""
    character = RecordingCharacter()
    for subtype in ("tap", "stroke", "swipe_left", "", "something_new"):
        live.route_event({"event_type": "touch", "subtype": subtype}, character)
    assert len(character.touched) == 5


def test_a_non_touch_event_is_ignored():
    character = RecordingCharacter()
    live.route_event({"event_type": "battery", "subtype": "low"}, character)
    assert character.touched == []


def test_a_malformed_event_does_not_raise():
    """Events come off a log the gateway writes; a missing key must not kill
    the tail task that reads it."""
    character = RecordingCharacter()
    live.route_event({}, character)
    assert character.touched == []
