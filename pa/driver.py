"""The character driver: which modifiers run when, and what an emotion means.

Ported from `hal/board/stackchan_display.cc` in M5's tree, which is the most
useful file in it for us: it is M5 solving *this exact problem* -- wiring their
avatar and modifier stack to xiaozhi-esp32's `SetEmotion` / `SetStatus` /
`SetChatMessage` display interface. Our device runs the same xiaozhi firmware,
so their integration is the reference rather than a rough analogy.

Three pieces, all theirs:

  - a **standing set** of modifiers installed once and never removed: breath,
    head-pet, and the tilt reaction.
  - **`set_status`**, which adds and removes the situational ones. Idle motion
    and idle expression exist only while standing by; speaking exists only
    while speaking. This is the part that makes him settle down when spoken to.
  - **`set_emotion`**, which maps an emotion word onto a face.

--- Why idle motion is added and removed rather than paused ---

M5's own choice, and worth keeping: a robot that keeps glancing around while
you are talking to it reads as not listening. `SetStatus(LISTENING)` removes
idle motion entirely and `SetStatus(STANDBY)` puts it back.

--- Where our six faces do not cover xiaozhi's emotions ---

xiaozhi sends emotion words; our board exposes six faces, which the firmware
maps onto M5's six Emotions. Two of those mappings are lossy, and it is better
to say so here than to have it discovered on a desk:

  - **angry has nowhere to go.** M5's renderer has `Emotion::Angry`, but this
    board's face list never mapped anything to it -- `apply-live-avatar-step1.sh`
    says as much ("reachable only once the office has something that should
    make Cubie cross"). `sad` is the least wrong of the six.
  - **sleepy arrives wearing a blush.** Our `embarrassed` is `Emotion::Sleepy`
    PLUS the shy decorator, because that is what made `embarrassed` read right.
    So routing sleepy there gets the heavy eyelids and an unwanted blush. The
    fix is a seventh face in firmware, not a different mapping here.
"""

from __future__ import annotations

import logging
import random

import modifiers
import mood as mood_mod
import units
from chan import FACES, Chan
from tracking import REST_PITCH, REST_YAW, Pose

logger = logging.getLogger(__name__)

#: The three statuses M5 branches on, from `Lang::Strings`. Anything else is
#: shown in the speech bubble, which is their fallback and a genuinely useful
#: one -- "Connecting...", "Updating..." and the like end up on his face.
LISTENING = "listening"
STANDBY = "standby"
SPEAKING = "speaking"

#: M5's LED colours per status: green while listening, blue while speaking,
#: dark while idle. Their `setRgbColor(w, r, g, b)` puts 50 on one channel.
STATUS_LEDS = {
    LISTENING: (0, 50, 0),
    SPEAKING: (0, 0, 50),
    STANDBY: (0, 0, 0),
}

#: xiaozhi's emotion words onto our six faces. M5's own table, retargeted --
#: see the module docstring for the two lossy rows.
EMOTION_FACES = {
    "neutral": "idle",
    "happy": "happy",
    "laughing": "happy",
    "angry": "sad",
    "sad": "sad",
    "crying": "sad",
    "sleepy": "embarrassed",
    "doubtful": "thinking",
    # Not in M5's table, but our board has the face and the assistant will
    # reach for the word.
    "surprised": "surprised",
    "thinking": "thinking",
    "embarrassed": "embarrassed",
}

#: M5's keyword reactions, from `app_avatar.cpp`'s text-message handler.
#: Two entries, and both mean the same thing -- being greeted, or being liked.
KEYWORD_FACES = (
    (("hello", "hi"), "happy", 2.0),
    (("love",), "happy", 2.0),
)

#: How long a chat message stays in the speech bubble. M5's 6000 ms.
MESSAGE_BUBBLE_S = 6.0

#: M5 pairs every message with two seconds of speaking animation.
MESSAGE_SPEAKING_S = 2.0


def contains_word(text: str, words: tuple[str, ...]) -> bool:
    """M5's `contains_word`, as a whole-word match.

    Whole words on purpose: their helper is named for it, and a substring match
    would make "this" contain "hi" and greet the office every time somebody
    said "think".
    """
    lowered = {token.strip(".,!?;:'\"").lower() for token in text.split()}
    return any(word in lowered for word in words)


