#!/usr/bin/env bash
# Let the device end its own listen when you stop talking.
#
# --- The bug this fixes, which is a missing feature ---
#
# In the xiaozhi protocol the SERVER ends every capture. Traced, not assumed:
#
#   * `Application::StopListening()` is called from exactly ONE place --
#     application.cc's incoming-JSON handler, on `{"type":"listen",
#     "state":"stop"}` from the server.
#   * the device HAS a voice-activity detector, but its callback only sets
#     `voice_detected_` and flashes an LED. Nothing reads it to end a capture.
#   * `kListeningModeAutoStop` names the mode the SERVER is told about. It does
#     not make the device stop by itself.
#
# So on the wake-word path nothing ever ends the capture: the device records,
# the gateway buffers, and both wait for the other. On the LCD-touch path
# `ToggleChatState` sets `kListeningModeManualStop` and a second touch ends it,
# which is why touching the screen records until you touch it again.
#
# stackchan-mcp's only way to end a listen is its `listen` tool -- start, wait
# a FIXED window, stop. Fine for a timed capture, useless for a conversation:
# it cannot know when a sentence finished.
#
# --- What this adds ---
#
# A silence timer, armed and cancelled by the VAD event the firmware already
# raises. Speech stops -> arm; speech resumes -> cancel; timer expires ->
# `StopListening()`, exactly as if the server had asked.
#
# That closes the loop the audio hook needs: the wake word starts a capture,
# the person stops talking, the device stops, and the gateway POSTs the audio
# to STACKCHAN_AUDIO_HOOK_URL. It also makes the FIXED-window `listen` tool
# less necessary, which is where most of a turn's latency was.
#
# Two guards, both load-bearing:
#
#   * it will not fire before any speech has been heard. Otherwise a wake word
#     with nobody talking would end the capture immediately and every turn
#     would transcribe silence.
#   * a maximum capture length ends it regardless. A room with a fan in it can
#     hold the VAD high indefinitely, and a capture that never ends is the bug
#     this patch exists to remove, not one to reintroduce from the other side.
#
# Manual-stop captures are left alone: someone who pressed to talk is holding
# the button on purpose, and taking that away would be surprising.
set -euo pipefail

FW="${1:-$HOME/stackchan-mcp/firmware}"
APP="$FW/main/application.cc"
HDR="$FW/main/application.h"

for f in "$APP" "$HDR"; do
  [ -f "$f" ] || { echo "ERROR: $f not found"; exit 1; }
done

# ------------------------------------------------------------ application.h --
python3 - "$HDR" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()

if "auto_stop_silence_timer_" in text:
    print("application.h: already declared")
    raise SystemExit(0)

old = "    ListeningMode listening_mode_ = kListeningModeAutoStop;"
new = '''    ListeningMode listening_mode_ = kListeningModeAutoStop;

    // --- added by cubie/firmware/apply-vad-auto-stop.sh ---
    // Ending an auto-stop capture when the speaker stops. The protocol leaves
    // this to the server, and stackchan-mcp can only do it on a fixed timer,
    // so a conversation has no way to know a sentence finished.
    esp_timer_handle_t auto_stop_silence_timer_ = nullptr;
    esp_timer_handle_t auto_stop_max_timer_ = nullptr;
    // Whether any speech has been heard in THIS capture. Without it, a wake
    // word with nobody talking would stop immediately and transcribe silence.
    bool auto_stop_heard_speech_ = false;
    void ArmAutoStopTimers();
    void CancelAutoStopTimers();
    void OnAutoStopVadChange(bool speaking);'''
if old not in text:
    print("ERROR: application.h anchor not found (listening_mode_ declaration)")
    raise SystemExit(1)
path.write_text(text.replace(old, new, 1))
print("application.h: auto-stop members declared")
PYEOF

# ----------------------------------------------------------- application.cc --
python3 - "$APP" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
edits = 0

