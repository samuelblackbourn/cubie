# cubie

**Cubie** is the Virtual-Office Personal Assistant, embodied — an M5Stack StackChan
(CoreS3, ESP32-S3) that mirrors the office's state in posture, LEDs and voice, and
takes approvals from the desk.

Canonical design lives in the memory vault at `Virtual-Office/Design-PA-Robot-Cubie.md`.
Build phases are S0–S5 there; this repo is S2 onward.

## Status

| Piece | State |
| --- | --- |
| S0 — discovery | **Done.** Stock firmware is a no-go; see the vault log |
| Factory firmware archived | **Done.** `office-archives/cubie/factory/`, verified |
| Contract guard | **Done**, this repo |
| S1 — self-hosted server + reflash | **Done.** Gateway on office-server, Cubie on our own build |
| S1a — M5's live avatar ported in | **Done.** Vendored MIT, blinking, six expressions |
| S1c — OTA from the office | **Done.** `build` → `publish` → reset. No cable |
| S2 — the office on his resting face | **Done.** `pa/mood.py`; the `bridge/` prototype it came from is gone |

Four things were merged on reasoning rather than a result — firmware that cannot
be compiled in the sandbox, a Kconfig symbol read off `master` because the
tagged esp-sr was unreachable, and a device-side VAD traced through the source
but never watched fire. Those are written down as checks in
[`HARDWARE-CHECKS.md`](HARDWARE-CHECKS.md), in the order that makes a failure
diagnosable, rather than left to turn into folklore.

## Building the firmware

```
bash ~/cubie/firmware/build.sh            # patch, build, publish
bash ~/cubie/firmware/build.sh --check    # report what would happen, change nothing
bash ~/cubie/firmware/build.sh --fresh    # discard the working tree, start clean
```

That is the whole build. It clones upstream at a pinned commit, vendors M5's
avatar at a pinned commit, applies the patch set **in a fixed order**, sets the
sdkconfig entries, compiles in the pinned ESP-IDF container and publishes for
OTA. Every step is idempotent, so a re-run on an already-patched tree prints
"already patched" rather than failing — which is what makes it safe to automate.

It does **not** reset Cubie. He takes the new image on his next reset, and that
is a deliberate human gate: a build is not a good enough reason to interrupt a
robot mid-sentence.

### Building automatically when `main` moves

```
sudo cp deploy/cubie-firmware-build.service deploy/cubie-firmware-build.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cubie-firmware-build.timer
journalctl -u cubie-firmware-build -f
```

A five-minute timer runs `deploy/build-on-merge.sh`, which fetches, and builds
only when `main` has actually moved **and** the change touched `firmware/` or
`tools/`. Merge a firmware change and it goes out; merge a README edit and
nothing happens — which matters more than it sounds, because the version is
minutes-since-epoch, so an empty rebuild would look like a real release to the
device.

Pull-based, deliberately. A self-hosted GitHub Actions runner would be faster
but costs a long-lived GitHub credential on office-server and a runner service
that can fail on its own. Polling a git remote needs no inbound access and no
secret, and survives office-server's address changing — which was not
hypothetical, because a DHCP lease used to be baked into the firmware.

### Where the robot looks for the office

`OFFICE_HOST` is **`office-server.local`**, a name rather than an address.
`192.168.0.84` was a DHCP lease: fine until the router hands out a different
one, at which point OTA stops with no symptom on the device and nothing to
notice except an update that silently never arrives.

The cost is a resolution step that an address does not have. `.local` is mDNS,
so `CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES=y` is set **explicitly** in `build.sh`
rather than left to the pinned IDF's default — a default is not a decision, and
if the symbol has moved the build says so where the device otherwise would not.
`make check-config` asserts the two travel together: a `.local` URL without the
mDNS option is a failure, verified by removing it.

What it does **not** cost, since this was overstated once: `CONFIG_OTA_URL` is
the *update* path only. If the name stops resolving he keeps running exactly as
he is and stops taking over-the-air updates until the cable comes out — the same
cable he was first flashed with. A lost convenience, not a dead robot.

An address is always still available, which matters most when resolution is the
thing that broke:

```
OFFICE_HOST=192.168.0.84 bash ~/cubie/firmware/build.sh
```

**Prerequisite:** `sam` must run docker without sudo, i.e. be in the `docker`
group. That is equivalent to root on this machine, so it is a deliberate trade
rather than an oversight — the alternative is a NOPASSWD sudo rule granting the
same thing less visibly. Check before enabling: `sudo -u sam docker info`.

Four things it refuses to do, each because a timer should not race a person:

- **Retry a failed commit.** A commit that fails to build is recorded and
  skipped until `main` moves again. Otherwise one bad merge means a full
  ESP-IDF build every five minutes, forever. The next commit is picked up
  normally, and the diff base stays the last commit actually *built*, so a
  firmware change inside a failed commit is not lost.
- **Touch a dirty or non-`main` checkout**, or one carrying local commits that
  cannot fast-forward.
- **Run `--fresh`.** That discards a tree currently producing working firmware.
  If the pin in `build.conf` changes, `build.sh` refuses and so does this: a
  person runs it once, deliberately.
- **Reset the robot.**

Configuration lives in `firmware/build.conf` — the two upstream pins, the
office's address as the robot sees it, and the toolchain image. All of it is
overridable from the environment.

### Convergence

```
make check-idempotent
```

Not part of `make check` — it clones two repositories — but run it after
touching anything in `firmware/*.sh`.

It proves the patch chain reaches the **same sources** on a re-run, not merely
that it exits 0. The distinction is load-bearing: `apply-live-avatar-step1.sh`
regenerated `avatar_live.cc` on every run while `fix-expression-depth.sh` exited
early on a `stackchan.cc` marker before reaching its `avatar_live.cc` edit. A
second pass therefore reverted `EyeSizeForFace`, and `surprised` shipped
narrower eyes. Nothing failed. The build succeeded. The firmware was quietly
wrong — and only on incremental builds, which is every build the timer does.

The check runs three passes plus the half-patched state a *failed* run leaves
behind, since healing from that without `--fresh` is the timer's job.

**The order in `build.sh` is not arbitrary and must not be sorted.** Each patch
is anchored on exact text the previous one emits; `apply-face-and-touch.sh`
anchors on a comment `fix-touch-classify.sh` writes, so it genuinely cannot run
before it. `fix-shy-ctor.sh` is not in the list — it repairs a tree where the
three-argument `ShyDecorator` call already landed, and a clean run cannot
produce that. It runs afterwards as an assertion instead, and would fail loudly
if a future edit reintroduced the bad form.

## Firmware and deploy scripts live here

`firmware/*.sh` and `tools/make_avatar.py` are versioned in this repo on purpose:
they are how Cubie's firmware is patched, built and published, and a script that
exists only in someone's chat history or home directory is not a deploy process.

