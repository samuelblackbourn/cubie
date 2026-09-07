"""Diagnose blink and expressions on the live avatar.

Prints the real input schemas first -- set_blink and set_mouth argument names
were guessed earlier and may have been wrong -- then enables blink and walks
the six expressions slowly enough to watch each one.
"""
import asyncio, json, os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

FACES = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]

async def main():
    async with streamablehttp_client(
            "http://127.0.0.1:8767/mcp",
            headers={"Authorization": "Bearer " + os.environ["T"]}) as (r, w, _):
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
