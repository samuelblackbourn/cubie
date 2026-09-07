#!/usr/bin/env bash
# Step 2: wire the vendored M5Stack avatar into the board.
#
# Step 1 proved the module compiles. This makes it the thing on screen.
#
# The whole integration is one interception. Every existing caller -- set_avatar,
# the four-step blink state machine, the lip-sync task, the touch reactions --
# already updates current_face_index_ / current_eyes_index_ / current_mouth_index_
# and then calls RenderAvatarLocked(). So we translate those indices onto M5's
# continuous API at that single point, and everything upstream keeps working
# unchanged -- and gains interpolation, because M5's elements animate toward a
# target instead of snapping to a frame.
#
# Every edit is anchored on exact text and ABORTS if the anchor is missing,
# rather than patching a 7,600-line file blindly. Idempotent.
set -euo pipefail

FW="${1:-$HOME/stackchan-mcp/firmware}"
BOARD="$FW/main/boards/stackchan"
SC="$BOARD/stackchan.cc"

[ -f "$SC" ] || { echo "ERROR: $SC not found"; exit 1; }
[ -f "$BOARD/avatar_live.h" ] || { echo "ERROR: run step 1 first"; exit 1; }

cp -n "$SC" "$SC.orig" 2>/dev/null || true   # keep one pristine copy

# --------------------------------------------------------------- Kconfig --
KC=$(grep -rl 'STACKCHAN_SERVO_FEETECH' "$FW/main"/Kconfig* 2>/dev/null | head -1 || true)
if [ -n "$KC" ] && ! grep -q 'STACKCHAN_LIVE_AVATAR' "$KC"; then
cat >> "$KC" <<'EOF'

config STACKCHAN_LIVE_AVATAR
    bool "StackChan: live vector avatar (vendored M5Stack renderer)"
    depends on BOARD_TYPE_STACKCHAN
    default y
    help
      Draw the face with M5Stack's vector avatar (boards/stackchan/m5avatar/,
      MIT -- see VENDORED.md) instead of the static frame tables in
      avatar_images.* / AvatarSet.

      The frame path shows one of N fixed pictures. The vector path is drawn
      from state each tick, so blinking and mouth movement interpolate rather
      than stepping, and expressions come from M5's own Emotion set -- the same
      face the factory firmware showed.

      Turn this off to fall back to the frame path, which stays fully intact.
      Note the frame path needs avatar_images.local.cc to be generated, or it
      renders a 2x2 placeholder with the chat UI showing through.
EOF
echo "Kconfig: added STACKCHAN_LIVE_AVATAR to $(basename "$KC")"
elif [ -n "$KC" ]; then
echo "Kconfig: STACKCHAN_LIVE_AVATAR already present"
else
echo "WARNING: no Kconfig defining STACKCHAN_SERVO_FEETECH found -- skipping gate"
fi

# ------------------------------------------------------------ stackchan.cc --
python3 - "$SC" <<'PYEOF'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
text = path.read_text()

if 'avatar_live.h' in text:
    print("stackchan.cc already patched, leaving it alone")
    sys.exit(0)

edits = []

# 1 -- include
edits.append((
'''#include "avatar_images.h"
#include "avatar_set.h"
#include "avatar_set_fetcher.h"''',
'''#include "avatar_images.h"
#include "avatar_set.h"
#include "avatar_set_fetcher.h"
#include "avatar_live.h"
#include <memory>'''))

# 2 -- members
edits.append((
'''    lv_obj_t* avatar_img_ = nullptr;
    esp_timer_handle_t avatar_init_timer_ = nullptr;''',
'''    lv_obj_t* avatar_img_ = nullptr;
    esp_timer_handle_t avatar_init_timer_ = nullptr;

    // Live vector avatar. When CONFIG_STACKCHAN_LIVE_AVATAR is set this
    // replaces avatar_img_ outright: avatar_img_ stays nullptr for the life of
    // the board and every render path routes through live_avatar_ instead.
    std::unique_ptr<stackchan_live::LiveAvatar> live_avatar_;
    esp_timer_handle_t live_avatar_tick_timer_ = nullptr;'''))

# 3 -- render interception
edits.append((
'''    bool RenderAvatarLocked() {
        const lv_image_dsc_t* dsc = nullptr;''',
'''    bool RenderAvatarLocked() {
#if CONFIG_STACKCHAN_LIVE_AVATAR
        if (!EnsureAvatarObject()) return false;
        RenderLiveAvatarLocked();
        BringListeningIndicatorToFrontLocked();
        return true;
#else
        const lv_image_dsc_t* dsc = nullptr;'''))

# close the #else opened above, at the end of RenderAvatarLocked
edits.append((
'''        lv_image_set_src(avatar_img_, dsc);
        lv_obj_move_foreground(avatar_img_);
        BringListeningIndicatorToFrontLocked();
        return true;
    }''',
'''        lv_image_set_src(avatar_img_, dsc);
        lv_obj_move_foreground(avatar_img_);
        BringListeningIndicatorToFrontLocked();
        return true;
#endif
    }'''))

