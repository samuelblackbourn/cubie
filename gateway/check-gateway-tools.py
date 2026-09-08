#!/usr/bin/env python3
"""Assert the gateway can route every tool the character stack calls.

This is the check that would have saved a debugging round on hardware. The
gateway proxies a HARDCODED table of tool names, so a tool added to the device
firmware is unreachable until the gateway knows it too -- and it answers an
unknown name with an ordinary, successful-looking result whose text happens to
be `{"error": "Unknown tool: ..."}`. Nothing anywhere raises.

So the invariant is worth asserting directly:

    every tool pa/live.py can send
      is in the gateway's tool_map      (so tools/call routes it)
      AND in its Tool(...) list         (so tools/list advertises it, which is
                                         what the SDK and live.py's own
                                         startup check read)

Read by AST rather than by importing, because importing the gateway needs its
own virtualenv and pulls in `mcp`; this has to be runnable from anywhere,
including a sandbox with neither.

    python3 gateway/check-gateway-tools.py                     # find the venv
    python3 gateway/check-gateway-tools.py /path/to/stdio_server.py

Exit codes, deliberately three-way like the contract guard's:
    0  every tool routes and is listed
    1  something is missing -- the message names it
    2  could not look (no gateway found, or it would not parse)

The third is distinct on purpose. Treating "I could not check" as "no problem"
is what makes a guard worthless.
"""

from __future__ import annotations

import ast
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PA = os.path.join(os.path.dirname(HERE), "pa")


def find_server(argv: list[str]) -> str | None:
    if len(argv) > 1:
        return argv[1] if os.path.isfile(argv[1]) else None
    for root in (
        os.environ.get("STACKCHAN_VENV", ""),
        os.path.expanduser("~/stackchan-gateway"),
    ):
        if not root:
            continue
        hits = glob.glob(
            os.path.join(root, "lib", "python3.*", "site-packages",
                         "stackchan_mcp", "stdio_server.py")
        )
        if hits:
            return hits[0]
    return None


def read_gateway(path: str) -> tuple[dict[str, str], set[str]]:
    """The tool_map and the advertised Tool names, by AST."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    routed: dict[str, str] = {}
    listed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and isinstance(value, ast.Tuple)
                    and len(value.elts) == 2
                    and isinstance(value.elts[0], ast.Constant)
                    and isinstance(value.elts[0].value, str)
                    and value.elts[0].value.startswith("self.")
                ):
                    routed[key.value] = value.elts[0].value
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Tool":
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    listed.add(keyword.value.value)
    return routed, listed


def needed_tools() -> tuple[set[str], set[str]]:
    """What live.py declares it needs, without importing it.

    By AST for the same reason as above -- and because importing live.py drags
    in the rest of the package for two dict literals.
    """
    tree = ast.parse(open(os.path.join(PA, "live.py"), encoding="utf-8").read())
    tables: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                name = getattr(target, "id", "")
                if name in ("REQUIRED_TOOLS", "OPTIONAL_TOOLS"):
                    tables[name] = {
                        k.value
                        for k in node.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
    return tables.get("REQUIRED_TOOLS", set()), tables.get("OPTIONAL_TOOLS", set())


def main(argv: list[str]) -> int:
    server = find_server(argv)
    if server is None:
        print("check-gateway-tools: could not find the gateway's stdio_server.py.")
        print("  Pass its path, or set STACKCHAN_VENV to the venv root.")
        print("  Exit 2 means COULD NOT CHECK, not 'nothing missing'.")
        return 2

    try:
        routed, listed = read_gateway(server)
        required, optional = needed_tools()
    except (OSError, SyntaxError) as exc:
        print(f"check-gateway-tools: could not parse: {exc}")
        return 2

    if not routed or not listed:
        print(f"check-gateway-tools: found no tool table in {server}")
        print("  The gateway's shape has changed. Exit 2: could not check.")
        return 2

    print(f"check-gateway-tools: {server}")
    print(f"  gateway routes {len(routed)} tools and advertises {len(listed)}")

    problems = []
    for name in sorted(required | optional):
        kind = "required" if name in required else "optional"
        if name not in routed:
            problems.append(f"{name} ({kind}) is NOT in tool_map -- calls return "
                            f'{{"error": "Unknown tool: {name}"}}')
        elif name not in listed:
            problems.append(f"{name} ({kind}) routes but is NOT advertised -- "
                            "live.py's startup check reads the list, not the map")

    if problems:
        print(f"  MISSING ({len(problems)}):")
        for problem in problems:
            print(f"    - {problem}")
        print()
        print("  Run: bash gateway/apply-gateway-tools.sh")
        print("  then: sudo systemctl restart stackchan-gateway")
        return 1

    print(f"  OK -- all {len(required | optional)} tools route and are advertised")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
