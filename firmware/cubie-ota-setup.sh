#!/usr/bin/env bash
# Make Cubie's builds OTA-updatable.
#
# Two parts, and the second is the interesting one.
#
# --- 1. Distinct versions per build -----------------------------------------
#
# ESP-IDF stamps PROJECT_VER into the app descriptor, and every build currently
# stamps "2.2.6". The device only updates when the offered version is newer, so
# with a constant version OTA can never fire.
#
# A git SHA suffix does not solve it: Ota::IsNewVersionAvailable parses the
# version into NUMERIC components, so "2.2.6-gAAAAAAA" and "2.2.6-gBBBBBBB"
# both parse to [2,2,6] and compare equal. (The ticker's
# "1.5.0-dev-main-07d2085" works because CrossPoint compares differently.)
#
# So we append a fourth component that increases: minutes since the Unix epoch.
# [2,2,6,29309876] beats [2,2,6] on length and beats an older build on value.
# It stays well inside int32 and is human-readable as "when this was built".
#
# The literal `set(PROJECT_VER "2.2.6")` line is left intact above it, because
# scripts/release.py parses that exact text to name the release zip.
#
# --- 2. Publish by reading the version back OUT of the image -----------------
#
# firmwareStore refuses to serve an image whose bytes do not carry the version
# declared in firmware.version beside it. That guard is currently -- correctly
# -- blocking the office ticker, whose two files disagree.
#
# The way to never hit it is to never type the version twice: publish reads the
# version from the built binary's own app descriptor and writes that. The two
# cannot disagree, because only one of them is authored.
set -euo pipefail

FW="${1:-$HOME/stackchan-mcp/firmware}"
CM="$FW/CMakeLists.txt"
[ -f "$CM" ] || { echo "ERROR: $CM not found"; exit 1; }

python3 - "$CM" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()

if 'STACKCHAN_BUILD_SERIAL' in t:
    print("CMakeLists.txt already patched")
    sys.exit(0)

old = '''set(PROJECT_VER "2.2.6")
project(xiaozhi)'''
new = '''set(PROJECT_VER "2.2.6")

# Append a monotonically increasing build component so OTA can tell two builds
# apart. Ota::IsNewVersionAvailable() compares numeric components, so a git SHA
# suffix would compare equal and no update would ever be offered; minutes since
# the epoch both increase and stay well inside int32.
#
# The literal set() above is deliberately left untouched: scripts/release.py
# parses that exact line to name the release zip.
string(TIMESTAMP STACKCHAN_BUILD_EPOCH "%s" UTC)
math(EXPR STACKCHAN_BUILD_SERIAL "${STACKCHAN_BUILD_EPOCH} / 60")
set(PROJECT_VER "${PROJECT_VER}.${STACKCHAN_BUILD_SERIAL}")
message(STATUS "StackChan build version: ${PROJECT_VER}")

project(xiaozhi)'''

if old not in t:
    sys.exit("ABORTING -- PROJECT_VER anchor not found")
p.write_text(t.replace(old, new, 1))
print("CMakeLists.txt: PROJECT_VER now carries a build serial")
PYEOF

# ---------------------------------------------------------------- publisher --
cat > "$HOME/publish-cubie-firmware.sh" <<'PUB'
#!/usr/bin/env bash
# Publish the built firmware for OTA.
#
# Reads the version out of the image's own ESP-IDF app descriptor and writes it
# to firmware.version, so the pair the office serves cannot disagree -- which is
# exactly the failure currently blocking the ticker's OTA.
set -euo pipefail

BIN="${1:-$HOME/stackchan-mcp/firmware/build/xiaozhi.bin}"
DEST="${2:-$HOME/cubie-firmware}"
[ -f "$BIN" ] || { echo "ERROR: no image at $BIN -- build first"; exit 1; }
mkdir -p "$DEST"

VERSION=$(python3 - "$BIN" <<'PYEOF'
import struct, sys
d = open(sys.argv[1], 'rb').read()
# esp_image_header_t (24) + esp_image_segment_header_t (8) = 0x20, then
# esp_app_desc_t: magic, secure_version, reserv[2], version[32] at +16.
if len(d) < 0x60 or d[0] != 0xE9:
    sys.exit("not an ESP image")
magic, = struct.unpack_from('<I', d, 0x20)
if magic != 0xABCD5432:
    sys.exit(f"no app descriptor at 0x20 (magic {magic:#x})")
version = d[0x30:0x50].split(b'\0')[0].decode('utf8')
if not version:
    sys.exit("app descriptor carries an empty version")
print(version)
PYEOF
)

cp "$BIN" "$DEST/firmware.bin"
printf '%s\n' "$VERSION" > "$DEST/firmware.version"

echo "published to $DEST"
echo "  version : $VERSION   (read from the image, not typed)"
echo "  size    : $(stat -c%s "$DEST/firmware.bin") bytes"
echo
echo "The office serves this once BOTH of these are set on office-server and it"
echo "has been restarted:"
echo "  AGENTHUB_CUBIE_FIRMWARE_DIR=$DEST"
echo "  AGENTHUB_COMPANION_TOKEN=..."
echo
echo "Both, not either: httpServer.ts mounts Cubie's OTA route pair only when it"
echo "has a companion token AND a firmware directory, so setting the directory"
echo "alone publishes into a route that was never registered -- a 404 that looks"
echo "like a bad path rather than a missing token."
PUB
chmod +x "$HOME/publish-cubie-firmware.sh"

echo
echo "Done."
echo "  CMakeLists.txt patched  -- next build gets a version like 2.2.6.29309876"
echo "  ~/publish-cubie-firmware.sh installed"