# 4 -- helpers, placed just before EnsureAvatarObject
edits.append((
'''    // Create avatar_img_ on the active LVGL screen, scaled to fill the LCD.''',
'''#if CONFIG_STACKCHAN_LIVE_AVATAR
    // Translate this board's frame-index state onto the live avatar's
    // continuous API. This is the whole integration: every caller above
    // already sets these indices and calls RenderAvatarLocked().
    void RenderLiveAvatarLocked() {
        if (!live_avatar_ || !live_avatar_->ready()) return;

        live_avatar_->SetFace(current_face_index_);

        // Feature::setWeight is 0..100. The index tables below are this
        // board's existing eye/mouth axes.
        //
        // NOTE: the direction of the eye weight is asserted on hardware, not
        // assumed -- if blinking looks inverted (eyes shut at rest, open on
        // blink), reverse kEyeWeight and nothing else needs to change.
        static constexpr int kEyeWeight[3]   = {0, 50, 100};   // open, half, closed
        static constexpr int kMouthWeight[5] = {0, 45, 100, 70, 35};  // closed, half, open, e, u

        const int e = (current_eyes_index_  >= 0 && current_eyes_index_  < 3) ? current_eyes_index_  : 0;
        const int m = (current_mouth_index_ >= 0 && current_mouth_index_ < 5) ? current_mouth_index_ : 0;
        live_avatar_->SetEyeWeight(kEyeWeight[e]);
        live_avatar_->SetMouthWeight(kMouthWeight[m]);
    }

    // ~30 fps render tick. This is what turns the existing four-step blink
    // into a smooth one: the state machine still sets a target, but M5's
    // elements interpolate toward it between ticks instead of snapping.
    void StartLiveAvatarTick() {
        if (live_avatar_tick_timer_ != nullptr) return;
        esp_timer_create_args_t args = {
            .callback = [](void* arg) {
                StackChanBoard* board = static_cast<StackChanBoard*>(arg);
                DisplayLockGuard lock(board->display_);
                if (board->live_avatar_) {
                    board->live_avatar_->Update();
                }
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "avatar_tick",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&args, &live_avatar_tick_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(live_avatar_tick_timer_, 33 * 1000));
        ESP_LOGI(TAG, "Live avatar tick started (~30 fps)");
    }
#endif

    // Create avatar_img_ on the active LVGL screen, scaled to fill the LCD.'''))

# 5 -- creation
edits.append((
'''        lv_obj_t* screen = lv_screen_active();
        if (screen == nullptr) {
            return false;
        }
        avatar_img_ = lv_image_create(screen);''',
'''        lv_obj_t* screen = lv_screen_active();
        if (screen == nullptr) {
            return false;
        }
#if CONFIG_STACKCHAN_LIVE_AVATAR
        if (live_avatar_ && live_avatar_->ready()) {
            return true;
        }
        if (!live_avatar_) {
            live_avatar_ = std::make_unique<stackchan_live::LiveAvatar>();
        }
        // Font left null: LiveAvatar falls back to M5's default for the
        // speech bubble. DefaultAvatar::init builds its own 320x240 panel,
        // so no scaling or alignment is needed here.
        if (!live_avatar_->Init(reinterpret_cast<_lv_obj_t*>(screen), nullptr)) {
            ESP_LOGW(TAG, "LiveAvatar init failed (screen tree not ready?)");
            return false;
        }
        if (auto* root = live_avatar_->root()) {
            lv_obj_move_foreground(reinterpret_cast<lv_obj_t*>(root));
        }
        StartLiveAvatarTick();
        ESP_LOGI(TAG, "Live avatar installed in place of avatar_img_");
        return true;
#endif
        avatar_img_ = lv_image_create(screen);'''))

# 6 -- hide on "off"
edits.append((
'''            if (avatar_img_ != nullptr) {
                lv_obj_add_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
            }''',
'''            if (avatar_img_ != nullptr) {
                lv_obj_add_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
            }
#if CONFIG_STACKCHAN_LIVE_AVATAR
            if (live_avatar_) {
                live_avatar_->SetVisible(false);
            }
#endif'''))

# 7 -- show on resume
edits.append((
'''            if (avatar_img_ != nullptr) {
                // Restore visibility if a previous SetAvatarOff() hid the
                // layer. Cheap no-op when the flag is already clear.
                lv_obj_clear_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
            }''',
'''            if (avatar_img_ != nullptr) {
                // Restore visibility if a previous SetAvatarOff() hid the
                // layer. Cheap no-op when the flag is already clear.
                lv_obj_clear_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
            }
#if CONFIG_STACKCHAN_LIVE_AVATAR
            if (live_avatar_) {
                live_avatar_->SetVisible(true);
            }
#endif'''))

missing = [old for old, _ in edits if old not in text]
if missing:
    print("ABORTING -- these anchors were not found (upstream must have moved):", file=sys.stderr)
    for m in missing:
        print("  ---\n" + "\n".join("  " + l for l in m.splitlines()[:3]) + "\n  ...", file=sys.stderr)
    sys.exit(1)

for old, new in edits:
    if text.count(old) != 1:
        sys.exit(f"ABORTING -- anchor appears {text.count(old)} times, expected 1:\n{old[:120]}")
    text = text.replace(old, new, 1)

path.write_text(text)
print(f"stackchan.cc patched ({len(edits)} edits)")
PYEOF

echo
echo "Done. Rebuild, then flash app-only at 0x20000 (preserves NVS)."
