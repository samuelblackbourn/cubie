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

    export STACKCHAN_TOKEN=$(sudo sed -nE 's/^STACKCHAN_TOKEN=//p' \\
        /etc/stackchan-gateway.env | tr -d '\\042\\047')
    ~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py get_status
    ~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py \\
        set_avatar '{"face":"embarrassed"}'

The token is read from the environment and never printed, including on error:
this repo's rule is that no command it ships may echo a secret.

The SDK's two incompatible generations are handled in `mcp_compat`, not here.

Verified against mcp 1.12.4, 1.30.0 and 2.2.0 on a local streamable-HTTP server
with bearer auth: a good call exits 0, a failing tool exits 1, a rejected token
exits 1, malformed arguments exit 2.
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

try:
    # Sibling module: sys.path[0] is this script's directory when it is run as
    # a path, which is how it is documented and used.
    from mcp_compat import ClientSession, auth_headers, open_streams, tool_failed
except ImportError as exc:
    sys.exit(
        f"ERROR: {exc}\n"
        "Run this with the gateway's interpreter, e.g.\n"
        "  ~/stackchan-gateway/bin/python ~/cubie/tools/cubie-call.py get_status"
    )

# Loopback by default, matching the gateway's MCP_HTTP_HOST. The control
# surface is not exposed to the LAN on purpose -- only the device-facing ports
# are -- so this tool runs on the gateway's own host.
DEFAULT_URL = "http://127.0.0.1:8767/mcp"


def describe(exc: BaseException) -> str:
    """Flatten an exception into something a person can act on.

    anyio wraps transport failures in an ExceptionGroup, so an HTTP 401 arrives
    as "ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)" --
    a message that says nothing about the token being wrong and sends the
    reader looking in the wrong place. This unwraps groups and `__cause__`
    chains so the actual status code survives to the terminal.
    """
    seen: list[str] = []

    def walk(e: BaseException) -> None:
        children = getattr(e, "exceptions", None)
        if children:
            for child in children:
                walk(child)
            return
        text = f"{type(e).__name__}: {e}".strip()
        if text not in seen:
            seen.append(text)
        cause = e.__cause__ or e.__context__
        if cause is not None and cause is not e:
            walk(cause)

    walk(exc)
    return " <- ".join(seen) if seen else f"{type(exc).__name__}: {exc}"


def endpoint() -> tuple[str, dict]:
    """The URL and auth headers, from the environment."""
    url = os.environ.get("CUBIE_GATEWAY_MCP_URL", DEFAULT_URL)
    token = os.environ.get("STACKCHAN_TOKEN", "")
    return url, auth_headers(token)


def probe_status(url: str, headers: dict) -> int | None:
    """Ask the endpoint directly what it makes of our credentials.

    The SDK does not surface the HTTP status. A rejected token arrives as
    `MCPError(-32603, "Server returned an error response")` -- true, and
    useless: the 401 appears nowhere in the exception or its causes. One direct
    request, taken only after something has already failed, recovers the number
    that says what to fix.
    """
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        # The probe is a diagnostic. If it cannot run, the original error still
        # gets reported -- it must never replace it with its own failure.
        return None


async def call(name: str, arguments: dict) -> int:
    url, headers = endpoint()

    async with open_streams(url, headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            for block in getattr(result, "content", None) or []:
                print(getattr(block, "text", block))
            return 1 if tool_failed(result) else 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(
            "usage: cubie-call.py TOOL ['{\"json\": \"arguments\"}']\n"
            "\n"
            "  cubie-call.py get_status\n"
            "  cubie-call.py set_avatar '{\"face\": \"embarrassed\"}'\n"
            "  cubie-call.py move_head '{\"yaw\": 0, \"pitch\": 45, \"speed\": 40}'\n"
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
        print(f"ERROR: {describe(exc)}", file=sys.stderr)
        url, headers = endpoint()
        status = probe_status(url, headers)
        # 400 is the endpoint's normal answer to a session-less POST -- it is
        # what "Missing session ID" looks like -- so reporting it here would
        # add a number that means nothing about the failure at hand. Only the
        # statuses that actually diagnose something are worth printing.
        if status is not None and (status in (401, 403, 404) or status >= 500):
            print(f"       {url} answered HTTP {status} to a direct request", file=sys.stderr)
        if status in (401, 403):
            print(
                "HINT: the bearer token was rejected. The gateway reads\n"
                "      STACKCHAN_TOKEN (or BEARER_TOKEN) from its EnvironmentFile:\n"
                "        systemctl cat stackchan-gateway.service | grep EnvironmentFile\n"
                "      Load it without printing it:\n"
                "        export STACKCHAN_TOKEN=$(sudo sed -nE "
                "'s/^STACKCHAN_TOKEN=//p' /etc/stackchan-gateway.env | tr -d '\\042\\047')",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
