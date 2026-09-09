# Hardware checks

Everything in this repo that could be proved without the robot has been. This
file is what is left: the checks that need Cubie in front of you, in the order
that makes a failure diagnosable rather than confusing.

**Why a written list.** Four of these were merged on reasoning alone —
firmware I could not compile here, a wake word whose Kconfig symbol I read from
`master` because the tagged esp-sr was unreachable, and a device-side VAD I
traced through the source but never watched fire. Each is a claim, not a
result, and a claim that stays unwritten is one that quietly turns into
folklore. Every entry below says what it *proves*, so a pass means something
specific.

**Order matters.** 1–3 put the new firmware on the device and the host wiring
in place; nothing downstream is meaningful until those pass. 4–8 are the
device behaviours that were reasoned about and never seen. 9–12 are the
end-to-end path. A failure in an early check invalidates the later ones rather
than merely delaying them — so stop at the first red and report it.

Paste back the output blocks, not a summary of them: several of these
distinguish two very different faults by a single log line.

---

## 1. Build the firmware that carries the wake word

**Proves:** that the pinned ESP-IDF's esp-sr actually offers
`CONFIG_SR_MN_EN_MULTINET6_QUANT`, which is the one config entry in the whole
patch set I could not check. The esp-sr tags were unreachable from the
sandbox, so that symbol was read off `master` — if the pinned version predates
it, Kconfig will say so here and nowhere else.

```bash
bash ~/cubie/firmware/build.sh --check
bash ~/cubie/firmware/build.sh
```

**Pass:** `--check` reports the patch chain and the config entries it would
set; the real run compiles and publishes an OTA image.

**If it fails:**

- A Kconfig error naming `CONFIG_SR_MN_EN_MULTINET6_QUANT` or
  `CONFIG_USE_CUSTOM_WAKE_WORD` means the pinned esp-sr does not have that
  symbol. Send the exact line — the fix is a different MultiNet version, and
  which one depends on what the error says is available. Do **not** fall back
  to MultiNet7 on a hunch: it accepts graphemes but runs runtime G2P at a
  documented accuracy cost, and MultiNet5 needs phonemes rather than the plain
  `"hi cubie"` in `build.conf`.
- "already patched" for every step is not a failure — it is the idempotence
  the chain is built on.
- Anything about `custom_wake_word.cc` not existing means the upstream pin
  moved under us; send the path it names.

## 2. Take the new image

```bash
# then reset Cubie by hand
```

**Proves:** the OTA path still works, and it is the deliberate human gate — a
build does not interrupt a robot mid-sentence, so nothing above reaches the
device until you reset it.

**Pass:** he comes up on the new build. If the version does not change, the
publish step is where to look, not the device.

## 3. Host wiring: tools, notify, service

Three things the character stack needs, each of which has failed before in a
way that looked like something else.

```bash
cd ~/cubie
make check-gateway
bash ~/cubie/deploy/install-notify.sh
sudo systemctl restart stackchan-gateway
systemctl status cubie-character --no-pager -l | head -20
```

**Pass:** `make check-gateway` exits 0. `install-notify.sh` prints what it set
or that it was already set. `cubie-character` is `active (running)`.

**If it fails:**

- `make check-gateway` exit **1** names the missing tool — run
  `bash ~/cubie/gateway/apply-gateway-tools.sh`, restart the gateway, re-check.
  Exit **2** means it could not look, which is a different problem from
  nothing being missing and is why the two codes are distinct.
- `Failed to load environment files` on `cubie-character` is a missing or
  dangling `/etc/cubie-character.env`, reported by systemd as result
  `'resources'` — it reads like a resource shortage and is a missing file.
- Any `Tool set_gaze not listed by server` / `set_feature not listed` warning
  in the character log means check 3's first line passed against a gateway
  that has since been reinstalled. `pip install --force-reinstall` reverts the
  patch by design; re-running the script puts it back.

## 4. Touch reaches the host

**Proves:** the `notify.yml` install actually landed, which is the open
question from the last hardware round — the reaction to a stroke was missing
and the cause was that no touch event ever left the gateway.

Stroke his head, then:

```bash
sudo tail -3 /var/lib/cubie/stackchan-events.jsonl
journalctl -u stackchan-gateway --since '2 min ago' --no-pager | grep -i notify
```

