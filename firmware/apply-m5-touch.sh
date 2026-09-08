#!/usr/bin/env bash
# Touch, done the way M5's own firmware does it.
#
# Their factory firmware detects stroking reliably. Ours did not, through two
# attempts. Reading m5stack/StackChan's hal_head_touch.cpp explains why: they
# are not measuring the same thing we were.
#
# --- What M5 actually does ---
#
# The Si12T reports a 2-bit LEVEL per pad in one register byte -- 0..3 each for
# CH1/CH2/CH3. M5 keeps those levels and computes a weighted centroid:
#
#     position = (i0*(-100) + i1*0 + i2*100) / (i0 + i1 + i2)      // -100..+100
#
# That is WHERE ON THE HEAD the finger is, continuously. A gesture is then the
# change in that position since touch-down:
#
#     delta > +40  -> SwipeForward
#     delta < -40  -> SwipeBackward
#
# Duration appears nowhere in their code. Nor do zones.
#
# --- Why ours failed ---
#
# Upstream reduces the same byte to three booleans with `!= 0`, discarding the
# magnitude -- so the position information is thrown away before anyone can
# use it. Classification then has only hold time to work with, and hold time
# cannot separate a tap from a stroke on this hardware: release confirmation is
# 4 samples x 100 ms, so the shortest measurable press is ~400 ms and real
# strokes measured 399-600 ms.
#
# That 400 ms release debounce exists, by its own comment, to "bridge
# finger-glide gaps that otherwise cut a stroke short" -- a workaround for
# having discarded the signal that would have made it unnecessary. My previous
# fix (counting distinct zones touched) was reconstructing a 3-level
# approximation of M5's continuous position from the bools that survived.
#
# --- What this changes ---
#
#   1. Keep the levels. `TouchState` gains level[3] alongside zone[3].
#   2. Compute M5's centroid position.
#   3. M5's state machine: IDLE -> TOUCHED -> SWIPING, with the swipe firing
#      WHILE THE FINGER IS STILL MOVING rather than on release. That is why
#      theirs feels immediate.
#   4. Poll at 50 ms, as M5 does -- twice the rate, so a swipe is caught
#      mid-gesture.
#   5. Symmetric 2-sample debounce. The long release confirm was only there to
#      protect a duration measurement that no longer decides anything.
#
# A stroke's DIRECTION is now known, which upstream never had. Both directions
# map to the same reaction for now; telling front-to-back from back-to-front is
# left for the behaviour layer to use.
#
# Idempotent, anchored on exact text, aborts rather than guessing.
set -euo pipefail

FW="${1:-$HOME/stackchan-mcp/firmware}"
SC="$FW/main/boards/stackchan/stackchan.cc"
[ -f "$SC" ] || { echo "ERROR: $SC not found"; exit 1; }

python3 - "$SC" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()

if 'TouchPosition' in t:
    print("stackchan.cc already carries the M5 touch model")
    sys.exit(0)

edits = []

# ---------------------------------------------------- 1. keep the levels --
edits.append((
'''    struct TouchState {
        bool zone[3];          // CH1, CH2, CH3 — true if any output level set
        uint8_t output1_raw;   // raw Output1 register byte (0x10)
        bool ok;               // false if the I2C read failed
    };''',
'''    struct TouchState {
        bool zone[3];          // CH1, CH2, CH3 — true if any output level set
        uint8_t level[3];      // CH1..CH3 raw 2-bit level, 0..3
        uint8_t output1_raw;   // raw Output1 register byte (0x10)
        bool ok;               // false if the I2C read failed
    };'''))

edits.append((
'''        s.ok = true;
        // Each channel uses 2 bits; nonzero = touched at some level.
        s.zone[0] = ((s.output1_raw >> 0) & 0x3) != 0;  // CH1
        s.zone[1] = ((s.output1_raw >> 2) & 0x3) != 0;  // CH2
        s.zone[2] = ((s.output1_raw >> 4) & 0x3) != 0;  // CH3
        return s;''',
'''        s.ok = true;
        // Each channel uses 2 bits. Keep the LEVEL, not just whether it is
        // nonzero: the magnitudes across the three pads are what give a
        // continuous finger position, and `!= 0` throws that away. M5's own
        // driver parses the identical byte the identical way
        // (si12t_parse_touch_result_to: (result >> j) & 0x03 for j = 0,2,4).
        s.level[0] = (uint8_t)((s.output1_raw >> 0) & 0x3);  // CH1
        s.level[1] = (uint8_t)((s.output1_raw >> 2) & 0x3);  // CH2
        s.level[2] = (uint8_t)((s.output1_raw >> 4) & 0x3);  // CH3
        s.zone[0] = s.level[0] != 0;
        s.zone[1] = s.level[1] != 0;
        s.zone[2] = s.level[2] != 0;
        return s;'''))

# ------------------------------------------------ 2. constants and state --
edits.append((
'''    static constexpr int TOUCH_POLL_MS    = 100;  // 100 Hz polling''',
'''    // 50 ms, matching M5's head-touch task. A swipe is detected from the
    // change in finger position while the finger is still moving, so the
    // sample rate sets how much of the gesture is seen before it ends.
    static constexpr int TOUCH_POLL_MS    = 50;

    // Swipe threshold in M5's position units (-100..+100). Theirs is 40.
    static constexpr int SWIPE_THRESHOLD  = 40;

    // Weighted centroid of the three pad levels: -100 at CH1, 0 at CH2,
    // +100 at CH3. This is M5's get_position(), and it is the whole reason
    // their stroke detection works where duration-based attempts did not.
    static int TouchPosition(const uint8_t level[3]) {
        const int total = level[0] + level[1] + level[2];
        if (total == 0) return 0;
        const int weighted = level[0] * (-100) + level[1] * 0 + level[2] * 100;
        return weighted / total;
    }'''))

