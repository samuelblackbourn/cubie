"""M5's modifiers, ported. One class per file in `stackchan/modifiers/`.

M5's STRUCTURE is copied closely, because the structure is the behaviour: each
modifier owns its own timing, reads the shared state, nudges it, and asks to be
destroyed when finished. Their intervals are kept exactly -- the feel of a
StackChan is mostly its cadence.

Their NUMBERS are converted where the units differ (see units.py) and left
alone where they do not. Feature positions are M5's own -100..100 scale on both
sides, so those transcribe directly; servo angles do not.

--- What is not here, and why ---

**No BlinkModifier.** M5 blinks by driving eye weight from a modifier. On this
device blinking is a firmware state machine, so a host blink would be two
things driving one axis over a LAN and the eyes would visibly fight. The port
of M5's blink is therefore `chan.face.blink_enabled`, which the driver asserts
on connect and `DanceModifier` suspends. What is genuinely lost is M5's
`resyncEyeWeights()` -- their blink restoring the exact weight an expression
had set -- and the firmware already handles that case with its own
`active_layer_` rule.

**No TimedSpeechModifier mouth animation.** M5 pairs every speech bubble with a
`SpeakingModifier` that flaps the mouth. Ours flaps from the audio instead.

--- Two upstream bugs worth knowing ---

1. `breath.h` documents its amplitude as "单位像素" (pixels), but
   `move_component` adds the delta to `getPosition()`, which is the -100..100
   normalised value. So the real amplitude is 16 *units* -- about 2.6 px of the
   +/-16 px travel -- not 16 px. Ported as 16 units, matching the code rather
   than its comment. It is a subtle wobble, which is what breathing should be.

2. `Random::getInt(a, b)` is inclusive of `b` (`uniform_int_distribution`), so
   M5's `getInt(0, 100)` has 101 outcomes and their "70%" branch is really
   70/101. Python's `randint` is inclusive too, so the thresholds transcribe
   exactly -- but they are not the round numbers the comments claim.
"""

from __future__ import annotations

import random

import animation
import idle
import units
from chan import LEFT_EYE, MOUTH, RIGHT_EYE, Chan, Modifier
from tracking import PITCH_MAX, PITCH_MIN, REST_PITCH, REST_YAW, YAW_MAX, YAW_MIN, Pose, clamp


# ------------------------------------------------------------------ breath --
class BreathModifier(Modifier):
    """`breath.h`: a slow sine that slides the whole face on y.

    Relative, not absolute, exactly as M5 has it: the offset is applied as a
    delta against the last one applied, so breathing composes with a gaze the
    idle-expression modifier set rather than overwriting it. That is the entire
    reason their version tracks `_last_applied_offset`.
    """

    #: M5's defaults: a 6.6 s cycle, recomputed every 600 ms.
    def __init__(
        self,
        cycle_s: float = 6.6,
        update_interval_s: float = 0.6,
        amplitude: int = 16,
        duration_s: float | None = None,
    ) -> None:
        self.cycle_s = cycle_s
        self.update_interval_s = update_interval_s
        self.amplitude = amplitude
        self.duration_s = duration_s
        self._start: float | None = None
        self._last_update = 0.0
        self._applied = 0

    def update(self, chan: Chan, now: float) -> None:
        import math

        if self._start is None:
            self._start = now
            self._last_update = now - self.update_interval_s
        if self.duration_s is not None and now - self._start >= self.duration_s:
            self._apply(chan, 0)
            self.request_destroy()
            return
        if now - self._last_update < self.update_interval_s:
            return
        self._last_update = now

        phase = ((now - self._start) % self.cycle_s) / self.cycle_s
        self._apply(chan, int(math.sin(phase * 2.0 * math.pi) * self.amplitude))

    def _apply(self, chan: Chan, offset: int) -> None:
        delta = offset - self._applied
        if delta == 0:
            return
        for name in (LEFT_EYE, RIGHT_EYE, MOUTH):
            chan.face.feature(name).y += delta
        self._applied = offset