# -- 1. the implementation ------------------------------------------------
impl = '''
// --- added by cubie/firmware/apply-vad-auto-stop.sh ---------------------

// How long a silence has to last before a capture is treated as finished.
// A natural pause mid-sentence is commonly 300-600 ms, so this has to be
// comfortably longer than that or he will interrupt; much longer and he
// feels slow to answer.
#define CUBIE_AUTO_STOP_SILENCE_MS 1200

// The longest any auto-stop capture may run. A room with a fan in it can hold
// the VAD high indefinitely, and a capture that never ends is the bug this
// patch removes -- reintroducing it from the other side would be no better.
#define CUBIE_AUTO_STOP_MAX_MS 15000

void Application::ArmAutoStopTimers() {
    CancelAutoStopTimers();
    auto_stop_heard_speech_ = false;

    if (auto_stop_silence_timer_ == nullptr) {
        esp_timer_create_args_t args = {
            .callback = [](void* arg) {
                Application* app = (Application*)arg;
                // Only ends a capture that actually carried speech.
                if (app->auto_stop_heard_speech_) {
                    ESP_LOGI(TAG, "auto-stop: silence, ending capture");
                    app->StopListening();
                }
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "cubie_auto_stop_silence",
            .skip_unhandled_events = true,
        };
        esp_timer_create(&args, &auto_stop_silence_timer_);
    }
    if (auto_stop_max_timer_ == nullptr) {
        esp_timer_create_args_t args = {
            .callback = [](void* arg) {
                Application* app = (Application*)arg;
                ESP_LOGW(TAG, "auto-stop: hit the maximum capture length");
                app->StopListening();
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "cubie_auto_stop_max",
            .skip_unhandled_events = true,
        };
        esp_timer_create(&args, &auto_stop_max_timer_);
    }

    esp_timer_start_once(auto_stop_max_timer_, CUBIE_AUTO_STOP_MAX_MS * 1000);
    // The silence timer is NOT armed here. It is armed the first time speech
    // stops, so the window before anyone speaks is bounded only by the
    // maximum above.
}

void Application::CancelAutoStopTimers() {
    if (auto_stop_silence_timer_ != nullptr) {
        esp_timer_stop(auto_stop_silence_timer_);
    }
    if (auto_stop_max_timer_ != nullptr) {
        esp_timer_stop(auto_stop_max_timer_);
    }
}

void Application::OnAutoStopVadChange(bool speaking) {
    // Manual-stop captures are left alone: someone holding a button to talk
    // is doing it on purpose.
    if (listening_mode_ != kListeningModeAutoStop) {
        return;
    }
    if (speaking) {
        auto_stop_heard_speech_ = true;
        if (auto_stop_silence_timer_ != nullptr) {
            esp_timer_stop(auto_stop_silence_timer_);
        }
        return;
    }
    if (auto_stop_silence_timer_ != nullptr && auto_stop_heard_speech_) {
        esp_timer_stop(auto_stop_silence_timer_);
        esp_timer_start_once(auto_stop_silence_timer_,
                             CUBIE_AUTO_STOP_SILENCE_MS * 1000);
    }
}

void Application::StopListening() {'''

old = "void Application::StopListening() {"
if "ArmAutoStopTimers" in text:
    pass
elif old in text:
    text = text.replace(old, impl, 1)
    edits += 1
else:
    print("ERROR: application.cc anchor not found (StopListening definition)")
    raise SystemExit(1)

# -- 2. drive it from the VAD event the firmware already raises -----------
old = '''        if (bits & MAIN_EVENT_VAD_CHANGE) {
            if (GetDeviceState() == kDeviceStateListening) {
                auto led = Board::GetInstance().GetLed();
                led->OnStateChanged();
            }
        }'''
new = '''        if (bits & MAIN_EVENT_VAD_CHANGE) {
            if (GetDeviceState() == kDeviceStateListening) {
                auto led = Board::GetInstance().GetLed();
                led->OnStateChanged();
                // Speech started or stopped. This event already existed and
                // only drove the LED; it is exactly the signal needed to end
                // a capture when the speaker finishes.
                OnAutoStopVadChange(audio_service_.IsVoiceDetected());
            }
        }'''
if "OnAutoStopVadChange(audio_service_" in text:
    pass
elif old in text:
    text = text.replace(old, new, 1)
    edits += 1
else:
    print("ERROR: application.cc anchor not found (VAD_CHANGE handler)")
    raise SystemExit(1)

# -- 3. arm on entering Listening ----------------------------------------
old = '''        case kDeviceStateListening: {
            display->SetStatus(Lang::Strings::LISTENING);
            display->SetEmotion("neutral");'''
new = '''        case kDeviceStateListening: {
            display->SetStatus(Lang::Strings::LISTENING);
            display->SetEmotion("neutral");
            // Start the capture-length guards. The silence timer arms itself
            // the first time speech stops; only the maximum starts here.
            ArmAutoStopTimers();'''
if "ArmAutoStopTimers();\n            // Make sure" in text or "ArmAutoStopTimers();" in text.split("void Application::ArmAutoStopTimers")[-1]:
    pass
elif old in text:
    text = text.replace(old, new, 1)
    edits += 1
else:
    print("ERROR: application.cc anchor not found (Listening state case)")
    raise SystemExit(1)

# -- 4. cancel on returning to Idle --------------------------------------
old = '''        case kDeviceStateIdle:
            display->SetStatus(Lang::Strings::STANDBY);'''
new = '''        case kDeviceStateIdle:
            CancelAutoStopTimers();
            display->SetStatus(Lang::Strings::STANDBY);'''
if "CancelAutoStopTimers();\n            display->SetStatus(Lang::Strings::STANDBY);" in text:
    pass
elif old in text:
    text = text.replace(old, new, 1)
    edits += 1
else:
    print("ERROR: application.cc anchor not found (Idle state case)")
    raise SystemExit(1)

if edits:
    path.write_text(text)
    print(f"application.cc patched ({edits} edits)")
else:
    print("application.cc: already patched")
PYEOF

echo
echo "Done. Rebuild and publish -- this one goes out over OTA."
