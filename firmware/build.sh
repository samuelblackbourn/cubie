#!/usr/bin/env bash
# One command from nothing to a publishable Cubie image.
#
# Until this existed, Cubie's firmware was the product of eight patch scripts
# applied to one machine's working tree in an order that lived only in a chat
# transcript -- including a `cp -r` of M5's avatar that was never scripted at
# all, and two sdkconfig entries whose origin nobody could name. That is the
# same objection already raised one level up: a process that exists only in
# someone's shell history is not a process.
#
# So this script owns the whole chain, and the order below is not arbitrary.
# Each patch is anchored on exact text the previous one emits and ABORTS rather
# than guess if the anchor is missing:
#
#   vendor M5 avatar        (cp -r; step1 refuses to run without it)
#   apply-live-avatar-step1 adds files and build wiring, calls nothing
#   apply-live-avatar-step2 wires the avatar into the board
#   fix-eye-weight          step2 rendered the eyes shut (100 = open, not 0)
#   fix-expression-depth    the mouth was never driven per face
#   fix-touch-classify      restores a reachable TAP window, logs at INFO
#   apply-face-and-touch    shy decorator + zone-travel classification
#                           ^ anchors on the comment fix-touch-classify writes,
#                             so it genuinely cannot run before it
#   cubie-ota-setup         build serial in PROJECT_VER, installs the publisher
#
# Every step is idempotent, so a re-run on an already-patched tree is a series
# of "already patched" lines rather than an error -- which is what makes this
# safe to hand to a timer.
#
# Usage:
#   bash firmware/build.sh              patch, build, publish
#   bash firmware/build.sh --no-publish patch and build only
#   bash firmware/build.sh --fresh      discard the working tree and start clean
#   bash firmware/build.sh --check      report what WOULD happen, change nothing
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=build.conf
. "$HERE/build.conf"

PUBLISH=1
FRESH=0
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --no-publish) PUBLISH=0 ;;
    --fresh)      FRESH=1 ;;
    --check)      CHECK=1; PUBLISH=0 ;;
    -h|--help)    sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n=== %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# --------------------------------------------------------------- preflight --
command -v git >/dev/null || die "git not found"
if [ "$CHECK" = 0 ]; then
  # The build runs in a container so the host needs no ESP-IDF at all -- which
  # is the point, and why docker is a hard requirement rather than a fallback.
  docker_cmd=(docker)
  if ! docker info >/dev/null 2>&1; then
    # `sudo -n` deliberately: this script has to run unattended from a timer,
    # and a sudo password prompt there would hang the unit until its timeout
    # rather than fail. But -n also fails when sudo would merely have ASKED,
    # which is a different problem with a different fix -- so say which.
    if sudo -n docker info >/dev/null 2>&1; then
      docker_cmd=(sudo docker)
    elif ! command -v docker >/dev/null 2>&1; then
      die "docker is not installed on this machine"
    else
      reason=$(docker info 2>&1 | head -3 || true)
      if printf '%s' "$reason" | grep -qi 'permission denied'; then
        die "docker is running, but this user cannot reach its socket.

       This is a permissions problem, not a dead daemon. Two fixes:

         sudo usermod -aG docker \$USER   # then log out and back in
         sudo -v                         # caches sudo ~15 min, then re-run

       The group is the one to prefer: the build timer runs unattended and
       cannot answer a password prompt. Note that membership of the docker
       group is equivalent to root on this machine -- a container can mount
       the host filesystem -- so it is a deliberate trade."
      else
        die "cannot reach the docker daemon:

$(printf '%s' "$reason" | sed 's/^/         /')"
      fi
    fi
  fi
fi

say "configuration"
cat <<CFG
  upstream   $STACKCHAN_REPO
             @ $STACKCHAN_PIN
  M5 avatar  $M5_REPO
             @ $M5_PIN
  office     http://$OFFICE_HOST:$OFFICE_PORT
  toolchain  $IDF_IMAGE
  tree       $WORK
  publish    $PUBLISH_DIR $([ "$PUBLISH" = 1 ] && echo "(will publish)" || echo "(publish skipped)")
CFG

if [ "$CHECK" = 1 ]; then
  say "--check: nothing was changed"
  if [ -d "$WORK/.git" ]; then
    head=$(git -C "$WORK" rev-parse HEAD)
    if [ "$head" = "$STACKCHAN_PIN" ]; then
      echo "  tree is at the pin"
    else
      echo "  tree is at $head -- NOT the pin; --fresh would reclone"
    fi
    echo "  local modifications:"
    git -C "$WORK" status --short | sed 's/^/    /'
  else
    echo "  no tree at $WORK -- a run would clone it"
  fi
  exit 0
