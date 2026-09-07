#!/usr/bin/env bash
# Make the six expressions actually distinct.
#
# Diagnosis from hardware: touch reactions were applying correctly all along
# ("TAP reaction: face=surprised applied=1"), but rendered almost identically
# to idle. Two causes, both in our mapping rather than M5's renderer:
#
# 1. Eyes only. setEmotion sets eye weight per emotion -- Neutral 100,
#    Doubt 75, Happy 72, Sad 70, Sleepy 35 -- and for `surprised` we mapped to
#    Doubt, i.e. 75 against idle's 100. A quarter of an eyelid is not a
#    startled robot.
#
# 2. The mouth never moved. Mouth weight was driven only from
#    current_mouth_index_, which is 0 (closed) except during lip-sync. So all
#    six expressions wore the same thin closed mouth -- and DefaultMouth does
#    not override setEmotion, so M5 never sets it either. The mouth is
#    entirely ours to drive, and we were not driving it.
#
# Fix: per-face resting mouth weight and eye size, applied whenever that axis
# is not being driven by something more specific (lip-sync for the mouth, a
# blink for the eyes). `surprised` additionally overrides the eye weight,
# since M5 has no Surprised emotion to borrow one from.
set -euo pipefail

SC="${1:-$HOME/stackchan-mcp/firmware}/main/boards/stackchan/stackchan.cc"
[ -f "$SC" ] || { echo "ERROR: $SC not found"; exit 1; }

python3 - "$SC" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()

old = '''        if (active_layer_ == ActiveLayer::EYES) {
            live_avatar_->SetEyeWeight(kEyeWeight[e]);
        }
        live_avatar_->SetMouthWeight(kMouthWeight[m]);'''

new = '''        if (active_layer_ == ActiveLayer::EYES) {
            // Mid-blink: the eye axis wins.
            live_avatar_->SetEyeWeight(kEyeWeight[e]);
        } else if (current_face_index_ == 4) {
            // `surprised` has no M5 emotion to borrow from -- it is mapped to
            // Doubt, whose eye weight (75) reads as marginally-narrowed rather
            // than startled. Force the eyes wide instead.
            live_avatar_->SetEyeWeight(100);
        }

        if (active_layer_ == ActiveLayer::MOUTH) {
            // Lip-sync is driving the mouth.
            live_avatar_->SetMouthWeight(kMouthWeight[m]);
        } else {
            // At rest the mouth is entirely ours: DefaultMouth does not
            // override setEmotion, so M5 never sets it from the expression.
            // Without this every face wore the same thin closed mouth, which
            // is most of why the expressions read as flat.
            //
            // DefaultMouth::setWeight maps 0..100 onto height minH..maxH while
            // narrowing the width, so a higher weight is a rounder, more open
            // mouth.
            static constexpr int kFaceMouthWeight[6] = {
                 0,   // idle        -- closed line
                40,   // happy       -- open, wide
                12,   // thinking    -- barely parted
                 6,   // sad         -- small
                85,   // surprised   -- round and open
                22,   // embarrassed -- slight
            };
            live_avatar_->SetMouthWeight(kFaceMouthWeight[current_face_index_]);
        }'''

# This script edits TWO files, and their states are independent. Exiting here
# on stackchan.cc's marker skipped the avatar_live.cc work below -- and
# apply-live-avatar-step1.sh regenerates avatar_live.cc, so it can be
# un-patched while stackchan.cc is patched. The result was silent: an
# incremental build shipped EyeSizeForFace reverted to the original one-liner,
# giving narrower `surprised` eyes than a fresh build of the same commit.
#
# So: record what stackchan.cc needs, and carry on to avatar_live.cc either way.
stackchan_done = 'kFaceMouthWeight' in text
if not stackchan_done:
    if old not in text:
        sys.exit("ABORTING -- anchor not found; apply fix-eye-weight.sh first")
    if text.count(old) != 1:
        sys.exit(f"ABORTING -- anchor appears {text.count(old)} times")
    text = text.replace(old, new, 1)

# Widen the `surprised` eye size. setSize maps -100..100 onto an 8..32 px eye,
# so 45 was only a few pixels above the default. 100 is the full 32 px.
old_size = '''int EyeSizeForFace(int face_index) { return face_index == 4 ? 45 : 0; }'''
new_size = '''int EyeSizeForFace(int face_index) {
    // Feature::setSize is -100..100 and DefaultEyes maps it onto an 8..32 px
    // eye, so 0 is the middle (20 px) and 100 is the largest. 45 was only a
    // few pixels above resting -- not readable as surprise on a 2in panel.
    switch (face_index) {
        case 4:  return 100;   // surprised -- eyes as wide as they go
        case 5:  return -25;   // embarrassed -- slightly smaller, with Sleepy lids
        default: return 0;
    }
}'''
LIVE = path.parent / "avatar_live.cc"
lt = LIVE.read_text()
if old_size in lt:
    LIVE.write_text(lt.replace(old_size, new_size, 1))
    print("avatar_live.cc: eye size per face widened")
elif 'case 4:  return 100;' in lt:
    print("avatar_live.cc: already patched")
else:
    print("WARNING: eye-size anchor not found in avatar_live.cc; skipped")

if stackchan_done:
    print("stackchan.cc: already patched")
else:
    path.write_text(text)
    print("stackchan.cc: per-face mouth weight and surprised eye override added")
PYEOF
