#!/usr/bin/env bash
# Give the face the two axes M5's renderer has and we never used: eye GAZE and
# feature ROTATION. Then use them to make `thinking` actually look like it.
#
# --- Why `thinking` read as flat ---
#
# It maps to Emotion::Doubt, whose only contribution is an eye weight of 75
# against idle's 100 -- a quarter of an eyelid. fix-expression-depth.sh added a
# resting mouth per face, which helped the other five, but a thinking face is
# not a mouth shape. It is where the eyes are LOOKING: up, and away from you.
#
# We had no way to say that, because nothing in this firmware ever called
# Element::setPosition. M5's own idle_expression.h and breath.h modifiers are
# built almost entirely on it.
#
# --- The axis is real, and worth this much travel ---
#
# From the vendored skin (m5avatar/avatar/skins/default/eyes.cpp):
#
#     static const Vector2i _eye_min_offset = Vector2i(-16, -16);
#     static const Vector2i _eye_max_offset = Vector2i( 16,  16);
#     pos_y = _eye_pos.y + map_range(_position.y, -100, 100, min.y, max.y);
#
# So the -100..100 that Element::setPosition takes maps onto +/-16 px of real
# travel, and it is an LVGL setPos, where +y is DOWN. Hence negative y is up.
# On eyes that are 8..32 px across, 16 px is a large and unambiguous shift --
# this is a proper gaze, not a nudge.
#
# --- Why the override has to live in the board, not in LiveAvatar ---
#
# RenderLiveAvatarLocked() calls live_avatar_->SetFace() on EVERY render, and a
# blink renders four times. So a per-face gaze applied inside SetFace() would be
# correct, but a host-set gaze stored there would be silently overwritten within
# five seconds by the next blink.
#
# It is kept as board state instead and applied in RenderLiveAvatarLocked(),
# which is exactly how the mouth already works: lip-sync wins while it is the
# active layer, the per-face resting value applies otherwise. A blink now
# replays the gaze rather than erasing it, and set_avatar clears the override --
# the same contract set_mouth documents ("held until the next set_avatar").
set -euo pipefail

FW="${1:-$HOME/stackchan-mcp/firmware}"
BOARD="$FW/main/boards/stackchan"
SC="$BOARD/stackchan.cc"
AH="$BOARD/avatar_live.h"
AC="$BOARD/avatar_live.cc"

for f in "$SC" "$AH" "$AC"; do
  [ -f "$f" ] || { echo "ERROR: $f not found (run apply-live-avatar-step1.sh first)"; exit 1; }
done

# ------------------------------------------------------------ avatar_live.h --
python3 - "$AH" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()

if "SetGaze" in text:
    print("avatar_live.h: gaze/rotation already declared")
    raise SystemExit(0)

old = '''    // 0..100. Direction is asserted on hardware, not guessed -- see kEyeWeightOpen.
    void SetEyeWeight(int weight);
    void SetMouthWeight(int weight);'''

new = '''    // 0..100. Direction is asserted on hardware, not guessed -- see kEyeWeightOpen.
    void SetEyeWeight(int weight);
    void SetMouthWeight(int weight);

    // Where the eyes are looking, as -100..100 on each axis. The skin maps
    // that onto +/-16 px of travel; +y is DOWN, so negative y looks up.
    // Both eyes move together: this is a gaze, not a squint.
    void SetGaze(int x, int y);

    // Mouth tilt in tenths of a degree, 0..3600 -- LVGL's own angle unit.
    // Express a negative angle the way M5's idle_expression.h does, as
    // 3600 + angle.
    void SetMouthRotation(int rotation);'''

if old not in text:
    print("ERROR: avatar_live.h anchor not found (SetEyeWeight declaration)")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1))
print("avatar_live.h: SetGaze / SetMouthRotation declared")
PYEOF

# ----------------------------------------------------------- avatar_live.cc --
python3 - "$AC" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()

if "LiveAvatar::SetGaze" in text:
    print("avatar_live.cc: gaze/rotation already defined")
    raise SystemExit(0)

old = '''void LiveAvatar::SetMouthWeight(int weight) {
    if (!ready_) return;
    impl_->avatar.mouth().setWeight(weight);
}'''

new = '''void LiveAvatar::SetMouthWeight(int weight) {
    if (!ready_) return;
    impl_->avatar.mouth().setWeight(weight);
}

void LiveAvatar::SetGaze(int x, int y) {
    if (!ready_) return;
    // Element::setPosition clamps to -100..100 itself, so no range check here.
    // The mouth is deliberately NOT moved: shifting it with the eyes reads as
    // the whole face sliding, which is what M5's breath modifier wants and a
    // gaze does not.
    impl_->avatar.leftEye().setPosition({x, y});
    impl_->avatar.rightEye().setPosition({x, y});
}

void LiveAvatar::SetMouthRotation(int rotation) {
    if (!ready_) return;
    // Element::setRotation clamps to 0..3600.
    impl_->avatar.mouth().setRotation(rotation);
}'''

