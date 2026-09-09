"""What the office looks like on his face, when nobody is talking to him.

A port of `bridge/src/posture.ts`, which was written for a device with no life
of its own: the bridge polled the office and asserted a whole pose, because a
held pose was the only expression it had. The character stack changed that, so
what carries over is the part worth keeping -- the DECISION, as one pure
function over a plain object -- and what does not is the pose.

--- Why the pose is dropped ---

The bridge set an absolute yaw and pitch per mood. Doing that here would fight
the idle system: breath, idle motion and gaze drift already own the head, and
they are most of what makes him read as alive. Overriding their rest point to
hold an attitude would trade the liveliness for a statue, which is the opposite
of the point.

`attention` is the one mood whose pose was doing real work -- looking up to
"catch a person's eye from across a desk". A held pose is a poor way to do that
anyway; a person notices a MOVEMENT. So the intent belongs in a one-shot
gesture on entering the mood, not a posture, and that waits on the nod
animation (see PERSONALITY.md). The face and the LEDs land now; the glance is
the animation's first real use.

--- What it owns, and when ---

The face and the LED ring, and **only while STANDBY**. During a conversation the
status owns both -- `LISTENING` already strips idle motion and gaze drift for
exactly this reason -- and an ambient mood that kept asserting itself mid-answer
would be the same two-things-one-axis fight the bridge would have had.

The LEDs are free to take because `STATUS_LEDS[STANDBY]` is dark: an idle robot
has nothing lit, so a mood that lights it amber is adding a signal rather than
overwriting one. That is the whole value of this file in one sentence -- the
ring glows when something is waiting, without anyone asking him.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Why a mood was chosen. Logged, and asserted in the tests -- the reason is
#: the interesting output, because the face is only its rendering.
OFFLINE = "offline"
ATTENTION = "attention"
REVIEW = "review"
BUSY = "busy"
RESTING = "resting"

#: Amber is the office's own "something is not right" signal, reused from the
#: K10's beacon so the fleet says the same thing the same way. Here it means a
#: FAULT -- he cannot see the office.
AMBER = (255, 140, 0)
#: The PA's own colour, for something waiting on a person right now.
PA_PINK = (255, 102, 153)
#: The same meaning at lower urgency: a queue rather than a summons. Half the
#: pink, which reads as the same signal turned down.
#:
#: This exists because dropping the bridge's poses collapsed two moods into one
#: appearance. The bridge separated `offline` from `review` by head angle alone
#: (pitch 38 and a yaw offset, against pitch 52 and centred) -- both wore
#: `thinking` and both were amber. With the pose gone they became
#: indistinguishable, which makes an ambient signal worse than none: a fault
#: that looks like an ordinary queue is a fault nobody investigates.
#:
#: Caught by porting the bridge's own "gives every reason a distinguishable
#: posture" test, which is the argument for porting a test you think you have
#: already satisfied.
PA_PINK_DIM = (128, 51, 77)
#: Agents at work.
WORKING = (0, 90, 130)
OFF = (0, 0, 0)


@dataclass(frozen=True)
class Mood:
    """The ambient reading: a face, a ring colour, and why."""

    face: str
    leds: tuple[int, int, int]
    reason: str


def mood_for(state) -> Mood:
    """Choose an ambient mood for the office as it currently is.

    `None` means the office could not be read -- a failed fetch, a bad status,
    or a payload that did not parse. Deliberately the *same* input as an
    outage, because from the desk they are the same thing: he does not know
    what the office is doing, and should say so rather than hold a stale mood.

    Precedence is by urgency, not by field order:

        offline > attention > review > busy > resting

    `attention` outranks `busy` because a person being needed matters more than
    machines being busy. `review` outranks `busy` for the same reason but sits
    below `attention`, because a blocked board item is not as loud as an
    approval waiting on the desk right now.
    """
    if state is None:
        return Mood(face="thinking", leds=AMBER, reason=OFFLINE)

    if state.needs_you or state.pending_total > 0:
        return Mood(face="surprised", leds=PA_PINK, reason=ATTENTION)

    if state.founder_tasks_total > 0:
        # Amber is reserved for a fault. Board work waiting is the same family
        # as `attention` -- something for a person -- turned down.
        return Mood(face="thinking", leds=PA_PINK_DIM, reason=REVIEW)

    if state.agents_running > 0 or state.mood == "busy":
        return Mood(face="idle", leds=WORKING, reason=BUSY)

    # Dozing. The bridge carried this as a dipped head, and noted that
    # half-closed eyes were unreachable because "eye weight is driven by the
    # firmware's own blink machine and there is no tool for it".
    #
    # That is no longer true -- `set_feature` carries weight. But overriding
    # that axis is what defeats the blink animation, so heavy eyelids would
    # cost blinking entirely. A dozing robot that does not blink may well be
    # right; it is a choice to make on hardware rather than a freebie to take
    # here, so resting stays a dark ring and an ordinary face.
    return Mood(face="idle", leds=OFF, reason=RESTING)