**Pass:** a JSON line with `"event_type": "touch"` and a `subtype`.

**Three outcomes, three different faults** — this is the check whose output is
worth pasting verbatim:

- **A touch line appears.** The path was the problem and it is fixed. He should
  also react (check 8).
- **The gateway complains that `STACKCHAN_NOTIFY_CONFIG` points at a
  non-existent file.** The install put the config somewhere the gateway is not
  reading. That message exists precisely so this case names itself instead of
  looking like a silent device.
- **Neither: no line, no complaint.** The device is not emitting touch events
  at all. Check `get_touch_sensor_enabled` on the device before touching
  anything on the host — the host is then not where the fault is.

## 5. "Hi cubie" wakes him

**Proves:** the custom wake word works with a plain-text (grapheme) command
string, which is the whole premise of choosing MultiNet6.

Say **"hi cubie"** from normal talking distance.

**Pass:** he enters listening — the LED ring and the recording indicator, the
same states a screen touch produces. With the hook receiver in place he should
then *answer*; if he wakes but never speaks, that is check 6 or the receiver
rather than the wake word, so record which half worked.

**If it fails:** report which of these it is, because they are different fixes.

- **Nothing at all, ever.** Either the build did not include the wake word
  (check 1's config output) or the threshold is too tight.
  `WAKE_WORD_THRESHOLD` is 1–99 and **lower is more sensitive** — the default
  is 20, so 10 is the direction to try.
- **He wakes at random, without being addressed.** Threshold too loose; raise
  it.
- **Only when shouted, or only from very close.** Threshold, same as the first
  case, but worth saying separately: a two-syllable name run through a command
  recogniser rather than a purpose-trained wake model needs the help.

## 6. He stops listening on his own

**Proves:** `apply-vad-auto-stop.sh` — the check I most want, because for two
messages I asserted the device already did this and it did not. In the xiaozhi
protocol the **server** ends every capture; the device's VAD only flashed an
LED. This patch is the missing half.

Wake him with "hi cubie", say one sentence, then **stop talking and do not
touch him**.

**Pass:** the capture ends about **1.2 s** after you stop. That number is
deliberate — a natural mid-sentence pause is commonly 300–600 ms, so the
silence window has to clear that without feeling laggy.

**If it fails:**

- **Records forever.** The VAD event is not reaching the new handler. Nothing
  to guess at on the host; send the device log.
- **Cuts you off mid-sentence.** The silence window is too short for how you
  actually pause. It is one constant, `CUBIE_AUTO_STOP_SILENCE_MS`.
- **Ends instantly, before you say anything.** The "heard speech first" guard
  is not holding — that guard exists so a wake word in an empty room does not
  transcribe silence.

## 7. The 15-second ceiling holds

**Proves:** the second guard, which is not a nicety — a room with a fan in it
can hold a VAD high indefinitely, and a capture that never ends is the exact
bug check 6 removes. It must not come back from the other side.

Wake him and then keep talking past 15 seconds.

**Pass:** the capture ends at roughly 15 s regardless.

## 8. Nothing regressed on the reflash

**Proves:** the parts that already worked still do. Checks 1–7 changed the
firmware, and the head, voice and faces are the things a bad merge would take
out quietly.

Watch him idle for a minute or two, then tap his head.

**Pass, in the order you will notice it:**

- Head motion is smooth and does not hunt or hit a limit.
- The face drifts and blinks while idle — that happens at every idle level,
  including 0, since the levels govern the head rather than the face.
- The voice is the tuned `retro` preset: `en_US-ryan-high`, pitched up, mild
  ring modulation. It was called perfect after the last round, so any change
  here is a regression, not a preference.
- A stroke of the head gets the head-pet reaction (this is check 4 paying off).
- Faces change with what he is doing; the thinking face looks aside rather than
  straight ahead.

## 9. Tap-to-talk end to end

**Proves:** the brain path — listen, think with the office's state in front of
him, speak.

Tap his head, ask him something about the office.

**Pass:** he listens, then answers out loud with something that reflects real
office state.

**If it fails:** a single startup line saying `ANTHROPIC_API_KEY` is not set
means tap-to-talk is off and everything else still runs — that is deliberate,
and it is a missing key rather than a broken tap.

## 10. He can act, not just answer

**Proves:** the four brain tools actually reach the office.

With a request waiting for approval in the office, ask him to approve it.

**Pass:** the office shows it approved, and he says so.

**Worth knowing before you read a failure:** the office resumes an agent by
spawning a **brand-new process**, so an approval covers the whole resumed turn
rather than the one action that was refused, and there is no unapprove
endpoint. A `409` is a state mismatch — something already handled it — and the
right behaviour is to say so, not to retry.

## 11. Sanity on the numbers

**Proves:** the one calibration in the port that is a stated guess rather than
a derivation. M5's angle units are tenths of a degree (proved from their
`hal_servo.cpp`), but their *speed* units are converted with
`DPS_PER_M5_SPEED = 0.25`, which was chosen, not measured.

