"""Tests for one exchange, with everything injected.

The real turn takes about five seconds and spans three processes, so the parts
worth pinning here are the ones that would be tedious to discover at a desk:
the status transitions, and that something always comes out of the speaker.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain  # noqa: E402
import conversation as conv  # noqa: E402
import driver as driver_mod  # noqa: E402
import office  # noqa: E402

STATE = office.parse_office_state(
    {
        "pendingApprovals": [{"id": "c1", "oneLine": "deploy"}],
        "pendingTotal": 1, "agentsRunning": 0, "needsYou": True,
        "founderTasks": [], "founderTasksTotal": 0, "mood": "attention", "ts": 1,
    }
)


class FakeCharacter:
    """Records the status and face changes, which are the interesting part."""

    def __init__(self):
        self.statuses: list[str] = []
        self.emotions: list[str] = []
        self.faces: list[str] = []

    def set_status(self, status):
        self.statuses.append(status)

    def set_emotion(self, emotion):
        self.emotions.append(emotion)
        return emotion

    def set_face(self, face):
        self.faces.append(face)
        return True


class FakeBrain:
    def __init__(self, reply=None, raises=None):
        self.reply = reply or brain.Reply(speech="Two things are waiting.")
        self.raises = raises
        self.seen: list[tuple] = []

    async def respond(self, transcript, state):
        self.seen.append((transcript, state))
        if self.raises:
            raise self.raises
        return self.reply


def build(transcript="what's waiting?", reply=None, raises=None, state=STATE):
    character = FakeCharacter()
    spoken: list[str] = []

    async def listen(ms):
        return transcript

    async def say(text):
        spoken.append(text)
        return True

    async def read_office():
        return state

    conversation = conv.Conversation(
        character=character,
        brain=FakeBrain(reply, raises),
        listen=listen,
        say=say,
        read_office=read_office,
    )
    return conversation, character, spoken


def test_a_turn_listens_thinks_then_speaks_and_returns_to_standby():
    """The status transitions are what make him stop glancing around while
    being spoken to, and start again afterwards."""
    conversation, character, spoken = build()
    result = asyncio.run(conversation.turn())

    assert character.statuses == [
        driver_mod.LISTENING,
        driver_mod.SPEAKING,
        driver_mod.STANDBY,
    ]
    assert spoken == ["Two things are waiting."]
    assert result.transcript == "what's waiting?"


def test_he_wears_a_thinking_face_while_the_model_works():
    """The one movement that makes a pause read as work rather than a fault."""
    conversation, character, _ = build()
    asyncio.run(conversation.turn())
    assert "doubtful" in character.emotions


def test_hearing_nothing_still_says_something():
    """Silence is indistinguishable from a crash, and the tap already made him
    look like he was listening."""
    conversation, character, spoken = build(transcript="")
    asyncio.run(conversation.turn())
    assert spoken == [conv.NOTHING_HEARD]
    assert character.statuses[-1] == driver_mod.STANDBY


def test_whitespace_only_counts_as_nothing_heard():
    _, _, spoken = build(transcript="   \n ")
    conversation, character, spoken = build(transcript="   \n ")
    asyncio.run(conversation.turn())
    assert spoken == [conv.NOTHING_HEARD]


def test_a_failing_brain_still_speaks_and_still_recovers():
    """A turn that ends in silence with idle motion switched off leaves him
    frozen mid-thought."""
    conversation, character, spoken = build(raises=RuntimeError("api down"))
    asyncio.run(conversation.turn())
    assert spoken == [conv.BRAIN_UNREACHABLE]
    assert character.statuses[-1] == driver_mod.STANDBY


def test_the_face_the_brain_chose_is_applied():
    reply = brain.Reply(speech="Bad news.", face="sad")
    conversation, character, _ = build(reply=reply)
    asyncio.run(conversation.turn())
    assert character.faces == ["sad"]


def test_a_second_tap_mid_turn_is_ignored_not_queued():
    """Two `listen` calls would fight for one microphone and two voices would
    talk over each other."""
    character = FakeCharacter()
    started = asyncio.Event()
    release = asyncio.Event()

    async def listen(ms):
        started.set()
        await release.wait()
        return "hello"

    async def say(text):
        return True

    async def read_office():
        return STATE

    conversation = conv.Conversation(
        character=character, brain=FakeBrain(), listen=listen,
        say=say, read_office=read_office,
    )

    async def scenario():
        first = asyncio.create_task(conversation.turn())
        await started.wait()
        second = await conversation.turn()       # while the first is listening
        release.set()
        return second, await first

    second, first = asyncio.run(scenario())
    assert second.skipped == "busy"
    assert first.transcript == "hello"
    assert conversation.turns == 1


def test_the_office_is_read_fresh_for_each_turn():
    """The answer is about what is waiting NOW, and a turn takes seconds."""
    conversation, _, _ = build()
    asyncio.run(conversation.turn())
    assert conversation._brain.seen[0][1] is STATE


def test_an_unreachable_office_still_gets_a_turn():
    """He should say he doesn't know, not fail to answer at all."""
    conversation, character, spoken = build(state=None)
    asyncio.run(conversation.turn())
    assert conversation._brain.seen[0][1] is None
    assert spoken


