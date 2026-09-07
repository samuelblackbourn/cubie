#!/usr/bin/env bash
# Fix: eyes were rendering shut.
#
# Two bugs, both in RenderLiveAvatarLocked():
#
# 1. Weight direction inverted. DefaultEyes::setWeight slides a black eyelid
#    square off the eye:  _eyelid_offset_y = -map_range(weight, 0,100, 0,height)
#    So weight 100 = eyelid fully clear = OPEN, weight 0 = eyelid covering =
#    SHUT. We passed 0 for "open", closing both eyes.
#
# 2. Unconditional override. setEmotion sets the eye weight itself -- Neutral
#    100, Happy 72, Doubt 75, Sad 70, Sleepy 35 -- and rotates the eyes for
#    character (Happy 1550, Sad -400). Overriding the weight on every render
#    flattened all six expressions to one eyelid position. The eye axis should
#    only win while it is the active layer, i.e. mid-blink.
set -euo pipefail

SC="${1:-$HOME/stackchan-mcp/firmware}/main/boards/stackchan/stackchan.cc"
[ -f "$SC" ] || { echo "ERROR: $SC not found"; exit 1; }

python3 - "$SC" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()

old = '''        static constexpr int kEyeWeight[3]   = {0, 50, 100};   // open, half, closed
        static constexpr int kMouthWeight[5] = {0, 45, 100, 70, 35};  // closed, half, open, e, u

        const int e = (current_eyes_index_  >= 0 && current_eyes_index_  < 3) ? current_eyes_index_  : 0;
        const int m = (current_mouth_index_ >= 0 && current_mouth_index_ < 5) ? current_mouth_index_ : 0;
        live_avatar_->SetEyeWeight(kEyeWeight[e]);
        live_avatar_->SetMouthWeight(kMouthWeight[m]);'''

new = '''        // DefaultEyes::setWeight slides a black eyelid square off the eye:
        //     _eyelid_offset_y = -map_range(weight, 0, 100, 0, eyelid height)
        // so 100 leaves the eye fully exposed and 0 leaves it fully covered.
        // Open is 100, shut is 0 -- the opposite of the first guess here.
        static constexpr int kEyeWeight[3]   = {100, 55, 0};   // open, half, closed
        static constexpr int kMouthWeight[5] = {0, 45, 100, 70, 35};  // closed, half, open, e, u

        const int e = (current_eyes_index_  >= 0 && current_eyes_index_  < 3) ? current_eyes_index_  : 0;
        const int m = (current_mouth_index_ >= 0 && current_mouth_index_ < 5) ? current_mouth_index_ : 0;

        // Only drive the eyelids while the eye axis is the active layer (a
        // blink in progress). At rest, SetFace() above has just applied the
        // emotion, and DefaultEyes::setEmotion sets its own eye weight and
        // rotation per expression -- Neutral 100, Happy 72 rotated 1550,
        // Doubt 75, Sad 70 rotated -400, Sleepy 35. Overriding unconditionally
        // would flatten all six to a single eyelid position and lose the
        // rotation that gives each expression its character.
        if (active_layer_ == ActiveLayer::EYES) {
            live_avatar_->SetEyeWeight(kEyeWeight[e]);
        }
        live_avatar_->SetMouthWeight(kMouthWeight[m]);'''

# Detect "already applied" by a marker that SURVIVES the rest of the chain,
# not by the whole replacement. fix-expression-depth.sh runs after this and
# rewrites the tail of the block written here, so `new in text` is False on an
# already-patched tree -- and with the original anchor long gone too, this
# aborted on every re-run. That made the whole chain non-idempotent, which is
# what the build timer depends on.
#
# The corrected weights are the actual change this script makes, and nothing
# downstream touches that line.
APPLIED = 'kEyeWeight[3]   = {100, 55, 0}'

if APPLIED in text:
    print("already fixed")
elif old not in text:
    sys.exit("ABORTING -- anchor not found; was step 2 applied?")
else:
    path.write_text(text.replace(old, new, 1))
    if APPLIED not in path.read_text():
        # The marker must appear in what we just wrote, or the next run will
        # abort exactly as this one would have.
        sys.exit("ABORTING -- wrote the fix but the idempotency marker is absent")
    print("stackchan.cc: eye weight direction and emotion override fixed")
PYEOF
