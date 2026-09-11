#!/usr/bin/env bash
# Write ONLY the assets partition over USB.
#
# --- why this exists -------------------------------------------------------
#
# The wake word is not in the app. The speech models and `index.json` -- which
# carries the phrase, the threshold, the language and the detection duration --
# live in `generated_assets.bin`, and `custom_wake_word.cc` reads all of them
# from there at runtime:
#
#     } else {
#         models_ = models_list;
#         ParseWakenetModelConfig();      // reads index.json out of the assets
#     }
#
# The Kconfig values are only consulted in the branch above that one, which
# does not run on this device. So CONFIG_CUSTOM_WAKE_WORD can be changed, built
# and OTA'd, and the robot will keep listening for the OLD phrase with nothing
# anywhere to say why.
#
# OTA writes `xiaozhi.bin` at 0x20000 and nothing else. That is the whole
# reason "hi cubie" never once executed on this device: the MultiNet models
# were never delivered, so `audio_service.cc` fell back to AfeWakeWord with
# wn9_nihaoxiaozhi_tts, and every adjustment made for months went into an image
# whose speech models had not changed since the factory.
#
# The other way to fix that is `merged-binary.bin` from 0x0, which does work --
# and takes NVS with it. That means re-entering the wifi credentials, the
# gateway URL and the token through the config portal every single time you
# want to try a different wake phrase. Once is a fix; every iteration is a
# reason to stop iterating.
#
# So: one partition, at its own offset, leaving the bootloader, the partition
# table, the app and NVS exactly where they are.
#
# --- the offset is READ, never typed ---------------------------------------
#
# From `flash_args`, written by the build that produced the image beside it. A
# literal 0x800000 here would be a number that is right until the partition
# table changes, and then writes an assets image over the middle of something
# else -- a class of mistake with no error message and a bricked device at the
# end of it. If flash_args and the image disagree about which build they came
# from, that is the caller's error to fix, not this script's to paper over.
set -euo pipefail

PORT=""
ASSETS=""
ARGS_FILE=""
BAUD="${BAUD:-921600}"

usage() {
    cat <<'USAGE'
usage: flash-assets.sh --port DEVICE [--assets FILE] [--args FILE]

  --port    the serial device, e.g. /dev/cu.usbmodem143101 on macOS or
            /dev/ttyACM0 on Linux. On macOS use /dev/cu.* and not /dev/tty.* --
            the tty variant blocks waiting for carrier detect and simply hangs.
  --assets  generated_assets.bin. Defaults to ./generated_assets.bin
  --args    flash_args from the SAME build. Defaults to ./flash_args

Copy both files off the build host together:

  scp office-server:~/stackchan-mcp/firmware/build/generated_assets.bin \
      office-server:~/stackchan-mcp/firmware/build/flash_args .

NVS is not touched, so wifi, the gateway URL and the token all survive.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)   PORT="${2:-}"; shift 2 ;;
        --assets) ASSETS="${2:-}"; shift 2 ;;
        --args)   ARGS_FILE="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$PORT" ] || { echo "ERROR: --port is required" >&2; usage >&2; exit 2; }
ASSETS="${ASSETS:-./generated_assets.bin}"
ARGS_FILE="${ARGS_FILE:-$(dirname "$ASSETS")/flash_args}"

[ -f "$ASSETS" ]    || { echo "ERROR: no assets image at $ASSETS" >&2; exit 1; }
[ -f "$ARGS_FILE" ] || { echo "ERROR: no flash_args at $ARGS_FILE -- it carries the offset, and this script will not guess one" >&2; exit 1; }

# The offset for THIS image, from the build's own manifest. A line in
# flash_args looks like:  0x800000 generated_assets.bin
name="$(basename "$ASSETS")"
offset="$(awk -v want="$name" '$2 == want { print $1; found=1 } END { if (!found) exit 1 }' "$ARGS_FILE")" || {
    echo "ERROR: flash_args does not mention $name -- wrong build, or a renamed image" >&2
    exit 1
}

case "$offset" in
    0x*) ;;
    *) echo "ERROR: offset for $name is '$offset', which is not a hex address" >&2; exit 1 ;;
esac

# A partition image that is implausibly small is a build that half-ran. Writing
# it would leave the models truncated, and a truncated model partition fails the
# way this whole exercise started: by silently selecting a different wake word.
size="$(wc -c < "$ASSETS")"
if [ "$size" -lt 1048576 ]; then
    echo "ERROR: $ASSETS is only $size bytes -- too small to be the assets partition. Refusing." >&2
    exit 1
fi

# The flash geometry from the same manifest, for the same reason as the offset.
geometry="$(awk '/--flash_mode/ { for (i = 1; i <= NF; i++) printf "%s ", $i; print ""; exit }' "$ARGS_FILE")"
[ -n "$geometry" ] || { echo "ERROR: flash_args carries no --flash_mode line" >&2; exit 1; }

echo "writing $name ($size bytes) to $offset on $PORT"
echo "  geometry: $geometry"
echo "  NVS is NOT in this write: wifi, gateway URL and token survive."

# shellcheck disable=SC2086 -- geometry is a deliberate word-split argument list
python3 -m esptool --chip esp32s3 -p "$PORT" -b "$BAUD" \
    write_flash $geometry "$offset" "$ASSETS"

echo
echo "Done. Reset him, then confirm the phrase actually changed:"
echo "  the boot log's 'CustomWakeWord: Command: ...' line is printed by"
echo "  ParseWakenetModelConfig reading the index.json you just wrote, so it"
echo "  IS the check -- if it still names the old phrase, this did not land."
