#!/usr/bin/env bash
# Teach the gateway the three display tools our firmware added.
#
# --- Why this script has to exist ---
#
# The gateway proxies a HARDCODED table of tool names (`tool_map` in
# stdio_server.py) onto device MCP methods. A tool added to the DEVICE firmware
# is therefore unreachable until the gateway knows it too -- it answers an
# unknown name with `{"error": "Unknown tool: set_gaze"}`, and does not forward
# it. There is no passthrough, and 0.17.0 is the latest release on PyPI, so
# there is no upstream fix to wait for either.
#
# Two places need the name, not one:
#
#   1. `tool_map`, so `tools/call` routes it to the device method.
#   2. the `Tool(...)` list behind `tools/list`, so clients can see it. The MCP
#      SDK warns on calling a tool the server did not list, and pa/live.py's
#      startup check refuses to run without the ones it needs -- both read this
#      list, not the map.
#
# --- Why patching rather than forking ---
#
# The README's position is that the gateway is a pinned PyPI release we run
# unmodified, and that is worth keeping: it means `pip install` reproduces the
# thing that works. A fork would replace that with a branch to maintain.
#
# So this is the same bargain the firmware already makes: the installed bytes
# stay a known release, and every local change is a small, anchored, idempotent
# script in this repo. Re-running it is a series of "already patched" lines. A
# `pip install --force-reinstall` reverts everything and re-running this puts it
# back, which is a property a fork does not have.
#
# --- Run it against the gateway's OWN site-packages ---
#
#   bash gateway/apply-gateway-tools.sh
#   sudo systemctl restart stackchan-gateway
#
# It defaults to ~/stackchan-gateway; pass a different venv root as $1. It
# aborts rather than guess if the anchors are missing, which is what a version
# bump looks like.
set -euo pipefail

VENV="${1:-$HOME/stackchan-gateway}"

# Find the installed package without hardcoding a Python version.
SERVER=$(ls "$VENV"/lib/python3.*/site-packages/stackchan_mcp/stdio_server.py 2>/dev/null | head -1 || true)
if [ -z "$SERVER" ] || [ ! -f "$SERVER" ]; then
  echo "ERROR: could not find stackchan_mcp/stdio_server.py under $VENV" >&2
  echo "       pass the venv root as the first argument" >&2
  exit 1
fi
echo "patching $SERVER"

python3 - "$SERVER" <<'PYEOF'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
text = path.read_text()
edits = 0

# ---------------------------------------------------------------- tool_map --
# Anchored on set_blink's entry: it is the neighbouring display tool, and its
# shape is stable across the releases we have seen.
map_old = '''        "set_blink": (
            "self.display.set_blink",
            arguments,
        ),'''

map_new = '''        "set_blink": (
            "self.display.set_blink",
            arguments,
        ),
        # --- added by cubie/gateway/apply-gateway-tools.sh ---
        # These three are implemented in this fleet's firmware
        # (firmware/apply-m5-expression.sh) and are what the host-side
        # character stack drives: gaze drift, breathing, mouth tilt, the dance
        # face animation and the speech bubble.
        "set_gaze": (
            "self.display.set_gaze",
            arguments,
        ),
        "set_feature": (
            "self.display.set_feature",
            arguments,
        ),
        "set_speech": (
            "self.display.set_speech",
            arguments,
        ),'''

# The "already patched" test comes FIRST, and that ordering is the whole
# reason this is idempotent: `map_new` CONTAINS `map_old` as its prefix, so the
# anchor is still present after a successful patch. A guard that tested the
# anchor first would match again on every re-run and append another copy --
# which is exactly the non-convergence that once shipped a wrong firmware
# build here, silently.
if '"set_gaze": (' in text:
    pass
elif map_old in text:
    text = text.replace(map_old, map_new, 1)
    edits += 1
else:
    print("ERROR: tool_map anchor not found (set_blink entry).")
    print("       The gateway's tool_map has changed shape -- most likely a")
    print("       version bump. Compare against 0.17.0 before editing this.")
    raise SystemExit(1)

# ------------------------------------------------------------ tools/list --
# Anchored on the set_mouth Tool definition's closing, which is unique.
list_old = '''            Tool(
                name="set_mouth_sequence",'''

