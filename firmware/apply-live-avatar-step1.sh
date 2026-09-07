#!/usr/bin/env bash
# Step 1 of vendoring M5Stack's live avatar into stackchan-mcp.
#
# This step adds files and build wiring ONLY. Nothing in stackchan.cc changes
# and nothing calls the new code yet. The point is to prove M5's module
# compiles inside this tree -- against ESP-IDF v5.5.2 and our own
# smooth_ui_toolkit -- before any integration exists to confuse the diagnosis.
#
# Idempotent: safe to re-run.
set -euo pipefail

FW="${1:-$HOME/stackchan-mcp/firmware}"
BOARD="$FW/main/boards/stackchan"
M5="$BOARD/m5avatar"

[ -d "$M5/avatar" ] || { echo "ERROR: $M5/avatar missing -- run the cp -r step first"; exit 1; }
[ -f "$FW/main/CMakeLists.txt" ] || { echo "ERROR: not a firmware tree: $FW"; exit 1; }

# ---------------------------------------------------------------- HAL shim --
# M5's five decorators call GetHAL().millis() and nothing else from their app
# framework. Rather than patch their sources -- which would make every future
# re-sync a merge -- satisfy the include with a shim on our own include path.
# M5's files stay byte-identical to upstream, so re-syncing is a plain cp -r.
mkdir -p "$M5/hal"
cat > "$M5/hal/hal.h" <<'EOF'
/*
 * Shim for vendored M5Stack avatar sources. NOT M5 code.
 *
 * m5stack/StackChan's decorators include <hal/hal.h> and use exactly one
 * thing from it: GetHAL().millis(). This provides that and nothing else, so
 * the vendored files under m5avatar/avatar/ compile unmodified and stay a
 * clean copy of upstream.
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <cstdint>
#include <esp_timer.h>

namespace m5avatar_shim {

class Hal {
public:
    // Milliseconds since boot. M5's decorators only ever diff two of these,
    // so wrap-around at ~49 days is harmless.
    uint32_t millis() const {
        return static_cast<uint32_t>(esp_timer_get_time() / 1000);
    }
};

inline Hal& instance() {
    static Hal hal;
    return hal;
}

}  // namespace m5avatar_shim

inline m5avatar_shim::Hal& GetHAL() { return m5avatar_shim::instance(); }
EOF

# --------------------------------------------------------- vendor provenance --
cat > "$M5/VENDORED.md" <<'EOF'
# Vendored: M5Stack StackChan avatar

`avatar/` is copied **unmodified** from M5Stack's official StackChan firmware.
`utils/object_pool.h` is copied for `ObjectPool<Decorator>`, which
`avatar/avatar/decorator.h` includes as `../../utils/object_pool.h` -- the
directory layout here preserves that relative path so no edit is needed.

| | |
| --- | --- |
| Upstream | https://github.com/m5stack/StackChan |
| Path | `firmware/main/stackchan/avatar/` |
| Commit | `1b5765599fba8aaad1811d9a79358ccc7051f5f3` |
| Licence | MIT, Copyright (c) 2026 M5Stack Technology CO LTD |

Every vendored file carries its own `SPDX-FileCopyrightText` and
`SPDX-License-Identifier: MIT` header. Do not remove them.

## Attribution

StackChan was created by **Shinya Ishikawa** (2021). The official project org is
`stack-chan/stack-chan` (Apache-2.0) -- not a personal fork. The Arduino servo
library is `stack-chan/stackchan-arduino` (MIT), maintained by **Takao Akaki**.
"StackChan" / "スタックチャン" is a registered trademark of Shinya Ishikawa,
defensively registered to protect the open-source project.

## Why vendored rather than a submodule

M5's tree is an ESP-IDF application, not a component: `avatar/` sits inside
their `main/` alongside their HAL, app framework (mooncake) and their own
xiaozhi-esp32 patch. Taking it as a submodule would drag in a second app
framework. It is ~1,700 lines of self-contained LVGL, so a copy with recorded
provenance is the smaller and more honest dependency.

## Re-syncing

Upstream files are unmodified, so:

    cp -r <m5-clone>/firmware/main/stackchan/avatar  m5avatar/avatar
    cp <m5-clone>/firmware/main/stackchan/utils/object_pool.h  m5avatar/utils/

then update the commit above. `hal/` is ours and must not be overwritten.

## The one external dependency

`smooth_ui_toolkit` (`uitk::lvgl_cpp`). M5 pins **v2.12.0**; this repo's
submodule is also **v2.12.0**, so the API matches. Check this first if the
vendored code stops compiling after a submodule bump.
EOF

# ------------------------------------------------------------- mapping layer --
cat > "$BOARD/avatar_live.h" <<'EOF'
/*
 * Live avatar: M5Stack's vector face, driven by this board's existing state.
 *
 * SPDX-License-Identifier: MIT
 *
 * The frame-based path (avatar_images.* / AvatarSet) shows one of N static
 * images. This renders instead, so blink and mouth movement interpolate
 * continuously rather than stepping between pictures.
 *
 * This header deliberately exposes no LVGL or M5 types: the board talks to it
 * in the vocabulary it already has -- a face index, an eye weight, a mouth
 * weight -- so stackchan.cc does not gain a dependency on the vendored tree.
 */