class CharacterDriver:
    """Runs the modifier stack, and decides which modifiers should exist.

    Holds no clock of its own: `update(now)` is called by whatever loop owns
    the process, so the whole driver is steppable in tests.
    """

    def __init__(
        self,
        chan: Chan,
        idle_motion_level: int = 2,
        rng: random.Random | None = None,
    ) -> None:
        self.chan = chan
        self.idle_motion_level = idle_motion_level
        self._rng = rng or random.Random()

        self.status: str | None = None
        self.sleeping = False
        #: The last office reading, or None until one arrives. Held rather than
        #: applied on receipt, so a poll landing mid-conversation waits for
        #: STANDBY instead of being dropped -- see `set_office_mood`.
        self.office_mood: "mood_mod.Mood | None" = None
        #: The last tick's clock, so an event arriving between ticks has a
        #: sensible `now` to schedule motion against. Events are asynchronous
        #: and the alternative -- making every caller pass a timestamp -- puts
        #: the clock in the wrong place.
        self._now = 0.0

        # The standing set. M5 installs breath, blink, head-pet and the IMU
        # reaction once, at avatar creation, and never removes them. Blink is
        # not among ours because the firmware owns it -- asserted below
        # instead.
        self.breath = chan.add(modifiers.BreathModifier())
        self.head_pet = chan.add(modifiers.HeadPetModifier(rng=self._rng))
        self.tilt = chan.add(modifiers.TiltReactionModifier())

        # Blinking is runtime state on the device and unknown until we say so.
        # This is the assertion that turns it on, and the reason it is here
        # rather than in a script somewhere.
        chan.face.blink_enabled = True

        self.idle_motion: modifiers.IdleMotionModifier | None = None
        self.idle_expression: modifiers.IdleExpressionModifier | None = None
        self.speaking: modifiers.SpeakingModifier | None = None

    # --------------------------------------------------------------- wake --
    #: How fast he returns to rest at startup. Slower than every idle speed
    #: (25-240 dps) on purpose: this is the one movement a person watches from
    #: cold, and a robot that snaps to attention on power-up reads as a fault
    #: where one that settles reads as waking.
    WAKE_SPEED_DPS = 40

    def wake(self, now: float, actual: Pose | None = None) -> None:
        """Sync to the head's real pose, then settle to rest.

        Two steps, and the order is the point.

        M5's boot does the first half -- `Servo::init()` teleports its state to
        `getCurrentAngle()` -- and then deliberately does NOT do the second:
        it disables torque and leaves the head limp. That suits a toy on a
        shelf. This is a desk assistant that should hold a known pose, so it
        settles to rest afterwards.

        What syncing first buys is that the settle is a MOVE rather than a
        snap: it starts from where the head actually is, at a speed we chose,
        and `is_moving()` is honest about how long it will take -- so idle
        motion defers instead of firing a competing target into the middle of
        it.

        `actual` is None when the read failed, which the firmware documents as
        a real mode (a transient ReadPos failure returns yaw/pitch null). Then
        we fall back to assuming rest, which is what the code did before this
        existed -- no worse, and it says so in the log.
        """
        if actual is not None:
            self.chan.adopt_pose(actual)
            logger.info(
                "woke at yaw=%.0f pitch=%.0f, settling to rest",
                actual.yaw, actual.pitch,
            )
        else:
            logger.warning(
                "could not read the head's angles; assuming it is at rest. "
                "The first relative idle move may start from the wrong place."
            )
        self.chan.motion.move_with_speed(REST_YAW, REST_PITCH, self.WAKE_SPEED_DPS, now)

    # ------------------------------------------------------------- status --
    def set_status(self, status: str) -> None:
        """M5's `SetStatus`. Adds and removes the situational modifiers."""
        self.status = status
        is_idle = False

        if status == LISTENING:
            self._stop_speaking()
        elif status == STANDBY:
            self._stop_speaking()
            is_idle = True
        elif status == SPEAKING:
            if self.speaking is None:
                self.speaking = self.chan.add(modifiers.SpeakingModifier(rng=self._rng))
        else:
            # M5's fallback: an unrecognised status is not an error, it is
            # something to show. "Connecting...", "Updating...".
            self.chan.face.speech = status

        led = STATUS_LEDS.get(status)
        if led is not None:
            # A recognised status is a state, not a message: clear any bubble a
            # previous unrecognised status left behind, or "Connecting..." stays
            # on his face for good.
            self.chan.face.speech = ""
            self.chan.face.leds = led

        if is_idle:
            # Handing the ring and the face to the office, now that the
            # conversation has finished with them. Only here: while listening or
            # speaking the status owns both, and a mood asserting itself
            # mid-answer would be two things driving one axis.
            self._apply_office_mood()

        if is_idle:
            self._start_idle()
        else:
            self._stop_idle()

        # M5 clears the sleep bubble on any status change.
        if self.sleeping:
            self.chan.face.speech = ""
            self.sleeping = False

    def _stop_speaking(self) -> None:
        if self.speaking is not None:
            self.chan.remove(self.speaking)
            self.chan.face.mouth.weight = None
            self.speaking = None

    def _start_idle(self) -> None:
        if self.idle_motion is not None or self.idle_expression is not None:
            return
        motion = modifiers.IdleMotionModifier.at_level(self.idle_motion_level, self._rng)
        if motion is not None:
            self.idle_motion = self.chan.add(motion)
        # Idle EXPRESSION runs even at motion level 0: the levels are about how
        # much the head moves, and a still head with a living face is a
        # coherent thing to want. M5 adds it inside the same branch.
        self.idle_expression = self.chan.add(
            modifiers.IdleExpressionModifier(rng=self._rng)
        )
        logger.info("idle behaviour on (motion level %d)", self.idle_motion_level)

    def _stop_idle(self) -> None:
        if self.idle_motion is not None:
            self.chan.remove(self.idle_motion)
            self.idle_motion = None
        if self.idle_expression is not None:
            self.chan.remove(self.idle_expression)
            self.idle_expression = None

    # ------------------------------------------------------------ emotion --
    def set_emotion(self, emotion: str) -> str:
        """M5's `SetEmotion`. Returns the face actually applied."""
        face = EMOTION_FACES.get((emotion or "").lower())
        if face is None:
            logger.warning("unknown emotion %r, using idle", emotion)
            face = "idle"
        self.chan.face.face = face

        if (emotion or "").lower() == "sleepy":
            # M5's sleepy is a whole state, not just a face: the bubble, and
            # idle motion stops so he does not doze off while looking around.
            self.chan.face.speech = "Zzz…"
            self.sleeping = True
            self._stop_idle()
            # M5 returns the pitch servo to its default here, slowly (speed 80
            # of their scale). Slowly is the point -- it is a robot settling,
            # not a robot resetting.
            self.chan.motion.move_with_speed(
                REST_YAW, REST_PITCH, units.m5_speed_to_dps(80), self._now
            )
        return face

    # -------------------------------------------------------- office mood --
    def set_office_mood(self, state) -> str:
        """Take a fresh reading of the office. Returns the reason chosen.

        Stored whatever the status is, applied only while STANDBY -- so a
        reading that arrives mid-conversation is not lost, it just waits for
        him to finish talking. Dropping it instead would mean the ring stayed
        wrong until the next poll, which is the sort of small staleness that
        makes an ambient signal untrustworthy.
        """
        mood = mood_mod.mood_for(state)
        changed = mood != self.office_mood
        self.office_mood = mood
        if changed:
            logger.info("office mood: %s (face %s)", mood.reason, mood.face)
        if self.status == STANDBY:
            self._apply_office_mood()
        return mood.reason

    def _apply_office_mood(self) -> None:
        """Put the stored mood on his face and ring. STANDBY only.

        Guarded on `sleeping` because M5's sleepy is a whole state -- a bubble,
        a settled head, idle motion stopped -- and an office poll landing in the
        middle of it would wipe the bubble's face out from under it.
        """
        mood = self.office_mood
        if mood is None or self.sleeping:
            return
        self.chan.face.face = mood.face
        self.chan.face.leds = mood.leds

    def set_face(self, face: str) -> bool:
        """Wear one of the board's six faces directly.

        The counterpart to `set_emotion`, which maps an emotion WORD onto a
        face. The brain picks from the six by name, so it wants this one --
        routing it through the emotion table would mean inventing an emotion
        word for every face and mapping it straight back.

        Returns False for a name the board does not have, rather than passing
        it to `set_avatar`, which rejects an unknown face -- and a rejected
        call at that end is a silent no-op.
        """
        if face not in FACES:
            logger.warning("unknown face %r ignored", face)
            return False
        self.chan.face.face = face
        return True

    # ------------------------------------------------------------ messages --
    def on_message(self, name: str, content: str) -> None:
        """M5's `onWsTextMessage`: a bubble, some mouth movement, and a reaction
        if the message contains one of the words they watch for."""
        self.chan.add(
            modifiers.TimedSpeechModifier(f"{name} says: {content}", MESSAGE_BUBBLE_S)
        )
        self.chan.add(
            modifiers.SpeakingModifier(MESSAGE_SPEAKING_S, rng=self._rng)
        )
        for words, face, duration in KEYWORD_FACES:
            if contains_word(content, words):
                self.chan.add(modifiers.TimedFaceModifier(face, duration))
                return

    # -------------------------------------------------------------- events --
    def on_touch(self, subtype: str) -> None:
        """A gateway touch event. `notify_config.py` names them tap and stroke.

        Both feed the head-pet reaction: M5's gesture signal carries
        SwipeForward / SwipeBackward / Release, and a tap is the degenerate
        stroke -- their modifier reacts to the swipe and settles on release.
        """
        if subtype in ("stroke", "tap"):
            self.head_pet.on_swipe()
            self.head_pet.on_release()

    def dance(self, name: str) -> modifiers.DanceModifier:
        """Play one of M5's four. Idle motion would fight it, so it stands down."""
        self._stop_idle()
        return self.chan.add(modifiers.DanceModifier.named(name))

    # -------------------------------------------------------------- update --
    def update(self, now: float) -> None:
        self._now = now
        self.chan.update(now)