Three things used to be missing from that claim, and `build.sh` closes them: the
`cp -r` that vendors M5's avatar was never scripted at all, nothing recorded
which upstream commit the build was made against, and `CONFIG_LV_FONT_MONTSERRAT_16`
and `CONFIG_OTA_URL` reached the working build from a source nobody could name —
which meant the font fix and the OTA endpoint were one `rm -rf` from gone.

On office-server, clone this repo once and then `git pull` — no copying files
around:

```
git -C ~/cubie pull
bash ~/cubie/firmware/fix-shy-ctor.sh
```

Name the script you actually want. The line above used to read
`firmware/<script>.sh`, which got pasted into a shell literally and failed —
placeholders in angle brackets do not survive a copy-paste workflow.

`tools/cubie-call.py` calls a single gateway tool from the command line. The
gateway speaks streamable-HTTP MCP, so a bare `curl` POST is answered with
`Bad Request: Missing session ID` — that is the `initialize` handshake missing,
not an auth failure. Run it with the gateway's own interpreter, which is where
the `mcp` package lives:

```
export STACKCHAN_TOKEN=$(sudo sed -nE 's/^STACKCHAN_TOKEN=//p' \
    /etc/stackchan-gateway.env | tr -d '\042\047')
~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py get_status
~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py \
    set_avatar '{"face":"embarrassed"}'
```

That `sed` is the only supported way to get the token into a shell: it never
prints the value. The gateway's token lives in `/etc/stackchan-gateway.env` as
`STACKCHAN_TOKEN`.

## His voice

Synthesised locally with Piper and streamed to the speaker; nothing leaves the
LAN. Lip-sync comes free — it is firmware-driven off the `tts.start` state
transition, so it works on the raw-PCM path too.

```
pa/.venv/bin/python pa/speech.py "Hello Sam."
pa/.venv/bin/python pa/speech.py --robot 0.7 "Approval waiting."
```

**Characters** bundle pitch, delivery and modulation, because those four
numbers interact in ways nobody predicts from the values:

```
pa/.venv/bin/python pa/speech.py --characters              # what they are
pa/.venv/bin/python pa/speech.py --character cute "Hello Sam."
pa/.venv/bin/python pa/audition.py --only en_GB-alan-low --characters
```

Pitch is the knob that matters. A voice 30–40% up sounds *small*, which is
most of what makes a robot endearing rather than menacing — far more than
modulation, which adds a machine edge but costs intelligibility as it rises.
Since this thing reads out approvals, the presets keep modulation modest and
leave heavy settings to anyone asking for them explicitly.

**Piper has no pitch control**, so it comes from the resampler: synthesise
slower via `length_scale`, then declare a proportionally higher sample rate.
The duration cancels out and the pitch multiplies. `PiperSynthesizer.pitch`
reports `sample_rate` above the model's true rate on purpose, and that is the
entire mechanism — pitch shifting costs no signal processing, just a number.

**Pick a voice by hearing it, not by reading names:**

```
pa/.venv/bin/python pa/audition.py --list --lang en_GB     # what exists
pa/.venv/bin/python pa/audition.py --lang en_GB --quality low
pa/.venv/bin/python pa/audition.py --only en_GB-alan-low --robot-sweep
```

Each voice announces itself *in its own voice*, so they are told apart by ear.

`--quality low` is not just cheaper. Low models are natively **16 kHz** — the
device's rate — so nothing is resampled anywhere, and they sound more
synthetic, which suits a robot.

**We always send 16 kHz**, and that is load-bearing. The gateway accepts any
rate but resamples each 8192-byte body chunk **independently** — its own
docstring calls the error "negligible for speech-rate inputs". At 22050 Hz it
is not: a 4.6 s sentence crosses ~25 chunk boundaries and picks up a
discontinuity at each, which is audible as a voice that breaks up. That was the
first thing heard from the robot. So `pa/audio.py` resamples once, carrying
interpolator state across chunks, and the gateway's step becomes a no-op.

`--robot` ring-modulates; `--crush` quantises. Both carry their state across
chunks for the same reason the resampler does.

### The retro voice

`--character retro` is `en_US-ryan-high` pitched up 25% and chopped at 15 Hz:
a young voice sounding like it is spoken through a desk fan.

The carrier frequency is the whole trick. Every other character modulates at
45–140 Hz, which is fast enough to fuse into a tone and read as electronic. A
fan chops at its blade-pass rate — tens of hertz — so in this band the ear
hears the individual chops. Verified by counting them through a flat test tone
rather than assumed from the parameter.

**Depth and frequency are independent knobs**, which is what makes them safe to
tune separately: depth sets *how much* the signal is ducked — the trough stays
31% of peak at any frequency — and the carrier sets *how often*.

The floor on the carrier is set by syllable length, not by taste. A syllable is
roughly 150–250 ms, and once the chop period approaches that, the modulation
stops being heard as timbre and starts ducking whole syllables unevenly — a
worse artefact than a fan, not a slower one:

| carrier | period | chops per syllable | reads as |
| --- | --- | --- | --- |
| 22 Hz | 45 ms | 4.4 | roughness; a fast fan |
| **15 Hz** | **67 ms** | **3.0** | **clearly a fan** |
| 12 Hz | 83 ms | 2.4 | about the floor |
| 8 Hz | 125 ms | 1.6 | pulsing, uneven |
| 6 Hz | 167 ms | 1.2 | a tremolo, not a fan |

So 12 Hz is roughly as slow as this can usefully go, and 15 leaves some room.

The depth is **measured, not chosen by ear**. `ring_modulate` applies
`gain = 1 − depth + depth·sin`, so depth 0.5 is exactly where the trough
reaches zero and anything above it inverts phase through a full gate. There is
a cliff between 0.45 and 0.40 — that is where the trough stops being
near-silent (durations below are at the 22 Hz carrier this was measured at):

| depth | gain range | swing | near-silent per chop |
| --- | --- | --- | --- |
| 0.55 | −0.10 … 1.00 | — | ~16 ms, and phase inverts |
| 0.45 | +0.10 … 1.00 | 10.0:1 | ~10 ms |
| 0.40 | +0.20 … 1.00 | 5.0:1 | none |
| **0.35** | **+0.30 … 1.00** | **3.3:1** | **none** |

A gap under about 10 ms is one the ear fills in; past that speech stutters
rather than throbs. **0.45 is the deepest chop available** before it costs
syllables — the right ceiling for a voice that reads out approvals, and more
than wanted in practice.

### Both numbers were tuned down on the robot

The measurements above set the ceilings; hearing it set the values, over three
passes on the robot:

| | first | now | why |
| --- | --- | --- | --- |
| pitch | 1.30 | **1.25** | +4.54 → +3.86 semitones; a 0.68 st drop, under the ~1 st that reads as a different voice |
| depth | 0.45 | **0.35** | clears the near-silence cliff and softens the throb; trough 11% → 31% of peak |
| carrier | 22 Hz | **15 Hz** | a slower fan; 3.0 chops per syllable, still comfortably above the ~2 floor |