fi

# ------------------------------------------------------- 1. upstream tree --
if [ "$FRESH" = 1 ] && [ -d "$WORK" ]; then
  # Check before announcing: saying "discarding" and then refusing reads as if
  # something was half-done. Nothing is.
  #
  # Refuse to delete anything that is not obviously ours to delete -- a typo in
  # WORK should not cost a home directory.
  [ -d "$WORK/.git" ] || die "$WORK is not a git checkout -- refusing to delete it"
  say "discarding $WORK (--fresh)"
  rm -rf "$WORK"
fi

if [ ! -d "$WORK/.git" ]; then
  say "cloning upstream at the pin"
  # --recurse-submodules is load-bearing: smooth_ui_toolkit is a submodule, and
  # M5's vendored avatar needs it at exactly v2.12.0 (see VENDORED.md). A clone
  # without it compiles until it reaches the avatar and then fails obscurely.
  git clone --recurse-submodules "$STACKCHAN_REPO" "$WORK"
  git -C "$WORK" checkout --quiet "$STACKCHAN_PIN"
  git -C "$WORK" submodule update --init --recursive
else
  head=$(git -C "$WORK" rev-parse HEAD)
  if [ "$head" != "$STACKCHAN_PIN" ]; then
    die "$WORK is at $head, not the pin $STACKCHAN_PIN.
       The patches are anchored on the pinned text and would abort or, worse,
       apply to something they were not written for. Re-run with --fresh to
       reclone, or update STACKCHAN_PIN in build.conf deliberately."
  fi
  say "upstream tree already at the pin"
fi

FW="$WORK/firmware"
BOARD="$FW/main/boards/stackchan"
[ -d "$BOARD" ] || die "$BOARD missing -- is the pin right?"

# --------------------------------------------------- 2. vendor M5's avatar --
M5DIR="$BOARD/m5avatar"
need_vendor=1
if [ -f "$M5DIR/VENDORED.md" ] && grep -q "$M5_PIN" "$M5DIR/VENDORED.md" 2>/dev/null; then
  need_vendor=0
  say "M5 avatar already vendored at the pin"
fi

if [ "$need_vendor" = 1 ]; then
  say "vendoring M5's avatar at $M5_PIN"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  # A blobless partial clone: this repo is an entire ESP-IDF application and we
  # want ~1,700 lines of it. Depth-1 on a named commit is not allowed by every
  # server, so fetch the commit explicitly.
  git init --quiet "$tmp/m5"
  git -C "$tmp/m5" remote add origin "$M5_REPO"
  git -C "$tmp/m5" fetch --quiet --depth 1 origin "$M5_PIN"
  git -C "$tmp/m5" checkout --quiet FETCH_HEAD

  src="$tmp/m5/firmware/main/stackchan"
  [ -d "$src/avatar" ] || die "$src/avatar not found -- has M5's layout moved? Update VENDORED.md and this script together."

  mkdir -p "$M5DIR/avatar" "$M5DIR/utils"
  # `hal/` is OURS -- a shim so M5's files stay byte-identical and re-syncing
  # is a plain cp -r. Copying over it would replace the shim with nothing.
  rm -rf "$M5DIR/avatar"
  cp -r "$src/avatar" "$M5DIR/avatar"
  cp "$src/utils/object_pool.h" "$M5DIR/utils/object_pool.h"
  rm -rf "$tmp"
  trap - EXIT

  if [ ! -f "$M5DIR/VENDORED.md" ]; then
    cat > "$M5DIR/VENDORED.md" <<VEOF
# Vendored: M5Stack StackChan avatar

