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

ARGS = ["http://192.168.0.84:8420/api/companion/ota", "LANGUAGE_EN_US",
        "hi cubie", "Cubie", "20"]


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
    out = appended(run(patcher, once, ARGS[:2] + ["hey cubie", "Cubie", "35"]))
    phrases = [e for e in out if e.startswith("CONFIG_CUSTOM_WAKE_WORD=")]
    check("one phrase, updated", phrases == ['CONFIG_CUSTOM_WAKE_WORD="hey cubie"'], phrases)
    check("the threshold followed it", "CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=35" in out)

    # 5. A config with no stackchan build is a loud failure, not a silent one.
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