def test_busy_clears_even_when_the_turn_raises():
    """Otherwise one bad turn wedges tap-to-talk until a restart."""
    character = FakeCharacter()

    async def listen(ms):
        raise RuntimeError("gateway died")

    async def say(text):
        return True

    async def read_office():
        return STATE

    conversation = conv.Conversation(
        character=character, brain=FakeBrain(), listen=listen,
        say=say, read_office=read_office,
    )
    try:
        asyncio.run(conversation.turn())
    except RuntimeError:
        pass
    assert conversation.busy is False
    assert character.statuses[-1] == driver_mod.STANDBY


# --- the wake word's way in -------------------------------------------------
#
# The device captured the audio and the gateway delivered a transcript, so
# there is nothing to listen for. What matters is that everything downstream is
# the same exchange -- one busy rule, one thinking face, one standby -- because
# two nearly-identical paths are two places for a fix to reach only one.


def test_a_delivered_transcript_answers_without_listening():
    """No LISTENING status: he stopped listening before we knew he had
    started, and showing a listening face after the fact would be a lie about
    what he is doing."""
    conversation, character, spoken = build()

    listened = []
    original = conversation._listen

    async def tripwire(ms):
        listened.append(ms)
        return await original(ms)

    conversation._listen = tripwire
    result = asyncio.run(conversation.turn_on_transcript("what's waiting?"))

    assert listened == [], "the wake-word path must not record again"
    assert character.statuses == [driver_mod.SPEAKING, driver_mod.STANDBY]
    assert spoken == ["Two things are waiting."]
    assert result.transcript == "what's waiting?"


def test_a_delivered_transcript_gets_the_same_thinking_face():
    conversation, character, _ = build()
    asyncio.run(conversation.turn_on_transcript("what's waiting?"))
    assert "doubtful" in character.emotions


def test_a_delivered_transcript_reaches_the_brain_with_fresh_office_state():
    """Read fresh rather than reused: a turn takes seconds and the answer is
    about what is waiting now."""
    conversation, _, _ = build()
    asyncio.run(conversation.turn_on_transcript("  what's waiting?  "))
    assert conversation._brain.seen[0][0] == "what's waiting?"
    assert conversation._brain.seen[0][1] is STATE


def test_an_empty_delivered_transcript_still_says_something():
    """He woke up. Going quiet after that is indistinguishable from a crash,
    which is the same reason the tap path speaks."""
    conversation, _, spoken = build()
    assert asyncio.run(conversation.turn_on_transcript("")).spoken == conv.NOTHING_HEARD
    assert spoken == [conv.NOTHING_HEARD]


def test_none_from_a_capture_is_treated_as_nothing_heard():
    conversation, _, spoken = build()
    asyncio.run(conversation.turn_on_transcript(None))
    assert spoken == [conv.NOTHING_HEARD]