Each was measured through a flat test tone after the change, which is how the
independence of the two modulation knobs got established: dropping the carrier
from 22 to 15 left the trough at 31% of peak, and dropping the depth left the
chop count untouched.

A character can now carry its own Piper model, which is new: `--voice` used to
default to a value rather than to `None`, so “did they ask for the default or
not ask at all?” was unanswerable and a character's own voice could never have
taken effect. Download the model once:

```
pa/.venv/bin/python -m piper.download_voices en_US-ryan-high     --data-dir ~/.local/share/piper-voices
pa/.venv/bin/python pa/speech.py --character retro "Approval waiting."
```

## Language

The firmware defaults to **`LANGUAGE_ZH_CN`**, so every on-screen string and
every locale sound asset — `welcome.ogg`, `activation.ogg`, `wificonfig.ogg`,
the spoken digits used to read an activation code aloud — is Chinese. That is
why the boot screen is unreadable and the startup voice unintelligible.

`FIRMWARE_LANGUAGE` in `build.conf` selects it, defaulting to
`LANGUAGE_EN_US`. It names a Kconfig `choice` option exactly — see
`firmware/main/Kconfig.projbuild` for the list.

Because it is a *choice*, the options are mutually exclusive but are different
Kconfig keys, so the config patcher removes any other `CONFIG_LANGUAGE_*`
before adding the wanted one. Without that, switching language leaves two
selected and the build picks one unpredictably.

**Not the same thing as removing the boot screen.** Activation is not the
culprit — our OTA manifest carries no activation code, so that loop exits
immediately — and `welcome.ogg` is referenced nowhere in the code. What the
boot screen actually is, and what actually speaks, still wants a serial capture
to identify rather than a guess.

## Gestures — nod, shake, laugh, glance

`pa/animation.GESTURES`, played by `CharacterDriver.gesture(name)`. The same
keyframe machinery as M5's four dances and a separate registry, because
`SEQUENCES` claims to be "what the port carried over" and a claim you keep
appending to stops being checkable.

A dance is a performance you ask for; a gesture is punctuation — under two
seconds, in the middle of something else. `glance` is the one the office drives
itself: entering the `attention` mood makes him look up and come back, because
a person notices movement rather than an attitude.

Three things this cost that were not obvious, all of which failed silently:

**Keyframes are absolute poses, and a gesture fires while the head is wherever
idle motion last left it.** A nod's dip is the absolute pose "pitch 33"; from a
head already lower than that it is a *rise*, so the gesture did not start
off-centre, it **inverted**. Every sequence now opens with a 500 ms settle to
rest, fast enough to arrive from anywhere the servos reach.

The envelope is the **servo range**, not a narrower idle envelope, and getting
that wrong is how the settle came to be 30° short on its first attempt. The
comment claimed "idle.py reaches yaw ±50 and pitch 25–55" — which is what three
of idle's four actions do in isolation, but `_small_observation` is *relative*,
so the walk compounds and is bounded only by the clamp. Simulated over 300 seeds
× 500 actions it reaches |yaw| 87.5 and pitch 6.1–80.5. A nod from a head idle
had walked down to pitch 6 still inverted — the exact bug the settle exists to
fix, surviving in the tail because the envelope was asserted from a reading
instead of measured.

**A frame must be given the speed to arrive.** 30° in 200 ms needs 150 dps;
command less and the next keyframe interrupts the move part-way, so the gesture
is shallower than it reads on the page and nothing reports it. Asserted per
keyframe, for gestures only: M5's dances are a mixed bag we did not write, and
`PANIC` depends on *not* arriving — its 40° reversals in 100 ms want 400 dps
against a 240 ceiling, which is exactly why it reads as frantic. A test records
which of their four can arrive rather than claiming panic is the only one that
cannot, which is what an earlier version of it said.

**`Chan.remove` runs no teardown.** Nothing had ever removed a *running* dance,
so it never mattered; one gesture replacing another does. Without
`DanceModifier.abandon`, blink stayed suspended and the eye weight stayed pinned
where the interrupted keyframe left them — one nod cut off by another and he
never blinks again.

**And releasing an axis host-side does not release it on the device.** Setting
`weight = None` stops *us* driving it; the board keeps every feature override it
was given until an expression change drops them, and the flush only re-sends
`set_avatar` when the face *name* changed. So a gesture ending on the same face
left the eyelids pinned where its last keyframe put them, flattening all six
expressions to one eyelid position — which is precisely the defect
`firmware/fix-eye-weight.sh` fixed once already. The teardown now re-asserts the
face, which is the only thing that clears them.

`glance` fires **once per waiting episode, at the first moment he is standing
by** — not once per poll, and not only on the poll that carries the transition.
The distinction is the case that matters: an approval arriving mid-answer. The
first version compared each reading against the previous one, so by the time he
returned to standby `office_mood.reason` was already `attention` and every later
poll failed the transition test. Something arrived and he never noticed, which
is the one thing the gesture exists to prevent.

And one defect they exposed rather than caused: **`dance()` stood idle motion
down and nothing ever put it back.** It recovered only if something later set
the status to STANDBY, which a conversation does in its `finally` — so a dance
during a turn recovered and the bug stayed hidden. A one-second gesture makes it
intolerable: the first nod would have been the last time he looked around.
`_perform` now remembers the sequence and `update` gives idle back when it ends.

## Idle behaviour

Blinking is already in the firmware — `set_blink(enabled: true)`, every 3–6
seconds at random. It is runtime state, so it needs asserting on connect
rather than porting.

Looking around is `pa/idle.py`, ported from M5's `modifiers/idle_motion.h`.
Their **structure** is copied exactly, because that is what makes it feel
right: a random 4–8 second interval rather than a fixed tick, four weighted
actions (50% look around, 30% small observation, 10% quick glance, 10%
recentre yaw), and "if the head is still moving, defer 500 ms rather than
queue another command".

Their **ranges are adapted, not copied**, and the distinction matters. M5 works
in tenths of a degree with `lookAtNormalized` coordinates whose meaning depends
on their servo mounting; our `move_head` takes degrees, pitch 5–85, and speed
in degrees per second against their opaque 100–400 scale. Copying their numbers
into different units would look faithful and behave wrong.

The recentre action is load-bearing, not decorative: the other three
random-walk, so without something pulling yaw back he drifts to one side over a
few minutes and parks facing a wall. A test simulates 3000 idle steps to prove
he doesn't — the only way that failure is visible without waiting at a desk.

**The head was not looking around because nothing ran this.** `idle.py` was
written, tested and never wired to a process. `pa/live.py` and
`deploy/cubie-character.service` are that missing half.

## The rest of M5's character stack