#pragma once

#include <memory>

struct _lv_obj_t;
struct _lv_font_t;

namespace stackchan_live {

// Face indices match the board's existing order, which is also AvatarSet's:
//   0 idle  1 happy  2 thinking  3 sad  4 surprised  5 embarrassed
inline constexpr int kFaceCount = 6;

class LiveAvatar {
public:
    LiveAvatar();
    ~LiveAvatar();

    // Build the avatar on `parent`. Returns false if it could not be created.
    // `font` may be null: the speech bubble is then left unstyled.
    bool Init(_lv_obj_t* parent, const _lv_font_t* font);
    bool ready() const { return ready_; }

    // The root object, for lv_obj_move_foreground() and friends.
    _lv_obj_t* root() const;

    void SetFace(int face_index);

    // 0..100. Direction is asserted on hardware, not guessed -- see kEyeWeightOpen.
    void SetEyeWeight(int weight);
    void SetMouthWeight(int weight);

    void SetSpeech(const char* text);
    void ClearSpeech();

    void SetVisible(bool visible);

    // Drive animation. Called from an LVGL timer; must run on the LVGL task.
    void Update();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    bool ready_ = false;
};

}  // namespace stackchan_live
EOF

cat > "$BOARD/avatar_live.cc" <<'EOF'
/*
 * SPDX-License-Identifier: MIT
 *
 * Bridges this board's avatar state onto M5Stack's stackchan::avatar API.
 * See m5avatar/VENDORED.md for provenance and attribution.
 */
#include "avatar_live.h"

#include "m5avatar/avatar/avatar.h"
#include "m5avatar/avatar/skins/default/default.h"

#include <esp_log.h>
#include <lvgl.h>

