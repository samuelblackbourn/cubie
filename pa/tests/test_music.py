"""Tests for dancing to the music.

Pure, seeded, no robot. The properties worth pinning are the refusals: every
one of them is a thing that would be discovered on a desk, on an afternoon,
by somebody who then has to work out why the robot will not stop moving.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import music  # noqa: E402
from office import NowPlaying, parse_office_state  # noqa: E402


@dataclass(frozen=True)
class Track:
    """Duck-typed stand-in -- `decide` deliberately does not import the client."""

    track_id: str = "track-1"
    title: str = "Bad Guy"
    artist: str = "Billie Eilish"
    in_the_room: bool = True


def floor() -> music.DanceFloor:
    return music.DanceFloor(rng=random.Random(7))


def allowed(**over):
    """Every gate open, so a test can close exactly one."""
    return {"awake": True, "busy": False, "needs_you": False, **over}


def test_a_new_track_starts_a_dance():
    f = floor()
    move = f.decide(Track(), 100.0, **allowed())
    assert move is not None
    assert move.kind == "dance"
    assert move.name in music.DANCES


def test_the_same_track_does_not_dance_again_immediately():
    f = floor()
    assert f.decide(Track(), 100.0, **allowed()) is not None
    assert f.decide(Track(), 110.0, **allowed()) is None


def test_a_continuing_track_is_punctuated_by_a_gesture():
    """A three-minute song must not be one flourish and four minutes of nothing --
    that reads as a coincidence, not as listening."""
    f = floor()
    f.decide(Track(), 100.0, **allowed())
    move = f.decide(Track(), 100.0 + music.GESTURE_GAP_S, **allowed())
    assert move is not None
    assert move.kind == "gesture"
    assert move.name in music.GESTURES


def test_a_track_change_dances_again_once_the_gap_has_passed():
    f = floor()
    f.decide(Track(), 100.0, **allowed())
    move = f.decide(Track(track_id="track-2"), 100.0 + music.MIN_GAP_S, **allowed())
    assert move is not None
    assert move.kind == "dance"


def test_skipping_through_an_album_does_not_stack_sequences():
    """`driver._perform` survives it -- a newer sequence abandons the older --
    but the result is a head twitching between abandoned timelines."""
    f = floor()
    assert f.decide(Track(track_id="a"), 100.0, **allowed()) is not None
    for i, t in enumerate(("b", "c", "d", "e")):
        assert f.decide(Track(track_id=t), 100.0 + i + 1, **allowed()) is None


def test_a_song_skipped_past_does_not_fire_late():
    """Recording the id even on a refusal is what stops this: otherwise the
    track stays 'new' and dances the moment the gap clears, to a song that
    finished twenty seconds ago."""
    f = floor()
    f.decide(Track(track_id="a"), 100.0, **allowed())
    f.decide(Track(track_id="b"), 105.0, **allowed())  # refused, gap too short
    move = f.decide(Track(track_id="b"), 100.0 + music.MIN_GAP_S + 1, **allowed())
    assert move is None or move.kind == "gesture"


def test_nothing_playing_never_moves():
    f = floor()
    assert f.decide(None, 100.0, **allowed()) is None


def test_music_returning_after_a_silence_is_a_change():
    """Forgetting the track when the music stops is the point: the moment it
    comes back is exactly the one worth marking."""
    f = floor()
    f.decide(Track(), 100.0, **allowed())
    f.decide(None, 200.0, **allowed())
    move = f.decide(Track(), 300.0, **allowed())
    assert move is not None
    assert move.kind == "dance"


def test_he_does_not_dance_to_music_that_is_not_in_the_room():
    """Headphones, or a phone three miles away. Still playing to Spotify; still
    nothing anybody here can hear."""
    f = floor()
    assert f.decide(Track(in_the_room=False), 100.0, **allowed()) is None


def test_out_of_room_music_still_counts_as_the_current_track():
    """So plugging headphones out mid-song does not then read as a new track."""
    f = floor()
    f.decide(Track(track_id="a", in_the_room=False), 100.0, **allowed())
    move = f.decide(Track(track_id="a"), 100.0 + music.MIN_GAP_S + 1, **allowed())
    assert move is None or move.kind == "gesture"


def test_he_does_not_dance_while_asleep():
    f = floor()
    assert f.decide(Track(), 100.0, **allowed(awake=False)) is None


def test_he_does_not_dance_mid_conversation():
    """One robot, one head, one speaker. The turn owns him."""
    f = floor()
    assert f.decide(Track(), 100.0, **allowed(busy=True)) is None


def test_he_does_not_dance_while_something_needs_a_person():
    """The one refusal that is not about wear or collisions. He exists so that
    turning to face someone MEANS something, and a dancing robot with a question
    outstanding has spent that signal on a song."""
    f = floor()
    assert f.decide(Track(), 100.0, **allowed(needs_you=True)) is None


def test_an_approval_arriving_mid_song_stops_the_dancing():
    f = floor()
    assert f.decide(Track(), 100.0, **allowed()) is not None
    later = 100.0 + music.GESTURE_GAP_S * 4
    assert f.decide(Track(), later, **allowed(needs_you=True)) is None


def test_no_rolling_hour_ever_exceeds_the_backstop():
    """These are hobby servos in a small head, and the failure is not a crash --
    it is a gearbox getting loose over weeks.

    The property asserted is the one the cap actually claims: no 3600-second
    window contains more than `MAX_MOVES_PER_HOUR` moves. A total over the whole
    run would be the wrong test -- the window is rolling, so the moves come in
    bursts, and a count that happened to pass would be pinning the burst shape
    rather than the guarantee.
    """
    f = floor()
    now = 0.0
    at = []
    for _ in range(400):
        now += music.MIN_GAP_S
        if f.decide(Track(track_id=f"t{int(now)}"), now, **allowed()) is not None:
            at.append(now)
    assert len(at) > music.MAX_MOVES_PER_HOUR, "the run was too short to reach the cap"
    for start in at:
        in_window = [t for t in at if start <= t < start + 3600.0]
        assert len(in_window) <= music.MAX_MOVES_PER_HOUR


def test_the_backstop_window_rolls_rather_than_latching():
    """A cap that never forgave would mean one busy hour stopped him for good."""
    f = floor()
    now = 0.0
    for _ in range(music.MAX_MOVES_PER_HOUR * 3):
        now += music.MIN_GAP_S
        f.decide(Track(track_id=f"t{int(now)}"), now, **allowed())
    assert f.decide(Track(track_id="much-later"), now + 7200.0, **allowed()) is not None


def test_panic_and_look_around_are_never_danced():
    """`panic` means something. `look-around` is what idle motion already does,
    so dancing it produces a robot whose dancing looks like standing still."""
    assert "panic" not in music.DANCES + music.GESTURES
    assert "look-around" not in music.DANCES + music.GESTURES


def test_every_name_is_one_the_driver_can_actually_play():
    import animation

    for name in music.DANCES + music.GESTURES:
        assert animation.lookup(name)


def test_the_same_move_is_not_played_twice_in_a_row():
    f = music.DanceFloor(rng=random.Random(1))
    seen = []
    now = 0.0
    for i in range(12):
        now += music.MIN_GAP_S + 1
        move = f.decide(Track(track_id=f"t{i}"), now, **allowed())
        if move is not None:
            seen.append(move.name)
    assert len(seen) >= 4
    assert all(a != b for a, b in zip(seen, seen[1:]))


# ------------------------------------------------------- the payload slice --


def payload(**over):
    body = {
        "pendingApprovals": [],
        "pendingTotal": 0,
        "agentsRunning": 0,
        "needsYou": False,
        "founderTasks": [],
        "founderTasksTotal": 0,
        "mood": "calm",
        "ts": 1,
    }
    body.update(over)
    return body


def test_now_playing_is_parsed_off_the_payload():
    state = parse_office_state(
        payload(
            nowPlaying={
                "trackId": "t1",
                "title": "Bad Guy",
                "artist": "Billie Eilish",
                "inTheRoom": True,
            }
        )
    )
    assert state is not None
    assert state.now_playing == NowPlaying("t1", "Bad Guy", "Billie Eilish", True)


def test_an_office_with_no_spotify_parses_fine():
    state = parse_office_state(payload())
    assert state is not None
    assert state.now_playing is None


def test_a_malformed_track_does_not_take_the_approvals_down_with_it():
    """The approvals in the same body are what the robot is actually for.
    Losing them because a title arrived as a number would be the worst trade
    available."""
    state = parse_office_state(
        payload(
            nowPlaying={"trackId": "t1", "title": 7, "artist": "x", "inTheRoom": True},
            pendingApprovals=[{"id": "c1", "oneLine": "may I?"}],
            pendingTotal=1,
            needsYou=True,
        )
    )
    assert state is not None
    assert state.now_playing is None
    assert len(state.pending_approvals) == 1


def test_in_the_room_must_be_a_real_boolean():
    """A missing flag would coerce to False, which is safe. A STRING would
    coerce to True and have him dance to headphones."""
    for bad in ("yes", 1, None):
        state = parse_office_state(
            payload(nowPlaying={"trackId": "t", "title": "a", "artist": "b", "inTheRoom": bad})
        )
        assert state is not None
        assert state.now_playing is None
