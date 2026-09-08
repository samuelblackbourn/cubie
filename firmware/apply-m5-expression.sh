#!/usr/bin/env bash
# Expose the whole face to the host, and give `thinking` a reason to look up.
#
# --- Why `thinking` read as flat ---
#
# It maps to Emotion::Doubt, whose only contribution is an eye weight of 75
# against idle's 100 -- a quarter of an eyelid. fix-expression-depth.sh added a
# resting mouth per face, which rescued the other five, but a thinking face is
# not a mouth shape. It is where the eyes are LOOKING: up, and away from you.
#
# We had no way to say that, because nothing in this firmware ever called
# Element::setPosition.
#
# --- Why the whole surface, not just gaze ---
#
# M5's renderer gives every feature four independent axes -- position, rotation,
# weight and size -- and their whole character stack is built on them:
# idle_expression.h drifts the gaze and tilts the mouth, breath.h slides all
# three features on y, and every keyframe of every dance in dance.h sets all
# four axes on all three features. Porting that choreography host-side needs the
# same four axes reachable from the host, so this exposes them once rather than
# growing a tool per animation.
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
# On eyes that are 8..32 px across, 16 px is a large and unambiguous shift.
#
# --- Weight and size are opt-in, and that is the important part ---
#
# Position and rotation are axes nothing else in this firmware drives, so a host
# override of those contends with nobody. Weight and size do NOT have that
# property: the four-phase blink owns eye weight, lip-sync owns mouth weight off
# the tts.start transition, and fix-expression-depth owns the resting values of
# both. A tool that set all four axes unconditionally would fight the two things
# on this device that already work well.
#
# So each axis is driven only when the caller passes it. kFeatureUnset is the
# sentinel; a host that wants the eyes squinted for a dance asks for the weight
# and disables blink for the duration with the set_blink tool it already has.
#
# --- Why the override lives in the board, not in LiveAvatar ---
#
# RenderLiveAvatarLocked() calls live_avatar_->SetFace() on EVERY render, and a
# blink renders four times. So a per-face gaze applied inside SetFace() would be
# correct, but a host-set override stored there would be silently overwritten
# within five seconds by the next blink.
#
# It is kept as board state instead and applied in RenderLiveAvatarLocked(),
# which is exactly how the mouth already works: lip-sync wins while it is the
# active layer, the per-face resting value applies otherwise. A blink now
# replays the override rather than erasing it, and a face change clears it --
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

if "SetFeaturePosition" in text:
    print("avatar_live.h: feature axes already declared")
    raise SystemExit(0)

old = '''    // 0..100. Direction is asserted on hardware, not guessed -- see kEyeWeightOpen.
    void SetEyeWeight(int weight);
    void SetMouthWeight(int weight);'''

new = '''    // 0..100. Direction is asserted on hardware, not guessed -- see kEyeWeightOpen.
    void SetEyeWeight(int weight);
    void SetMouthWeight(int weight);

    // The three features M5's renderer draws, in its own order.
    enum Feature { kLeftEye = 0, kRightEye = 1, kMouth = 2, kFeatureCount = 3 };

    // The four axes M5 gives every feature. Ranges are M5's own and are
    // clamped by the renderer, so no range check is needed here:
    //   position  -100..100 each axis, mapped onto +/-16 px of travel.
    //             It is an LVGL setPos, so +y is DOWN.
    //   rotation  0..3600, tenths of a degree (LVGL's own angle unit).
    //             Express a negative angle as 3600 + angle, as M5 does.
    //   weight    0..100. Contends with blink and lip-sync -- see the note in
    //             apply-m5-expression.sh about why it is opt-in.
    //   size      -100..100, 0 being normal, mapped onto an 8..32 px eye.
    void SetFeaturePosition(int feature, int x, int y);
    void SetFeatureRotation(int feature, int rotation);
    void SetFeatureWeight(int feature, int weight);
    void SetFeatureSize(int feature, int size);

    // Both eyes together. Shorthand for the common case: this is a gaze, not a
    // squint, and every animation M5 ships moves the two eyes identically.
    void SetGaze(int x, int y);'''

if old not in text:
    print("ERROR: avatar_live.h anchor not found (SetEyeWeight declaration)")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1))
print("avatar_live.h: feature axes declared")
PYEOF

# ----------------------------------------------------------- avatar_live.cc --
python3 - "$AC" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()

if "LiveAvatar::SetFeaturePosition" in text:
    print("avatar_live.cc: feature axes already defined")
    raise SystemExit(0)

old = '''void LiveAvatar::SetMouthWeight(int weight) {
    if (!ready_) return;
    impl_->avatar.mouth().setWeight(weight);
}'''

