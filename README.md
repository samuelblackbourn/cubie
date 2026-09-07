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

Configuration lives in `firmware/build.conf` — the two upstream pins, the
office's address as the robot sees it, and the toolchain image. All of it is
overridable from the environment.

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
