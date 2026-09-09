# Personality

What Cubie should be like, and what it costs to get there. Written down before
any of it is built, because most of the ask turns out to be free and one part of
it is expensive, and knowing which is which changes the order.

## The brief

Jokes, sarcasm, swearing when things go wrong, arguing back, anger, sadness,
laughing, shaking his head, nodding, happiness, an evil look, suspicion — and
the faces, icons and animations to show it.

## Decisions already taken

These were asked and answered rather than assumed, and the rest of this file is
written to them.

| | |
| --- | --- |
| **Swearing** | Full strength, but **only when things break** — a failed action, an office that won't answer, an agent that died. Neutral otherwise, so it lands when it happens. |
| **Arguing back** | **Opinions only.** He can disagree, be sarcastic about the choice, say it's a bad idea — and then he does exactly what was asked. Attitude never changes the action. |
| **Office mood on his face** | The bridge's `posture.ts` mapping comes into the character stack as an ambient modifier; `bridge/` is then deleted. It drove `set_avatar` and `move_head`, which the character stack owns, so the two could never have run together. |

## What "things break" actually means

Worth pinning down, because "when things go wrong" is a feeling and the code
needs a condition. Three real signals already exist:

- **A tool outcome.** `office.py`'s `Outcome` is already `OK` / `STALE` (409) /
  `REJECTED` (400) / `UNREACHABLE`. Anything but `OK` is something going wrong,
  and he already knows which.
- **The office itself.** `read_office()` returning `None` — he was asked about
  the office and cannot see it.
- **His own failures.** `BRAIN_UNREACHABLE` and a refused capture
  (`ogg_opus.OggError`) are both already distinct paths that speak.

So the gate is a state he is already in, not a judgement he has to make. That
matters: a model deciding for itself whether things have "gone wrong" would
swear at a slow reply.

## Tier 0 — free, host-side, no firmware at all

Everything here uses tools the device already exposes and code already ported.
This is most of the brief.

**Nod, shake, laugh-bounce, recoil.** `pa/animation.py` is a keyframe engine
with M5's four dances already in it. A nod is pitch keyframes; a shake is yaw; a
laugh is a fast small bounce on both. No new tools, no firmware — the same
mechanism the dances use.

**Suspicious, evil, smug, unimpressed.** These need no new faces. The four
feature axes are already exposed — `set_gaze`, and `set_feature` for position,
rotation, weight and size, per eye and per mouth — so an expression is a face
plus overrides:

| look | how |
| --- | --- |
| suspicious | `thinking` + narrowed eyes (weight down) + gaze held to one side |
| evil | `happy` + eyes narrowed + mouth rotated |
| smug | `happy` + gaze away + one-sided mouth rotation |
| unimpressed | `idle` + eyelids low + gaze straight, no drift |

**One hazard, and it is the whole reason this works.** A face change *resets*
the board's overrides — `RenderLiveAvatarLocked` calls `SetFace` on every render,
so the override lives in the board beside `current_face_index_`. `pa/chan.py`
already flushes the face **first** for exactly this reason. Composition is
therefore "set face, then set overrides", which is the order the flush already
uses. Nothing to change; something not to break.

**Jokes, sarcasm, swearing, arguing.** Prompt and character work in
`pa/brain.py`, plus the gate above. The voice is already the tuned `retro`
preset and will say any of it.

**Office mood on the idle face.** `bridge/src/posture.ts` is pure and already
tested — it maps office state onto a mood. As an ambient modifier that becomes
"his resting face reflects the office without being asked", which is a large
part of feeling alive and is code that already exists.

Two things about that port worth settling before it is written:

- **It applies while STANDBY only.** During a conversation the status owns the
  face and the LEDs — `LISTENING` already strips idle motion and gaze drift for
  exactly this reason. An ambient mood that kept asserting itself mid-answer
  would be the same two-things-one-axis fight that the bridge would have had
  with the character stack.
- **One of its own notes is now out of date.** `posture.ts` says dozing cannot
  have half-closed eyes because "eye weight is driven by the firmware's own
  blink machine and there is no tool for it". There is now: `set_feature`
  carries weight, added as an opt-in override precisely because blink owns that
  axis. So half-closed eyes are reachable — **at the cost of blinking**, since
  overriding weight is what defeats the blink animation. A dozing robot that
  does not blink may well be right, but it is a choice to make deliberately
  rather than a freebie to take, so the first port keeps the bridge's posture-
  only dozing and this stays a follow-up.

## Tier 1 — one firmware line each

**`angry`.** `Emotion::Angry` is in M5's renderer and **nothing maps to it**.
`apply-live-avatar-step1.sh` says so itself: "reachable only once the office has
something that should make Cubie cross." The office now does. This is a seventh
entry in `EmotionForFace` plus the four `[6]` tables becoming `[7]`, and
`"angry"` appended to the host's `FACES`. It also removes a lossy mapping —
`angry` currently renders as `sad`, which reads as sulking rather than cross.

**`sleepy`, separately from `embarrassed`.** Today `embarrassed` is
`Emotion::Sleepy` **plus** the blush decorator, so routing `sleepy` there gets
heavy eyelids and an unwanted blush. `pa/driver.py` already records that the fix
is another face in firmware rather than a different mapping.

⚠️ **The host's `FACES` tuple and the board's tables are index-matched.**
`FACES = ("idle", "happy", "thinking", "sad", "surprised", "embarrassed")` lines
up positionally with `EmotionForFace`, `EyeSizeForFace`, `kFaceMouthWeight`,
`kFaceGazeX/Y` and `kFaceMouthRotation`. **Appending is safe; reordering or
inserting silently gives every face the wrong expression** — and it would look
like a rendering bug rather than an off-by-one. Any change here needs both sides
in the same commit, and a check that the lengths agree.

## Tier 2 — the expensive part, and worth scoping separately

**Icons and arbitrary animations on screen.** The avatar owns the display: it is
M5's renderer drawing a face, not a framebuffer we blit into. Anything that is
not a face needs new firmware draw calls, an asset pipeline, and a PSRAM budget
— a different kind of work from everything above, and the only part of the brief
that is not cheap.

**Try the speech bubble first.** `set_speech` already exists and already works.
Text and emoji in the bubble may cover a good deal of what "icons" is reaching
for, at zero cost, and it would be daft to build an asset pipeline before
finding out. That is the first experiment, not the fallback.

## Order

1. Port `posture.ts` in, delete `bridge/`. Settled, unblocks the ambient face.
2. Tier 0 motion — nod, shake, laugh. Visible, testable without hardware.
3. Tier 0 composed expressions, with the face-first flush order asserted.
4. The persona and the swearing gate in `pa/brain.py`.
5. Tier 1 `angry` + `sleepy`, both sides in one commit, with a length check.
6. Try `set_speech` for icons. Decide about Tier 2 only after that.

## Open, and honestly unknown

- **Whether M5's `Angry` looks right on this board.** Never rendered here. It is
  their art, at 320×240, and it may read as comic rather than cross.
- **Laughing needs a sound.** Piper will not laugh convincingly; "ha. ha." read
  aloud is not a laugh. Either a sound asset — the firmware already ships locale
  sound assets, so there is a place for one — or accept that the laugh is a
  movement and a face rather than a noise.
- **How often is too often.** Sarcasm every turn stops being funny by the third
  one. There is no way to tune this from a desk; it wants a rate limit and then
  your judgement on hardware.
