"""Diagnose blink and expressions on the live avatar.

Prints the real input schemas first -- set_blink and set_mouth argument names
were guessed earlier and may have been wrong -- then enables blink and walks
the six expressions slowly enough to watch each one.

Run it with the GATEWAY'S python, which is where the `mcp` package lives:

    export STACKCHAN_TOKEN=$(sudo sed -nE 's/^STACKCHAN_TOKEN=//p' \
        /etc/stackchan-gateway.env | tr -d '\042\047')
    ~/stackchan-gateway/bin/python ~/cubie/firmware/face-check.py
"""
import asyncio
import json
import os
import pathlib
import sys

# The SDK-compatibility shim lives in tools/. Importing it by path rather than
# copying it here is deliberate: the two spellings of CallToolResult.isError
# are a trap worth having exactly one answer to. See tools/mcp_compat.py.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
from mcp_compat import ClientSession, auth_headers, open_streams  # noqa: E402

FACES = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]

# Loopback: the gateway's MCP control surface is not exposed to the LAN, so
# this runs on the gateway's own host.
URL = os.environ.get("CUBIE_GATEWAY_MCP_URL", "http://127.0.0.1:8767/mcp")


async def main():
    # Was os.environ["T"], which is not a name anything else in this repo uses
    # and would KeyError with no explanation. STACKCHAN_TOKEN is what the
    # gateway's EnvironmentFile calls it and what every other script here reads.
    token = os.environ.get("STACKCHAN_TOKEN", "")

    async with open_streams(URL, auth_headers(token)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}

            for n in ("set_blink", "set_mouth", "set_mouth_sequence"):
                if n in tools:
                    print(f"{n}: {json.dumps(tools[n].inputSchema)}\n")

            async def call(n, **a):
                try:
                    out = await s.call_tool(n, a)
                    print(f"  OK  {n}{a} -> {str(out.content)[:90]}")
                except Exception as e:
                    print(f"  ERR {n}{a} -> {type(e).__name__}: {str(e)[:140]}")

            # Enable blink. Argument name unknown -- try the plausible ones and
            # let the errors name the right one.
            print("-- enabling blink")
            for kwargs in ({"enabled": True}, {"enable": True}, {"on": True}, {"blink": True}):
                await call("set_blink", **kwargs)

            print("\n-- watch the face for 20s: is it blinking on its own?")
            await asyncio.sleep(20)

            print("\n-- walking the six expressions, 4s each")
            for f in FACES:
                print(f"  >>> {f}")
                await call("set_avatar", face=f)
                await asyncio.sleep(4)
            await call("set_avatar", face="idle")


asyncio.run(main())
