#!/usr/bin/env python3
"""Exercise build.sh's sdkconfig patcher, without running a build.

It governs three Kconfig `choice` groups now -- the UI language, the wake-word
implementation, and the MultiNet model. Members of a choice are mutually
exclusive but are DIFFERENT KEYS, so the patcher's by-key replacement cannot
drop a stale sibling on its own; it needs the group logic, and two selected
options in one choice is an ill-defined build rather than a loud failure.

That trap has already been hit once, when switching the language from EN_US to
JA_JP left both set. The check that caught it was ad-hoc and never landed, and
the logic has since gone from one group to three.

The patcher is a heredoc inside build.sh rather than its own file, so this
extracts it and runs it against a temporary config.json -- the same technique
the firmware C++ checks use. Testing the shipped text beats testing a copy.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
BUILD_SH = HERE / "build.sh"

START = 'python3 - "$BOARD/config.json"'
END = "PYEOF"


def extract_patcher() -> str:
    text = BUILD_SH.read_text()
    start = text.index(START)
    body_start = text.index("\n", text.index("<<'PYEOF'", start)) + 1
    body_end = text.index(f"\n{END}", body_start)
    return text[body_start:body_end]


def run(patcher: str, config: dict, args: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "config.json"
        path.write_text(json.dumps(config, indent=4) + "\n")
        result = subprocess.run(
            [sys.executable, "-c", patcher, str(path), *args],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"patcher failed: {result.returncode}\n{result.stdout}\n{result.stderr}"
            )
        return json.loads(path.read_text())


def appended(config: dict) -> list[str]:
    build = next(b for b in config["builds"] if b["name"] == "stackchan")
    return build.get("sdkconfig_append", [])


def keys_matching(entries: list[str], members: set[str]) -> list[str]:
    return [e for e in entries if e.split("=", 1)[0] in members]


LANGUAGES = {f"CONFIG_LANGUAGE_{n}" for n in ("EN_US", "ZH_CN", "JA_JP")}
WAKE_TYPES = {
    "CONFIG_WAKE_WORD_DISABLED",
    "CONFIG_USE_ESP_WAKE_WORD",
    "CONFIG_USE_AFE_WAKE_WORD",
    "CONFIG_USE_CUSTOM_WAKE_WORD",
}
MULTINETS = {
    "CONFIG_SR_MN_EN_NONE",
    "CONFIG_SR_MN_EN_MULTINET5_SINGLE_RECOGNITION_QUANT8",
    "CONFIG_SR_MN_EN_MULTINET6_QUANT",
    "CONFIG_SR_MN_EN_MULTINET7_QUANT",
}

#: The trailing "" is AUDIO_DEBUG_UDP, unset -- which is what a normal build
#: passes. It is spelled out here rather than defaulted inside the patcher so
#: that forgetting it is an IndexError at the first check rather than a
#: microphone tap that quietly survives into a shipped image.
ARGS = ["http://office-server.local:8420/api/companion/ota", "LANGUAGE_EN_US",
        "hi cubie", "Cubie", "20", "", "CONFIG_SR_MN_EN_MULTINET6_QUANT"]


def main() -> int:
    patcher = extract_patcher()
    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {detail}")
            failures.append(name)

    base = {"builds": [{"name": "stackchan", "sdkconfig_append": []}]}

    # 1. A clean config gets exactly one member of each choice.
    out = appended(run(patcher, base, ARGS))
    check("one language selected", len(keys_matching(out, LANGUAGES)) == 1, keys_matching(out, LANGUAGES))
    check("one wake-word type selected", len(keys_matching(out, WAKE_TYPES)) == 1, keys_matching(out, WAKE_TYPES))
    check("one MultiNet model selected", len(keys_matching(out, MULTINETS)) == 1, keys_matching(out, MULTINETS))
    check("the wake word is the custom one", "CONFIG_USE_CUSTOM_WAKE_WORD=y" in out)
    check("MultiNet6, whose native input is graphemes",
          "CONFIG_SR_MN_EN_MULTINET6_QUANT=y" in out)
    check("the phrase is quoted", 'CONFIG_CUSTOM_WAKE_WORD="hi cubie"' in out)

    # A `.local` OTA URL needs mDNS lookups, or the name never resolves and
    # updates stop with no symptom on the device. The two travel together, so
    # they are asserted together rather than trusted to be edited together.
    ota = [e for e in out if e.startswith("CONFIG_OTA_URL=")]
    check("one OTA URL", len(ota) == 1, ota)
    if ota and ".local" in ota[0]:
        check("a .local OTA URL brings mDNS lookups with it",
              "CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES=y" in out, out)

    # ... and an ADDRESS must still work, because that is the escape hatch when
    # resolution is the thing that broke.
    by_ip = appended(run(patcher, base,
                         ["http://192.168.0.84:8420/api/companion/ota"] + ARGS[1:]))
    check("an address still produces one OTA URL",
          [e for e in by_ip if e.startswith("CONFIG_OTA_URL=")]
          == ['CONFIG_OTA_URL="http://192.168.0.84:8420/api/companion/ota"'],
          [e for e in by_ip if e.startswith("CONFIG_OTA_URL=")])

    # 2. A stale sibling in each group is DROPPED, not left alongside.
    stale = {"builds": [{"name": "stackchan", "sdkconfig_append": [
        "CONFIG_LANGUAGE_ZH_CN=y",
        "CONFIG_USE_AFE_WAKE_WORD=y",
        "CONFIG_SR_MN_EN_MULTINET7_QUANT=y",
    ]}]}
    out = appended(run(patcher, stale, ARGS))
    check("the old language is gone", keys_matching(out, LANGUAGES) == ["CONFIG_LANGUAGE_EN_US=y"],
          keys_matching(out, LANGUAGES))
    check("the old wake-word type is gone",
          keys_matching(out, WAKE_TYPES) == ["CONFIG_USE_CUSTOM_WAKE_WORD=y"],
          keys_matching(out, WAKE_TYPES))
    check("the old MultiNet model is gone",
          keys_matching(out, MULTINETS) == ["CONFIG_SR_MN_EN_MULTINET6_QUANT=y"],
          keys_matching(out, MULTINETS))

    # 3. Running twice changes nothing -- the build timer runs this every time.
    once = run(patcher, base, ARGS)
    twice = run(patcher, once, ARGS)
    check("idempotent", appended(once) == appended(twice))

    # 4. A changed phrase REPLACES rather than accumulating.
    out = appended(run(patcher, once, ARGS[:2] + ["hey cubie", "Cubie", "35", "", ARGS[6]]))
    phrases = [e for e in out if e.startswith("CONFIG_CUSTOM_WAKE_WORD=")]
    check("one phrase, updated", phrases == ['CONFIG_CUSTOM_WAKE_WORD="hey cubie"'], phrases)
    check("the threshold followed it", "CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=35" in out)

    # 5. The microphone tap goes on when asked -- and comes OFF again.
    #
    # The second half is the half that matters. sdkconfig_append is merged by
    # key, so an option nobody re-states survives from the previous build: a
    # tap switched on once to diagnose a wake word would otherwise persist into
    # every image afterwards, streaming a continuous copy of the room onto the
    # LAN with nothing on the device to show for it. "Built without it" has to
    # MEAN off, not merely "not mentioned".
    debug_on = appended(run(patcher, base,
                            ARGS[:5] + ["office-server.local:8098", ARGS[6]]))
    check("the tap goes on", "CONFIG_USE_AUDIO_DEBUGGER=y" in debug_on, debug_on)
    check("the tap has somewhere to send",
          'CONFIG_AUDIO_DEBUG_UDP_SERVER="office-server.local:8098"' in debug_on,
          [e for e in debug_on if "AUDIO_DEBUG" in e])

    # Kconfig has AUDIO_DEBUG_UDP_SERVER `depends on USE_AUDIO_DEBUGGER`, so an
    # address without the enable is silently dropped and the enable without an
    # address streams nowhere. They are asserted as a pair because they only
    # work as one.
    check("neither half arrives alone",
          ("CONFIG_USE_AUDIO_DEBUGGER=y" in debug_on)
          == any(e.startswith("CONFIG_AUDIO_DEBUG_UDP_SERVER=") for e in debug_on))

    debug_off = appended(run(patcher, {"builds": [{"name": "stackchan",
                                                   "sdkconfig_append": list(debug_on)}]}, ARGS))
    check("building again without it takes it back off",
          not [e for e in debug_off if "AUDIO_DEBUG" in e],
          [e for e in debug_off if "AUDIO_DEBUG" in e])

    # 6. Switching the MultiNet model leaves ONE member of the choice.
    #
    # This is the switch that tests whether an invented wake word needs
    # MultiNet7's runtime grapheme-to-phoneme rather than MultiNet6. It is worth
    # a check of its own because the failure mode is silent: two members of one
    # Kconfig choice set at once is not an error, it is an ill-defined build,
    # and the device would come back running whichever the generator read last
    # -- which looks exactly like the model change having no effect.
    mn7 = appended(run(patcher, once, ARGS[:6] + ["CONFIG_SR_MN_EN_MULTINET7_QUANT"]))
    check("switching the MultiNet model leaves one selected",
          keys_matching(mn7, MULTINETS) == ["CONFIG_SR_MN_EN_MULTINET7_QUANT=y"],
          keys_matching(mn7, MULTINETS))

    # ... and switching back is symmetric, so a diagnostic build does not become
    # the permanent state of the tree by being harder to undo than to do.
    back = appended(run(patcher, {"builds": [{"name": "stackchan",
                                              "sdkconfig_append": list(mn7)}]}, ARGS))
    check("and switching back leaves one selected",
          keys_matching(back, MULTINETS) == ["CONFIG_SR_MN_EN_MULTINET6_QUANT=y"],
          keys_matching(back, MULTINETS))

    # 7. A config with no stackchan build is a loud failure, not a silent one.
    try:
        run(patcher, {"builds": [{"name": "other"}]}, ARGS)
    except SystemExit:
        check("a missing stackchan build aborts", True)
    else:
        check("a missing stackchan build aborts", False)

    print()
    if failures:
        print(f"FAIL -- {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("PASS -- the sdkconfig patcher keeps one option per choice")
    return 0


if __name__ == "__main__":
    sys.exit(main())
