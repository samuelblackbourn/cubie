#!/usr/bin/env bash
# Record everything Cubie says on his USB serial console, forever.
#
# --- why this exists ---------------------------------------------------
#
# He crashes. Not often, not on demand, and every attempt to catch it has
# failed the same way: a crash prints its evidence ONCE, on the serial console,
# at a moment nobody can predict. Catching that has meant a person sitting with
# `cat /dev/cu.usbmodem*` running on a laptop, waiting. Across several
# sessions that produced exactly zero backtraces, because he never crashed
# while anyone was watching.
#
# The device is on the same desk as the machine that runs everything else. So
# it records itself, into the journal, whether or not anyone is there.
#
# --- what the journal buys -------------------------------------------------
#
# Rotation, timestamps and `--since` for free, and -- the point -- the SAME
# clock as stackchan-gateway and cubie-character. A crash can finally be lined
# up against the turn that preceded it:
#
#   journalctl -u cubie-serial -u cubie-character --since "10 min ago"
#
# That interleaving is the whole reason this writes to stdout rather than to a
# file of its own.
#
# --- the reset problem -----------------------------------------------------
#
# CoreS3 speaks USB-CDC from the ESP32 itself, not through a separate UART
# chip. So when he reboots, the USB device DISAPPEARS and comes back -- often
# under a different /dev/ttyACM number. `cat` dies at that moment, which is
# precisely the moment worth recording.
#
# Hence: resolve the port by its stable by-id name, and let systemd restart
# this on exit. The gap costs the first second of a boot; the alternative,
# holding a device node that no longer exists, costs everything after it.
set -euo pipefail

# by-id rather than /dev/ttyACM0: the number is assigned in enumeration order,
# so a reset, another serial device, or a reboot can move it. The by-id name is
# derived from the device itself and does not move.
PORT="${CUBIE_SERIAL_PORT:-}"
if [ -z "$PORT" ]; then
    for candidate in /dev/serial/by-id/*; do
        [ -e "$candidate" ] || continue
        PORT="$candidate"
        break
    done
fi

if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
    # Loud and non-zero rather than quiet: a recorder that silently records
    # nothing is worse than no recorder, because it is believed. systemd will
    # retry, so an unplugged robot resolves itself when he is plugged back in.
    echo "cubie-serial: no serial device found (looked in /dev/serial/by-id;" \
         "set CUBIE_SERIAL_PORT to name one)" >&2
    exit 1
fi

echo "cubie-serial: recording $PORT" >&2

# Raw, no echo: the console is one-way and a terminal line discipline would
# mangle it -- and worse, echo can write bytes BACK to the device, which on a
# console that accepts input is a way to disturb the thing being observed.
stty -F "$PORT" raw -echo 115200 2>/dev/null || true

# exec so systemd supervises `cat` directly: no shell sitting between the
# process that dies on re-enumeration and the supervisor that restarts it.
exec cat "$PORT"