new = '''void LiveAvatar::SetMouthWeight(int weight) {
    if (!ready_) return;
    impl_->avatar.mouth().setWeight(weight);
}

namespace {
// Resolve a Feature index onto the M5 element. Returns nullptr for an index
// out of range, so every setter below is a no-op rather than a crash on a bad
// value arriving from the host.
stackchan::avatar::Feature* FeatureAt(stackchan::avatar::DefaultAvatar& avatar,
                                      int feature) {
    switch (feature) {
        case LiveAvatar::kLeftEye:  return &avatar.leftEye();
        case LiveAvatar::kRightEye: return &avatar.rightEye();
        case LiveAvatar::kMouth:    return &avatar.mouth();
        default: return nullptr;
    }
}
}  // namespace

void LiveAvatar::SetFeaturePosition(int feature, int x, int y) {
    if (!ready_) return;
    auto* f = FeatureAt(impl_->avatar, feature);
    if (f != nullptr) f->setPosition({x, y});
}

void LiveAvatar::SetFeatureRotation(int feature, int rotation) {
    if (!ready_) return;
    auto* f = FeatureAt(impl_->avatar, feature);
    if (f != nullptr) f->setRotation(rotation);
}

void LiveAvatar::SetFeatureWeight(int feature, int weight) {
    if (!ready_) return;
    auto* f = FeatureAt(impl_->avatar, feature);
    if (f != nullptr) f->setWeight(weight);
}

void LiveAvatar::SetFeatureSize(int feature, int size) {
    if (!ready_) return;
    auto* f = FeatureAt(impl_->avatar, feature);
    if (f != nullptr) f->setSize(size);
}

void LiveAvatar::SetGaze(int x, int y) {
    // The mouth is deliberately NOT moved with the eyes: shifting all three
    // together reads as the whole face sliding, which is what M5's breath
    // modifier wants and a gaze does not. Breath asks for the mouth
    // separately, via SetFeaturePosition.
    SetFeaturePosition(kLeftEye, x, y);
    SetFeaturePosition(kRightEye, x, y);
}'''

if old not in text:
    print("ERROR: avatar_live.cc anchor not found (SetMouthWeight definition)")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1))
print("avatar_live.cc: feature axes defined")
PYEOF

# ------------------------------------------------------------- stackchan.cc --
python3 - "$SC" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
edits = 0