list_new = '''            # --- added by cubie/gateway/apply-gateway-tools.sh ---
            Tool(
                name="set_gaze",
                description=(
                    "Point the avatar's eyes without moving the head. "
                    "x and y are -100..100, mapping onto about 16 pixels of "
                    "travel each way; y is positive DOWNWARD, so negative y "
                    "looks up. Both eyes move together. Held until the next "
                    "set_avatar, which restores that expression's own gaze."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {
                            "type": "integer",
                            "minimum": -100,
                            "maximum": 100,
                            "description": "Horizontal gaze, -100..100.",
                        },
                        "y": {
                            "type": "integer",
                            "minimum": -100,
                            "maximum": 100,
                            "description": (
                                "Vertical gaze, -100..100. POSITIVE IS DOWN."
                            ),
                        },
                    },
                    "required": ["x", "y"],
                },
            ),
            Tool(
                name="set_feature",
                description=(
                    "Drive one of the avatar's features directly. feature is "
                    "'eyes' (both), 'left_eye', 'right_eye' or 'mouth'. Each "
                    "axis is applied only if given; pass -1000 to leave an "
                    "axis to whatever already drives it. x and y count as ONE "
                    "axis and must both be given or both omitted. weight "
                    "contends with blink and lip-sync, so disable blink with "
                    "set_blink for the duration if you drive it. Held until "
                    "the next set_avatar."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "feature": {
                            "type": "string",
                            "enum": ["eyes", "left_eye", "right_eye", "mouth"],
                            "description": (
                                "One of: eyes, left_eye, right_eye, mouth."
                            ),
                        },
                        "x": {
                            "type": "integer",
                            "description": "-100..100, or -1000 to leave alone.",
                        },
                        "y": {
                            "type": "integer",
                            "description": (
                                "-100..100 (positive is DOWN), or -1000 to "
                                "leave alone."
                            ),
                        },
                        "rotation": {
                            "type": "integer",
                            "description": (
                                "Tenths of a degree, 0..3600, a negative angle "
                                "expressed as 3600 plus the angle. -1000 to "
                                "leave alone."
                            ),
                        },
                        "weight": {
                            "type": "integer",
                            "description": (
                                "0..100, or -1000 to leave alone. Contends "
                                "with blink and lip-sync."
                            ),
                        },
                        "size": {
                            "type": "integer",
                            "description": (
                                "-100..100 with 0 normal, or -1000 to leave "
                                "alone."
                            ),
                        },
                    },
                    "required": ["feature"],
                },
            ),
            Tool(
                name="set_speech",
                description=(
                    "Show text in the avatar's speech bubble. An empty string "
                    "clears it. Unlike the feature overrides this is NOT "
                    "cleared by set_avatar -- a caller that shows a bubble "
                    "owns hiding it again."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": (
                                "The text to show; empty string clears it."
                            ),
                        },
                    },
                    "required": ["text"],
                },
            ),
            Tool(
                name="set_mouth_sequence",'''

# Same ordering, for the same reason -- `list_new` also ends with its anchor.
if 'name="set_gaze"' in text:
    pass
elif list_old in text:
    text = text.replace(list_old, list_new, 1)
    edits += 1
else:
    print("ERROR: tools/list anchor not found (set_mouth_sequence Tool).")
    print("       Most likely a version bump. Compare against 0.17.0 first.")
    raise SystemExit(1)

if edits:
    path.write_text(text)
    print(f"stdio_server.py patched ({edits} edits)")
else:
    print("stdio_server.py: already patched")
PYEOF

# A syntax error here takes the gateway down, and the gateway owns the device
# connection. So it is checked before anyone restarts anything -- with the
# gateway's own interpreter, since that is the one that will import it.
PY=$(ls "$VENV"/bin/python 2>/dev/null || true)
if [ -x "$PY" ]; then
  "$PY" -m py_compile "$SERVER" && echo "py_compile: OK"
else
  python3 -m py_compile "$SERVER" && echo "py_compile: OK (system python3)"
fi

echo
echo "Done. Restart the gateway, then confirm all three are listed:"
echo "  sudo systemctl restart stackchan-gateway"
