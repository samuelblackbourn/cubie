#!/usr/bin/env bash
# Two firmware changes, one build, deployed by OTA.
#
# 1. `embarrassed` gets M5's shy decorator (the blush).
#
#    Mapping it to Emotion::Sleepy alone was always a compromise -- Sleepy is
#    heavy eyelids, not embarrassment. M5's own decorator set carries the blush
#    as a separate overlay, which is what the factory firmware showed. It is
#    added on entering `embarrassed` and removed on leaving, with the avatar's
#    own update() driving its animation through our 30 fps tick.
#
# 2. Touch is classified by ZONE TRAVEL, not just hold time.
#
#    Measured on this unit: taps report start_zones=100, strokes report 110 /
#    011 / 001. A stroke glides across the three head zones; a tap stays in one.
#    The "finger-glide between zones" that upstream's comment treats as noise --
#    the reason STROKE_MIN_MS was lowered to 400, which made TAP unreachable --
#    is actually the signal.
#
#    Duration alone cannot separate them here: release confirmation is 4 samples
#    x 100 ms, so the shortest measurable press is ~400 ms, and real strokes came
#    in at 399-600 ms. The two distributions overlap almost completely. Zones do
#    not overlap at all.
#
#    So: two or more distinct zones during a press => STROKE, whatever the
#    duration. Otherwise fall back to the duration test, which still catches a
#    long hold in a single zone. Both gestures become reachable.
set -euo pipefail

FW="${1:-$HOME/stackchan-mcp/firmware}"
BOARD="$FW/main/boards/stackchan"
SC="$BOARD/stackchan.cc"
AL="$BOARD/avatar_live.cc"
for f in "$SC" "$AL"; do [ -f "$f" ] || { echo "ERROR: $f not found"; exit 1; }; done

# ------------------------------------------------------- 1. shy decorator --
python3 - "$AL" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
if 'ShyDecorator' in t:
    print("avatar_live.cc already patched"); sys.exit(0)

edits = [
(
'''#include "m5avatar/avatar/skins/default/default.h"''',
'''#include "m5avatar/avatar/skins/default/default.h"
#include "m5avatar/avatar/decorators/decorators.h"'''),
(
'''struct LiveAvatar::Impl {
    stackchan::avatar::DefaultAvatar avatar;
};''',
'''struct LiveAvatar::Impl {
    stackchan::avatar::DefaultAvatar avatar;

    // Id of the blush overlay while `embarrassed` is showing, or -1.
    // Held rather than re-added per render: addDecorator() allocates, and
    // RenderAvatarLocked() runs on every blink step.
    int shy_id = -1;
};'''),
(
'''    impl_->avatar.setEmotion(EmotionForFace(face_index));
    const int size = EyeSizeForFace(face_index);
    impl_->avatar.leftEye().setSize(size);
    impl_->avatar.rightEye().setSize(size);
}''',
'''    impl_->avatar.setEmotion(EmotionForFace(face_index));
    const int size = EyeSizeForFace(face_index);
    impl_->avatar.leftEye().setSize(size);
    impl_->avatar.rightEye().setSize(size);

    // `embarrassed` wears M5's blush on top of the expression. Sleepy alone is
    // heavy eyelids, which reads as tired rather than embarrassed; the
    // decorator is what the factory firmware actually showed.
    //
    // destroyAfterMs = 0 means no self-destruct: this is a state, not a
    // reaction, so we own its lifetime.
    //
    // Two arguments, not three: ShyDecorator takes no animationIntervalMs
    // because it does not animate -- it is two static blush images, left and
    // right, unlike Heart/Angry/Sweat/Dizzy which cycle frames.
    const bool want_shy = (face_index == 5);
    auto* panel = impl_->avatar.getPanel();
    if (want_shy && impl_->shy_id < 0 && panel != nullptr) {
        impl_->shy_id = impl_->avatar.addDecorator(
            std::make_unique<stackchan::avatar::ShyDecorator>(panel->get(), 0));
        ESP_LOGI(TAG, "shy decorator added (id=%d)", impl_->shy_id);
    } else if (!want_shy && impl_->shy_id >= 0) {
        impl_->avatar.removeDecorator(impl_->shy_id);
        impl_->shy_id = -1;
    }
}'''),
]
for old, new in edits:
    if t.count(old) != 1:
        sys.exit(f"ABORTING -- anchor x{t.count(old)}: {old.splitlines()[0][:60]}")
    t = t.replace(old, new, 1)
