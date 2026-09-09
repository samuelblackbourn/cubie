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
#   apply-m5-touch          replaces that classification with M5's own model:
#                           finger POSITION from the pad levels, not zones and
#                           not duration. Anchors on the block
#                           apply-face-and-touch emits, so it must follow it
#   apply-m5-expression     the eye-GAZE and rotation axes, which nothing in
#                           this firmware ever used -- and a `thinking` face
#                           built on them. Anchors on the resting-mouth block
#                           fix-expression-depth emits, so it must follow it
#   apply-vad-auto-stop     let the device end its own listen when the speaker
#                           stops. Touches application.cc/h, which nothing else
#                           in this chain touches, so its position is free
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

  # No VENDORED.md is written here. apply-live-avatar-step1.sh writes it
  # unconditionally a few lines below, so anything written now is overwritten
  # before the build sees it -- and a dead heredoc that LOOKS like the
  # provenance record is worse than none, because the next person to bump the
  # pin would edit it and watch nothing happen.
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
  apply-m5-touch.sh
  apply-m5-expression.sh
  apply-vad-auto-stop.sh
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

# --------------------------------------- 3b. the provenance record is true --
# step1 hardcodes the M5 commit in VENDORED.md. build.conf carries it too, and
# the vendor step above skips when the file already names the pin. If those two
# ever disagree, the sources copied in are one commit and the record says
# another -- a lying provenance record in the one file whose whole job is
# provenance. Fail rather than ship that.
say "provenance"
if ! grep -q "$M5_PIN" "$M5DIR/VENDORED.md"; then
  recorded=$(grep -oE '[0-9a-f]{40}' "$M5DIR/VENDORED.md" | head -1)
  die "VENDORED.md records ${recorded:-no commit}, but build.conf pins $M5_PIN.
       apply-live-avatar-step1.sh hardcodes the commit it writes there, so
       bumping M5_PIN alone is not enough -- update the Commit row in that
       script's heredoc to match, then re-run with --fresh."
fi
echo "  VENDORED.md records $M5_PIN, matching build.conf"

# ------------------------------------------------ 4. build configuration --
# These two entries reached the working build from a source nobody could name,
# which means they were not reproducible and not reviewable. They are set here
# instead, in the board config release.py actually reads.
say "sdkconfig_append"
python3 - "$BOARD/config.json" "http://$OFFICE_HOST:$OFFICE_PORT/api/companion/ota" \
    "$FIRMWARE_LANGUAGE" "$WAKE_WORD" "$WAKE_WORD_DISPLAY" "$WAKE_WORD_THRESHOLD" <<'PYEOF'
import json, pathlib, sys

path = pathlib.Path(sys.argv[1])
ota_url = sys.argv[2]
language = sys.argv[3]
wake_word = sys.argv[4]
wake_word_display = sys.argv[5]
wake_word_threshold = sys.argv[6]
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
    # Without this the build is zh-CN: Chinese on screen and Chinese locale
    # sound assets. It selects a Kconfig `choice`, so whether appending is
    # enough to override the choice's own default is something only the build
    # can confirm -- watch for this line in the sdkconfig_append echo, and
    # then watch the boot screen.
    f"CONFIG_{language}=y",
    # --- the wake word -------------------------------------------------
    # A CUSTOM phrase, so it can be his own name rather than one of
    # Espressif's pre-trained WakeNet words. The firmware supports this
    # directly: main/audio/wake_words/custom_wake_word.cc drives esp-sr's
    # MultiNet command recogniser and registers the phrase at runtime with
    # esp_mn_commands_add().
    #
    # MultiNet6 specifically, because its native input is graphemes.
    # Espressif's docs: "MultiNet5 requires the input command string to be
    # phonemes, and MultiNet6 and MultiNet7 only accepts grapheme inputs to
    # API calls" -- and MultiNet7 fed graphemes runs its own
    # grapheme-to-phoneme step at runtime, which the same docs say costs "a
    # little accuracy drop". So 6 is the right model for a phrase written
    # as words.
    #
    # Requires ESP32-S3 with PSRAM, which CoreS3 is. Whether the pinned
    # esp-sr (~2.3.0) actually carries MULTINET6_QUANT is something only the
    # build can confirm -- watch for these lines in the echo below, and then
    # watch the boot log for "Custom wake word" from custom_wake_word.cc.
    "CONFIG_USE_CUSTOM_WAKE_WORD=y",
    "CONFIG_SR_MN_EN_MULTINET6_QUANT=y",
    f'CONFIG_CUSTOM_WAKE_WORD="{wake_word}"',
    f'CONFIG_CUSTOM_WAKE_WORD_DISPLAY="{wake_word_display}"',
    f"CONFIG_CUSTOM_WAKE_WORD_THRESHOLD={wake_word_threshold}",
    # Lets you interrupt him mid-answer by saying the wake word again.
    # Default is off; for a conversation rather than a query, being able to
    # cut in is most of what makes it feel like talking to someone.
    "CONFIG_WAKE_WORD_DETECTION_IN_LISTENING=y",
]

builds = config.get("builds") or []
target = next((b for b in builds if b.get("name") == "stackchan"), None)
if target is None:
    sys.exit("ABORTING -- no build named 'stackchan' in config.json")

appended = target.setdefault("sdkconfig_append", [])
changed = False

# Kconfig `choice` groups. Members are mutually exclusive but are DIFFERENT
# KEYS, so the by-key replacement below cannot drop the loser -- it would leave
# both set, and two selected options in one choice is ill-defined.
#
# Found by a test that switched EN_US to JA_JP and got both. The same trap
# applies to the wake-word type and the MultiNet model, which is why this is a
# list rather than the one language special case it started as.
CHOICE_GROUPS = [
    # The UI language and its locale sound assets.
    lambda key: key.startswith("CONFIG_LANGUAGE_"),
    # choice WAKE_WORD_TYPE in main/Kconfig.projbuild.
    lambda key: key in {
        "CONFIG_WAKE_WORD_DISABLED",
        "CONFIG_USE_ESP_WAKE_WORD",
        "CONFIG_USE_AFE_WAKE_WORD",
        "CONFIG_USE_CUSTOM_WAKE_WORD",
    },
    # choice SR_MN_EN in esp-sr's Kconfig.projbuild.
    lambda key: key in {
        "CONFIG_SR_MN_EN_NONE",
        "CONFIG_SR_MN_EN_MULTINET5_SINGLE_RECOGNITION_QUANT8",
        "CONFIG_SR_MN_EN_MULTINET6_QUANT",
        "CONFIG_SR_MN_EN_MULTINET7_QUANT",
    },
]

for in_group in CHOICE_GROUPS:
    chosen = next((e for e in wanted if in_group(e.split("=", 1)[0])), None)
    if chosen is None:
        continue
    stale = [e for e in appended
             if in_group(e.split("=", 1)[0]) and e != chosen]
    for entry in stale:
        print(f"  - {entry}   (a choice allows only one)")
        appended.remove(entry)
        changed = True

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
