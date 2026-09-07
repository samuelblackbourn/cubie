#!/usr/bin/env bash
# Prove the patch chain converges. Not part of `make check` -- it clones two
# repositories and takes minutes -- but run it after touching anything in
# firmware/*.sh, because the failure it catches is silent.
#
# What went wrong once, and would again:
#
#   apply-live-avatar-step1.sh regenerated avatar_live.cc on every run, and
#   fix-expression-depth.sh exited early on a stackchan.cc marker before
#   reaching its avatar_live.cc edit. So a SECOND pass over an already-patched
#   tree produced different sources from the first: EyeSizeForFace reverted to
#   the original one-liner, and `surprised` shipped narrower eyes.
#
#   Nothing failed. The build succeeded. The firmware was just quietly wrong,
#   and only on incremental builds -- which is every build the timer does.
#
# So the test is not "does it run twice without erroring" but "does it converge
# to the same bytes". Three passes, plus the half-patched state a failed run
# leaves behind.
#
# docker is stubbed out: the compile proves nothing here and costs minutes. The
# patch chain needs only sources.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${IDEMPOTENT_WORKDIR:-$(mktemp -d)}"
KEEP="${IDEMPOTENT_KEEP:-0}"
BOARD_REL="firmware/main/boards/stackchan"

cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORKDIR"; }
trap cleanup EXIT

mkdir -p "$WORKDIR/bin"
printf '#!/bin/sh\nexit 0\n' > "$WORKDIR/bin/docker"
chmod +x "$WORKDIR/bin/docker"
export PATH="$WORKDIR/bin:$PATH"

TREE="$WORKDIR/tree"
fail=0

# Every build.sh call below ends `|| true`: the stubbed docker produces no
# image, so build.sh correctly reports failure at its very last step. That is
# not what is under test -- the patch chain has already run by then -- and
# swallowing it here rather than special-casing it means a genuine patch ABORT
# still surfaces, both in the log grep and in the snapshot comparison.

echo "== pass 1 (fresh clone, vendor, patch)"
WORK="$TREE" bash "$HERE/build.sh" --fresh --no-publish > "$WORKDIR/p1.log" 2>&1 || true
if ! [ -f "$TREE/$BOARD_REL/avatar_live.cc" ]; then
  echo "FAIL: pass 1 did not produce avatar_live.cc"; sed -n '$p' "$WORKDIR/p1.log"; exit 1
fi
cp -r "$TREE/$BOARD_REL" "$WORKDIR/s1"

for pass in 2 3; do
  echo "== pass $pass (re-run over the patched tree)"
  WORK="$TREE" bash "$HERE/build.sh" --no-publish > "$WORKDIR/p$pass.log" 2>&1 || true
  if grep -q "ABORTING" "$WORKDIR/p$pass.log"; then
    echo "FAIL: a patch script aborted on pass $pass:"
    grep -B2 "ABORTING" "$WORKDIR/p$pass.log" | sed 's/^/    /'
    fail=1
  fi
  cp -r "$TREE/$BOARD_REL" "$WORKDIR/s$pass"
  if diff -r -x '*.orig' "$WORKDIR/s1" "$WORKDIR/s$pass" > "$WORKDIR/d$pass.diff" 2>&1; then
    echo "   converged: identical to pass 1"
  else
    echo "FAIL: pass $pass produced a different tree from pass 1:"
    head -40 "$WORKDIR/d$pass.diff" | sed 's/^/    /'
    fail=1
  fi
done

# The state a failed run leaves behind. step1 regenerates avatar_live.* when
# they are absent, while stackchan.cc stays patched -- so the two files
# disagree about how far the chain has got. The next run must heal it without
# --fresh, because that next run is the timer's.
echo "== heal (avatar_live.* regenerated, stackchan.cc still patched)"
rm -f "$TREE/$BOARD_REL/avatar_live.cc" "$TREE/$BOARD_REL/avatar_live.h"
WORK="$TREE" bash "$HERE/build.sh" --no-publish > "$WORKDIR/heal.log" 2>&1 || true
if diff -r -x '*.orig' "$WORKDIR/s1" "$TREE/$BOARD_REL" > "$WORKDIR/heal.diff" 2>&1; then
  echo "   healed: identical to pass 1"
else
  echo "FAIL: did not heal back to the fresh-build tree:"
  head -40 "$WORKDIR/heal.diff" | sed 's/^/    /'
  fail=1
fi

echo
if [ "$fail" = 0 ]; then
  echo "PASS -- the patch chain converges, and heals from a failed run"
else
  echo "FAILED. Logs in $WORKDIR (re-run with IDEMPOTENT_KEEP=1 to keep them)"
  KEEP=1
fi
exit "$fail"
