"""The deploy unit's sandbox has to leave the speech model writable.

Not a unit test of any code. This asserts a property of a systemd file,
because that file broke the robot in a way nothing else could have caught.

--- What happened ---

`ProtectHome=read-only` is right: the process reads its interpreter and its
code from /home/sam and has no business writing there. But faster-whisper
loads through `huggingface_hub`, which revalidates the model against the Hub
on every load and writes back a tree cache and a commit hash -- even when the
weights are already local and unchanged. Under a read-only home that raises,
and the turn dies:

    ERROR cubie.live conversation turn failed: OSError(30, 'Read-only file system')

Nothing in that message says whisper, or model, or speech. The audio had
arrived (the receiver answered 202), the gateway had done everything right,
and the device had recorded and played its confirmation sound. It presented as
a robot that ignored you -- and cost a full evening of chasing the microphone,
the wake word, the sample rate, the recogniser and the LEDs before the real
line surfaced in a log nobody had yet thought to read at the right minute.

--- Why a test rather than a comment ---

The unit already carried a comment predicting this exact class of failure, and
naming ReadWritePaths as the narrow fix. It was marked NOT VERIFIED and it was
right, and being right in a comment did not stop it happening.

A comment cannot fail. This can: tighten the sandbox again, or move the cache,
and it says so before the robot goes quiet in a way that takes an evening to
read back.
"""

from __future__ import annotations

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
UNIT = HERE.parent.parent / "deploy" / "cubie-character.service"

#: Where huggingface_hub puts its cache for this unit's User, absent HF_HOME or
#: XDG_CACHE_HOME -- and the unit sets neither. Written out rather than derived
#: from the running user's home, because what matters is the path INSIDE the
#: deployed unit, not on whatever machine the tests happen to run on.
WHISPER_CACHE = "/home/sam/.cache/huggingface"


def directives(name: str) -> list[str]:
    """Every value of one directive, ignoring comments.

    Comments matter here: this file explains itself at length, and a naive
    substring search finds `ReadWritePaths` in the prose that recommends it
    just as happily as in the line that does it.
    """
    out = []
    for line in UNIT.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = re.match(rf"^{re.escape(name)}=(.*)$", stripped)
        if match:
            out.append(match.group(1).strip())
    return out


def test_the_unit_exists() -> None:
    assert UNIT.is_file(), f"no unit at {UNIT}"


def test_home_is_still_protected() -> None:
    """The fix must not have been "drop the sandbox".

    If this ever fails because ProtectHome was removed, the cache test below
    passes vacuously and the process gains write access to the whole of /home
    -- including the repo it is running from.
    """
    assert directives("ProtectHome") == ["read-only"], directives("ProtectHome")


def test_the_whisper_cache_is_writable() -> None:
    """The actual regression guard.

    huggingface_hub writes to its cache on every model load. Under
    ProtectHome=read-only that raises OSError(30) and every conversation turn
    fails -- with no mention of speech anywhere in the error.
    """
    paths = " ".join(directives("ReadWritePaths")).split()
    assert WHISPER_CACHE in paths, (
        f"{WHISPER_CACHE} is not in ReadWritePaths={paths!r} -- "
        "faster-whisper will raise OSError(30) on every turn"
    )


def test_the_failure_is_recorded_where_someone_will_look() -> None:
    """The error text itself has to be in the file.

    Anyone hitting this again will search for the message they can see, which
    is `OSError(30, 'Read-only file system')` and nothing more specific. If the
    unit does not carry that string, the next person searching the repo for it
    finds nothing and starts the evening over.
    """
    text = UNIT.read_text()
    assert "OSError(30" in text
    assert "faster-whisper" in text
