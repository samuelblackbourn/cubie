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
| S2 — bridge, Rung 1 (posture mirrors the office) | **Written**, `bridge/`. Not yet deployed |

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
secret, and survives office-server's address changing — which is not
hypothetical, since that address is a DHCP lease baked into the firmware.

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
`STACKCHAN_TOKEN` and is the same value the bridge needs in
`/etc/cubie-bridge.env`.

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

`--character retro` is `en_US-ryan-high` pitched up 30% and chopped at 22 Hz:
a young voice sounding like it is spoken through a desk fan.

The carrier frequency is the whole trick. Every other character modulates at
45–140 Hz, which is fast enough to fuse into a tone and read as electronic. A
fan chops at its blade-pass rate — tens of hertz — so at 22 Hz the ear hears
the individual chops. Verified as 22 chops a second through a flat test tone
rather than assumed from the parameter.

The depth is **measured, not chosen by ear**. `ring_modulate` applies
`gain = 1 − depth + depth·sin`, so depth 0.5 is exactly where the trough
reaches zero and anything above it inverts phase through a full gate. At 22 Hz
each chop lasts 45 ms, and the question is how much of it the gate eats:

| depth | gain range | near-silent per chop |
| --- | --- | --- |
| 0.40 | +0.20 … 1.00 | none |
| **0.45** | **+0.10 … 1.00** | **~10 ms** |
| 0.55 | −0.10 … 1.00 | ~16 ms, and phase inverts |

A gap under about 10 ms is one the ear fills in; past that speech stutters
rather than throbs. So 0.45 is the deepest chop available before it costs
syllables — which matters for a voice that reads out approvals.

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
bridge (TypeScript, this repo)          ← S2
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

**The bridge is an MCP client, not a protocol implementation.** Rather than reimplement
WebSocket MCP, the upstream gateway runs as-is and the bridge is a client of it — a
pattern the office already uses. The bridge stays small and testable: poll, diff, call
tools.

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
file does not exist — the bridge is written but has never been deployed — and
systemd reports a dangling `EnvironmentFile` as result `'resources'`, which
reads like a resource problem and is a missing file.

Then, for touch to reach him at all — it is off by default:

```
sudo install -d -o stackchan-gateway -g stackchan-gateway     /var/lib/stackchan-gateway/.config/stackchan-mcp
sudo install -o stackchan-gateway -g stackchan-gateway -m 0644     ~/cubie/deploy/stackchan-notify.yml     /var/lib/stackchan-gateway/.config/stackchan-mcp/notify.yml
sudo systemctl restart stackchan-gateway
```

It runs on the **gateway's** interpreter, not `pa/.venv` — the `mcp` package
lives there and nowhere else. The character logic itself imports nothing from
it; the robot sits behind a protocol precisely so `pa/.venv` can run the tests
with no `mcp` installed at all.

`--idle-level` carries M5's four levels: 0 no head movement, 1 every 8–12 s, 2
every 4–8 s (default), 3 every 2–4 s. The **face drifts at every level**,
including 0 — the levels are about how much the head moves, and a still head
with a living face is a coherent thing to want.

## The green bar

No CI, by design — same call as the sibling device repos. The bar is `make check`:

```
VO_REPO=/path/to/virtual-office make check
```

`check-contract` exit codes: **0** in sync, **1** drifted, **2** could not check.

That third one is load-bearing twice over. Treating "I could not look" as "no drift"
makes the guard decoration — and **a checkout behind its own remote is exit 2 as well**,
because comparing against stale bytes proves nothing. That is not hypothetical: on
2026-09-05 the K10's copy of this guard passed cleanly against a working tree 35
commits behind `main`, while `main` had grown two fields it knew nothing about.

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
  upstream value; the bridge clamps every motion command.
- **An unreachable office must never look like a quiet one.** Confused posture and
  amber LEDs, never the calm idle.