\`avatar/\` is copied **unmodified** from M5Stack's official StackChan firmware.
\`utils/object_pool.h\` is copied for \`ObjectPool<Decorator>\`, which
\`avatar/avatar/decorator.h\` includes as \`../../utils/object_pool.h\` -- the
directory layout here preserves that relative path so no edit is needed.

| | |
| --- | --- |
| Upstream | $M5_REPO |
| Path | \`firmware/main/stackchan/avatar/\` |
| Commit | \`$M5_PIN\` |
| Licence | MIT, Copyright (c) 2026 M5Stack Technology CO LTD |

Written by firmware/build.sh. \`hal/\` is ours and must not be overwritten.
VEOF
  fi
fi

# ------------------------------------------------------- 3. the patch set --
# Order matters and is explained at the top of this file. Do not sort it.
PATCHES=(
  apply-live-avatar-step1.sh
  apply-live-avatar-step2.sh
  fix-eye-weight.sh
  fix-expression-depth.sh
  fix-touch-classify.sh
  apply-face-and-touch.sh
  cubie-ota-setup.sh
)
for patch in "${PATCHES[@]}"; do
  [ -f "$HERE/$patch" ] || die "$HERE/$patch missing"
done

for patch in "${PATCHES[@]}"; do
  say "$patch"
  bash "$HERE/$patch" "$FW"
done

# fix-shy-ctor.sh is deliberately NOT in the list. It repairs a tree where the
# three-argument ShyDecorator call already landed; apply-face-and-touch.sh now
# emits the correct two-argument form, so on a clean run it has nothing to do.
# Run it here anyway, as a cheap assertion that the emitted call is right --
# it prints "already correct" and exits 0, and would fail loudly if a future
# edit reintroduced the bad form.
say "fix-shy-ctor.sh (assertion: expect \"already correct\")"
bash "$HERE/fix-shy-ctor.sh" "$FW"

# ------------------------------------------------ 4. build configuration --
# These two entries reached the working build from a source nobody could name,
# which means they were not reproducible and not reviewable. They are set here
# instead, in the board config release.py actually reads.
say "sdkconfig_append"
python3 - "$BOARD/config.json" "http://$OFFICE_HOST:$OFFICE_PORT/api/companion/ota" <<'PYEOF'
import json, pathlib, sys

path = pathlib.Path(sys.argv[1])
ota_url = sys.argv[2]
config = json.loads(path.read_text())

wanted = [
    # The live avatar's speech bubble uses this font. Setting it here rather
    # than editing M5's lv_conf keeps every vendored file byte-identical, so
    # re-syncing stays a plain cp -r.
    "CONFIG_LV_FONT_MONTSERRAT_16=y",
    # Where the device asks for updates. Note there is no "force" field in the
    # manifest the office serves: Ota::IsNewVersionAvailable compares numeric
    # version components, and a forced update that the device cannot decline
    # is a boot loop waiting for a bad image.
    f'CONFIG_OTA_URL="{ota_url}"',
]

builds = config.get("builds") or []
target = next((b for b in builds if b.get("name") == "stackchan"), None)
if target is None:
    sys.exit("ABORTING -- no build named 'stackchan' in config.json")

appended = target.setdefault("sdkconfig_append", [])
changed = False
for entry in wanted:
    key = entry.split("=", 1)[0]
    # Replace by key rather than appending: OTA_URL's value changes with the
    # office's address, and two conflicting CONFIG_OTA_URL lines would leave
    # the winner to whichever the generator reads last.
    existing = next((i for i, e in enumerate(appended) if e.split("=", 1)[0] == key), None)
    if existing is None:
        appended.append(entry)
        changed = True
        print(f"  + {entry}")
    elif appended[existing] != entry:
        print(f"  ~ {appended[existing]}\n    -> {entry}")
        appended[existing] = entry
        changed = True
    else:
        print(f"  = {entry}")

if changed:
    path.write_text(json.dumps(config, indent=4) + "\n")
    print("  config.json updated")
else:
    print("  config.json already correct")
PYEOF

# ------------------------------------------------------------- 5. compile --
say "building in $IDF_IMAGE"
# --ulimit nofile is not decoration: ESP-IDF's build opens a great many files
# and the default container limit makes it fail well into a long build.
"${docker_cmd[@]}" run --rm \
  --cpus="$BUILD_CPUS" \
  --ulimit nofile=65536:65536 \
  -v "$FW:/project" -w /project \
  "$IDF_IMAGE" \
  python ./scripts/release.py stackchan

BIN="$FW/build/xiaozhi.bin"
[ -f "$BIN" ] || die "build reported success but $BIN is missing"

# ------------------------------------------------------------- 6. publish --
if [ "$PUBLISH" = 0 ]; then
  say "built, not published"
  echo "  image: $BIN"
  exit 0
fi

PUBLISHER="$HOME/publish-cubie-firmware.sh"
[ -x "$PUBLISHER" ] || die "$PUBLISHER not found -- cubie-ota-setup.sh installs it"

say "publishing"
# The publisher reads the version out of the image's own ESP-IDF app
# descriptor, so firmware.bin and firmware.version cannot disagree. That guard
# is the one currently -- correctly -- blocking the office ticker, whose two
# files do disagree.
"$PUBLISHER" "$BIN" "$PUBLISH_DIR"

say "done"
echo "  Cubie takes this on his next reset. Nothing here resets him:"
echo "  a build is not a good enough reason to interrupt a robot."