def test_a_wake_word_during_a_tap_turn_is_refused():
    """`listen()` and a device-driven capture share one recording slot in the
    gateway, which drops whichever arrives second. Refusing here means we know
    that happened rather than wondering where the audio went."""
    conversation, _, spoken = build()
    conversation.busy = True
    result = asyncio.run(conversation.turn_on_transcript("hello"))
    assert result.skipped == "busy"
    assert spoken == []


def test_a_tap_during_a_wake_word_turn_is_refused():
    """The same rule in the other direction, which is the point of sharing it."""
    conversation, _, spoken = build()
    conversation.busy = True
    assert asyncio.run(conversation.turn()).skipped == "busy"
    assert spoken == []


def test_the_busy_flag_is_cleared_after_a_delivered_turn():
    """Otherwise the first wake word is also the last."""
    conversation, _, _ = build()
    asyncio.run(conversation.turn_on_transcript("hello"))
    assert conversation.busy is False
    asyncio.run(conversation.turn_on_transcript("again"))
    assert conversation.turns == 2


def test_a_brain_failure_on_a_delivered_turn_still_speaks_and_returns_to_standby():
    conversation, character, spoken = build(raises=RuntimeError("no network"))
    result = asyncio.run(conversation.turn_on_transcript("hello"))
    assert spoken == [conv.BRAIN_UNREACHABLE]
    assert result.spoken == conv.BRAIN_UNREACHABLE
    assert character.statuses[-1] == driver_mod.STANDBY


# --- the voice the office asked for -----------------------------------------


def test_the_overrides_the_office_set_reach_the_voices_command_line():
    """End to end through `speak_line`, by intercepting the subprocess.

    The first version of this only poked `VoiceOverrides` and asserted its own
    `flags()` -- so the entire wiring in `live.py` could have been deleted with
    the suite green. What matters is that the flags land on the argv of the
    process that actually speaks."""
    import asyncio
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live
    import voice_settings

    seen = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        return FakeProcess()

    voice = live.VoiceOverrides()
    voice.take({"pitch": 1.4, "robot": 0.2})

    original = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_exec
    try:
        # Exactly how run_once wires it: the closure reads `current` at speak
        # time, not when it was built.
        say = lambda text: live.speak_line(text, settings=voice.current)  # noqa: E731
        assert asyncio.run(say("hello")) is True
    finally:
        asyncio.create_subprocess_exec = original

    argv = [str(a) for a in seen["argv"]]
    assert "--pitch" in argv and argv[argv.index("--pitch") + 1] == "1.4"
    assert "--robot" in argv and argv[argv.index("--robot") + 1] == "0.2"
    assert argv[-1] == "hello", "the text must be last, after the flags"
    assert "--variation" not in argv, "an unset override must not be sent"
    assert voice_settings  # names what built those flags


def test_a_later_poll_with_no_opinion_does_not_reset_the_voice():
    """`None` means the office said nothing. An office too old to know about
    the panel must not silently change how he sounds."""
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live

    voice = live.VoiceOverrides()
    voice.take({"pitch": 1.4})
    voice.take(None)
    assert voice.current.pitch == 1.4


def test_a_poll_with_an_opinion_replaces_the_whole_set():
    """So a slider returned to its default clears that override rather than
    leaving it stuck at the last non-default value."""
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live
    import voice_settings

    voice = live.VoiceOverrides()
    voice.take({"pitch": 1.4, "robot": 0.2})
    voice.take({"pitch": 1.1})
    assert voice.current == voice_settings.VoiceSettings(pitch=1.1)


def test_a_preview_and_a_turn_contend_for_one_lock():
    """The collision the preview's check exists to prevent, and which the first
    version left open: it READ `busy` without taking it, so a tap could start a
    turn while the preview was mid-sentence."""
    conversation, _, spoken = build()

    assert conversation.claim("preview") is True
    assert conversation.busy is True
    # A turn now finds it held, which is the half that already worked.
    assert asyncio.run(conversation.turn()).skipped == "busy"
    assert spoken == []
    # And a second preview finds it held, which is the half that did not.
    assert conversation.claim("preview") is False

    conversation.release()
    assert conversation.claim("preview") is True