namespace stackchan_live {

namespace {
constexpr const char* TAG = "LiveAvatar";

using stackchan::avatar::Emotion;

// Our six face names onto M5's six emotions. Two do not map one-to-one:
//
//   surprised   -- M5 has no Surprised. Doubt plus enlarged eyes reads as it,
//                  and matches what the frame-based `surprised` showed.
//   embarrassed -- Sleepy is the closest emotion; the `shy` decorator (blush)
//                  is the truer match and is added once decorator wiring lands.
//
// M5's Angry has no counterpart on our side yet -- it is reachable only once
// the office has something that should make Cubie cross.
Emotion EmotionForFace(int face_index) {
    switch (face_index) {
        case 0: return Emotion::Neutral;   // idle
        case 1: return Emotion::Happy;     // happy
        case 2: return Emotion::Doubt;     // thinking
        case 3: return Emotion::Sad;       // sad
        case 4: return Emotion::Doubt;     // surprised (+ eye size below)
        case 5: return Emotion::Sleepy;    // embarrassed
        default: return Emotion::Neutral;
    }
}

// Feature::setSize is -100..100, 0 being normal. Only `surprised` deviates.
int EyeSizeForFace(int face_index) { return face_index == 4 ? 45 : 0; }
}  // namespace

struct LiveAvatar::Impl {
    stackchan::avatar::DefaultAvatar avatar;
};

LiveAvatar::LiveAvatar() : impl_(std::make_unique<Impl>()) {}
LiveAvatar::~LiveAvatar() = default;

bool LiveAvatar::Init(_lv_obj_t* parent, const _lv_font_t* font) {
    if (ready_) return true;
    if (parent == nullptr) return false;

    // M5's init takes a font by reference-or-default; passing our own keeps the
    // speech bubble consistent with the rest of this firmware's UI.
    impl_->avatar.init(reinterpret_cast<lv_obj_t*>(parent),
                       font != nullptr ? reinterpret_cast<const lv_font_t*>(font)
                                       : &lv_font_montserrat_16);
    if (impl_->avatar.getPanel() == nullptr) {
        ESP_LOGE(TAG, "DefaultAvatar::init produced no panel");
        return false;
    }
    ready_ = true;
    ESP_LOGI(TAG, "live avatar created (M5Stack DefaultAvatar, 320x240)");
    return true;
}

_lv_obj_t* LiveAvatar::root() const {
    if (!ready_) return nullptr;
    auto* panel = impl_->avatar.getPanel();
    return panel == nullptr ? nullptr : reinterpret_cast<_lv_obj_t*>(panel->get());
}

void LiveAvatar::SetFace(int face_index) {
    if (!ready_) return;
    if (face_index < 0 || face_index >= kFaceCount) return;
    impl_->avatar.setEmotion(EmotionForFace(face_index));
    const int size = EyeSizeForFace(face_index);
    impl_->avatar.leftEye().setSize(size);
    impl_->avatar.rightEye().setSize(size);
}

void LiveAvatar::SetEyeWeight(int weight) {
    if (!ready_) return;
    impl_->avatar.leftEye().setWeight(weight);
    impl_->avatar.rightEye().setWeight(weight);
}

void LiveAvatar::SetMouthWeight(int weight) {
    if (!ready_) return;
    impl_->avatar.mouth().setWeight(weight);
}

void LiveAvatar::SetSpeech(const char* text) {
    if (!ready_ || text == nullptr) return;
    impl_->avatar.setSpeech(text);
}

void LiveAvatar::ClearSpeech() {
    if (!ready_) return;
    impl_->avatar.clearSpeech();
}

void LiveAvatar::SetVisible(bool visible) {
    auto* obj = reinterpret_cast<lv_obj_t*>(root());
    if (obj == nullptr) return;
    if (visible) {
        lv_obj_clear_flag(obj, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
    }
}

void LiveAvatar::Update() {
    if (!ready_) return;
    impl_->avatar.update();
}

}  // namespace stackchan_live
EOF

# ------------------------------------------------------------------- CMake --
CM="$FW/main/CMakeLists.txt"
if grep -q 'STACKCHAN_M5AVATAR_DIR' "$CM"; then
    echo "CMakeLists.txt already patched, leaving it alone"
else
    python3 - "$CM" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()

# Anchor on the FeetechScs block's closing endif() inside the stackchan block,
# so our sources are appended in the same place and the same way.
anchor = '''        message(STATUS "StackChan: using MIT FeetechScs driver, excluding SCServo_lib sources from build")
    endif()
endif()'''
if anchor not in text:
    sys.exit("ANCHOR NOT FOUND in CMakeLists.txt -- refusing to patch blindly")

block = '''        message(STATUS "StackChan: using MIT FeetechScs driver, excluding SCServo_lib sources from build")
    endif()

    # Vendored M5Stack live avatar (see boards/stackchan/m5avatar/VENDORED.md).
    # The BOARD_SOURCES glob above only picks up *.cc / *.c at the top of the
    # board directory, so M5's nested *.cpp sources are listed explicitly.
    # Inlined into main rather than registered as a component, for the same
    # MINIMAL_BUILD reason documented for feetech_scs above.
    set(STACKCHAN_M5AVATAR_DIR ${STACKCHAN_BOARD_DIR}/m5avatar)
    file(GLOB_RECURSE STACKCHAN_M5AVATAR_SOURCES
        ${STACKCHAN_M5AVATAR_DIR}/avatar/*.cpp
        ${STACKCHAN_M5AVATAR_DIR}/avatar/*.c)
    list(APPEND BOARD_SOURCES ${STACKCHAN_M5AVATAR_SOURCES})
    list(APPEND BOARD_SOURCES ${STACKCHAN_BOARD_DIR}/avatar_live.cc)
    # m5avatar/ on the include path resolves M5's <hal/hal.h> to our shim.
    list(APPEND INCLUDE_DIRS ${STACKCHAN_M5AVATAR_DIR})
    message(STATUS "StackChan: vendored M5Stack live avatar enabled")
endif()'''

path.write_text(text.replace(anchor, block, 1))
print("CMakeLists.txt patched")
PYEOF
fi

echo
echo "Files in place:"
find "$M5" -maxdepth 2 -name '*.h' -o -maxdepth 2 -name '*.md' | sort
ls -1 "$BOARD"/avatar_live.* 2>/dev/null
echo
echo "Next: rebuild. Nothing calls this code yet -- we are proving it compiles."