The whole of `stackchan/modifiers/`, `stackchan/animation/` and M5's own
xiaozhi integration (`hal/board/stackchan_display.cc`) are now ported. That
last file is the most useful one in their tree for us: it is M5 wiring their
avatar and modifier stack to the *same* xiaozhi-esp32 display interface our
firmware runs, so it is a reference rather than an analogy.

| Ported | From | What it does |
| --- | --- | --- |
| `chan.py` | `stackchan.h`, `modifiable.h` | the modifier pool, the state, the flush |
| `units.py` | `hal_servo.cpp` | M5's units → ours |
| `animation.py` | `animation/`, `dance.h` | keyframe engine + Happy, Robot, Panic, LookAround |
| `modifiers.py` | `modifiers/*.h` | breath, gaze idle, head idle, speaking, head-pet, tilt, timers, dance |
| `driver.py` | `stackchan_display.cc`, `app_avatar.cpp` | which modifiers exist when; emotion and keyword triggers |
| `live.py` | their LVGL task | one MCP session, a 10 Hz tick, and a tail of the event log |

**Modifiers never call the gateway.** They mutate state; `Chan.flush()` diffs
it and sends only what changed. That is what makes every behaviour pure and
seeded — the same split as `tracking.py` and `posture.ts` — and it is also the
only version that is neither a flood nor a robot that never moves. A quiet
second is zero calls.

`set_status` is the part worth keeping faithful: idle motion and gaze drift
exist only while standing by, and **listening removes them entirely**. A robot
that keeps glancing around while you talk to it reads as not listening.

### Three units that had to be converted, not copied

1. **Angles are tenths of a degree.** Traced through `hal_servo.cpp`
   (`angle * 16 / 5 / 10` steps at 0.3125°), not assumed. Their yaw limit of
   ±1280 is ±128°.
2. **Pitch is measured from a different zero, and this one would have broken
   every call.** M5's pitch zero is their home and increasing pitch raises the
   head; ours is 5–85 with 45 level, and `move_head` **rejects** out-of-range
   values rather than clamping. Their dances use pitch 0 as the centre of a
   gesture and go negative — taken literally, every one of those poses is
   refused and the head simply never moves while the face animates. So M5 pitch
   is read as an offset from our resting pitch. A test asserts every keyframe
   of every dance lands inside the window.
3. **Speed is not degrees per second.** `moveWithSpeed`'s 0–1000 feeds a spring
   stiffness pair. There is nothing to derive, so the mapping is a stated
   calibration and the knob to turn if ported animations feel wrong.

### What the host cannot reach, and what that costs

Recorded here rather than rediscovered per modifier:

- **Decorators** (heart, dizzy, sweat, angry) have no host tool, so head-pet
  loses its hearts and the tilt reaction its dizzy spiral.
- **Element visibility** is unreachable, which the tilt reaction uses to swap
  the eyes for the dizzy overlay.
- **The device's IMU is not surfaced at all.** The only "IMU" in the gateway's
  tool surface is a host-fed pose stream for head tracking. So
  `TiltReactionModifier` is ported and **dormant** — nothing can trigger it
  until firmware surfaces a shake. It is here so the reaction exists when that
  lands, and so the gap is written down somewhere other than a chat log.
- **Touch events are off by default.** `notify_config.py` defaults
  `jsonl.enabled` to false, so stroking his head reaches nothing until
  `deploy/stackchan-notify.yml` is installed. That is a config step, and
  without it head-pet looks broken rather than unconfigured.

### Two axes the firmware owns better than the host does

`SpeakingModifier` and `BlinkModifier` both drive weights, and here the
firmware drives them from closer to the truth: **lip-sync** off the `tts.start`
transition, so it is synchronised to the audio rather than a 180 ms host timer,
and **blink** as a state machine immune to LAN jitter.

So there is deliberately **no host blink modifier** — two things driving one
axis over a LAN and the eyes would visibly fight. And `SpeakingModifier` takes
the half M5's own xiaozhi integration turns off: theirs is
`SpeakingModifier(0, 180, false)`, mouth on and motion off; ours is the
opposite, because the head nod is the half nothing else provides.

The dances do need the weights, so `DanceModifier` suspends blink for its
duration and restores it. Without that the eyes blink over the squint.

### Three upstream bugs found while reading

- **Every M5 dance applies an indeterminate eye size.**
  `FeatureKeyframe`'s four-argument constructor — the only one any dance uses
  — never initialises `size`, and `Keyframe::apply()` calls `setSize(kf.size)`
  regardless. `Feature::setSize` clamps to ±100 so it cannot crash, which is
  why it survives: it shows up as eye size varying between builds. Ported as 0.
- **`breath.h`'s amplitude is not pixels.** Its comment says
  “单位像素”, but `move_component` adds the delta to `getPosition()`, the
  −100..100 normalised value. So the real amplitude is 16 *units*, about
  2.6 px — a subtle wobble, which is what breathing should be.
- **Half the tilt-reaction wobble never shows.** It passes −25 straight to
  `setRotation`, which clamps to 0..3600, so the negative half is silently
  pinned to 0. Written here as 3600−25.

### Waking up: sync first, then settle

M5's boot does not recentre. `Servo::init()` reads the real angle and syncs its
state to it, then goes limp:

```cpp
_angle_anim.teleport(getCurrentAngle());
setTorqueEnabled(false);
```

`goHome()` exists but is only ever called by the shake reaction. Their own
`servo.h` gives the reason for the read: a mismatch between assumed and actual
"may cause a snap".

It matters more here than it looks, because **three modifiers work relative to
that pose** — idle's small-observation adds an offset to it, speaking baselines
on it, and head-pet records it as the pose to restore to. Starting from an
assumption makes all three compute from the wrong place until some absolute
move happens to correct it.

So `wake()` takes the half M5 is right about and then deliberately differs on
the second: it reads `get_head_angles`, adopts that pose without commanding
anything, and **then settles to rest at 40 dps** — slower than every idle speed,
because this is the one movement a person watches from cold, and a robot that
snaps to attention on power-up reads as a fault where one that settles reads as
waking. M5 leaves the head limp, which suits a toy on a shelf; this is a desk
assistant that should hold a known pose.

Syncing first is what makes that settle a *move* rather than a snap: it starts
from where the head actually is, and `is_moving()` is then honest about how long
it will take, so idle motion defers instead of firing a competing target into
the middle of it.

The read can fail — the firmware documents a persistent `ReadPos` failure as
returning `{"yaw": null, "pitch": null, "error": ...}`, and a single failure
mid-motion is a known transient. That falls back to assuming rest, which is
what the code did before any of this existed, and logs that it did.

## Expressions, and the axis we were missing

M5's renderer gives every feature four independent axes — `setWeight`,
`setSize`, `setPosition` and `setRotation`. Until `apply-m5-expression.sh` this
firmware drove two of them. `setPosition` on the eyes is **gaze**, and its
absence is most of why `thinking` looked like nothing.