def test_a_turn_holds_the_lock_against_a_preview():
    """The other direction, which is what makes it a lock rather than a
    courtesy."""
    conversation, _, _ = build()
    conversation.busy = True
    assert conversation.claim("preview") is False


def test_preview_once_holds_the_lock_while_it_speaks():
    """The wiring, not just `Conversation.claim`. While this lived inside a
    closure in `run_once`, reverting it to a read-only `busy` check -- the exact
    bug it exists to fix -- broke no test at all."""
    import asyncio
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live
    import voice_settings

    conversation, _, _ = build()
    held = {}

    async def fake_speak(text, character=None, settings=None):
        held["busy_while_speaking"] = conversation.busy
        held["text"] = text
        held["settings"] = settings
        return True

    original = live.speak_line
    live.speak_line = fake_speak
    try:
        ok, detail, busy = asyncio.run(
            live.preview_once(conversation, voice_settings.VoiceSettings(pitch=1.3))
        )
    finally:
        live.speak_line = original

    assert ok is True and busy is False and detail == "spoke"
    assert held["busy_while_speaking"] is True, "the preview spoke without holding the lock"
    assert held["settings"].pitch == 1.3
    assert held["text"] == live.PREVIEW_LINE
    assert conversation.busy is False, "the lock was not given back"


def test_preview_once_refuses_and_reports_busy_separately_from_failure():
    import asyncio
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live
    import voice_settings

    conversation, _, _ = build()
    conversation.busy = True
    ok, detail, busy = asyncio.run(
        live.preview_once(conversation, voice_settings.VoiceSettings())
    )
    assert (ok, busy) == (False, True)
    assert "mid-conversation" in detail


def test_preview_once_reports_a_voice_failure_as_a_failure_not_as_busy():
    """The distinction that keeps the panel from saying "he is busy" when he is
    mute."""
    import asyncio
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live
    import voice_settings

    conversation, _, _ = build()

    async def fake_speak(text, character=None, settings=None):
        return False

    original = live.speak_line
    live.speak_line = fake_speak
    try:
        ok, detail, busy = asyncio.run(
            live.preview_once(conversation, voice_settings.VoiceSettings())
        )
    finally:
        live.speak_line = original

    assert (ok, busy) == (False, False), "a broken voice is not a busy robot"
    assert "voice failed" in detail
    assert conversation.busy is False, "the lock was not given back after a failure"


def test_say_once_speaks_the_line_it_was_given_and_holds_the_lock():
    """The other half of the preview test, and the half `/say` depends on: a
    caller's line must be the line that comes out, under the same lock.

    Worth pinning separately even though `preview_once` now delegates here. If
    `say_once` ever grew a fixed line -- or `/say` were wired to `preview_once`
    by mistake -- every preview test would still pass while the office announced
    "two approvals are waiting" whatever it was asked to say.
    """
    import asyncio
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live
    import voice_settings

    conversation, _, _ = build()
    held = {}

    async def fake_speak(text, character=None, settings=None):
        held["busy_while_speaking"] = conversation.busy
        held["text"] = text
        return True

    original = live.speak_line
    live.speak_line = fake_speak
    try:
        ok, detail, busy = asyncio.run(
            live.say_once(conversation, "the build is green", voice_settings.VoiceSettings())
        )
    finally:
        live.speak_line = original

    assert (ok, busy, detail) == (True, False, "spoke")
    assert held["text"] == "the build is green"
    assert held["text"] != live.PREVIEW_LINE, "it said the sample line instead"
    assert held["busy_while_speaking"] is True, "it spoke without holding the lock"
    assert conversation.busy is False, "the lock was not given back"


def test_say_once_refuses_while_he_is_mid_conversation():
    """One robot, one speaker, one room. The refusal is the whole reason the
    office asks the character stack to speak rather than speaking itself."""
    import asyncio
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import live
    import voice_settings

    conversation, _, _ = build()
    conversation.busy = True
    ok, detail, busy = asyncio.run(
        live.say_once(conversation, "hello", voice_settings.VoiceSettings())
    )
    assert (ok, busy) == (False, True)
    assert "mid-conversation" in detail
