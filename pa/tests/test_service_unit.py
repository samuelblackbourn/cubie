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

#: The systemd-managed cache the unit points HF_HOME at. `CacheDirectory=` makes
#: systemd create /var/cache/<name> owned by the unit's User before the process
#: starts, so unlike a path under $HOME there is nothing to pre-create and
#: nothing to get wrong on a fresh machine.
CACHE_DIRECTORY = "cubie-character"
HF_HOME = "/var/cache/cubie-character"


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

    huggingface_hub writes to its cache on every model load, and creates it if
    it is not there. Under ProtectHome=read-only with nowhere else to go, that
    raises OSError(30) and every conversation turn fails -- with no mention of
    speech anywhere in the error.
    """
    assert CACHE_DIRECTORY in directives("CacheDirectory"), (
        f"CacheDirectory={CACHE_DIRECTORY} is missing -- faster-whisper has "
        "nowhere writable and will raise OSError(30) on every turn"
    )


def test_the_cache_is_where_huggingface_will_look() -> None:
    """The two halves only work together.

    CacheDirectory without HF_HOME creates a directory nothing uses, and the
    model still lands under $HOME. HF_HOME without CacheDirectory names a path
    that may not exist -- and that is not a quiet failure: ReadWritePaths= or a
    missing directory makes systemd refuse to start the unit with
    `status=226/NAMESPACE`, five times, and then give up. The robot goes from
    mute to off.
    """
    environment = directives("Environment")
    assert any(e == f"HF_HOME={HF_HOME}" for e in environment), (
        f"HF_HOME={HF_HOME} is not set -- huggingface_hub will use $HOME and "
        "the CacheDirectory will sit empty"
    )


def test_the_cache_is_not_granted_through_the_home_directory_again() -> None:
    """The first attempt at the whisper fix, which was worse than the bug.

    `ReadWritePaths=/home/sam/.cache/huggingface` requires the path to already
    exist. It did not -- this user never had a Hugging Face cache, because the
    gateway keeps its own under its StateDirectory -- and systemd refused to
    start the unit at all (status=226/NAMESPACE).

    THE RULE IS NOT "nothing under /home". This test said that for a while and
    it was over-general: it would have blocked
    `ReadWritePaths=/home/sam/.claude`, which is correct and necessary, because
    that directory certainly exists -- it is where the CLI keeps the
    credentials, so nothing works at all without it.

    The real rule is that a ReadWritePaths path must be one the deploy host is
    CERTAIN to have, and a test cannot check that. So this pins the specific
    path that actually broke, and the docstring carries the reasoning for the
    next one.
    """
    paths = " ".join(directives("ReadWritePaths")).split()
    assert "/home/sam/.cache/huggingface" not in paths, (
        "the whisper cache is granted with CacheDirectory, which systemd "
        "creates; ReadWritePaths would refuse to start the unit when the path "
        "does not exist yet"
    )


def test_the_cli_brain_can_save_a_conversation() -> None:
    """Memory is `--resume`, and a session has to be saved to be resumed.

    That write goes to ~/.claude/projects, which ProtectHome=read-only forbids.
    Without this the very first turn after enabling memory fails, and it fails
    as `brain failed:` with no mention of permissions anywhere -- the same
    shape as the whisper cache bug, which took an evening.
    """
    paths = " ".join(directives("ReadWritePaths")).split()
    assert "/home/sam/.claude" in paths, (
        "the CLI brain cannot persist a session, so --resume has nothing to "
        f"resume: ReadWritePaths={paths!r}"
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
    # And the second failure, for the same reason: someone hitting a unit that
    # will not start sees only this code.
    assert "226/NAMESPACE" in text
