#!/usr/bin/env bash
# Two changes to the touch path.
#
# 1. Restore a reachable TAP window.
#
#    Upstream classifies on hold duration:  d >= STROKE_MIN_MS -> STROKE,
#    else TAP. STROKE_MIN_MS was lowered 600 -> 400 to stop finger-glide gaps
#    cutting strokes short. But release confirmation takes 4 samples at
#    TOUCH_POLL_MS=100, so the shortest duration the sensor can ever report is
#    ~400 ms -- exactly the threshold. TAP became unreachable: measured
#    durations for deliberately quick taps on this unit were 400, 600, 400 ms,
#    all classified STROKE.
#
#    The file's own comments still describe the 600 ms design, including a
#    "400 <= duration < 600 -> treated as TAP (greyzone)" line sitting directly
#    above code that cannot produce it. Restoring 600 makes the comments true
#    again and gives TAP the grey zone back.
#
#    Trade-off, accepted deliberately: a real stroke that measures under 600 ms
#    now reads as a TAP. An occasionally-misclassified gesture beats a branch
#    that can never fire.
#
# 2. Log the reaction outcome at INFO.
#
#    SetAvatarExpressionIfActive only logs at DEBUG, so there is currently no
#    way to tell from a serial capture whether a touch reaction reached the
#    face or was skipped. The servo wobble is loud and the eyelid change is
#    quiet, which makes "the face didn't change" unfalsifiable by eye.
set -euo pipefail

SC="${1:-$HOME/stackchan-mcp/firmware}/main/boards/stackchan/stackchan.cc"
[ -f "$SC" ] || { echo "ERROR: $SC not found"; exit 1; }

python3 - "$SC" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
edits = []

edits.append((
'''    static constexpr int STROKE_MIN_MS    = 400;  // was 600; lowered because''',
'''    // Restored to 600 (was briefly 400). At 400 the TAP branch was
    // unreachable: release confirmation is 4 samples x TOUCH_POLL_MS = 400 ms,
    // so no touch can measure below the threshold. Measured on hardware:
    // deliberately quick taps reported duration=400 ms and classified STROKE.
    // 600 also makes the comment block above true again, including its
    // "400 <= duration < 600 -> treated as TAP (greyzone)" line.
    static constexpr int STROKE_MIN_MS    = 600;  // originally 600; lowered to 400 because'''))

edits.append((
'''        SetAvatarExpressionIfActive("surprised");
        ScheduleIdleRevert();''',
'''        const bool face_ok = SetAvatarExpressionIfActive("surprised");
        ESP_LOGI(TAG, "TAP reaction: face=surprised applied=%d (current='%s')",
                 (int)face_ok, current_avatar_face_.c_str());
        ScheduleIdleRevert();'''))

edits.append((
'''        SetAvatarExpressionIfActive("embarrassed");
        StartServoWobble();''',
'''        const bool face_ok = SetAvatarExpressionIfActive("embarrassed");
        ESP_LOGI(TAG, "STROKE reaction: face=embarrassed applied=%d (current='%s')",
                 (int)face_ok, current_avatar_face_.c_str());
        StartServoWobble();'''))

if 'STROKE reaction: face=embarrassed' in text:
    print("already patched")
    sys.exit(0)

missing = [o for o, _ in edits if o not in text]
if missing:
    print("ABORTING -- anchors not found:", file=sys.stderr)
    for m in missing:
        print("  " + m.splitlines()[0], file=sys.stderr)
    sys.exit(1)

for old, new in edits:
    if text.count(old) != 1:
        sys.exit(f"ABORTING -- anchor appears {text.count(old)} times: {old.splitlines()[0]}")
    text = text.replace(old, new, 1)

path.write_text(text)
print("stackchan.cc: STROKE_MIN_MS restored to 600, reaction logging added")
PYEOF