if old not in text:
    print("ERROR: avatar_live.cc anchor not found (SetMouthWeight definition)")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1))
print("avatar_live.cc: SetGaze / SetMouthRotation defined")
PYEOF

# ------------------------------------------------------------- stackchan.cc --
python3 - "$SC" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
edits = 0

# -- 1. per-face gaze + tilt, applied on every render ----------------------
old = '''            live_avatar_->SetMouthWeight(kFaceMouthWeight[current_face_index_]);
        }
    }'''

new = '''            live_avatar_->SetMouthWeight(kFaceMouthWeight[current_face_index_]);
        }

        // Where the eyes look. -100..100 per axis onto +/-16 px of travel,
        // +y DOWN (see eyes.cpp's _eye_min_offset / _eye_max_offset), so
        // negative y is up.
        //
        // `thinking` is why this axis exists. Doubt contributes only an eye
        // weight of 75 against idle's 100, which is not a thinking face --
        // a thinking face is one looking up and away from you, and until now
        // there was no way to say that.
        static constexpr int kFaceGazeX[6] = {
              0,   // idle        -- straight at you
              0,   // happy
            -60,   // thinking    -- away, to his left
              0,   // sad
              0,   // surprised   -- dead ahead, eyes wide
             45,   // embarrassed -- can't quite look at you
        };
        static constexpr int kFaceGazeY[6] = {
              0,   // idle
            -25,   // happy       -- a little up; bright
            -70,   // thinking    -- up, where people look to think
             60,   // sad         -- down
              0,   // surprised
             40,   // embarrassed -- down and away
        };

        if (gaze_override_active_) {
            // A host set_gaze wins until the next set_avatar. Applied here
            // rather than held in LiveAvatar because SetFace() runs on every
            // render, and a blink renders four times -- storing it there
            // would let the next blink erase it.
            live_avatar_->SetGaze(gaze_override_x_, gaze_override_y_);
        } else {
            live_avatar_->SetGaze(kFaceGazeX[current_face_index_],
                                  kFaceGazeY[current_face_index_]);
        }

        // Mouth tilt, tenths of a degree, 0..3600, negatives as 3600+angle.
        //
        // UNVERIFIED ON HARDWARE, unlike the gaze above. DefaultMouth calls
        // setRotation but sets no transform pivot of its own the way
        // DefaultEyes does, so the pivot it rotates about is LVGL's default
        // for that object and has not been seen on a panel. If the thinking
        // mouth looks wrong, kFaceMouthRotation is the one knob -- set it to
        // all zeros and nothing else changes.
        static constexpr int kFaceMouthRotation[6] = {
              0,   // idle
              0,   // happy
            150,   // thinking    -- 15 deg, the wry not-quite-sure mouth
              0,   // sad
              0,   // surprised
              0,   // embarrassed
        };
        live_avatar_->SetMouthRotation(
            mouth_rotation_override_active_ ? mouth_rotation_override_
                                            : kFaceMouthRotation[current_face_index_]);
    }'''

if old in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'kFaceGazeX' not in text:
    print("ERROR: stackchan.cc anchor not found (end of RenderLiveAvatarLocked)")
    raise SystemExit(1)

# -- 2. the override state -------------------------------------------------
old = '''    bool SetAvatarExpressionLocked(const char* face) {'''

new = '''    // Host gaze / mouth-tilt overrides, each held until the next set_avatar.
    // Not atomics: every read and write is under the display lock, the same
    // as current_face_index_ and the rest of the avatar state beside them.
    bool gaze_override_active_ = false;
    int gaze_override_x_ = 0;
    int gaze_override_y_ = 0;
    bool mouth_rotation_override_active_ = false;
    int mouth_rotation_override_ = 0;

    bool SetAvatarExpressionLocked(const char* face) {'''

if old in text and 'gaze_override_active_ = false;' not in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'gaze_override_active_' not in text:
    print("ERROR: stackchan.cc anchor not found (SetAvatarExpressionLocked)")
    raise SystemExit(1)

# -- 3. an expression change drops both overrides --------------------------
# Done here rather than in the MCP handler so it covers every caller -- the
# touch reactions and the deferred-fetch replay path call this directly.
old = '''        const int idx = FaceNameToIndex(face);
        if (idx < 0) return false;
        current_face_index_ = idx;
        active_layer_ = ActiveLayer::FACE;
        if (!RenderAvatarLocked()) return false;'''

