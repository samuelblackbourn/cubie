#!/usr/bin/env bash
# Build Cubie's firmware when `main` moves. Driven by cubie-firmware-build.timer.
#
# Pull-based on purpose. A GitHub Actions self-hosted runner would be
# push-based and faster, but it costs a long-lived GitHub credential on this
# box and a runner service that can break independently. Polling a git remote
# needs no inbound access, no secret, and survives office-server's address
# changing -- which matters, because the design note rules out a tunnel for
# this device and the address is already a DHCP lease.
#
# What it will NOT do:
#
#   - Reset the robot. Publishing makes the image available; Cubie takes it on
#     his next reset. That is a free human gate and worth keeping.
#   - Retry a failed commit forever. A commit that fails to build is recorded
#     as failed and skipped until `main` moves again. Without that, one bad
#     merge means a full ESP-IDF build every few minutes, indefinitely.
#   - Touch a dirty checkout, or one that is not fast-forwardable. Both mean a
#     human is mid-something, and a timer should not race them.
#   - Delete a working tree. If the pin in build.conf has changed, build.sh
#     refuses and so does this: a `--fresh` run discards a tree that currently
#     produces working firmware, and that is a decision for a person.
set -euo pipefail

REPO="${CUBIE_REPO:-$HOME/cubie}"
STATE_DIR="${STATE_DIRECTORY:-/var/lib/cubie-build}"
BUILT="$STATE_DIR/built"
FAILED="$STATE_DIR/failed"

# Paths whose change justifies a rebuild. A README edit should not spend eight
# minutes of CPU and hand the robot a new version number for no reason -- and
# because the version is minutes-since-epoch, an empty rebuild really would
# look like a new release to the device.
WATCH=(firmware tools)

log() { printf '%s\n' "$*"; }
mkdir -p "$STATE_DIR"

[ -d "$REPO/.git" ] || { log "no checkout at $REPO"; exit 1; }

if ! git -C "$REPO" diff --quiet || ! git -C "$REPO" diff --cached --quiet; then
  log "checkout at $REPO has uncommitted changes -- standing down"
  exit 0
fi

branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
if [ "$branch" != "main" ]; then
  log "checkout is on '$branch', not main -- standing down"
  exit 0
fi

git -C "$REPO" fetch --quiet origin main
target=$(git -C "$REPO" rev-parse origin/main)
current=$(git -C "$REPO" rev-parse HEAD)

previous=""
[ -f "$BUILT" ] && previous=$(cat "$BUILT")

if [ "$target" = "$previous" ]; then
  # The common case, and it must be silent: this runs every few minutes and a
  # log line each time would bury the ones that matter.
  exit 0
fi

if [ -f "$FAILED" ] && [ "$target" = "$(cat "$FAILED")" ]; then
  log "$target already failed to build -- skipping until main moves"
  exit 0
fi

# Which watched paths changed. Compare against the last commit we BUILT, not
# against HEAD: if a previous run skipped a docs-only commit, the firmware
# change before it must still count.
base="${previous:-$current}"
changed=""
if [ -n "$previous" ] && git -C "$REPO" cat-file -e "$previous^{commit}" 2>/dev/null; then
  changed=$(git -C "$REPO" diff --name-only "$previous" "$target" -- "${WATCH[@]}")
else
  # No record, or a record we can no longer resolve. Build rather than assume
  # nothing relevant changed -- guessing "no" here means silently never
  # building again.
  changed="(no build on record)"
fi

if [ -z "$changed" ]; then
  log "main moved to $target but nothing under ${WATCH[*]} changed -- recording, not building"
  git -C "$REPO" merge --ff-only --quiet origin/main
  printf '%s\n' "$target" > "$BUILT"
  exit 0
fi

log "main moved: ${previous:-<none>} -> $target"
log "changed under ${WATCH[*]}:"
printf '%s\n' "$changed" | sed 's/^/  /'

# advice.diverging off: git's multi-line "you need to either merge or rebase"
# hint is addressed to a person at a terminal, and this runs in a journal where
# it only buries the one line that says what happened.
if ! git -C "$REPO" -c advice.diverging=false merge --ff-only --quiet origin/main; then
  log "cannot fast-forward to origin/main -- local commits present. Standing down."
  exit 0
fi

log "building"
if bash "$REPO/firmware/build.sh"; then
  printf '%s\n' "$target" > "$BUILT"
  rm -f "$FAILED"
  log "built and published $target"
  log "Cubie takes it on his next reset. Nothing here resets him."
else
  status=$?
  printf '%s\n' "$target" > "$FAILED"
  log "build FAILED for $target (exit $status) -- will not retry this commit"
  log "fix it, push, and the next commit is picked up normally"
  exit "$status"
fi
