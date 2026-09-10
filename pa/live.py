"""Run the character stack against the real robot.

Everything in `chan.py`, `modifiers.py`, `animation.py` and `driver.py` is pure
and clock-driven precisely so that this file can be thin and boring. It does
three things:

  1. opens one long-lived MCP session to the gateway,
  2. ticks the driver on a fixed interval,
  3. tails the gateway's event log so a stroke of the head reaches the head-pet
     reaction.

--- Run it with the GATEWAY's interpreter ---

The `mcp` package lives in the gateway's virtualenv, not in `pa/.venv`. This is
the same split `tools/cubie-call.py` documents, and the reason the character
logic keeps the robot behind the `Effector` protocol: `pa/.venv` runs the tests
with no `mcp` at all.

    export STACKCHAN_TOKEN=$(sudo sed -nE 's/^STACKCHAN_TOKEN=//p' \\
        /etc/stackchan-gateway.env | tr -d '\\042\\047')
    ~/stackchan-gateway/bin/python ~/cubie/pa/live.py

--- Why a fixed tick and not an event loop per modifier ---

M5 runs its whole modifier pool off one `update()` in the LVGL task. A tick is
the same thing, and it means the fastest thing here (breath, every 600 ms) and
the slowest (idle motion, every 4-8 s) share one clock with no scheduling. At
10 Hz the tick costs nothing: the flush sends only what changed, so a quiet
second is zero calls.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import brain as brain_mod  # noqa: E402
import cli_brain as cli_brain_mod  # noqa: E402
import conversation as conversation_mod  # noqa: E402
import driver as driver_mod  # noqa: E402
import hook as hook_mod  # noqa: E402
import office as office_mod  # noqa: E402
import ogg_opus  # noqa: E402
import transcribe as transcribe_mod  # noqa: E402
import voice_settings  # noqa: E402
from chan import Chan  # noqa: E402
from tracking import Pose  # noqa: E402

logger = logging.getLogger("cubie.live")

#: The gateway's loopback MCP surface. Same default as the bridge's.
DEFAULT_MCP_URL = "http://127.0.0.1:8767/mcp"

#: Where the gateway appends physical events.
#:
#: MUST MATCH `EVENTS_PATH` in deploy/install-notify.sh, which is what sets
#: the gateway's `STACKCHAN_EVENTS_PATH`. A test asserts the two agree,
#: because when they disagree the gateway writes events nobody tails -- which
#: looks exactly like a device that is not reporting, and is the failure that
#: script exists to remove.
#:
#: An explicit path rather than the gateway's default of
#: `~/.claude/stackchan-events.jsonl`: that resolves against the gateway's
#: HOME, which is whatever its unit drop-in says, and guessing it wrong is
#: silent at both ends.
#:
#: **This log is OFF by default.** `notify_config.py` defaults `jsonl.enabled`
#: to false, so touch events reach nothing until a `notify.yml` turns it on --
#: see `deploy/stackchan-notify.yml` and the README. Head-pet is wired and
#: dormant until then, which is a config step and not a code one.
DEFAULT_EVENT_LOG = Path("/var/lib/cubie/stackchan-events.jsonl")

#: 10 Hz. Fast enough for breath's 600 ms and blink's 200 ms, slow enough to be
#: free.
TICK_S = 0.1


def failure_reason(result) -> str | None:
    """An error the MCP layer does NOT flag as one, or None.

    The gateway answers a tool it does not know with an ordinary, successful
    result whose text happens to be `{"error": "Unknown tool: set_gaze"}` -- no
    `isError`, no exception. So `tool_failed()` returns False and a call that
    did nothing at all reads as a success.

    That is exactly the silent no-op this project keeps paying for, and it hid
    a real blocker for a while: the gateway's tool table is hardcoded, so a
    tool added to the DEVICE firmware is not reachable until the gateway knows
    it too. Worth detecting rather than trusting the flag.
    """
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            error = payload.get("error")
            if error:
                return str(error)
            # Our firmware tools answer {"ok": false, "error": "..."}; the
            # `error` branch above catches those, but a bare ok:false should
            # not pass either.
            if payload.get("ok") is False:
                return "tool reported ok=false"
    return None


class McpEffector:
    """The `Effector` protocol, spoken to the gateway.

    Every method is fire-and-forget onto a queue rather than awaited: a
    modifier must never block the tick on a round trip, and the flush already
    guarantees we only send what changed. A dropped call is corrected by the
    next flush that sees a difference -- which is why the sent-record is
    updated optimistically rather than on acknowledgement.
    """

    def __init__(
        self, session, loop: asyncio.AbstractEventLoop, available: set[str] | None = None
    ) -> None:
        self._session = session
        self._loop = loop
        # None means "assume everything works" -- only the tests do that.
        self._available = available
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.dropped = 0
        self.failed = 0
        self._failed_tools: dict[str, int] = {}

    def _send(self, tool: str, arguments: dict) -> None:
        # Drop calls the gateway cannot route, rather than queueing them to be
        # refused. check_tools() has already said what is missing and why; this
        # keeps the queue for work that can actually happen.
        if self._available is not None and tool not in self._available:
            return
        try:
            self._queue.put_nowait((tool, arguments))
        except asyncio.QueueFull:
            # Better to lose a frame of breathing than to stall the tick.
            self.dropped += 1
            if self.dropped % 50 == 1:
                logger.warning("gateway queue full, dropped %d calls", self.dropped)

    async def drain(self) -> None:
        from mcp_compat import tool_failed

        while True:
            tool, arguments = await self._queue.get()
            try:
                result = await self._session.call_tool(tool, arguments)
                reason = failure_reason(result)
                if tool_failed(result) or reason is not None:
                    self._count_failure(tool, reason or "call reported an error")
            except Exception as exc:  # noqa: BLE001 - one bad call must not end the run
                self._count_failure(tool, str(exc))
            finally:
                self._queue.task_done()

    def _count_failure(self, tool: str, reason: str) -> None:
        """Log the first failure of each tool loudly, then rate-limit it.

        A tool that cannot work never starts working, so logging every attempt
        buries the diagnosis under thousands of identical lines -- breath alone
        retries twice a second forever. The first line is the one that matters.
        """
        self.failed += 1
        seen = self._failed_tools.get(tool, 0)
        self._failed_tools[tool] = seen + 1
        if seen == 0:
            logger.error("%s FAILED: %s (further failures rate-limited)", tool, reason)
        elif seen % 500 == 0:
            logger.warning("%s has now failed %d times: %s", tool, seen + 1, reason)

    # ------------------------------------------------------------- tools --
    def set_avatar(self, face: str) -> None:
        self._send("set_avatar", {"face": face})

    def set_feature(self, feature: str, **axes) -> None:
        # The firmware's sentinel for "leave this axis alone". Only the axes the
        # flush decided had changed are present in `axes`.
        arguments = {"feature": feature, "x": -1000, "y": -1000,
                     "rotation": -1000, "weight": -1000, "size": -1000}
        arguments.update({k: v for k, v in axes.items() if v is not None})
        self._send("set_feature", arguments)

    def set_gaze(self, x: int, y: int) -> None:
        self._send("set_gaze", {"x": x, "y": y})

    def set_mouth(self, shape: str) -> None:
        self._send("set_mouth", {"mouth": shape})

    def set_speech(self, text: str) -> None:
        self._send("set_speech", {"text": text})

    def set_blink(self, enabled: bool) -> None:
        self._send("set_blink", {"enabled": enabled})

    def set_all_leds(self, r: int, g: int, b: int) -> None:
        self._send("set_all_leds", {"r": r, "g": g, "b": b})

    def move_head(self, yaw: float, pitch: float, speed_dps: int) -> None:
        self._send(
            "move_head",
            {"yaw": round(yaw), "pitch": round(pitch), "speed_dps": speed_dps},
        )


#: What the character stack calls, and what it loses without each. The gateway
#: proxies a HARDCODED table of tool names (`tool_map` in its stdio_server),
#: so a tool added to the device firmware is unreachable until the gateway
#: knows it too -- and 0.17.0, the latest release, does not know these three.
REQUIRED_TOOLS = {
    "move_head": "the head cannot move: no idle motion, no head-pet reaction",
    "set_avatar": "expressions cannot change",
    "set_blink": "blinking cannot be turned on",
}
OPTIONAL_TOOLS = {
    "listen": "no tap-to-talk: he cannot hear, so the brain never gets a turn",
    "get_head_angles": "he cannot sync to the head's real pose at boot, and "
                       "assumes it is at rest instead",
    "set_gaze": "no gaze drift (idle expression) and no breathing",
    "set_feature": "no mouth tilt, no eye size, no dance face animation",
    "set_speech": "no speech bubble, so timed speech and the sleepy 'Zzz…' are lost",
    "set_all_leds": "no status colour on the ring",
    "set_mouth": "no host-driven mouth shapes (firmware lip-sync is unaffected)",
}


async def check_tools(session) -> set[str]:
    """Compare what we need against what the gateway advertises.

    Done once at startup because the alternative is what actually happened: a
    missing tool answered with a successful-looking error, twice a second,
    forever, with the diagnosis buried in thousands of identical lines.
    """
    listed = {tool.name for tool in (await session.list_tools()).tools}

    missing_required = sorted(set(REQUIRED_TOOLS) - listed)
    for name in missing_required:
        logger.error("gateway does not provide %r -- %s", name, REQUIRED_TOOLS[name])

    missing_optional = sorted(set(OPTIONAL_TOOLS) - listed)
    for name in missing_optional:
        logger.warning("gateway does not provide %r -- %s", name, OPTIONAL_TOOLS[name])
    if missing_optional:
        logger.warning(
            "degraded: the gateway's tool table is hardcoded, so firmware tools "
            "it has not been taught are unreachable. Everything else still runs."
        )

    if missing_required:
        raise SystemExit(
            "cannot run: the gateway is missing "
            + ", ".join(missing_required)
            + ". Check that stackchan-gateway is up and the device is connected."
        )
    return listed


#: The voice runs in pa/.venv, this process runs on the gateway's interpreter,
#: and those are two different virtualenvs on purpose -- piper-tts is not in
#: the gateway's and must not be put there. So speaking is a subprocess.
PA_PYTHON = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
SPEECH_SCRIPT = Path(__file__).resolve().parent / "speech.py"

#: The voice he answers in.
DEFAULT_CHARACTER = "retro"


async def speak_line(
    text: str,
    character: str = DEFAULT_CHARACTER,
    settings: "voice_settings.VoiceSettings | None" = None,
) -> bool:
    """Say one line, by running the voice with its own interpreter.

    Deliberately the same command a person would type, rather than a private
    entry point: it is already tested, and it already refuses to print the
    token. Its stderr is surfaced on failure because "the voice model is not
    downloaded" is the likely first-run problem and its message names the
    exact command to fix it.
    """
    if not PA_PYTHON.exists():
        logger.error("no voice: %s does not exist -- run `make pa-install`", PA_PYTHON)
        return False

    # The overrides go on the command line rather than into the preset,
    # because the preset is a record of what was tuned by ear and its numbers
    # are pinned by tests. The CLI's own defaults are None so an explicit flag
    # always wins -- including a 0, which is how a preset's modulation is
    # silenced rather than merely reduced.
    overrides = settings.flags() if settings is not None else []

    process = await asyncio.create_subprocess_exec(
        str(PA_PYTHON), str(SPEECH_SCRIPT), "--character", character,
        *overrides, text,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(
            "voice failed (exit %s): %s",
            process.returncode,
            (stderr or b"").decode("utf-8", "replace").strip()[:400],
        )
        return False
    return True


async def listen_once(session, duration_ms: int) -> str | None:
    """Record and transcribe, via the gateway's `listen` tool.

    `motion="face-only"` is what makes the pause read as work: he keeps the
    thinking face and does not swing his head about while a person is talking.

    This is the fixed-window listener, and it is now the SECOND-best one: the
    wake word takes the device's own VAD, which ends the capture when the
    speaker stops rather than always waiting the window out (`hook.py`). It
    stays because it is the only listener we can *start* -- the gateway has no
    tool that makes the device begin listening, so a tap has nothing else to
    call. Checked against the gateway's tool table, not assumed.
    """
    try:
        result = await session.call_tool(
            "listen",
            {"duration_ms": duration_ms, "motion": "face-only", "language": "en"},
        )
    except Exception as exc:  # noqa: BLE001 - a failed listen is a quiet turn
        logger.warning("listen raised: %s", exc)
        return None

    reason = failure_reason(result)
    if reason:
        logger.warning("listen failed: %s", reason)
        return None

    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            return payload["text"]
    return None


async def read_head_pose(session) -> Pose | None:
    """Where the head actually is, or None if the device could not say.

    None is a documented outcome, not a defensive maybe: the firmware's own
    `get_head_angles` description says a persistent ReadPos failure returns
    `{"yaw": null, "pitch": null, "error": ...}`, and a single-call failure
    while the servo is mid-motion is a known transient. So this must not
    invent a pose -- `wake()` says so in the log and falls back to assuming
    rest, which is what the code did before any of this existed.
    """
    try:
        result = await session.call_tool("get_head_angles", {})
    except Exception as exc:  # noqa: BLE001 - a failed read is not fatal
        logger.warning("get_head_angles raised: %s", exc)
        return None

    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        yaw, pitch = payload.get("yaw"), payload.get("pitch")
        # `null` is the documented failure, and bool is an int in Python --
        # neither is an angle.
        if (
            isinstance(yaw, (int, float))
            and isinstance(pitch, (int, float))
            and not isinstance(yaw, bool)
            and not isinstance(pitch, bool)
        ):
            return Pose(float(yaw), float(pitch))
        if payload.get("error"):
            logger.warning("get_head_angles reported: %s", payload["error"])
    return None


def _log_turn_failure(task: "asyncio.Task") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("conversation turn failed: %r", exc)


#: Which brain to run. `api` is the Messages API with an API key; `cli` is a
#: headless `claude` on this machine, which needs no key because office-server
#: is already logged in for its own agents. `auto` -- the default -- prefers
#: the key when there is one, so upgrading this repo never silently changes
#: which model an existing deployment is paying for.
BRAIN_CHOICES = ("auto", "api", "cli")


def choose_brain(office_client):
    """Build the brain named by `CUBIE_BRAIN`, or None when there is none.

    Returning None rather than raising is deliberate and matches what this
    already did without a key: a Cubie who cannot think still breathes, blinks,
    looks around and reacts to being stroked, and losing all of that because a
    credential is missing would be a much worse failure than losing speech.

    An unrecognised value is a typo, and a typo that silently selected a
    fallback would be discovered as "why is it still billing the API". So it is
    named and rejected, and the run continues with no brain rather than the
    wrong one.
    """
    wanted = os.environ.get("CUBIE_BRAIN", "auto").strip().lower() or "auto"
    if wanted not in BRAIN_CHOICES:
        logger.error(
            "CUBIE_BRAIN=%r is not one of %s -- running with no brain rather "
            "than guessing which you meant",
            wanted, ", ".join(BRAIN_CHOICES),
        )
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if wanted == "auto":
        wanted = "api" if api_key else "cli"

    if wanted == "api":
        if not api_key:
            logger.warning("CUBIE_BRAIN=api but ANTHROPIC_API_KEY is not set")
            return None
        return brain_mod.Brain(
            api_key,
            office_client,
            model=os.environ.get("CUBIE_MODEL", brain_mod.DEFAULT_MODEL),
        )

    if not cli_brain_mod.available():
        logger.warning(
            "the CLI brain was asked for but `claude` is not on PATH. "
            "office-server has it at ~/.npm-global/bin -- check the unit's PATH."
        )
        return None
    # No CUBIE_MODEL default here on purpose: `Brain` has to name a model
    # because the API requires one, and the CLI does not -- letting it pick its
    # own default means one fewer place that pins a model by hand.
    return cli_brain_mod.CliBrain(
        office_client, model=os.environ.get("CUBIE_MODEL", "")
    )


def route_event(event: dict, character) -> None:
    """Send one gateway event to the character stack.

    Lifted out of `run_once`'s closure so it can be tested at all: the routing
    here was wrong for the whole life of the project and nothing could assert
    it, because it lived inside a function that needs an MCP session, a hook
    receiver and an office poll to construct.

    EVERY touch is affection. Touching him is not how you start a conversation
    -- the wake word is.

    A tap used to call `conversation.turn()` instead, and the cost was not the
    extra path but what it displaced: with a conversation configured a tap
    NEVER reached `on_touch`, so `HeadPetModifier` -- ported, tested, and the
    thing that makes him look pleased to be stroked -- had never once fired on
    the robot. Petting him opened a five-second microphone.

    `apply-m5-touch.sh` also makes a TAP the default outcome of any release
    that did not swipe, so a stroke confined to one pad arrives here as a tap.
    Under the old routing that was a recording; now it is a fuss, which is what
    the person doing it meant.
    """
    if event.get("event_type") != "touch":
        return
    subtype = event.get("subtype") or ""
    logger.info("touch event: %s", subtype)
    character.on_touch(subtype)


async def _read_office(client) -> "office_mod.OfficeState | None":
    """The office's state, or None -- the reason goes to the log, not the model.

    Unreachable, 401 and malformed all mean the same thing to an assistant on a
    desk: it does not know what the office is doing, and should say so rather
    than guess.
    """
    state, reason = await client.read()
    if state is None:
        logger.warning("office unreadable: %s", reason)
    return state


class VoiceOverrides:
    """The voice settings the office is currently asking for.

    A mutable holder rather than a value passed around, because two things need
    the same reading at different times: the poll writes it every fifteen
    seconds, and every utterance reads whatever is there when it speaks. Passing
    a value would freeze it at whichever moment the closure was built.

    `None` from the office means "said nothing about the voice", which is
    deliberately not the same as "use no overrides". An office too old to know
    about the panel would otherwise silently reset the voice on the first poll,
    and the symptom would be Cubie changing how he sounds for no visible reason.
    """

    def __init__(self) -> None:
        self.current = voice_settings.VoiceSettings()
        #: The last payload seen, so an unchanged reading is silent rather than
        #: re-logging its complaints on every poll.
        self._last_raw: object = None

    def take(self, raw) -> None:
        if raw is None:
            return
        settings, notes = voice_settings.coerce(raw)
        # Complaints only when the reading CHANGES. The poll runs every fifteen
        # seconds against an office whose stored settings rarely move, so
        # logging on every pass would put the same "clamped robot 9 -> 1.0" line
        # in the journal four times a minute for as long as a slider stayed
        # wrong -- flooding the one log that is the diagnostic for this feature.
        if settings == self.current and raw == self._last_raw:
            return
        self._last_raw = raw
        for note in notes:
            logger.warning("office voice settings: %s", note)
        if settings != self.current:
            logger.info("voice settings changed: %s", settings)
        self.current = settings


#: What a preview says. Fixed rather than free text: the panel is a settings
#: surface, not a way to put words in his mouth, and a line with a bit of
#: everything in it is what you want to hear repeatedly while tuning anyway.
PREVIEW_LINE = "Two approvals are waiting, and one agent is still working."

#: How long to wait for a preview before answering anyway. Piper loads a 60 MB
#: model per utterance, so this is generous on purpose.
PREVIEW_TIMEOUT_S = 30.0

#: How often to read the office for the ambient mood. The bridge polled at its
#: own interval; this is the same job, and the office caps its own cost rather
#: than relying on us to. Slow on purpose: an approval that shows up on his ring
#: fifteen seconds late is fine, and a robot hammering the office to look
#: attentive is not.
OFFICE_POLL_S = 15.0


async def preview_once(conversation, settings) -> tuple[bool, str, bool]:
    """Say the sample line, holding the same lock a conversation turn holds.

    Module level rather than a closure inside `run_once` so it can be tested:
    while it lived in the closure, reverting it to a read-only `busy` CHECK --
    the bug it was written to fix -- broke no test at all.

    TAKE the flag, do not merely read it. The first version checked `busy` and
    left it alone, so a tap could start a turn while the preview was
    mid-sentence: a refusal in one direction only, which is not a lock. Two
    producers streaming into the same capture endpoint is exactly the collision
    the check was added to prevent, and reading without taking also let two
    previews overlap each other.

    Returns (spoke, detail, was_busy). The third is what keeps "he is talking"
    (409) apart from "the voice broke" (500), which must not be conflated: one
    is a state and the other is him being mute.
    """
    if conversation is None or not conversation.claim("preview"):
        return False, "he is mid-conversation", True
    try:
        ok = await speak_line(PREVIEW_LINE, settings=settings)
    finally:
        conversation.release()
    return ok, "spoke" if ok else "the voice failed; see the log", False


async def poll_office_mood(
    client, character, voice: "VoiceOverrides | None" = None,
    interval_s: float = OFFICE_POLL_S,
) -> None:
    """Keep his resting face and ring -- and his voice -- following the office.

    One poll carrying two concerns, which is a conflation worth being explicit
    about: they are the same GET of the same payload at the same cadence, and
    splitting them would mean two requests fifteen seconds apart asking the
    office the same question.

    Failure is a reading, not an error: `_read_office` returning None becomes
    the `offline` mood -- amber and a thinking face -- because "I cannot see
    the office" is exactly the thing an ambient signal should show. Holding the
    last good mood instead would be the worst of the options, since a stale
    green ring is indistinguishable from a calm office.
    """
    while True:
        try:
            state = await _read_office(client)
            character.set_office_mood(state)
            if voice is not None:
                voice.take(state.voice if state is not None else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad poll must not end the loop
            logger.warning("office poll raised: %r", exc)
        await asyncio.sleep(interval_s)


async def tail_events(path: Path, on_event) -> None:
    """Follow the gateway's JSONL event log, from the end.

    From the end deliberately: replaying the log on start would act out every
    stroke of the head since the Pi last booted. A missing file is not an error
    -- it appears the first time the device reports anything.
    """
    while not path.exists():
        await asyncio.sleep(2.0)
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if not line:
                await asyncio.sleep(0.25)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A half-written line at the tail is normal, not data.
                continue
            on_event(event)


#: How long to keep trying to reach the gateway before giving up.
#:
#: The gateway restarts -- for its own upgrades, and every time this fleet's
#: tools are re-patched into it -- and it owns the device connection, so this
#: process cannot do anything while it is down. Exiting immediately would make
#: a routine `systemctl restart stackchan-gateway` look like a crash, and with
#: a start limit on the unit a slow restart could leave the character stack
#: dead until someone noticed.
#:
#: Bounded rather than infinite so a genuinely broken configuration still
#: surfaces as a failed unit rather than a process retrying forever.
CONNECT_TIMEOUT_S = 300.0
CONNECT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 15.0)


async def run_once(mcp_url: str, event_log: Path, idle_level: int) -> int:
    from mcp_compat import ClientSession, auth_headers, open_streams

    token = os.environ.get("STACKCHAN_TOKEN") or os.environ.get("BEARER_TOKEN")
    loop = asyncio.get_running_loop()

    async with open_streams(mcp_url, auth_headers(token)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = await check_tools(session)
            effector = McpEffector(session, loop, available)
            chan = Chan(effector)
            character = driver_mod.CharacterDriver(chan, idle_motion_level=idle_level)

            # Sync to where the head actually is, then settle to rest -- the
            # better half of M5's boot sequence. Before this, the first flush
            # commanded rest from an ASSUMED pose, so the three relative
            # modifiers (idle's small observation, speaking's baseline,
            # head-pet's restore point) all computed from the wrong place
            # until an absolute move happened to correct it.
            character.wake(0.0, await read_head_pose(session))

            # He starts standing by, which is what brings idle motion to life.
            character.set_status(driver_mod.STANDBY)
            logger.info("character stack running (idle level %d)", idle_level)

            # What the office is asking the voice to sound like. Built before
            # the conversation, because its `say` closes over this.
            voice = VoiceOverrides()

            # --- the brain, when there is one to be had --------------------
            office_client = office_mod.OfficeClient(
                base_url=os.environ.get("OFFICE_HUB", office_mod.DEFAULT_BASE_URL),
                token=os.environ.get("AGENTHUB_COMPANION_TOKEN", ""),
            )
            conversation = None
            thinker = choose_brain(office_client)
            if thinker is not None:
                conversation = conversation_mod.Conversation(
                    character=character,
                    brain=thinker,
                    listen=lambda ms: listen_once(session, ms),
                    say=lambda text: speak_line(text, settings=voice.current),
                    read_office=lambda: _read_office(office_client),
                    listen_ms=int(os.environ.get(
                        "CUBIE_LISTEN_MS", conversation_mod.DEFAULT_LISTEN_MS)),
                )
                logger.info(
                    "brain ready (%s, model %s)",
                    type(thinker).__name__,
                    thinker.model or "the CLI's default",
                )
            else:
                # Not fatal: everything else -- idle motion, breathing, the
                # face, head-pet -- works without it, and saying so once beats
                # a tap that silently does nothing.
                logger.warning(
                    "no brain: neither ANTHROPIC_API_KEY nor the claude CLI is "
                    "available, so tap-to-talk is off. Everything else still runs."
                )

            # --- the wake word's audio, when we can transcribe it ---------
            #
            # The gateway POSTs a device-driven capture (wake word, button or
            # LCD touch) to STACKCHAN_AUDIO_HOOK_URL. With nothing serving that
            # URL it drops every frame -- so before this the wake word could
            # wake him and nothing could come of it.
            #
            # The token is the gateway's own fallback chain, mirrored rather
            # than re-decided: STACKCHAN_AUDIO_HOOK_TOKEN when set, otherwise
            # the STACKCHAN_TOKEN both ends already share. The hook therefore
            # needs no new secret.
            receiver = None
            engine = None
            decode = None
            hook_token = ""
            if conversation is None:
                logger.warning(
                    "the wake word can wake him but not be answered: the brain "
                    "is off, so device captures are not served."
                )
            else:
                hook_token = os.environ.get("STACKCHAN_AUDIO_HOOK_TOKEN") or token or ""
                try:
                    engine = transcribe_mod.load_engine()
                    decode = transcribe_mod.load_decoder()
                except transcribe_mod.TranscriberUnavailable as exc:
                    # Named at startup rather than on the first wake word, for
                    # the same reason check_tools runs before the first tick: a
                    # preflight that reports at use time reports into a log
                    # nobody is reading yet.
                    logger.error("wake-word audio cannot be transcribed: %s", exc)
                    engine = decode = None

            async def capture_turn(capture: hook_mod.Capture) -> None:
                """Transcribe a delivered capture, then take a normal turn."""
                try:
                    heard = await transcribe_mod.transcribe_capture(
                        capture.body, engine=engine, decode=decode
                    )
                except ogg_opus.OggError as exc:
                    # A mangled body is not a quiet turn: transcribing it would
                    # invent words nobody said, and the brain would act on them.
                    logger.warning("capture refused (session=%s): %s",
                                   capture.session_id or "(none)", exc)
                    return
                except transcribe_mod.TranscriberUnavailable as exc:
                    logger.error("capture could not be transcribed: %s", exc)
                    return
                await conversation.turn_on_transcript(heard, time.monotonic() - start)

            def on_preview(settings) -> tuple[bool, str]:
                """Say a sample line. Called on the RECEIVER's thread.

                The busy check happens inside the coroutine, on the loop
                thread, rather than out here: reading `conversation.busy` from
                this thread and then speaking would leave a window where a turn
                starts in between, and two producers streaming into the same
                capture endpoint is the collision this refusal exists to avoid.
                """
                try:
                    # Inside the try: a loop that is shutting down makes THIS
                    # raise, not `result()`, and an uncaught exception on the
                    # server thread answers the office with a closed connection
                    # rather than a reason.
                    future = asyncio.run_coroutine_threadsafe(
                        preview_once(conversation, settings), loop)
                    return future.result(timeout=PREVIEW_TIMEOUT_S)
                except TimeoutError:
                    # The utterance may still be in flight; we just stop
                    # waiting. Saying "timed out" is honest about what we know,
                    # and it is not a "busy" answer -- the lock is still held by
                    # the preview itself and will be released when it finishes.
                    return False, f"no answer within {PREVIEW_TIMEOUT_S:.0f}s", False
                except Exception as exc:  # noqa: BLE001 - a preview is not worth a crash
                    logger.warning("preview raised: %r", exc)
                    return False, "the preview failed; see the log", False

            def on_capture(capture: hook_mod.Capture) -> None:
                """Called from the receiver's thread; must not block.

                `call_soon_threadsafe` is the whole bridge: the HTTP response
                has already been written by this point, so the turn runs on the
                event loop with the socket closed, which is what keeps the
                gateway's 10 second POST timeout irrelevant to how long a turn
                takes.
                """
                def spawn() -> None:
                    task = asyncio.create_task(capture_turn(capture))
                    task.add_done_callback(_log_turn_failure)

                loop.call_soon_threadsafe(spawn)

            def on_event(event: dict) -> None:
                route_event(event, character)

            # Defined before the event handler can fire, since it closes over it.
            start = time.monotonic()

            if engine is not None and decode is not None:
                try:
                    receiver = hook_mod.HookReceiver(
                        hook_token,
                        on_capture,
                        on_preview=on_preview if conversation is not None else None,
                        host=os.environ.get("CUBIE_HOOK_HOST", hook_mod.DEFAULT_HOST),
                        port=int(os.environ.get("CUBIE_HOOK_PORT", hook_mod.DEFAULT_PORT)),
                    )
                    receiver.start()
                except (OSError, ValueError) as exc:
                    # A bound port or a missing token costs the wake word and
                    # nothing else, so it is reported rather than fatal --
                    # tap-to-talk, the face and the head all still work.
                    logger.error("audio hook is not listening: %s", exc)
                    receiver = None

            drain = asyncio.create_task(effector.drain())
            tail = asyncio.create_task(tail_events(event_log, on_event))
            # The office on his resting face. Only when there is an office
            # client to ask -- without a key there is no conversation either,
            # and a mood poll on its own would be a robot reacting to news it
            # cannot discuss.
            mood_poll = (
                asyncio.create_task(poll_office_mood(office_client, character, voice))
                if conversation is not None
                else None
            )
            try:
                while True:
                    character.update(time.monotonic() - start)
                    await asyncio.sleep(TICK_S)
            finally:
                if receiver is not None:
                    receiver.stop()
                for task in (drain, tail, mood_poll):
                    if task is None:
                        continue
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
    return 0


async def run(mcp_url: str, event_log: Path, idle_level: int) -> int:
    """Keep the stack up across a gateway restart.

    Only CONNECTION failures are retried. A missing required tool raises
    SystemExit out of `check_tools`, and that is a configuration error which
    retrying cannot fix -- so it propagates rather than spinning.
    """
    started = time.monotonic()
    attempt = 0
    while True:
        try:
            return await run_once(mcp_url, event_log, idle_level)
        except SystemExit:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - anything unreachable is a retry
            elapsed = time.monotonic() - started
            if elapsed > CONNECT_TIMEOUT_S:
                logger.error(
                    "could not reach the gateway at %s for %.0fs, giving up: %s",
                    mcp_url, elapsed, exc,
                )
                return 1
            delay = CONNECT_BACKOFF_S[min(attempt, len(CONNECT_BACKOFF_S) - 1)]
            attempt += 1
            logger.warning(
                "gateway unreachable (%s); retrying in %.0fs", exc, delay
            )
            await asyncio.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Cubie's character stack against the gateway."
    )
    parser.add_argument("--mcp-url", default=os.environ.get("CUBIE_GATEWAY_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument(
        "--event-log",
        type=Path,
        # The gateway's own variable name, so one setting moves both ends.
        default=Path(os.environ.get("STACKCHAN_EVENTS_PATH", DEFAULT_EVENT_LOG)).expanduser(),
    )
    parser.add_argument(
        "--idle-level",
        type=int,
        default=int(os.environ.get("CUBIE_IDLE_LEVEL", "2")),
        choices=(0, 1, 2, 3),
        help="M5's levels: 0 no head movement, 1 every 8-12s, 2 every 4-8s "
             "(default), 3 every 2-4s. The face drifts at every level.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run(args.mcp_url, args.event_log, args.idle_level))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