# -------------------------------------------------------- idle expression --
class IdleExpressionModifier(Modifier):
    """`idle_expression.h`: the face's own idle, distinct from the head's.

    This is the modifier whose absence was most visible: it is the one that
    drifts the gaze. Nothing in this firmware could do that until `set_feature`
    landed, which is why the face looked fixed even while the head moved.
    """

    def __init__(
        self,
        interval_min_s: float = 2.0,
        interval_max_s: float = 6.0,
        rng: random.Random | None = None,
    ) -> None:
        self.interval_min_s = interval_min_s
        self.interval_max_s = interval_max_s
        self._rng = rng or random.Random()
        self._next: float | None = None

    def update(self, chan: Chan, now: float) -> None:
        if self._next is None:
            # M5 starts 500 ms after construction.
            self._next = now + 0.5
        if now < self._next:
            return
        self._perform(chan)
        self._next = now + self._rng.uniform(self.interval_min_s, self.interval_max_s)

    def _perform(self, chan: Chan) -> None:
        action = self._rng.randint(0, 100)
        if action < 70:
            # Gaze drift, with the mouth following a little. M5's ranges.
            chan.face.set_gaze(self._rng.randint(-20, 20), self._rng.randint(-15, 15))
            chan.face.mouth.x = 0
            chan.face.mouth.y = self._rng.randint(0, 10)
        elif action < 80:
            # A crooked mouth. M5 expresses a negative angle as 3600+angle,
            # because Element::setRotation clamps to 0..3600.
            rotation = self._rng.randint(-30, 30)
            chan.face.mouth.rotation = 3600 + rotation if rotation < 0 else rotation
        else:
            self._reset(chan)

    def _reset(self, chan: Chan) -> None:
        """M5's `reset_to_neutral`, with one deliberate difference.

        They set eye size and mouth weight to 0 because they own those axes.
        We RELEASE them (None) instead, so the firmware's per-face resting
        mouth and the `surprised` eye size stand again. Setting 0 here would
        flatten every expression each time this action came up -- roughly every
        fifth idle beat.
        """
        chan.face.set_gaze(0, 0)
        chan.face.mouth.x = 0
        chan.face.mouth.y = 0
        chan.face.mouth.rotation = 0
        chan.face.mouth.weight = None
        chan.face.left_eye.size = None
        chan.face.right_eye.size = None


# -------------------------------------------------------------- idle motion --
class IdleMotionModifier(Modifier):
    """`idle_motion.h`: the head looking around.

    The action set and its weights already live in `idle.py`, ported earlier
    with M5's structure and our angles. This is the missing half -- the thing
    that actually runs it on a clock, which is why the head has not been
    looking around: `idle.py` was written and never wired to anything.

    `interval_min_s` / `interval_max_s` carry M5's four levels, from
    `CreateIdleMotionModifier`: level 1 is (8, 12), level 2 -- the default --
    is (4, 8), level 3 is (2, 4), and level 0 means this modifier is simply not
    added.
    """

    LEVELS = {1: (8.0, 12.0), 2: (4.0, 8.0), 3: (2.0, 4.0)}

    def __init__(
        self,
        interval_min_s: float = idle.INTERVAL_MIN_S,
        interval_max_s: float = idle.INTERVAL_MAX_S,
        rng: random.Random | None = None,
    ) -> None:
        self.interval_min_s = interval_min_s
        self.interval_max_s = interval_max_s
        self._rng = rng or random.Random()
        self._next: float | None = None
        self.paused = False
        #: The last action chosen, for logs and tests.
        self.last_kind: str | None = None

    @classmethod
    def at_level(cls, level: int, rng: random.Random | None = None) -> "IdleMotionModifier | None":
        """M5's levels. Level 0 is not a slow modifier, it is no modifier."""
        if level not in cls.LEVELS:
            return None
        low, high = cls.LEVELS[level]
        return cls(low, high, rng)

    def pause(self) -> None:
        self.paused = True

    def resume(self, now: float) -> None:
        if self.paused:
            self.paused = False
            self._next = now + 0.5

    def update(self, chan: Chan, now: float) -> None:
        if self._next is None:
            # M5 waits a second after start: a robot that lurches the instant
            # it powers on reads as a fault, not as life.
            self._next = now + idle.FIRST_ACTION_DELAY_S
        if self.paused or now < self._next:
            return
        # M5's rule, and the reason idle motion does not fight itself: if the
        # head is still travelling, defer rather than queue another target.
        if chan.motion.is_moving(now):
            self._next = now + idle.BUSY_RETRY_S
            return
        if chan.motion.locked:
            return

        move = idle.next_move(chan.motion.pose, self._rng)
        self.last_kind = move.kind
        chan.motion.move_with_speed(move.yaw, move.pitch, move.speed_dps, now)
        self._next = now + self._rng.uniform(self.interval_min_s, self.interval_max_s)


