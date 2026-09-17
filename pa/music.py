"""Dancing to whatever is already playing. The decision, not the doing.

P0 of the office's `docs/design-spotify-dj.md`: the office reports what is on
(`nowPlaying` on the companion payload) and he moves to it. Nothing here talks
to the robot -- choosing the next move is pure and testable, sending it is the
caller's job, the same split `idle.py` and `tracking.py` already use.

--- He does not beat-match, and this is the module that admits it ---

Spotify's `audio-features` endpoint, which carried tempo, has been dead for new
apps since 2024-11-27 with no replacement. There is no BPM to have. So the
office polls every 15 seconds and he plays a keyframe sequence; he is not phase-
locked to anything and could not be.

That reads as dancing anyway, and it is worth being clear about why rather than
hoping: from two feet away, what makes a two-axis head look like it is dancing
is that it MOVES WHILE MUSIC IS ON and CHANGES WHEN THE SONG CHANGES. Nobody
perceives phase in a servo neck. The track-change flourish is therefore the
whole effect, and the periodic bob is what stops a three-minute song looking
like one coincidence.

--- The vocabulary, and the two that are not in it ---

`happy` (4.2s) and `robot` (2.7s) are the dance-shaped sequences, played when
the song changes. `nod` (1.5s) and `glance` (1.9s) punctuate a track that is
still going.

`panic` is excluded: it is 1.1s of alarm and it means something. `look-around`
is excluded for a subtler reason -- it is 7.5s of scanning the room, which is
exactly what `idle.py` already does between actions, so using it here would
produce a robot whose "dancing" is indistinguishable from his standing still.

--- Why the refusals are the interesting part ---

Every rule below is a reason NOT to move, and they matter more than the moves.
The programme's definition of done is Sam looking up because Cubie turned to
face him and said one sentence about something that needs him. A robot that
flails all afternoon spends exactly that, and no amount of charm buys it back.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

#: Played when the song changes. The flourish that carries the whole effect.
DANCES = ("happy", "robot")

#: Played mid-track, so a long song is not one dance and four minutes of nothing.
GESTURES = ("nod", "glance")

#: Shortest gap between any two moves.
#:
#: Sized off the longest thing in the vocabulary (`happy`, 4.2s) with room to
#: spare, so a burst of track changes -- somebody skipping through an album --
#: cannot queue sequences on top of each other. `driver._perform` would survive
#: it (a newer sequence abandons the older one), but the result is a head that
#: twitches between abandoned timelines rather than dancing.
MIN_GAP_S = 20.0

#: How often he punctuates a track that is still playing.
#:
#: Roughly twice a song. Often enough to read as "he is listening", rare enough
#: that it stays a flourish -- and it is a gesture rather than a dance, so a
#: quiet bob rather than a performance.
GESTURE_GAP_S = 60.0

#: A backstop on servo wear, not a behaviour.
#:
#: The rules above already land around 25-35 moves an hour. This exists for the
#: case they do not cover -- a skip loop, a Spotify device flapping in and out of
#: the room -- because these are hobby servos in a small head and the failure is
#: not a crash, it is a gearbox getting loose over weeks. If this cap is ever the
#: thing doing the limiting, something upstream is wrong and the log says so.
MAX_MOVES_PER_HOUR = 60

_HOUR_S = 3600.0


@dataclass(frozen=True)
class Move:
    """What to play, and which kind it is. The caller maps this onto the driver."""

    name: str
    #: "dance" or "gesture" -- the caller logs it, and they read differently.
    kind: str


class DanceFloor:
    """Decides whether he dances, and to what. One instance, held by the caller.

    Stateful on purpose: "has the song changed" and "how long since he last
    moved" are the entire decision, and both need a memory of the last answer.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._last_track: str | None = None
        self._last_move_at: float | None = None
        self._last_name: str | None = None
        #: Monotonic timestamps of recent moves, for the hourly backstop.
        self._recent: list[float] = []

    def decide(
        self,
        playing,  # office.NowPlaying | None
        now: float,
        *,
        awake: bool,
        busy: bool,
        needs_you: bool,
    ) -> Move | None:
        """The next move, or None. Called once per office poll.

        `playing` is duck-typed rather than imported: this module has no reason
        to depend on the office client, and a test should be able to hand it any
        object with the four attributes.
        """
        if playing is None:
            # Nothing is on. Forget the track so that the same song starting
            # again later is a change rather than a continuation -- the music
            # coming back is exactly the moment worth marking.
            self._last_track = None
            return None

        if not playing.in_the_room:
            # Playing on headphones, or on a phone somewhere else. Still music
            # to Spotify; not music to anyone here. Dancing to it would be a bug
            # nobody is in the room to notice.
            self._last_track = playing.track_id
            return None

        if not awake:
            # He is dozing. M5's sleepy modifier stops idle motion on purpose,
            # and a dance would undo it more rudely than the idle it replaced.
            return None

        if busy:
            # Mid-conversation. One robot, one head, one speaker: the turn owns
            # him, and this is the same lock `say` refuses under rather than
            # queues behind.
            return None

        if needs_you:
            # An approval is waiting. This is the one that is not about wear or
            # collisions: the robot exists so that his turning to face someone
            # MEANS something, and a dancing robot with a question outstanding
            # has spent that signal on a song.
            return None

        self._forget_old(now)
        if len(self._recent) >= MAX_MOVES_PER_HOUR:
            return None

        changed = playing.track_id != self._last_track
        since = None if self._last_move_at is None else now - self._last_move_at

        if changed and (since is None or since >= MIN_GAP_S):
            self._last_track = playing.track_id
            return self._take(now, DANCES, "dance")

        # The track id is recorded even when the gap refuses the move, so a
        # skipped-past song does not stay "new" and fire the moment the gap
        # clears -- that would be him dancing to a track that is already over.
        self._last_track = playing.track_id

        if since is None or since >= GESTURE_GAP_S:
            return self._take(now, GESTURES, "gesture")

        return None

    def _take(self, now: float, pool: tuple[str, ...], kind: str) -> Move:
        """Pick from `pool`, avoiding an immediate repeat, and record the move."""
        choices = [n for n in pool if n != self._last_name] or list(pool)
        name = self._rng.choice(choices)
        self._last_name = name
        self._last_move_at = now
        self._recent.append(now)
        return Move(name=name, kind=kind)

    def _forget_old(self, now: float) -> None:
        """Drop moves older than an hour, so the backstop is a rolling window."""
        cutoff = now - _HOUR_S
        self._recent = [t for t in self._recent if t >= cutoff]