`thinking` maps to `Emotion::Doubt`, and Doubt's entire contribution is an eye
weight of 75 against idle's 100 — a quarter of an eyelid. A thinking face is
not a mouth shape or an eyelid position. It is a face looking *up and away from
you*, and there was no way to say that.

The axis is worth using. From the vendored skin's `eyes.cpp`:

```cpp
static const Vector2i _eye_min_offset = Vector2i(-16, -16);
static const Vector2i _eye_max_offset = Vector2i( 16,  16);
pos_y = _eye_pos.y + map_range(_position.y, -100, 100, min.y, max.y);
```

So the `-100..100` the API takes is ±16 px of real travel, on eyes 8–32 px
across. It is an LVGL `setPos`, so **+y is down** and negative y looks up.

Three new tools expose it. The first two are **held until the next
`set_avatar`** — the same contract `set_mouth` already documents:

```
set_gaze     x, y: -100..100        point the eyes without moving the head
set_feature  feature, x, y,         all four M5 axes on one feature:
             rotation, weight,      'eyes', 'left_eye', 'right_eye', 'mouth'.
             size                   Pass -1000 to leave an axis alone.
set_speech   text                   the speech bubble; '' clears it
```

`set_feature` exists because the whole character stack needs it: M5's
`idle_expression`, `breath` and every keyframe of every dance set all four axes
on all three features. Exposing them once beat growing a tool per animation.

Two details in it are deliberate:

- **Omitted axes are left alone**, hence the `-1000` sentinel rather than 0.
  Weight belongs to blink and lip-sync, and size to the `surprised` override;
  a tool that set all four unconditionally would fight the two things on this
  device that already work well.
- **x and y count as one axis** and must both be given. Half a position is a
  silent jump to zero on the other, and a call that returns `ok` having done
  something else is the failure this repo has already paid for once.

`set_speech` brings `LiveAvatar::SetSpeech` to life — it was vendored in with
M5's renderer and had been dead code, with nothing in the firmware calling it.
M5's own driver uses the bubble for “Zzz…” and for any status string it does
not recognise, which is how “Connecting…” ends up on his face.

**Why the override lives in the board and not in `LiveAvatar`.**
`RenderLiveAvatarLocked()` calls `SetFace()` on *every* render, and a blink
renders four times. A host gaze stored inside the avatar object would be wiped
by the next blink, about five seconds later — not by the next `set_avatar`. So
it is board state applied at render time, exactly how the mouth already works:
lip-sync wins while it is the active layer, the per-face resting value applies
otherwise. Clearing it happens in `SetAvatarExpressionLocked`, which covers the
touch reactions and the post-fetch replay path as well as the MCP tool.

The per-face gaze table is in `RenderLiveAvatarLocked`. `thinking` is
`(-60, -70)` — up, and off to his left.

One axis is **unverified on hardware**: the mouth rotation. `DefaultMouth` calls
`setRotation` but, unlike `DefaultEyes`, sets no transform pivot of its own, so
what it rotates about is LVGL's default and has not been seen on a panel. Only
`thinking` uses it (15°). If it looks wrong, `kFaceMouthRotation` is the one
knob — zero it and nothing else changes.

## The shape of it

```
Cubie (CoreS3, xiaozhi firmware)
   │  dials OUT over WebSocket MCP, Bearer auth
   ▼
stackchan-mcp gateway (Python, upstream, run as-is)
   │  MCP
   ▼
character stack (Python, this repo)     ← pa/
   │  GET /api/companion/status, POST approve|deny|presence
   ▼
Virtual-Office on office-server
```

**Two decisions worth not re-deriving**, both from S0:

**The firmware gets replaced, and that was not the first preference.** The design note
said stock firmware stays. It cannot: M5's factory build reaches the office only
through xiaozhi's cloud, which the same note forbids, and it exposes **no touch tool
at all** — so approve-from-the-desk is impossible on it, independently of the cloud
argument. We build on `kisaragi-mochi/stackchan-mcp`'s xiaozhi fork, which already
carries a StackChan board definition and adds `avatar.set_face`, `touch.get_touch_state`
and per-LED control.

**We are an MCP client, not a protocol implementation.** Rather than reimplement
WebSocket MCP, the upstream gateway runs as-is and we are a client of it — a pattern
the office already uses. That decision outlived the code that first made it: the
TypeScript `bridge/` was the prototype, and `pa/` is a client on the same terms.

**The bridge itself is gone**, and its removal is the more interesting half. It polled
the office and asserted a whole pose — `set_avatar` and `move_head` — which is exactly
what the character stack owns, so the two could never have run together: one robot,
two things driving the head. What was worth keeping was never the polling but the
DECISION, one pure function from office state to a mood, and that is now `pa/mood.py`.
The pose went with it, because breath, idle motion and gaze drift already make the head
live and holding an attitude would have traded that for a statue.

## Two brains

`Conversation` calls exactly one method on whatever it is given --
`respond(transcript, state) -> Reply` -- so what runs the model is a choice
rather than a rewrite. `CUBIE_BRAIN` makes it:

| value | what runs | what it costs |
| --- | --- | --- |
| `api` | `brain.Brain`, the Messages API | `ANTHROPIC_API_KEY`, billed per token |
| `cli` | `cli_brain.CliBrain`, a headless `claude` on this machine | no key; a process spawn per turn |
| `auto` (default) | the key when there is one, else the CLI | — |

The CLI brain exists because office-server was already doing this. The office
spawns headless `claude` for its own agents (`agents/claudeCommand.ts` in
Virtual-Office) authenticated by the CLI's own login, so the machine on Cubie's
desk can run a model without a second billing relationship.

`auto` prefers the key **deliberately**. Upgrading this repo must never quietly
move a running deployment onto a different bill or a different model. The
corollary is worth knowing: a key that is *present but wrong* still wins under
`auto`, so if you mean the CLI, say so rather than leaving a stale key in
place.

### What it costs

A spawn per turn. Measured on office-server: **~3.7 s** wall for a trivial
prompt against roughly 2 s for a direct POST, and **~5.3 s** for a turn that
also calls a tool. He gains a beat before answering.

One of those seconds was free and is not paid any more: with an inherited stdin
the CLI waits for piped input before starting, and announces *"no stdin data
received in 3s"*. `CliBrain` passes `DEVNULL`, which is worth three seconds of
every conversation.

### The tool surface is closed, and that is the point

His brain is prompted by whatever is said in the room, and he demonstrably
wakes on the television. An agent with a shell on office-server, driven by
ambient speech, next to the gateway token and the office credentials, is not a
theoretical problem. So the invocation closes the surface three times over,
and any one of them would do it:

```
--tools ""            every built-in tool removed: no Bash, no Read, no WebFetch
--strict-mcp-config   every MCP server ignored except the one named here
--setting-sources ""  no user, project or local settings, so no CLAUDE.md
```