edits.append((
'''    uint8_t    press_start_output1_raw_ = 0;''',
'''    uint8_t    press_start_output1_raw_ = 0;

    // M5's gesture state. `swipe_fired_` is what makes a tap a tap: a press
    // that ends without a swipe having fired is a tap, so the two gestures
    // cannot both trigger and neither depends on hold time.
    int  press_start_position_ = 0;
    bool swipe_fired_ = false;'''))

# ------------------------------------------- 3. symmetric debounce --
edits.append((
'''        const int needed = touch_pressed_pending_ ? 2 : 4;''',
'''        // Symmetric now. The 4-sample release confirm existed to bridge
        // finger-glide gaps that would cut a duration measurement short --
        // and duration no longer classifies anything, so the protection is
        // no longer needed. It was also what put a 400 ms floor under every
        // press and made the TAP branch unreachable.
        const int needed = 2;'''))

# --------------------------------- 4. swipe detection during the press --
edits.append((
'''        bool any_pressed = s.zone[0] || s.zone[1] || s.zone[2];

        // Accumulate before debouncing, not on edges: a glide's second zone
        // often appears mid-press and would be invisible to an edge-only view.
        if (any_pressed) {
            press_zone_mask_ |= (uint8_t)((s.zone[0] ? 1 : 0) | (s.zone[1] ? 2 : 0) |
                                          (s.zone[2] ? 4 : 0));
        }''',
'''        bool any_pressed = s.zone[0] || s.zone[1] || s.zone[2];

        // Accumulate before debouncing, not on edges: a glide's second zone
        // often appears mid-press and would be invisible to an edge-only view.
        if (any_pressed) {
            press_zone_mask_ |= (uint8_t)((s.zone[0] ? 1 : 0) | (s.zone[1] ? 2 : 0) |
                                          (s.zone[2] ? 4 : 0));
        }

        // Swipe detection runs on EVERY sample while pressed, before the
        // debounce and before any edge handling -- this is M5's TOUCHED ->
        // SWIPING transition. Firing mid-gesture rather than on release is
        // what makes the reaction feel immediate, and it means a stroke does
        // not need to survive the debounce to be seen.
        if (any_pressed && touch_pressed_prev_ && !swipe_fired_) {
            const int position = TouchPosition(s.level);
            const int delta = position - press_start_position_;
            if (delta > SWIPE_THRESHOLD || delta < -SWIPE_THRESHOLD) {
                swipe_fired_ = true;
                ESP_LOGI(TAG,
                         "touch swipe %s: start=%d now=%d delta=%d levels=%u/%u/%u",
                         delta > 0 ? "FORWARD" : "BACKWARD",
                         press_start_position_, position, delta,
                         s.level[0], s.level[1], s.level[2]);
                HandleStroke(0);
            }
        }'''))

# ------------------------------------- 5. record position on touch-down --
edits.append((
'''            press_start_output1_raw_ = s.output1_raw;''',
'''            press_start_output1_raw_ = s.output1_raw;
            // Where the finger landed. Every swipe is measured against this.
            press_start_position_ = TouchPosition(s.level);
            swipe_fired_ = false;'''))

# --------------------------------------- 6. retire the zone-travel code --
# ZoneCount is dead once position decides the gesture, and the comment above
# press_zone_mask_ describes a discriminator that is no longer the
# discriminator. Dead code that reads as load-bearing is worse than none:
# the next person to touch this would believe zone travel still classifies.
# The mask itself stays -- it is logged, and it is genuinely useful when
# diagnosing what the pads saw.
edits.append((
"""    // Bitmask of head zones touched at any point during the current press.
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
""",
"""    // Bitmask of head zones touched at any point during the current press.
    // Bit 0/1/2 = zone 0/1/2. Reset on the rising edge, OR-ed every poll.
    //
    // Diagnostic only now. It was briefly the TAP/STROKE discriminator, as a
    // 3-level approximation of the continuous finger position that M5's
    // driver computes from the pad LEVELS -- levels this firmware was
    // discarding with `!= 0`. Now that TouchPosition() exists, the mask is
    // kept because it is informative in the release log, not because
    // anything decides on it.
    uint8_t press_zone_mask_ = 0;
"""))

# ------------------------------------------------------ 7. classify --
edits.append((
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
            }''',
'''            // A swipe already fired during the press, so a release only ever
            // has to decide "was that a tap?". Duration is logged for
            // diagnosis and consulted for nothing: it cannot separate the
            // gestures on this hardware, which is what two previous attempts
            // established the hard way.
            ESP_LOGI(TAG,
                     "touch release: duration=%u ms zones_seen=0x%X swiped=%d -> %s",
                     (unsigned)duration_ms, press_zone_mask_, swipe_fired_ ? 1 : 0,
                     swipe_fired_ ? "already STROKE" : "TAP");
            if (!swipe_fired_) {
                HandleTap(duration_ms);
            }
            swipe_fired_ = false;'''))

for old, new in edits:
    if t.count(old) != 1:
        sys.exit(f"ABORTING -- anchor x{t.count(old)}: {old.splitlines()[0][:70]}")
    t = t.replace(old, new, 1)

p.write_text(t)
print("stackchan.cc: touch now uses M5's position-based gesture model")
PYEOF

echo
echo "Done. Rebuild and publish -- this goes out over OTA."