Watch a dance.

**Pass:** it looks like a dance — the motion reads as deliberate rather than
frantic or sluggish.

**If it is off:** it is one constant in `pa/units.py`, and "too fast" or "too
slow" is enough for me to correct it. This is the only entry here where the
right answer is a matter of taste.

## 12. The wake word actually answers

**Proves:** the receiver and the gateway meeting — the one thing about
`pa/hook.py` that cannot be tested without both. Everything up to the socket is
covered by tests; what is not is whether the gateway's POST arrives, is
authorised, and holds audio the borrowed recogniser can read.

This check only means something once 5 and 6 pass. Say "hi cubie", ask him
something about the office, and stop talking.

**Pass:** he answers out loud, without being touched.

**If it fails, the logs separate the cases, and they are different faults:**

```bash
journalctl -u stackchan-gateway --since '3 min ago' --no-pager | grep -i -e listen -e hook
journalctl -u cubie-character --since '3 min ago' --no-pager | tail -30
```

- **"device-driven listen.start ignored (STACKCHAN_AUDIO_HOOK_URL not
  configured)"** — the gateway was never told where to POST. Re-run
  `bash ~/cubie/deploy/install-notify.sh` and restart the gateway.
- **Connection refused, on the gateway side** — the URL is set but nothing is
  listening. The character stack said why at startup: either `audio hook is not
  listening` (a bound port) or `wake-word audio cannot be transcribed` (the
  gateway's `[stt]` extra is missing, which would also mean tap-to-talk has
  never worked).
- **A 401, on the gateway side** — the two ends disagree about the token. They
  should not: nothing sets `STACKCHAN_AUDIO_HOOK_TOKEN`, so both fall back to
  the `STACKCHAN_TOKEN` they already share. If one *is* set, that is the thing
  to remove.
- **`capture refused ... CRC`** in the character log — the body arrived
  mangled. Deliberately not transcribed, because whisper would turn the noise
  into words and the brain would act on them. Send the line.
- **He answers, but slowly.** Transcription is most of it. The model is the
  gateway's own, so `STACKCHAN_FASTER_WHISPER_MODEL` moves both listeners at
  once — `tiny.en` is the next thing to try, and it costs accuracy.

**Also worth trying while you are there:** touch the LCD, speak, touch it
again. The gateway sends device-driven captures down this same path however
they started, so that should now produce a turn too — ending on the second
touch, because that path is manual-stop by design. It is not a separate
feature, and if it works while the wake word does not, that is evidence the
receiver is sound and the recogniser is not hearing his name.

---

## Not on this list, and why

- **Anything I could prove without the robot** is a test, not a check: the
  patch chain runs against a real fetched tree and the inserted C++ compiles
  under ASAN and UBSan with behavioural assertions, which is what caught the
  silent no-op on a half-given feature position. `make check` is the bar.
- **The hook receiver.** Now built (`pa/hook.py`), and tested over a real
  socket with no robot — including a round-trip against the gateway's own Ogg
  packer, which is the producer of every body it will ever see. What hardware
  adds is the two ends meeting, which is check 12.
- **Screen-touch behaviour.** Touching the LCD records until you touch it
  again. That is `ToggleChatState` setting `kListeningModeManualStop`, and the
  auto-stop patch deliberately leaves manual-stop captures alone — a held
  button is deliberate. It is the firmware working as designed, so there is
  nothing to check.