`--allowedTools` then names exactly `mcp__cubie__approve_request`,
`deny_request`, `set_presence` and `set_face`. It is the allowlist, not the
wall. `tests/test_cli_brain.py` asserts each of the three flags separately, so
a failure names which layer went — and the tool-lockdown guard was checked by
removing `--tools ""` from the real invocation and watching it fail.

The subprocess also runs in an empty temporary directory rather than the
repository, and its MCP server is handed `OFFICE_HUB`, the companion token and
the turn log — **not** `STACKCHAN_TOKEN`. That server can approve and deny; it
cannot reach the gateway, move the head or speak.

### Why the tools go through MCP at all

A separate process cannot call the character stack's Python, so the four tools
are served to it over stdio MCP by `pa/office_mcp.py` — the protocol written
out rather than the `mcp` SDK imported, because this process is spawned on
every single utterance and every import it does happens while a person waits.

Both brains dispatch through one implementation, `brain_tools.run_tool`. What
is subtle there is not the schema but the OUTCOMES: `STALE` means somebody else
already handled it and a retry is a second grant, `REJECTED` means our bug and
retrying fails identically, `UNREACHABLE` means the work is still waiting. A
second hand-written copy of that mapping would drift, and the drift would be
invisible — the model told "done" for something that never happened.

`--output-format json` reports the final text and nothing about what was called
along the way, so the MCP server appends each call to a turn log that
`CliBrain` reads back. That is how `set_face` reaches a process that has a face
to set, and how an approval that really happened still gets logged when the
turn times out.

### What must never reach the speaker

`stdout.strip()` would have him read MCP warnings aloud — your own first run
printed three. The answer is parsed strictly out of the JSON `result` and
anything else raises, so machine output ends the turn rather than being spoken.

## Configuration

Copy `config.example.env` to `config.env` and fill it in. `config.env` is gitignored —
the companion token must never be committed, and rotating `AGENTHUB_COMPANION_TOKEN`
on office-server is the only way to revoke this device.

LAN only. The design note rules out a tunnel path for this device entirely.

## Running the character stack

```
sudo cp deploy/cubie-character.service /etc/systemd/system/
sudo sh -c 'umask 077; printf "STACKCHAN_TOKEN=%s\n" \
  "$(sed -nE "s/^STACKCHAN_TOKEN=//p" /etc/stackchan-gateway.env | tr -d "\042\047")" \
  > /etc/cubie-character.env'
sudo awk -F= '/^STACKCHAN_TOKEN=/{print "token length:", length($2)}' \
  /etc/cubie-character.env      # expect 64
sudo systemctl daemon-reload && sudo systemctl enable --now cubie-character
```

The token is **derived, never pasted**: that `sed` reads the gateway's own env
file and the `awk` reports a length rather than a value, so no command here
prints a secret into a terminal that later gets pasted somewhere.

Do **not** symlink `/etc/cubie-character.env` to `/etc/cubie-bridge.env`. That
file does not exist and now never will — the bridge it belonged to has been
removed — and
systemd reports a dangling `EnvironmentFile` as result `'resources'`, which
reads like a resource problem and is a missing file.

Then, for touch to reach him at all — it is off by default:

```
bash ~/cubie/deploy/install-notify.sh
sudo systemctl restart stackchan-gateway
```

**A script, because two attempts at doing it by hand both failed on a guess.**
The first named a `stackchan-gateway` user that does not exist — the gateway
runs as the same user as everything else. The second installed the file under
`$HOME/.config/stackchan-mcp/`, where `$HOME` is whatever the unit's drop-in
says: deployment state that lives on one machine and is written down nowhere.

So both paths are set **explicitly** instead, using overrides the gateway
already provides:

| variable | what it does |
| --- | --- |
| `STACKCHAN_NOTIFY_CONFIG` | where to read `notify.yml` from |
| `STACKCHAN_EVENTS_PATH` | where to append the JSONL event log |

The first earns its place beyond removing the guess: when it points at a
missing file the gateway logs *"STACKCHAN_NOTIFY_CONFIG points to a
non-existent file"*, where the `HOME`-relative default just silently finds
nothing and looks identical to a device that is not reporting. **A wrong path
that says so beats a right path that might not be.**

The script reads `User=` off the unit rather than assuming, checks the account
exists before handing it to `install` — `install -o` otherwise dies with a bare
`invalid user`, which is what sent this round twice — and appends to the
gateway's env file only what is missing, never rewriting it, because that file
carries the token.

### Teaching the gateway this fleet's tools

**Required, and the character stack refuses to start without it.** The gateway
proxies a **hardcoded** table of tool names, so a tool added to the device
firmware is unreachable until the gateway knows it too — it answers an unknown
name with `{"error": "Unknown tool: set_gaze"}` and never forwards it. 0.17.0
is the latest release on PyPI, so there is no upstream fix to wait for.

```
bash ~/cubie/gateway/apply-gateway-tools.sh
sudo systemctl restart stackchan-gateway
make check-gateway
```

That adds `set_gaze`, `set_feature` and `set_speech` in the two places a name
has to appear — `tool_map` so `tools/call` routes it, and the `Tool(...)` list
so `tools/list` advertises it, which is what the MCP SDK and `pa/live.py`'s own
startup check actually read.

Patched rather than forked, which is the same bargain the firmware makes: the
installed bytes stay a known PyPI release and every local change is a small,
anchored, idempotent script in this repo. A `pip install --force-reinstall`
reverts it and re-running puts it back — a property a fork does not have.

`make check-gateway` is the guard, with the contract guard's three exit codes:
**0** everything routes, **1** something is missing and named, **2** could not
look. The third is distinct because treating "I could not check" as "nothing
missing" is what let this blocker reach hardware in the first place.

Without those three tools the stack **runs degraded rather than failing**: head
motion, blink, the six faces and the LED ring all work, and it logs what is
lost — breathing, gaze drift, mouth tilt, dance faces, speech bubbles.

It runs on the **gateway's** interpreter, not `pa/.venv` — the `mcp` package
lives there and nowhere else. The character logic itself imports nothing from
it; the robot sits behind a protocol precisely so `pa/.venv` can run the tests
with no `mcp` installed at all.

`--idle-level` carries M5's four levels: 0 no head movement, 1 every 8–12 s, 2
every 4–8 s (default), 3 every 2–4 s. The **face drifts at every level**,
including 0 — the levels are about how much the head moves, and a still head
with a living face is a coherent thing to want.

## The brain

Tap his head and he listens, thinks with the office's state in front of him,
answers out loud, and can act on what is waiting.

```
tap  ->  listen (5 s)  ->  Claude, with the office state  ->  speak  +  act
```

Four tools: **approve** an approval, **decline** one with a reason, tell the
office whether **someone is at the desk**, and **change his face** while he
answers. Stroking his head is still just affection — only a *tap* starts a
conversation, or being petted would begin one every time.

### It only acts when told to