# ----------------------------------------------------------------- speaking --
class SpeakingModifier(Modifier):
    """`speaking.h`: liveliness while talking.

    M5 does two things here -- flap the mouth every 180 ms, and nudge the head
    every 1.5-2.5 s. We take the SECOND ONE ONLY by default, which is the
    opposite of what M5's own xiaozhi integration does: theirs constructs
    `SpeakingModifier(0, 180, false)`, mouth on and motion off.

    The reason to differ is that our two devices are not in the same position.
    On this board lip-sync is driven in FIRMWARE off the `tts.start` transition,
    so the mouth is already synchronised to the audio -- better than any 180 ms
    host timer, and a host flap would fight it. That leaves the head motion,
    which nothing else provides, as the half worth having.

    Pass `drive_mouth=True` to get M5's flap as well; it is there for the case
    where firmware lip-sync is off.
    """

    def __init__(
        self,
        duration_s: float | None = None,
        mouth_interval_s: float = 0.18,
        enable_motion: bool = True,
        drive_mouth: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        self.duration_s = duration_s
        self.mouth_interval_s = mouth_interval_s
        self.enable_motion = enable_motion
        self.drive_mouth = drive_mouth
        self._rng = rng or random.Random()
        self._start: float | None = None
        self._next_mouth = 0.0
        self._next_motion = 0.0
        self._mouth_open = False
        self._baseline: Pose | None = None

    def update(self, chan: Chan, now: float) -> None:
        if self._start is None:
            self._start = now
            self._next_mouth = now + self.mouth_interval_s
            self._next_motion = now + self._rng.uniform(1.0, 2.0)
        if self.duration_s is not None and now - self._start >= self.duration_s:
            if self.drive_mouth:
                chan.face.mouth.weight = None
            self.request_destroy()
            return

        if self.drive_mouth and now >= self._next_mouth:
            self._next_mouth = now + self.mouth_interval_s
            self._mouth_open = not self._mouth_open
            weight = (
                self._rng.randint(40, 80) if self._mouth_open else self._rng.randint(0, 20)
            )
            chan.face.mouth.weight = weight

        if self.enable_motion and now >= self._next_motion:
            self._next_motion = now + self._rng.uniform(1.5, 2.5)
            self._nudge(chan, now)

    def _nudge(self, chan: Chan, now: float) -> None:
        if chan.motion.is_moving(now) or chan.motion.locked:
            return
        current = chan.motion.pose
        if self._baseline is None:
            self._baseline = current
        else:
            # M5 resyncs its baseline when something else has moved the head a
            # long way, so speaking does not snap back to a stale pose. Their
            # threshold is 300 of their units -- 30 degrees.
            if (
                abs(current.yaw - self._baseline.yaw) > 30.0
                or abs(current.pitch - self._baseline.pitch) > 30.0
            ):
                self._baseline = current

        yaw, pitch = self._baseline.yaw, self._baseline.pitch
        # M5's speeds here are 100-200 -- "说话时的动作都很慢", all the
        # movements while speaking are slow.
        speed = units.m5_speed_to_dps(self._rng.randint(100, 200))
        if self._rng.randint(0, 10) < 5:
            pitch += units.M5_DEGREES_PER_UNIT * self._rng.randint(-20, 50)  # a nod
        else:
            yaw += units.M5_DEGREES_PER_UNIT * self._rng.randint(-40, 40)
            pitch += units.M5_DEGREES_PER_UNIT * self._rng.randint(-20, 20)
        chan.motion.move_with_speed(
            clamp(yaw, YAW_MIN, YAW_MAX), clamp(pitch, PITCH_MIN, PITCH_MAX), speed, now
        )


# ----------------------------------------------------------------- head pet --
class HeadPetModifier(Modifier):
    """`head_pet.h`: being stroked makes him happy, and he settles afterwards.

    Driven by the gateway's touch events (`notify_config.py` templates them as
    `head_pat` and `head_stroke`), rather than M5's `onHeadPetGesture` signal.
    Call `on_swipe()` and `on_release()` from whatever is reading those.

    What is lost: the heart and blush decorators. The host has no decorator
    tool -- only `embarrassed`'s blush exists and it is wired in firmware to
    that face. So this is the motion and the expression, without the confetti.
    """

    def __init__(self, restore_delay_s: float = 3.0, rng: random.Random | None = None) -> None:
        self.restore_delay_s = restore_delay_s
        self._rng = rng or random.Random()
        self._swipe = False
        self._release = False
        self._happy = False
        self._waiting = False
        self._restore_at = 0.0
        self._prev_face: str | None = None
        self._prev_pose: Pose | None = None

    def on_swipe(self) -> None:
        self._swipe = True

    def on_release(self) -> None:
        self._release = True

    @property
    def reacting(self) -> bool:
        return self._happy

    def update(self, chan: Chan, now: float) -> None:
        if self._swipe:
            self._swipe = False
            self._handle_swipe(chan, now)
            # Still being stroked: push the settle back.
            self._waiting = False

        if self._release:
            self._release = False
            if self._happy:
                self._waiting = True
                self._restore_at = now + self.restore_delay_s

        if self._waiting and now >= self._restore_at:
            self._waiting = False
            self._restore(chan, now)

    def _handle_swipe(self, chan: Chan, now: float) -> None:
        if not self._happy:
            self._happy = True
            self._prev_face = chan.face.face
            self._prev_pose = chan.motion.pose
        chan.face.face = "happy"
        self._pet_motion(chan, now)

    def _restore(self, chan: Chan, now: float) -> None:
        if not self._happy:
            return
        if self._prev_face is not None:
            chan.face.face = self._prev_face
        if self._prev_pose is not None:
            chan.motion.move_with_speed(
                self._prev_pose.yaw, self._prev_pose.pitch, units.m5_speed_to_dps(200), now
            )
        self._happy = False

    def _pet_motion(self, chan: Chan, now: float) -> None:
        if chan.motion.locked or chan.motion.is_moving(now):
            return
        base = self._prev_pose or chan.motion.pose
        yaw, pitch = base.yaw, base.pitch
        # M5: speed 300-500, and three reactions -- raise the head, tilt it, or
        # a big happy lift.
        speed = units.m5_speed_to_dps(self._rng.randint(300, 500))
        action = self._rng.randint(0, 2)
        u = units.M5_DEGREES_PER_UNIT
        if action == 0:
            pitch += u * self._rng.randint(150, 250)
            yaw += u * self._rng.randint(-50, 50)
        elif action == 1:
            pitch -= u * self._rng.randint(0, 50)
            yaw += u * (150 if self._rng.randint(0, 1) == 0 else -150)
        else:
            pitch += u * self._rng.randint(250, 400)
        chan.motion.move_with_speed(
            clamp(yaw, YAW_MIN, YAW_MAX), clamp(pitch, PITCH_MIN, PITCH_MAX), speed, now
        )


# ------------------------------------------------------------ tilt reaction --
class TiltReactionModifier(Modifier):
    """`imu.h`: the dizzy reaction to being shaken.

    **DORMANT: nothing can currently trigger this.** The gateway surfaces no
    device IMU event at all -- the only "IMU" in its tool surface is a
    host-fed pose stream for head tracking. It is ported so the reaction exists
    for when the firmware does surface a shake, and so the gap is recorded
    somewhere other than a chat log. Call `on_shake()` to drive it by hand.

    Two thirds of M5's reaction cannot be reproduced from the host: they hide
    the eyes with `setVisible(false)` and replace them with the dizzy and blush
    decorators, and the host has neither tool. What survives is the mouth
    wobble, the motion lock, and the return home -- which is most of what reads
    as "stop shaking me".
    """

    def __init__(self, reaction_duration_s: float = 4.0) -> None:
        self.reaction_duration_s = reaction_duration_s
        self._shake = False
        self._reacting = False
        self._phase = False
        self._restore_at = 0.0
        self._next_toggle = 0.0

    def on_shake(self) -> None:
        self._shake = True

    @property
    def reacting(self) -> bool:
        return self._reacting

    def update(self, chan: Chan, now: float) -> None:
        if self._shake:
            self._shake = False
            if not self._reacting:
                self._reacting = True
            self._restore_at = now + self.reaction_duration_s
            if self._next_toggle <= now:
                self._next_toggle = now

        if not self._reacting:
            return

        if now >= self._next_toggle:
            self._next_toggle = now + 0.6
            self._phase = not self._phase
            # M5 passes -25 / 25 straight to setRotation, which clamps to
            # 0..3600 -- so their negative half is silently clamped to 0 and
            # only one side of the wobble ever shows. Expressed properly here
            # as 3600-25, which is what they meant.
            chan.face.mouth.rotation = 3575 if self._phase else 25
            chan.face.mouth.weight = 65

        if not chan.motion.locked:
            chan.motion.move_with_speed(
                REST_YAW, REST_PITCH, units.m5_speed_to_dps(300), now
            )
            chan.motion.locked = True

        if now >= self._restore_at:
            self._restore(chan)

    def _restore(self, chan: Chan) -> None:
        chan.face.mouth.rotation = 0
        chan.face.mouth.weight = None
        chan.motion.locked = False
        self._reacting = False


# -------------------------------------------------------------- the timers --
class TimedEventModifier(Modifier):
    """`timed.h`: run something, then undo it after a duration.

    A duration of 0 starts and ends in the same tick, which is M5's behaviour
    and is how a one-shot is expressed.
    """

    def __init__(self, duration_s: float) -> None:
        self.duration_s = duration_s
        self._start: float | None = None

    def on_start(self, chan: Chan) -> None: ...

    def on_end(self, chan: Chan) -> None: ...

    def update(self, chan: Chan, now: float) -> None:
        if self._start is None:
            self._start = now
            self.on_start(chan)
            if self.duration_s == 0:
                self.on_end(chan)
                self.request_destroy()
            return
        if now - self._start >= self.duration_s:
            self.on_end(chan)
            self.request_destroy()


class TimedFaceModifier(TimedEventModifier):
    """`TimedEmotionModifier`: wear a face for a while, then go back.

    Named for our vocabulary: M5 sets an Emotion, we set one of the six face
    names the board exposes, and the firmware maps that onto M5's Emotion.
    """

    def __init__(self, face: str, duration_s: float) -> None:
        super().__init__(duration_s)
        self.face = face
        self._prev: str | None = None

    def on_start(self, chan: Chan) -> None:
        self._prev = chan.face.face
        chan.face.face = self.face

    def on_end(self, chan: Chan) -> None:
        if self._prev is not None:
            chan.face.face = self._prev


class TimedSpeechModifier(TimedEventModifier):
    """`TimedSpeechModifier`: show a speech bubble for a while.

    Reachable only because `set_speech` was added alongside this port --
    `LiveAvatar::SetSpeech` was vendored in with M5's renderer and had been
    dead code, with nothing in the firmware calling it.
    """

    def __init__(self, text: str, duration_s: float) -> None:
        super().__init__(duration_s)
        self.text = text

    def on_start(self, chan: Chan) -> None:
        chan.face.speech = self.text

    def on_end(self, chan: Chan) -> None:
        chan.face.speech = ""


# ------------------------------------------------------------------- dance --
class DanceModifier(Modifier):
    """`dance.h`: play a keyframe sequence, then get out of the way.

    Adds one thing M5 does not need: the dances drive eye and mouth weight, and
    on this device those belong to the firmware's blink and lip-sync. So blink
    is suspended for the duration and restored afterwards, using the
    `set_blink` tool that already existed. Without that the eyes would blink
    over the squint and the dance would flicker.
    """

    def __init__(self, sequence: tuple[animation.Keyframe, ...], loop: bool = False) -> None:
        self.sequence = sequence
        self._timeline = animation.Timeline(sequence, loop)
        self._started = False
        self._blink_was: bool | None = None

    @classmethod
    def named(cls, name: str, loop: bool = False) -> "DanceModifier":
        """A dance or a gesture, by name. Raises rather than doing nothing quietly.

        Both kinds run through this one class because they need the same two
        things: the keyframe machinery, and blink suspended while the sequence
        drives eye weight. A nod does not care about the suspension -- it runs
        1.5 s and you would not notice a blink it skipped -- while the laugh
        squints deliberately and needs it, so one implementation serves both.
        """
        return cls(animation.lookup(name), loop)

    def update(self, chan: Chan, now: float) -> None:
        if not self._started:
            self._started = True
            self._blink_was = chan.face.blink_enabled
            chan.face.blink_enabled = False
            self._timeline.start(now)

        keyframe = self._timeline.update(now)
        if keyframe is not None:
            animation.apply_keyframe(chan, keyframe, now)

        if self._timeline.finished:
            self._finish(chan)

    def abandon(self, chan: Chan) -> None:
        """Give back what the sequence took, without playing the rest of it.

        `Chan.remove` drops a modifier from the pool and nothing else -- it
        does not run any teardown, which is why `_stop_speaking` resets the
        mouth weight by hand after removing the speaking modifier. Removing a
        RUNNING dance was impossible until gestures made one sequence replace
        another, and without this the eyes would keep the weight the last
        keyframe pinned and blink would stay suspended for good: one nod
        interrupted by another and he never blinks again.
        """
        self._timeline.stop()
        self._finish(chan)

    def _finish(self, chan: Chan) -> None:
        if self._blink_was is not None:
            chan.face.blink_enabled = self._blink_was
        # Release the axes the dance was driving, so the face goes back to
        # whatever the expression and the firmware say it should be.
        #
        # Both halves are needed and they do different things. Setting these to
        # None stops US driving them, so the flush stops sending them. It does
        # NOT tell the device to let go -- the board keeps every override it was
        # given until an expression change drops them. So the face is re-asserted
        # too, which is what actually clears them.
        for name in (LEFT_EYE, RIGHT_EYE, MOUTH):
            feature = chan.face.feature(name)
            feature.weight = None
            feature.size = None
        chan.reassert_face()
        self.request_destroy()
