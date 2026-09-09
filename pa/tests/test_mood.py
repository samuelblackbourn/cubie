"""Tests for the office on his resting face.

Ported alongside `pa/mood.py` from `bridge/test/posture.test.ts`, and kept as
tests of the DECISION rather than of the rendering: the reason is the output
that matters, because the face is only how a reason looks.

What is deliberately not ported: the bridge's pose assertions (it set an
absolute yaw and pitch per mood, which the character stack's idle system now
owns) and its yaw-travel bound (no posture here moves the head at all, so there
is no travel to bound). Both are recorded in `pa/mood.py`'s docstring rather
than dropped silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import driver as driver_mod  # noqa: E402
import mood as mood_mod  # noqa: E402
import office  # noqa: E402
from chan import Chan, RecordingEffector  # noqa: E402


def state(**overrides):
    payload = {
        "pendingApprovals": [], "pendingTotal": 0, "agentsRunning": 0,
        "needsYou": False, "founderTasks": [], "founderTasksTotal": 0,
        "mood": "calm", "ts": 1,
    }
    payload.update(overrides)
    return office.parse_office_state(payload)


# --- the decision -----------------------------------------------------------


def test_an_unreadable_office_is_a_mood_not_a_silence():
    """The bridge's reasoning, kept: a failed fetch and a network outage are
    the same thing from the desk. Holding the last good mood would be worse
    than either, because a stale calm ring is indistinguishable from a calm
    office."""
    m = mood_mod.mood_for(None)
    assert m.reason == mood_mod.OFFLINE
    assert m.leds == mood_mod.AMBER


def test_something_waiting_on_a_person_outranks_machines_being_busy():
    """Precedence by urgency, not field order. A person being needed matters
    more than agents working."""
    m = mood_mod.mood_for(state(needsYou=True, agentsRunning=4))
    assert m.reason == mood_mod.ATTENTION


def test_a_pending_approval_is_attention_even_without_needs_you():
    assert mood_mod.mood_for(state(pendingTotal=2)).reason == mood_mod.ATTENTION


def test_review_outranks_busy_but_not_attention():
    assert mood_mod.mood_for(
        state(founderTasksTotal=1, agentsRunning=2)
    ).reason == mood_mod.REVIEW
    assert mood_mod.mood_for(
        state(founderTasksTotal=1, pendingTotal=1)
    ).reason == mood_mod.ATTENTION


def test_agents_running_is_busy():
    assert mood_mod.mood_for(state(agentsRunning=1)).reason == mood_mod.BUSY


def test_a_busy_mood_string_counts_even_with_no_agents_reported():
    assert mood_mod.mood_for(state(mood="busy")).reason == mood_mod.BUSY


def test_a_quiet_office_is_resting_with_a_dark_ring():
    m = mood_mod.mood_for(state())
    assert m.reason == mood_mod.RESTING
    assert m.leds == mood_mod.OFF


def test_every_mood_names_a_face_the_board_actually_has():
    """A face the board does not know is rejected by `set_avatar` at the far
    end, and a rejected call there is a silent no-op."""
    from chan import FACES

    for st in (None, state(), state(needsYou=True), state(founderTasksTotal=1),
               state(agentsRunning=1)):
        assert mood_mod.mood_for(st).face in FACES


# --- who owns the face and the ring -----------------------------------------


def build():
    chan = Chan(RecordingEffector())
    return driver_mod.CharacterDriver(chan, idle_motion_level=0), chan


def test_the_mood_shows_while_standing_by():
    character, chan = build()
    character.set_status(driver_mod.STANDBY)
    character.set_office_mood(state(needsYou=True))
    assert chan.face.face == "surprised"
    assert chan.face.leds == mood_mod.PA_PINK


def test_the_ring_lights_up_where_standby_leaves_it_dark():
    """The whole value of this file: an idle robot has nothing lit, so a mood
    that lights the ring adds a signal rather than overwriting one."""
    character, chan = build()
    character.set_status(driver_mod.STANDBY)
    assert chan.face.leds == driver_mod.STATUS_LEDS[driver_mod.STANDBY]
    character.set_office_mood(state(pendingTotal=1))
    assert chan.face.leds != driver_mod.STATUS_LEDS[driver_mod.STANDBY]


def test_a_reading_that_arrives_mid_conversation_waits_rather_than_intruding():
    """Two things driving one axis is the fight this ordering exists to avoid.
    While he is speaking, the status owns the face and the ring."""
    character, chan = build()
    character.set_status(driver_mod.SPEAKING)
    speaking_leds = chan.face.leds
    character.set_office_mood(state(needsYou=True))
    assert chan.face.leds == speaking_leds, "the mood interrupted a conversation"
    assert chan.face.face != "surprised"


def test_but_it_is_not_lost_and_lands_when_he_finishes():
    """Dropping it would leave the ring wrong until the next poll, which is the
    small staleness that makes an ambient signal untrustworthy."""
    character, chan = build()
    character.set_status(driver_mod.SPEAKING)
    character.set_office_mood(state(needsYou=True))
    character.set_status(driver_mod.STANDBY)
    assert chan.face.face == "surprised"
    assert chan.face.leds == mood_mod.PA_PINK


def test_it_does_not_wipe_the_sleep_bubble():
    """M5's sleepy is a whole state -- a bubble, a settled head, idle motion
    stopped -- and a poll landing in the middle would take the face out from
    under it."""
    character, chan = build()
    character.set_status(driver_mod.STANDBY)
    character.set_emotion("sleepy")
    assert chan.face.speech == "Zzz…"
    character.set_office_mood(state(needsYou=True))
    assert chan.face.speech == "Zzz…"
    assert chan.face.face != "surprised"


def test_the_reason_comes_back_for_the_log():
    character, _ = build()
    assert character.set_office_mood(None) == mood_mod.OFFLINE


def test_an_unreadable_office_reaches_his_face_rather_than_only_the_log():
    character, chan = build()
    character.set_status(driver_mod.STANDBY)
    character.set_office_mood(None)
    assert chan.face.leds == mood_mod.AMBER


# --- the two bridge tests worth keeping -------------------------------------


def test_every_reason_looks_different_from_every_other():
    """Ported from the bridge, and it earned its passage immediately: dropping
    the bridge's poses collapsed `offline` and `review` into the same face and
    the same amber, because there they differed by head angle alone. An
    ambient signal where a fault looks like an ordinary queue is worse than no
    signal, since nobody investigates it."""
    readings = {
        mood_mod.OFFLINE: None,
        mood_mod.ATTENTION: state(needsYou=True),
        mood_mod.REVIEW: state(founderTasksTotal=1),
        mood_mod.BUSY: state(agentsRunning=1),
        mood_mod.RESTING: state(),
    }
    seen: dict[tuple, str] = {}
    for reason, st in readings.items():
        m = mood_mod.mood_for(st)
        assert m.reason == reason, f"{reason} reading produced {m.reason}"
        key = (m.face, m.leds)
        assert key not in seen, f"{reason} is indistinguishable from {seen.get(key)}"
        seen[key] = reason


def test_every_led_channel_is_one_the_device_will_take():
    """`set_all_leds` takes 0..255 per channel. These are constants, so a bad
    one is a typo -- and a typo that silently truncates is the kind that ships."""
    for st in (None, state(), state(needsYou=True), state(founderTasksTotal=1),
               state(agentsRunning=1)):
        for channel in mood_mod.mood_for(st).leds:
            assert isinstance(channel, int)
            assert 0 <= channel <= 255


# --- the glance -------------------------------------------------------------
#
# What `pa/mood.py` was written around and could not have while the office's
# mood was a held pose. A person notices movement, not an attitude.


def gesture_running(character):
    import modifiers

    return [m for m in character.chan.modifiers
            if isinstance(m, modifiers.DanceModifier)]


def test_entering_attention_makes_him_look_up_and_come_back():
    character, _ = build()
    character.set_status(driver_mod.STANDBY)
    character.set_office_mood(state())
    assert not gesture_running(character)
    character.set_office_mood(state(needsYou=True))
    assert gesture_running(character), "entering attention should glance"


def test_staying_in_attention_does_not_glance_again():
    """Every fifteen seconds for as long as an approval goes unanswered is
    nagging rather than noticing."""
    character, _ = build()
    character.set_status(driver_mod.STANDBY)
    character.set_office_mood(state(needsYou=True))
    now = 0.0
    while now < 3.0:
        character.update(now)
        now += 0.05
    assert not gesture_running(character)
    character.set_office_mood(state(needsYou=True, pendingTotal=1))
    assert not gesture_running(character), "still attention: no second glance"


def test_leaving_and_re_entering_attention_glances_again():
    """A new thing arriving after the last was cleared is a new event, and
    worth noticing."""
    character, _ = build()
    character.set_status(driver_mod.STANDBY)
    character.set_office_mood(state(needsYou=True))
    now = 0.0
    while now < 3.0:
        character.update(now)
        now += 0.05
    character.set_office_mood(state())
    character.set_office_mood(state(needsYou=True))
    assert gesture_running(character)


def test_he_does_not_glance_while_being_spoken_to():
    """The status owns the head then, and a reading arriving mid-answer is held
    rather than acted on."""
    character, _ = build()
    character.set_status(driver_mod.LISTENING)
    character.set_office_mood(state(needsYou=True))
    assert not gesture_running(character)


def test_no_other_mood_glances():
    """Only `attention` means something arrived for a person."""
    for st in (None, state(founderTasksTotal=1), state(agentsRunning=1), state()):
        character, _ = build()
        character.set_status(driver_mod.STANDBY)
        character.set_office_mood(st)
        assert not gesture_running(character), mood_mod.mood_for(st).reason


def test_the_first_reading_of_the_day_glances_if_something_is_waiting():
    """No previous mood at all is still a transition INTO attention. Treating
    an absent previous reading as 'already attention' would mean he never
    noticed anything that was waiting before he started up."""
    character, _ = build()
    character.set_status(driver_mod.STANDBY)
    assert character.office_mood is None
    character.set_office_mood(state(pendingTotal=3))
    assert gesture_running(character)


def test_something_arriving_while_he_talks_is_still_noticed_afterwards():
    """The case the first version lost, and the one that matters most.

    An approval lands mid-answer. The mood is held and painted on his face when
    he returns to standby -- but the first version compared each reading's
    reason against the previous one, and by then `office_mood.reason` was
    already ATTENTION, so every later poll failed the transition test and the
    glance never happened at all. Something arrived and he never noticed, which
    is the one thing this gesture exists to prevent."""
    character, _ = build()
    character.set_status(driver_mod.SPEAKING)
    character.set_office_mood(state(needsYou=True))
    assert not gesture_running(character), "not while he is talking"

    character.set_status(driver_mod.STANDBY)
    assert gesture_running(character), "he never noticed it"


def test_he_does_not_glance_twice_for_the_same_waiting_thing():
    """Returning to standby repeatedly -- several short conversations while one
    approval sits there -- must not glance each time."""
    character, _ = build()
    character.set_status(driver_mod.STANDBY)
    character.set_office_mood(state(needsYou=True))
    now = 0.0
    while now < 3.0:
        character.update(now)
        now += 0.05
    for _ in range(3):
        character.set_status(driver_mod.SPEAKING)
        character.set_status(driver_mod.STANDBY)
        assert not gesture_running(character)


def test_the_next_thing_to_arrive_is_noticed_too():
    """The flag has to clear, or he notices the first approval of the day and
    nothing else ever again."""
    character, _ = build()
    character.set_status(driver_mod.STANDBY)
    character.set_office_mood(state(needsYou=True))
    now = 0.0
    while now < 3.0:
        character.update(now)
        now += 0.05
    character.set_office_mood(state())            # cleared
    assert not character.glanced_for_attention
    character.set_office_mood(state(pendingTotal=1))
    assert gesture_running(character)


def test_he_does_not_glance_in_his_sleep():
    character, _ = build()
    character.set_status(driver_mod.STANDBY)
    character.set_emotion("sleepy")
    character.set_office_mood(state(needsYou=True))
    assert not gesture_running(character)
