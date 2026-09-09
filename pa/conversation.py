"""One exchange: listen, think, answer, act.

Two ways in, one exchange. A tap records for a window and then answers
(`turn`); the wake word arrives with the words already captured on the device
and answers directly (`turn_on_transcript`). Everything after the transcript --
the busy rule, the thinking face, the speaking status, standby in the
`finally` -- is deliberately shared, because two nearly-identical paths are two
places for a fix to be applied to only one.

Orchestration only. Everything it touches is injected, so the whole flow is
testable with no robot, no model and no network -- which matters because the
real thing takes about five seconds per turn and involves three processes.

--- Why speaking is a subprocess ---

`piper-tts` lives in `pa/.venv`. `live.py` runs on the GATEWAY's interpreter,
because `mcp` lives there and nowhere else. Those are two different
virtualenvs on purpose, and the Makefile says why installing our dependencies
into the gateway's would be wrong.

So the voice is invoked the way a person would invoke it -- `pa/speech.py` as a
command, with its own interpreter. That interface already exists, is already
tested, and already refuses to print the token. The cost is loading the 60 MB
Piper model per utterance, which is unmeasured on this hardware; if it turns
out to hurt, the fix is a long-lived voice worker rather than merging the
virtualenvs.

--- Why one turn at a time ---

A second tap while he is already listening is almost always someone tapping
again because nothing appeared to happen. Starting a second overlapping turn
would have two `listen` calls fighting for one microphone and two voices
talking over each other, so a tap during a turn is ignored and logged rather
than queued.

--- The status transitions are not decoration ---

`set_status` is what makes him stop glancing around while being spoken to --
LISTENING removes idle motion and gaze drift, SPEAKING adds the head movement
that goes with talking, STANDBY brings idle behaviour back. Getting these
right is most of what makes the exchange feel like a conversation rather than
a robot reciting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import driver as driver_mod

logger = logging.getLogger(__name__)

#: How long to record when he is asked something -- the TAP path only. The
#: wake word uses the device's own VAD instead, which ends the capture when the
#: speaker stops (`hook.py`), and a tap cannot: nothing in the gateway's tool
#: table makes the device start listening, so tap-to-talk has only the fixed
#: window to call. Five seconds measured about 2.3 s of transcription on top
#: with the `base` model.
DEFAULT_LISTEN_MS = 5000

#: What he says when he heard nothing at all. Silence would be indistinguishable
#: from a crash, and the tap already made him look like he was listening.
NOTHING_HEARD = "I didn't catch that."

#: What he says when the model could not be reached. Not the exception text --
#: that is for the log.
BRAIN_UNREACHABLE = "I can't think straight just now."


@dataclass
class TurnResult:
    """What happened, for the log and the tests."""

    transcript: str | None = None
    spoken: str | None = None
    face: str | None = None
    actions: tuple = ()
    skipped: str | None = None


class Conversation:
    """Drives one exchange at a time.

    `listen` returns a transcript or None. `say` speaks a line and returns when
    it has been handed to the device. `read_office` returns the state or None.
    """

    def __init__(
        self,
        character: Any,
        brain: Any,
        listen: Callable[[int], Awaitable[str | None]],
        say: Callable[[str], Awaitable[bool]],
        read_office: Callable[[], Awaitable[Any]],
        listen_ms: int = DEFAULT_LISTEN_MS,
    ) -> None:
        self._character = character
        self._brain = brain
        self._listen = listen
        self._say = say
        self._read_office = read_office
        self.listen_ms = listen_ms
        self.busy = False
        self.turns = 0

    async def turn(self, now: float = 0.0) -> TurnResult:
        """A turn we drive: record for a window, then answer."""
        return await self._exchange(lambda: self._turn(now), "tap")

    async def turn_on_transcript(
        self, transcript: str | None, now: float = 0.0
    ) -> TurnResult:
        """A turn the DEVICE drove: the words are already in hand.

        The wake-word path captures on the device, ends on the device's own
        VAD, and arrives here as a finished transcript -- so there is nothing
        to listen for, and no LISTENING status either. He stopped listening
        before we knew he had started, and showing a listening face after the
        fact would be a lie about what he is doing.

        Everything downstream is deliberately identical to a tap: same busy
        rule, same thinking face, same STANDBY in the `finally`. Two ways in,
        one exchange -- otherwise the two paths drift and only one gets fixed.
        """
        return await self._exchange(
            lambda: self._respond(transcript, now), "capture"
        )

    def claim(self, source: str) -> bool:
        """Take the one-at-a-time flag, or report that it is already held.

        Public because a conversation turn is not the only thing that makes him
        speak: the voice-settings panel previews a line through `hook.py`, and
        that has to hold the same flag rather than merely reading it. The first
        version of the preview CHECKED `busy` and never set it, so a tap could
        start a turn while the preview was mid-sentence -- a refusal in one
        direction only, which is not a lock.
        """
        if self.busy:
            logger.info("%s ignored: already mid-conversation", source)
            return False
        self.busy = True
        return True

    def release(self) -> None:
        """Give the flag back. Safe to call when it is not held."""
        self.busy = False

    async def _exchange(self, body, source: str) -> TurnResult:
        # Almost always someone tapping again because nothing appeared to
        # happen. Two `listen` calls would fight for one microphone.
        #
        # It guards the wake word too, and there it is doing more than
        # politeness: `listen()` and a device-driven capture share one recording
        # slot in the gateway, and it drops whichever arrives second. Refusing
        # here means we know that happened.
        #
        # Through `claim` rather than the flag directly, so the panel's preview
        # and a conversation turn contend for one lock with one implementation.
        if not self.claim(source):
            return TurnResult(skipped="busy")

        try:
            return await body()
        finally:
            self.release()
            # Back to standby whatever happened, or he stays frozen mid-thought
            # with idle motion switched off.
            self._character.set_status(driver_mod.STANDBY)
            self.turns += 1

    async def _turn(self, now: float) -> TurnResult:
        self._character.set_status(driver_mod.LISTENING)
        transcript = await self._listen(self.listen_ms)
        return await self._respond(transcript, now)

    async def _respond(self, transcript: str | None, now: float) -> TurnResult:
        if not transcript or not transcript.strip():
            self._character.set_status(driver_mod.SPEAKING)
            await self._say(NOTHING_HEARD)
            return TurnResult(transcript=transcript, spoken=NOTHING_HEARD)

        transcript = transcript.strip()
        logger.info("heard: %s", transcript)

        # Thinking has a face, and it is the one movement that makes a pause
        # read as work rather than as a fault.
        self._character.set_emotion("doubtful")

        # Read the office fresh rather than reusing the poll: the answer is
        # about what is waiting NOW, and a turn takes seconds.
        state = await self._read_office()

        try:
            reply = await self._brain.respond(transcript, state)
        except Exception as exc:  # noqa: BLE001 - a failed turn must still speak
            logger.warning("brain failed: %s", exc)
            self._character.set_status(driver_mod.SPEAKING)
            await self._say(BRAIN_UNREACHABLE)
            return TurnResult(transcript=transcript, spoken=BRAIN_UNREACHABLE)

        if reply.face:
            self._character.set_face(reply.face)

        self._character.set_status(driver_mod.SPEAKING)
        spoken = reply.speech or NOTHING_HEARD
        await self._say(spoken)

        for action in reply.actions:
            logger.info(
                "action %s(%s) -> %s %s",
                action.name, action.arguments, action.outcome, action.detail,
            )

        return TurnResult(
            transcript=transcript,
            spoken=spoken,
            face=reply.face,
            actions=reply.actions,
        )
