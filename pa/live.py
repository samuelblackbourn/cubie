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
import conversation as conversation_mod  # noqa: E402
import driver as driver_mod  # noqa: E402
import office as office_mod  # noqa: E402
from chan import Chan  # noqa: E402
from tracking import Pose  # noqa: E402

logger = logging.getLogger("cubie.live")

#: The gateway's loopback MCP surface. Same default as the bridge's.
DEFAULT_MCP_URL = "http://127.0.0.1:8767/mcp"

#: Where the gateway appends physical events: `event_log.py`'s
#: `~/.claude/stackchan-events.jsonl`, resolved against the gateway's HOME,
#: which the systemd drop-in sets to its StateDirectory.
#:
#: **This log is OFF by default.** `notify_config.py` defaults `jsonl.enabled`
#: to false, so touch events reach nothing until a `notify.yml` turns it on --
#: see `deploy/stackchan-notify.yml` and the README. Head-pet is wired and
#: dormant until then, which is a config step and not a code one.
DEFAULT_EVENT_LOG = Path("/var/lib/stackchan-gateway/.claude/stackchan-events.jsonl")

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


async def speak_line(text: str, character: str = DEFAULT_CHARACTER) -> bool:
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

    process = await asyncio.create_subprocess_exec(
        str(PA_PYTHON), str(SPEECH_SCRIPT), "--character", character, text,
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

    The device's own VAD would be better -- it stops when you stop speaking
    rather than always waiting the full window -- but that path delivers the
    audio to STACKCHAN_AUDIO_HOOK_URL, a webhook this does not serve yet.
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

            # --- the brain, when there is a key for it ---------------------
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            conversation = None
            if api_key:
                office_client = office_mod.OfficeClient(
                    base_url=os.environ.get("OFFICE_HUB", office_mod.DEFAULT_BASE_URL),
                    token=os.environ.get("AGENTHUB_COMPANION_TOKEN", ""),
                )
                thinker = brain_mod.Brain(
                    api_key,
                    office_client,
                    model=os.environ.get("CUBIE_MODEL", brain_mod.DEFAULT_MODEL),
                )
                conversation = conversation_mod.Conversation(
                    character=character,
                    brain=thinker,
                    listen=lambda ms: listen_once(session, ms),
                    say=speak_line,
                    read_office=lambda: _read_office(office_client),
                    listen_ms=int(os.environ.get(
                        "CUBIE_LISTEN_MS", conversation_mod.DEFAULT_LISTEN_MS)),
                )
                logger.info("brain ready (model %s)", thinker.model)
            else:
                # Not fatal: everything else -- idle motion, breathing, the
                # face, head-pet -- works without it, and saying so once beats
                # a tap that silently does nothing.
                logger.warning(
                    "ANTHROPIC_API_KEY is not set, so tap-to-talk is off. "
                    "Everything else still runs."
                )

            def on_event(event: dict) -> None:
                if event.get("event_type") != "touch":
                    return
                subtype = event.get("subtype") or ""
                logger.info("touch event: %s", subtype)
                # A TAP means "listen to me"; a STROKE is affection and belongs
                # to the head-pet reaction. Routing both to both would start a
                # conversation every time he was petted.
                if subtype == "tap" and conversation is not None:
                    task = asyncio.create_task(
                        conversation.turn(time.monotonic() - start)
                    )
                    # Detached tasks swallow their exceptions until interpreter
                    # exit, and a turn that dies silently looks exactly like a
                    # tap that did nothing.
                    task.add_done_callback(_log_turn_failure)
                else:
                    character.on_touch(subtype)

            # Defined before the event handler can fire, since it closes over it.
            start = time.monotonic()
            drain = asyncio.create_task(effector.drain())
            tail = asyncio.create_task(tail_events(event_log, on_event))
            try:
                while True:
                    character.update(time.monotonic() - start)
                    await asyncio.sleep(TICK_S)
            finally:
                for task in (drain, tail):
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