# -- 1. per-face gaze and tilt, plus the host overrides, on every render ----
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

        live_avatar_->SetGaze(kFaceGazeX[current_face_index_],
                              kFaceGazeY[current_face_index_]);
        live_avatar_->SetFeaturePosition(stackchan_live::LiveAvatar::kMouth, 0, 0);
        live_avatar_->SetFeatureRotation(stackchan_live::LiveAvatar::kMouth,
                                         kFaceMouthRotation[current_face_index_]);

        // Host overrides last, so they win over the resting values above.
        // Per AXIS rather than per feature: position and rotation contend with
        // nothing, but weight is owned by blink and lip-sync and size by the
        // `surprised` override, so those two are applied only when the host
        // actually asked for them.
        for (int f = 0; f < stackchan_live::LiveAvatar::kFeatureCount; ++f) {
            const FeatureOverride& o = feature_override_[f];
            if (o.has_position) live_avatar_->SetFeaturePosition(f, o.x, o.y);
            if (o.has_rotation) live_avatar_->SetFeatureRotation(f, o.rotation);
            if (o.has_weight)   live_avatar_->SetFeatureWeight(f, o.weight);
            if (o.has_size)     live_avatar_->SetFeatureSize(f, o.size);
        }
    }'''

if old in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'kFaceGazeX' not in text:
    print("ERROR: stackchan.cc anchor not found (end of RenderLiveAvatarLocked)")
    raise SystemExit(1)

# -- 2. the override state -------------------------------------------------
old = '''    bool SetAvatarExpressionLocked(const char* face) {'''

new = '''    // A host override of M5's four feature axes, held until the next face
    // change. Per-axis flags rather than one active flag: see the note in
    // RenderLiveAvatarLocked about weight and size having other owners.
    //
    // Not atomics: every read and write is under the display lock, the same as
    // current_face_index_ and the rest of the avatar state beside them.
    struct FeatureOverride {
        bool has_position = false;
        int x = 0;
        int y = 0;
        bool has_rotation = false;
        int rotation = 0;
        bool has_weight = false;
        int weight = 0;
        bool has_size = false;
        int size = 0;
    };
    FeatureOverride feature_override_[stackchan_live::LiveAvatar::kFeatureCount];

    // What the host passes for an axis it does not want to drive. Below every
    // axis minimum (-100 for position and size, 0 for rotation and weight), so
    // it cannot collide with a real request.
    static constexpr int kFeatureUnset = -1000;

    bool SetAvatarExpressionLocked(const char* face) {'''

if old in text and 'struct FeatureOverride {' not in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'struct FeatureOverride {' not in text:
    print("ERROR: stackchan.cc anchor not found (SetAvatarExpressionLocked)")
    raise SystemExit(1)

# -- 3. an expression change drops every override --------------------------
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
        for (auto& o : feature_override_) o = FeatureOverride{};
        if (!RenderAvatarLocked()) return false;'''

if old in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'The new expression brings its own gaze' not in text:
    print("ERROR: stackchan.cc anchor not found (SetAvatarExpressionLocked body)")
    raise SystemExit(1)

# -- 4. the setters --------------------------------------------------------
old = '''    // Step callback for the four-phase blink sequence. Each invocation'''

new = '''    // Map a host feature name onto a stackchan_live::LiveAvatar::Feature, or -1.
    // "eyes" is the common case and covers both: every animation M5 ships
    // moves the two eyes identically.
    static int FeatureNameToIndex(const char* name) {
        if (name == nullptr) return -1;
        if (strcmp(name, "left_eye") == 0)  return stackchan_live::LiveAvatar::kLeftEye;
        if (strcmp(name, "right_eye") == 0) return stackchan_live::LiveAvatar::kRightEye;
        if (strcmp(name, "mouth") == 0)     return stackchan_live::LiveAvatar::kMouth;
        return -1;
    }

    // Apply one axis set to one feature index. Caller holds the display lock.
    void ApplyFeatureOverrideLocked(int feature, int x, int y, int rotation,
                                    int weight, int size) {
        FeatureOverride& o = feature_override_[feature];
        // Position is one axis, not two: applying x while leaving y at zero
        // would move the feature somewhere the caller did not ask for. Half a
        // position is rejected by the tool below rather than half-applied.
        if (x != kFeatureUnset && y != kFeatureUnset) {
            o.has_position = true;
            o.x = x;
            o.y = y;
        }
        if (rotation != kFeatureUnset) {
            o.has_rotation = true;
            o.rotation = rotation;
        }
        if (weight != kFeatureUnset) {
            o.has_weight = true;
            o.weight = weight;
        }
        if (size != kFeatureUnset) {
            o.has_size = true;
            o.size = size;
        }
    }

    // `feature` is "eyes", "left_eye", "right_eye" or "mouth". Returns false
    // for an unknown name so the tool can say so rather than silently no-op.
    bool SetAvatarFeature(const char* feature, int x, int y, int rotation,
                          int weight, int size) {
        if (display_ == nullptr) return false;
        const bool both_eyes = (feature != nullptr && strcmp(feature, "eyes") == 0);
        const int single = both_eyes ? -1 : FeatureNameToIndex(feature);
        if (!both_eyes && single < 0) return false;
        // Exactly one of x/y given is a caller mistake, and a silent no-op is
        // the worst answer to it -- this firmware has already been bitten once
        // by a request that returned ok and did nothing.
        if ((x == kFeatureUnset) != (y == kFeatureUnset)) return false;

        DisplayLockGuard lock(display_);
        if (both_eyes) {
            ApplyFeatureOverrideLocked(stackchan_live::LiveAvatar::kLeftEye, x, y, rotation, weight, size);
            ApplyFeatureOverrideLocked(stackchan_live::LiveAvatar::kRightEye, x, y, rotation, weight, size);
        } else {
            ApplyFeatureOverrideLocked(single, x, y, rotation, weight, size);
        }
        return RenderAvatarLocked();
    }

    bool SetAvatarGaze(int x, int y) {
        return SetAvatarFeature("eyes", x, y, kFeatureUnset, kFeatureUnset,
                                kFeatureUnset);
    }

    // Step callback for the four-phase blink sequence. Each invocation'''

if old in text and 'bool SetAvatarFeature(' not in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'bool SetAvatarFeature(' not in text:
    print("ERROR: stackchan.cc anchor not found (BlinkStepCb comment)")
    raise SystemExit(1)

# -- 5. the MCP tools ------------------------------------------------------
old = '''        mcp_server.AddTool(
            "self.display.set_blink",'''

new = '''        // The gaze axis on its own, because it is the one the assistant
        // reaches for: glancing at something without turning the head.
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

        // The complete surface: M5's four axes on any one feature, for
        // host-side choreography (their idle_expression, breath and dances are
        // built entirely on these). Omitted axes are left to whatever already
        // owns them -- blink and lip-sync own weight, the per-face table owns
        // the resting values -- which is why -1000 rather than 0 means "leave
        // this one alone".
        mcp_server.AddTool(
            "self.display.set_feature",
            "Drive one of the avatar's features directly. feature is 'eyes' "
            "(both), 'left_eye', 'right_eye' or 'mouth'. Each axis is applied "
            "only if given; pass -1000 to leave an axis to whatever already "
            "drives it. x and y count as ONE axis and must both be given or "
            "both omitted. x/y are -100..100 (y positive DOWNWARD); rotation is "
            "tenths of a degree 0..3600, a negative angle expressed as 3600 "
            "plus the angle; weight is 0..100 and CONTENDS WITH BLINK AND "
            "LIP-SYNC, so disable blink with set_blink for the duration if you "
            "drive it; size is -100..100 with 0 normal. Held until the next "
            "set_avatar.",
            PropertyList({Property("feature", kPropertyTypeString),
                          Property("x", kPropertyTypeInteger, kFeatureUnset, kFeatureUnset, 100),
                          Property("y", kPropertyTypeInteger, kFeatureUnset, kFeatureUnset, 100),
                          Property("rotation", kPropertyTypeInteger, kFeatureUnset, kFeatureUnset, 3600),
                          Property("weight", kPropertyTypeInteger, kFeatureUnset, kFeatureUnset, 100),
                          Property("size", kPropertyTypeInteger, kFeatureUnset, kFeatureUnset, 100)}),
            [this](const PropertyList& properties) -> ReturnValue {
                std::string feature = properties["feature"].value<std::string>();
                const int x = properties["x"].value<int>();
                const int y = properties["y"].value<int>();
                const int rotation = properties["rotation"].value<int>();
                const int weight = properties["weight"].value<int>();
                const int size = properties["size"].value<int>();

                cJSON* root = cJSON_CreateObject();
                cJSON_AddStringToObject(root, "feature", feature.c_str());
                const bool applied = SetAvatarFeature(feature.c_str(), x, y,
                                                      rotation, weight, size);
                cJSON_AddBoolToObject(root, "ok", applied);
                if (!applied) {
                    cJSON_AddStringToObject(root, "error",
                        "Unknown feature (allowed: eyes, left_eye, right_eye, "
                        "mouth), or x and y were not both given, or the "
                        "display is not ready yet.");
                }
                ESP_LOGI(TAG,
                         "set_feature: feature=%s x=%d y=%d rotation=%d "
                         "weight=%d size=%d applied=%d",
                         feature.c_str(), x, y, rotation, weight, size,
                         applied ? 1 : 0);
                return root;
            });

        // The speech bubble. LiveAvatar::SetSpeech / ClearSpeech were vendored
        // in with M5's renderer and have been dead code ever since -- nothing
        // in this firmware called them. M5's own driver uses the bubble for
        // "Zzz..." on sleepy and for any status string it does not recognise,
        // and their TimedSpeechModifier is built on it.
        mcp_server.AddTool(
            "self.display.set_speech",
            "Show text in the avatar's speech bubble. An empty string clears "
            "it. Unlike the feature overrides this is NOT cleared by "
            "set_avatar -- a caller that shows a bubble owns hiding it again.",
            PropertyList({Property("text", kPropertyTypeString)}),
            [this](const PropertyList& properties) -> ReturnValue {
                std::string text = properties["text"].value<std::string>();
                bool applied = false;
                if (display_ != nullptr) {
                    DisplayLockGuard lock(display_);
                    if (live_avatar_ && live_avatar_->ready()) {
                        if (text.empty()) {
                            live_avatar_->ClearSpeech();
                        } else {
                            live_avatar_->SetSpeech(text.c_str());
                        }
                        applied = true;
                    }
                }
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "ok", applied);
                cJSON_AddNumberToObject(root, "length", (int)text.size());
                if (!applied) {
                    cJSON_AddStringToObject(root, "error",
                        "Display not ready yet; retry after a moment.");
                }
                // The text itself is not logged: a speech bubble carries
                // whatever the assistant was told, and that is not this
                // firmware's to put in a serial log.
                ESP_LOGI(TAG, "set_speech: length=%d applied=%d",
                         (int)text.size(), applied ? 1 : 0);
                return root;
            });

        mcp_server.AddTool(
            "self.display.set_blink",'''

if old in text and 'self.display.set_feature' not in text:
    text = text.replace(old, new, 1)
    edits += 1
elif 'self.display.set_feature' not in text:
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