new = '''        const int idx = FaceNameToIndex(face);
        if (idx < 0) return false;
        current_face_index_ = idx;
        active_layer_ = ActiveLayer::FACE;
        // The new expression brings its own gaze and tilt. Clearing here
        // rather than in the set_avatar tool covers the touch reactions and
        // the post-fetch replay path too, which call this directly.
        gaze_override_active_ = false;
        mouth_rotation_override_active_ = false;
        if (!RenderAvatarLocked()) return false;'''

if old in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'The new expression brings its own gaze' not in text:
    print("ERROR: stackchan.cc anchor not found (SetAvatarExpressionLocked body)")
    raise SystemExit(1)

# -- 4. the two setters ----------------------------------------------------
old = '''    // Step callback for the four-phase blink sequence. Each invocation'''

new = '''    bool SetAvatarGaze(int x, int y) {
        if (display_ == nullptr) return false;
        DisplayLockGuard lock(display_);
        gaze_override_active_ = true;
        gaze_override_x_ = x;
        gaze_override_y_ = y;
        return RenderAvatarLocked();
    }

    bool SetAvatarMouthRotation(int rotation) {
        if (display_ == nullptr) return false;
        DisplayLockGuard lock(display_);
        mouth_rotation_override_active_ = true;
        mouth_rotation_override_ = rotation;
        return RenderAvatarLocked();
    }

    // Step callback for the four-phase blink sequence. Each invocation'''

if old in text and 'bool SetAvatarGaze(' not in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'bool SetAvatarGaze(' not in text:
    print("ERROR: stackchan.cc anchor not found (BlinkStepCb comment)")
    raise SystemExit(1)

# -- 5. the MCP tools ------------------------------------------------------
old = '''        mcp_server.AddTool(
            "self.display.set_blink",'''

new = '''        // The gaze axis, for host-side choreography: M5's idle_expression and
        // breath modifiers are built on this, and so is anything that wants
        // Cubie to glance at something without turning his head.
        mcp_server.AddTool(
            "self.display.set_gaze",
            "Point the avatar's eyes without moving the head. x and y are "
            "-100..100, mapping onto about 16 pixels of travel each way; "
            "y is positive DOWNWARD, so negative y looks up. Both eyes move "
            "together. Held until the next set_avatar, which restores that "
            "expression's own gaze.",
            PropertyList({Property("x", kPropertyTypeInteger, 0, -100, 100),
                          Property("y", kPropertyTypeInteger, 0, -100, 100)}),
            [this](const PropertyList& properties) -> ReturnValue {
                const int x = properties["x"].value<int>();
                const int y = properties["y"].value<int>();
                const bool applied = SetAvatarGaze(x, y);
                cJSON* root = cJSON_CreateObject();
                cJSON_AddNumberToObject(root, "x", x);
                cJSON_AddNumberToObject(root, "y", y);
                cJSON_AddBoolToObject(root, "ok", applied);
                if (!applied) {
                    cJSON_AddStringToObject(root, "error",
                        "Display not ready yet; retry after a moment.");
                }
                ESP_LOGI(TAG, "set_gaze: x=%d y=%d applied=%d", x, y, applied ? 1 : 0);
                return root;
            });

        mcp_server.AddTool(
            "self.display.set_mouth_rotation",
            "Tilt the avatar's mouth. rotation is in tenths of a degree, "
            "0..3600; express a negative angle as 3600 plus the angle "
            "(e.g. -15 degrees is 3450). Held until the next set_avatar.",
            PropertyList({Property("rotation", kPropertyTypeInteger, 0, 0, 3600)}),
            [this](const PropertyList& properties) -> ReturnValue {
                const int rotation = properties["rotation"].value<int>();
                const bool applied = SetAvatarMouthRotation(rotation);
                cJSON* root = cJSON_CreateObject();
                cJSON_AddNumberToObject(root, "rotation", rotation);
                cJSON_AddBoolToObject(root, "ok", applied);
                if (!applied) {
                    cJSON_AddStringToObject(root, "error",
                        "Display not ready yet; retry after a moment.");
                }
                ESP_LOGI(TAG, "set_mouth_rotation: rotation=%d applied=%d",
                         rotation, applied ? 1 : 0);
                return root;
            });

        mcp_server.AddTool(
            "self.display.set_blink",'''

if old in text and 'self.display.set_gaze' not in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'self.display.set_gaze' not in text:
    print("ERROR: stackchan.cc anchor not found (set_blink AddTool)")
    raise SystemExit(1)

if edits:
    path.write_text(text)
    print(f"stackchan.cc patched ({edits} edits)")
else:
    print("stackchan.cc: already patched")
PYEOF

echo
echo "Done. Rebuild and publish -- this one goes out over OTA."