This can approve real work in a real office, and there is no unapprove
endpoint. So the guardrail is narrow and deliberate: act on an instruction
actually present in what was said, never on an inference that acting would be
helpful. *"Is anything waiting?"* is a question, not permission.

The prompt also carries the thing that is easy to get wrong about approving:
the office resumes an agent by spawning a **brand-new process**, so the grant
covers that agent's whole next turn rather than the one action it stopped on.
An assistant saying "I've allowed that one action" would be describing
something the office does not do.

### Three processes, and why

| what | where | why |
| --- | --- | --- |
| character stack + brain | the **gateway's** venv | `mcp` lives only there |
| the voice | `pa/.venv`, as a **subprocess** | `piper-tts` lives only there |
| the office | HTTP | it is a different machine's service |

The two virtualenvs are separate on purpose — the Makefile says why putting our
dependencies in the gateway's would be wrong — so the brain calls the Messages
API over `httpx` rather than adding the Anthropic SDK to a venv that must stay
a pinned upstream release, and the voice is invoked as the same command a
person would type.

The cost is loading the 60 MB Piper model per utterance, which is unmeasured on
this hardware. If it hurts, the fix is a long-lived voice worker, not merging
the virtualenvs.

### He answers to his own name

The wake word is **"hi cubie"**, and it is his name rather than one of
Espressif's pre-trained phrases because the firmware supports a **custom** one
directly — `CONFIG_USE_CUSTOM_WAKE_WORD`, implemented in
`main/audio/wake_words/custom_wake_word.cc`, which drives esp-sr's MultiNet
command recogniser and registers the phrase at runtime with
`esp_mn_commands_add()`.

WakeNet, the always-on detector, only knows phrases Espressif has trained — the
list runs to Alexa, Jarvis, Computer, Hi ESP and a dozen others, and you cannot
add to it, because each is a neural net trained on that specific phrase.
MultiNet is the other half of esp-sr, and it takes phrases you give it.

**Plain words, not phonemes**, and that is a model choice. Espressif's docs:
*"MultiNet5 requires the input command string to be phonemes, and MultiNet6 and
MultiNet7 only accepts grapheme inputs to API calls."* MultiNet7 fed graphemes
runs its own grapheme-to-phoneme step at runtime, which the same docs say costs
*"a little accuracy drop"* — so **MultiNet6**, whose native input is graphemes,
is the right model for a phrase written as words.

Three knobs in `build.conf`:

```
WAKE_WORD="hi cubie"        plain lower-case words
WAKE_WORD_DISPLAY="Cubie"   what the firmware calls him
WAKE_WORD_THRESHOLD=20      1-99, and LOWER IS MORE SENSITIVE
```

*"hi cubie"* rather than a bare *"cubie"*: a two-syllable name on its own
false-fires far more than one with a carrier word, and MultiNet is a command
recogniser rather than a purpose-trained wake model, so it needs the help. The
threshold is the other knob — raise it if he answers when nobody spoke to him.

`CONFIG_WAKE_WORD_DETECTION_IN_LISTENING` is also on, which the firmware
defaults off. It lets you cut in while he is talking, and for a conversation
rather than a query that is most of what makes it feel like talking to someone.

Requires ESP32-S3 with PSRAM, which CoreS3 is. Whether the pinned esp-sr
(`~2.3.0`) carries `MULTINET6_QUANT` is something only the build can confirm —
the `sdkconfig_append` echo lists what it set, and the boot log carries
`custom_wake_word.cc`'s own lines.

### Why the wake word needed a firmware change

The short version: **in the xiaozhi protocol the server ends every capture, and
nothing was ending the wake word's.** Traced through the firmware rather than
assumed:

- `Application::StopListening()` is called from exactly **one** place — the
  incoming-JSON handler, on `{"type":"listen","state":"stop"}` from the server.
- The device *has* a voice-activity detector, but its callback only sets
  `voice_detected_` and flashes an LED. **Nothing reads it to end a capture.**
- `kListeningModeAutoStop` names the mode the *server* is told about. It does
  not make the device stop by itself.

So on the wake-word path the device records, the gateway buffers, and each
waits for the other. On the LCD-touch path `ToggleChatState` sets
`kListeningModeManualStop`, which is why touching the screen records until you
touch it again — that is the firmware working as designed, not a fault.

And the gateway's only way to end a listen is its `listen` tool: start, wait a
**fixed** window, stop. Fine for a timed capture, useless for a conversation,
because it cannot know when a sentence finished.

`apply-vad-auto-stop.sh` adds the missing half, using the VAD event the
firmware already raises and already ignores: speech stops → arm a timer; speech
resumes → cancel it; timer expires → `StopListening()`, exactly as if the
server had asked. **1200 ms** of silence, because a natural mid-sentence pause
is commonly 300–600 ms.

Two guards, both load-bearing. It will not fire before any speech has been
heard, or a wake word with nobody talking would end the capture instantly and
transcribe silence every time. And a **15 second** maximum ends it regardless,
because a room with a fan in it can hold the VAD high indefinitely — a capture
that never ends is the bug this removes, not one to reintroduce from the other
side. Manual-stop captures are left alone: a held button is deliberate.

### What M5's software does about conversation: nothing

Worth recording, because it is not what it looks like. Their `app_ai_agent` is
**57 lines** whose entire body is `GetHAL().requestXiaozhiStart()`. The
conversation belongs to the xiaozhi firmware talking to a xiaozhi-protocol
server — theirs is a Go service in `server/` with "XiaoZhi integration logic".

What M5 *does* contribute is the avatar reacting to those states, and that is
already ported: `SetStatus` driving idle motion and speaking, `SetEmotion`
mapping emotion words onto faces, the modifier stack, the dances.

So there is nothing left in M5's tree to port for the conversation itself. The
missing piece was never theirs — it is that **we** have to be the server side
of that loop, which is what the gateway plus a hook receiver are.

### The hook receiver — what happens after the wake word

`pa/hook.py` serves `STACKCHAN_AUDIO_HOOK_URL`. Until it existed the gateway
logged *"device-driven listen.start ignored (STACKCHAN_AUDIO_HOOK_URL not
configured)"* and **dropped every frame** — so the wake word could wake him and
nothing could come of it. Two listeners now, and they are not equivalent:

|  | starts on | ends on |
| --- | --- | --- |
| `listen` tool (tap) | us | a fixed window, always waited out |
| the hook (wake word) | the device | the device's **own VAD** |

The hook path is the better listener, which was the whole point: five seconds
of window plus about 2.3 s of transcription is most of the latency in a turn,
and most of that window is usually silence.

`listen` stays anyway, because **it is the only listener we can start.**
Nothing in the gateway's tool table makes the device begin listening — checked
against the table, not assumed — so a tap has nothing else to call, and a
follow-up question still needs the wake word again.

The path, and where each piece lives:

```
device VAD stops -> gateway packs Ogg -> POST -> hook.py -> ogg_opus.py
   -> the gateway's own faster-whisper -> Conversation.turn_on_transcript
```

**It borrows the gateway's recogniser rather than loading a second one.**
`transcribe.py` pulls `faster-whisper` out of `stackchan_mcp.stt`'s registry —
one model, one cache, one set of settings, and `STACKCHAN_FASTER_WHISPER_MODEL`
keeps working because it is the engine's own variable. A transcript from the
wake word therefore *matches* a transcript from a tap instead of merely
resembling it.

**It answers 202 before doing the work.** `push_audio_capture` gives the POST a
10 second total timeout and a turn is transcription plus a model call plus
speech, so holding the connection would make every *successful* turn appear in
the gateway's log as a failed push. The honest consequence: a 202 means
"received", not "understood" — nothing the receiver returns can report a failed
transcription, so those are the character stack's to log.

**No new secret.** The gateway signs the POST with
`STACKCHAN_AUDIO_HOOK_TOKEN`, falling back to the `STACKCHAN_TOKEN` both ends
already share, so the receiver checks against the token it already has. The
installer sets the URL and deliberately not a token: a second secret to keep in
step would only add a way for them to disagree.

**It is stdlib-only, on a thread.** `aiohttp` is in the gateway's virtualenv,
but importing it would put the receiver out of reach of `pa/.venv`, where the
tests run — and the tests are worth more, because they drive the whole thing
over a real socket with no robot, no gateway and no model.

**A mangled body is refused, not transcribed.** Every Ogg page carries a CRC;
whisper turns noise into words happily, and the brain would then act on a
sentence nobody said.

**The LCD touch goes down this path too.** The gateway treats wake word, button
and `ToggleChatState` alike, so touching the screen now produces a turn as
well — one that ends when you touch it again, because that path is
manual-stop by design.

### What is not built yet

**A follow-up turn without the wake word.** He answers and stops; carrying on
means saying "hi cubie" again. The device's VAD cannot be re-armed from the
host — there is no tool for it — so the options are a fixed-window `listen`
after each answer (the worse listener, and it would record the room whether or
not anyone meant to continue) or a firmware change. Deliberately unresolved
until the single-turn path has been used on hardware.

### Configuration

`ANTHROPIC_API_KEY` in `/etc/cubie-character.env`. **Without it tap-to-talk is
off and everything else still runs** — idle motion, breathing, the face,
head-pet — and it says so once at startup rather than failing or, worse,
leaving a tap that silently does nothing.

## The green bar

No CI, by design — same call as the sibling device repos. The bar is `make check`:

```
VO_REPO=/path/to/virtual-office make check
```

`VO_REPO` defaults to `~/virtual-office`, which is where the office's checkout
lives on office-server. It used to default to a sandbox path, so the default was
wrong on every machine the bar is actually run on.

The bar is five checks: `check-contract`, `check-lint`, `check-bridge`,
`check-pa`, `check-config`.

`check-lint` is pyflakes over every `.py` in the repo, and **only** pyflakes:
one question — is any name here undefined or unused — with no style opinions to
configure, argue about or silence. It earns its place on evidence. Two real
defects had this exact shape: `Pose` and `Any` used in annotations that were
never imported (harmless under PEP 563, but `get_type_hints` raises), and five
stale imports that accumulated while nothing was watching, two of which survived
a file being rewritten around them.

`check-contract` exit codes: **0** in sync, **1** drifted, **2** could not check.

That third one is load-bearing twice over. Treating "I could not look" as "no drift"
makes the guard decoration — and **a checkout behind its own remote is exit 2 as well**,
because comparing against stale bytes proves nothing. That is not hypothetical: on
2026-09-05 the K10's copy of this guard passed cleanly against a working tree 35
commits behind `main`, while `main` had grown two fields it knew nothing about.

**And the first version of that staleness check had the same blind spot it was
written to close.** It compared `HEAD..origin/main` — but `origin/main` is a
*local* ref, only as fresh as the last fetch, so a checkout nobody had fetched
in a fortnight had an equally old `origin/main`, counted zero commits behind,
and passed. It now asks the remote directly with `ls-remote`, which needs
network and credentials; when it cannot reach them that is exit 2, not a pass.
Read-only on purpose — a guard that fetches into someone else's checkout is a
guard people stop running.

## Touch, and why it took three attempts

M5's factory firmware detects stroking reliably. Ours did not, twice. Their
`hal_head_touch.cpp` explains it: **they are not measuring what we were.**

The Si12T reports a 2-bit *level* per pad in one register byte. M5 keeps those
levels and computes a weighted centroid:

```
position = (i0*(-100) + i1*0 + i2*100) / (i0 + i1 + i2)     // -100..+100
```

That is *where on the head* the finger is. A gesture is the change in that
position since touch-down: `delta > +40` forward, `< -40` backward. **Duration
appears nowhere in their code. Neither do zones.**

Upstream reduces the same byte to three booleans with `!= 0`, discarding the
magnitude — so position is gone before anyone can use it, and classification is
left with hold time, which cannot separate the gestures here: release
confirmation was 4 samples × 100 ms, so the shortest measurable press was
~400 ms while real strokes measured 399–600 ms.

The 400 ms release debounce existed, by its own comment, to "bridge
finger-glide gaps that otherwise cut a stroke short" — a workaround for having
thrown away the signal that made it unnecessary. Attempt two (counting distinct
zones touched) was reconstructing a 3-level approximation of M5's continuous
position from the booleans that survived.

`apply-m5-touch.sh` keeps the levels, computes M5's position, and fires the
swipe **while the finger is still moving** rather than on release — which is
also why theirs feels immediate. Stroke *direction* is now known, which
upstream never had.

## Design constraints carried from S0

- **`present` absent ≠ `present: false`.** Absent means nothing is reporting; false
  means the desk is known empty. They route differently.
- **`pendingTotal` and `founderTasksTotal` may exceed their lists.** The lists are
  capped. Showing a short list as if it were everything is the failure those fields
  exist to prevent.
- **`blocked` and `review` are different actions**, not shades of one. A device that
  collapsed them would send its owner to do something without saying what.
- **`ts` is epoch milliseconds**, so it needs 64 bits. `long` is 32-bit on an ESP32
  and 64-bit on a test host — the K10 shipped a parser that passed every host test
  and would have saturated on the device.
- **Mood is decided server-side.** Deriving it on-device would be a second definition
  of "busy", free to disagree with the wall.
- **Servo pitch is clamped `5..85`** (firmware hard limit `0..88`). Never trust an
  upstream value; `tracking.py` clamps every motion command.
- **An unreachable office must never look like a quiet one.** A thinking face and
  amber LEDs, never the calm idle. `pa/mood.py` holds that, and a test asserts no two
  moods look alike — which caught `offline` and `review` colliding the moment the
  bridge's poses stopped separating them.
