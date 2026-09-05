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
| S1 — self-hosted server + reflash | Not started |
| S2 — bridge, Rung 1 (posture mirrors the office) | Not started |

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
