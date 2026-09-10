"""Cubie's four tools, served over MCP stdio to a headless `claude`.

`CliBrain` runs the model as a subprocess, so the model cannot reach the
character stack's Python directly the way `Brain` can. This is the bridge: a
stdio MCP server that `claude` spawns via `--mcp-config`, exposing exactly
`approve_request`, `deny_request`, `set_presence` and `set_face` and nothing
else.

--- Why the protocol is written out rather than imported ---

`mcp` is importable here -- the character stack runs on the gateway's
interpreter precisely because that is where it lives. It is still not used, for
two reasons that both come from this being a per-turn subprocess.

The first is latency. This process is spawned fresh for every single thing
anyone says to him, and every import it does happens while a person stands
there waiting. The handshake we actually need is four methods of newline-
delimited JSON-RPC; paying an SDK's import cost per turn to get them is a poor
trade.

The second is that a hand-written loop is testable from anywhere. The gateway's
interpreter is one machine's deployment state; a module with no imports beyond
the standard library and this repo can be driven by a test that pipes bytes at
it, which is what `tests/test_office_mcp.py` does.

The cost is that protocol drift becomes our problem. That is bounded by only
implementing the handshake, `tools/list` and `tools/call`, and by echoing the
client's own `protocolVersion` back rather than asserting one of our own.

--- What it deliberately cannot do ---

It holds the companion token and nothing else. It cannot move the head, blink,
speak, or reach the gateway -- there is no `chan` here and no `STACKCHAN_TOKEN`
-- so the worst a confused model can do through this surface is approve or deny
something, or ask for a face.

`set_face` is the odd one out: it has no office call to make, and the process
that could apply it is the parent. So it is validated here and recorded, and
`CliBrain` applies it when the turn ends. That is the same split
`brain_tools.run_tool` already makes for `Brain`.

--- The turn log ---

Every call is appended as one JSON object to the file named by
`CUBIE_TURN_LOG`. That file is how the actions get back to the parent: the
model's tool calls are invisible in `--output-format json`, which reports only
the final text. Without it a turn's approvals would happen and be unlogged,
which is precisely the kind of silent action this stack refuses everywhere
else.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from typing import Any

import brain_tools
import office as office_mod

#: JSON-RPC error codes we actually emit. -32601 and -32700 are the standard
#: ones; a tool that fails is NOT an error at this level -- it is a successful
#: result whose content says what went wrong, because that is what the model
#: needs to read and act on.
METHOD_NOT_FOUND = -32601
PARSE_ERROR = -32700

#: The newest protocol version we know how to speak. Only used when the client
#: does not name one, which no real client does.
FALLBACK_PROTOCOL_VERSION = "2025-06-18"

SERVER_NAME = "cubie-office"


def _tool_list() -> list[dict[str, Any]]:
    """`brain_tools.TOOLS` in MCP's shape.

    The only difference from the Messages API's is the key name --
    `input_schema` there, `inputSchema` here. Translated rather than duplicated,
    so adding `angry` to `FACES` still reaches both brains from one edit.
    """
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
        }
        for tool in brain_tools.TOOLS
    ]


class _TurnLog:
    """Append-only record of what the model did this turn.

    Opened lazily and closed after every write: the parent may read the file at
    any moment after the subprocess exits, and a buffered handle that never
    flushed would lose the last action -- which would be an approval that
    happened and was never reported.
    """

    def __init__(self, path: str | None) -> None:
        self._path = path

    def record(self, record: brain_tools.ActionRecord) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record)) + "\n")
        except OSError as exc:
            # Never fail the turn over the log. The action already happened;
            # losing the record is bad, losing the answer is worse.
            print(f"cubie-office: could not write the turn log: {exc}", file=sys.stderr)


async def handle(message: dict, office: Any, log: _TurnLog) -> dict | None:
    """One JSON-RPC message in, at most one out.

    Returns None for notifications, which by the spec get no reply at all --
    answering one is a protocol error that some clients treat as fatal.
    """
    method = message.get("method")
    ident = message.get("id")

    if ident is None:
        # A notification: `notifications/initialized` and friends. Nothing to
        # say, and saying something would be wrong.
        return None

    if method == "initialize":
        params = message.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": ident,
            "result": {
                "protocolVersion": params.get("protocolVersion")
                or FALLBACK_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": ident, "result": {"tools": _tool_list()}}

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        record, text = await brain_tools.run_tool(name, arguments, office)
        log.record(record)
        # `isError` is deliberately not set even when the tool refused. The
        # gateway's own unknown-tool bug is the cautionary tale in the other
        # direction -- but here the refusal text IS the answer the model needs
        # ("that one was already handled; do not retry"), and flagging it as a
        # protocol error invites a client to hide it.
        return {
            "jsonrpc": "2.0",
            "id": ident,
            "result": {"content": [{"type": "text", "text": text}]},
        }

    return {
        "jsonrpc": "2.0",
        "id": ident,
        "error": {"code": METHOD_NOT_FOUND, "message": f"no method {method!r}"},
    }


async def serve(reader: Any, writer: Any, office: Any, log: _TurnLog) -> None:
    """Read newline-delimited JSON-RPC until the client hangs up."""
    while True:
        line = await reader.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            writer.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": PARSE_ERROR, "message": "not JSON"},
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            continue
        reply = await handle(message, office, log)
        if reply is not None:
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()


async def _main() -> int:
    office = office_mod.OfficeClient(
        base_url=os.environ.get("OFFICE_HUB", office_mod.DEFAULT_BASE_URL),
        token=os.environ.get("AGENTHUB_COMPANION_TOKEN", ""),
    )
    log = _TurnLog(os.environ.get("CUBIE_TURN_LOG"))

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)

    await serve(reader, writer, office, log)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
