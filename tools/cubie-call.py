#!/usr/bin/env python3
"""Call one tool on the stackchan-mcp gateway, from the command line.

Why this exists: the gateway speaks streamable-HTTP MCP, which requires an
`initialize` handshake and carries a session id on every subsequent request.
`curl` cannot do that in one shot -- a bare `tools/call` comes back

    {"code":-32600,"message":"Bad Request: Missing session ID"}

which reads like an auth or a routing problem and is neither. Composing the
handshake by hand in curl is possible and not worth doing twice, so this is the
handshake, once, in the repo.

It is deliberately NOT part of the bridge: the bridge is a long-lived client
with its own reconnect behaviour, and conflating "poke the robot from a
terminal" with "mirror the office continuously" would give both jobs to one
piece of code. This is the poking tool.

Run it with the GATEWAY'S python, not the system one -- the `mcp` package lives
in the gateway's virtualenv:

    export STACKCHAN_TOKEN=$(sudo sed -nE 's/^STACKCHAN_TOKEN=//p' \
        /etc/stackchan-gateway.env | tr -d '\042\047')
    ~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py get_status
    ~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py \
        set_avatar '{"face":"embarrassed"}'

The token is read from the environment and never printed, including on error:
this repo's rule is that no command it ships may echo a secret.
"""

import asyncio
import json
import os
import sys

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    sys.exit(
        "ERROR: the `mcp` package is not importable.\n"
        "Run this with the gateway's interpreter, e.g.\n"
        "  ~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py get_status"
    )

# Loopback by default, matching the gateway's MCP_HTTP_HOST. The control
# surface is not exposed to the LAN on purpose -- only the device-facing ports
# are -- so this tool runs on the gateway's own host.
DEFAULT_URL = "http://127.0.0.1:8767/mcp"


async def call(name: str, arguments: dict) -> int:
    url = os.environ.get("CUBIE_GATEWAY_MCP_URL", DEFAULT_URL)
    token = os.environ.get("STACKCHAN_TOKEN", "")
    # An empty token is not an error: the gateway disables auth entirely when
    # neither STACKCHAN_TOKEN nor BEARER_TOKEN is set, so sending an empty
    # `Bearer ` header would fail against a server that would have accepted no
    # header at all.
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            for block in result.content:
                print(getattr(block, "text", block))
            # isError is the tool reporting failure, distinct from a transport
            # error raising. Both should make the shell unhappy.
            return 1 if getattr(result, "isError", False) else 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(
            "usage: cubie-call.py TOOL ['{\"json\":\"arguments\"}']\n"
            "\n"
            "  cubie-call.py get_status\n"
            "  cubie-call.py set_avatar '{\"face\":\"embarrassed\"}'\n"
            "  cubie-call.py move_head '{\"yaw\":0,\"pitch\":45,\"speed\":40}'\n"
            "\n"
            "Reads STACKCHAN_TOKEN and CUBIE_GATEWAY_MCP_URL from the environment.",
            file=sys.stderr,
        )
        return 2

    name = sys.argv[1]
    if len(sys.argv) > 2:
        try:
            arguments = json.loads(sys.argv[2])
        except json.JSONDecodeError as exc:
            print(f"ERROR: arguments are not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(arguments, dict):
            print("ERROR: arguments must be a JSON object", file=sys.stderr)
            return 2
    else:
        arguments = {}

    try:
        return asyncio.run(call(name, arguments))
    except Exception as exc:
        # Deliberately not re-raising: a traceback here would be the SDK's
        # internals, and the useful part is the message. The token cannot
        # appear in it -- it is only ever a header value.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