p.write_text(t)
print("avatar_live.cc: shy decorator wired to `embarrassed`")
PYEOF

# ------------------------------------------- 2. zone-travel classification --
python3 - "$SC" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
if 'press_zone_mask_' in t:
    print("stackchan.cc already patched"); sys.exit(0)

edits = [
# member
(
'''    void TouchPollTick() {''',
'''    // Bitmask of head zones touched at any point during the current press.
    // Bit 0/1/2 = zone 0/1/2. Reset on the rising edge, OR-ed every poll.
    //
    // This is the primary TAP/STROKE discriminator. Hold duration cannot
    // separate them on this hardware: release confirmation is 4 samples x
    // TOUCH_POLL_MS, so the shortest measurable press is ~400 ms, and measured
    // strokes ran 399-600 ms -- the distributions overlap almost entirely.
    // Zone travel does not overlap: a stroke crosses zones, a tap does not.
    uint8_t press_zone_mask_ = 0;

    static inline int ZoneCount(uint8_t mask) {
        return (mask & 1) + ((mask >> 1) & 1) + ((mask >> 2) & 1);
    }

    void TouchPollTick() {'''),
# accumulate every poll while pressed
(
'''        bool any_pressed = s.zone[0] || s.zone[1] || s.zone[2];''',
'''        bool any_pressed = s.zone[0] || s.zone[1] || s.zone[2];

        // Accumulate before debouncing, not on edges: a glide's second zone
        // often appears mid-press and would be invisible to an edge-only view.
        if (any_pressed) {
            press_zone_mask_ |= (uint8_t)((s.zone[0] ? 1 : 0) | (s.zone[1] ? 2 : 0) |
                                          (s.zone[2] ? 4 : 0));
        }'''),
# reset on rising edge
(
'''            press_start_output1_raw_ = s.output1_raw;''',
'''            press_start_output1_raw_ = s.output1_raw;
            // Start a fresh travel record. The zones seen on this very tick are
            // already in the mask from the accumulate above, so seed rather
            // than clear.
            press_zone_mask_ = (uint8_t)((s.zone[0] ? 1 : 0) | (s.zone[1] ? 2 : 0) |
                                         (s.zone[2] ? 4 : 0));'''),
# classify
(
'''            if (duration_ms >= STROKE_MIN_MS) {
                HandleStroke(duration_ms);
            } else {
                // Treat the 400-600 ms grey zone as TAP.
                HandleTap(duration_ms);
            }''',
'''            const int zones = ZoneCount(press_zone_mask_);
            const bool travelled = zones >= 2;
            ESP_LOGI(TAG, "touch classify: zones_seen=0x%X (%d distinct) duration=%u ms -> %s",
                     press_zone_mask_, zones, (unsigned)duration_ms,
                     (travelled || duration_ms >= STROKE_MIN_MS) ? "STROKE" : "TAP");
            if (travelled) {
                // A finger that crossed zones was moving. Duration is not
                // consulted: a fast stroke is still a stroke.
                HandleStroke(duration_ms);
            } else if (duration_ms >= STROKE_MIN_MS) {
                // One zone, but held. Still a deliberate hold rather than a tap.
                HandleStroke(duration_ms);
            } else {
                HandleTap(duration_ms);
            }'''),
]
for old, new in edits:
    if t.count(old) != 1:
        sys.exit(f"ABORTING -- anchor x{t.count(old)}: {old.splitlines()[0][:60]}")
    t = t.replace(old, new, 1)
p.write_text(t)
print("stackchan.cc: touch classified by zone travel, duration as fallback")
PYEOF

echo
echo "Done. Rebuild, then publish -- this one goes out over OTA."
