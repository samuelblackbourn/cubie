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

import driver as driver_mod  # noqa: E402
from chan import Chan  # noqa: E402

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


async def run(mcp_url: str, event_log: Path, idle_level: int) -> int:
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

            # He starts standing by, which is what brings idle motion to life.
            character.set_status(driver_mod.STANDBY)
            logger.info("character stack running (idle level %d)", idle_level)

            def on_event(event: dict) -> None:
                if event.get("event_type") == "touch":
                    subtype = event.get("subtype") or ""
                    logger.info("touch event: %s", subtype)
                    character.on_touch(subtype)

            drain = asyncio.create_task(effector.drain())
            tail = asyncio.create_task(tail_events(event_log, on_event))
            try:
                start = time.monotonic()
                while True:
                    character.update(time.monotonic() - start)
                    await asyncio.sleep(TICK_S)
            finally:
                for task in (drain, tail):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
    return 0


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
