"""The serial recorder must not record nothing quietly.

Not a unit test of any code we run in-process. It asserts properties of a shell
script and a systemd unit, because the failure they exist to prevent is a
recorder that LOOKS like it is working.

--- what this is for ---

Cubie crashes. Rarely, unpredictably, and a crash prints its evidence exactly
once, on the serial console, at a moment nobody can predict. Every attempt to
catch it has needed a person sitting with `cat /dev/cu.usbmodem*` open on a
laptop, waiting -- and across several sessions that produced zero backtraces,
because he never crashed while anyone was watching.

The device sits on the same desk as the machine running everything else, so it
records itself into the journal instead.

--- why a silent failure is the one to guard ---

If the script cannot find the port and exits 0 having printed nothing, the
service is `active`, the journal is empty, and the natural reading is "he has
not crashed yet". The recorder would then be believed, which is worse than not
having one: the next crash passes uncaptured AND appears to have been watched.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
DEPLOY = HERE.parent.parent / "deploy"
SCRIPT = DEPLOY / "cubie-serial.sh"
UNIT = DEPLOY / "cubie-serial.service"


def unit_directives(name: str) -> list[str]:
    out = []
    for line in UNIT.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = re.match(rf"^{re.escape(name)}=(.*)$", stripped)
        if match:
            out.append(match.group(1).strip())
    return out


def test_both_halves_exist() -> None:
    assert SCRIPT.is_file(), SCRIPT
    assert UNIT.is_file(), UNIT


def test_the_script_is_executable() -> None:
    """systemd runs it directly, so the bit matters as much as the contents."""
    assert SCRIPT.stat().st_mode & 0o111, "cubie-serial.sh is not executable"


def test_a_missing_port_fails_loudly() -> None:
    """The guard this file exists for.

    Exiting 0 with no output would leave the service `active` and the journal
    empty, which reads as "he has not crashed yet" rather than "nothing is
    being recorded".
    """
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "CUBIE_SERIAL_PORT": "/dev/definitely-not-here"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "a missing port must not look like success"
    assert "no serial device found" in result.stderr


def test_a_named_port_is_used_rather_than_searched_for() -> None:
    """CUBIE_SERIAL_PORT has to win, so a second serial device on the desk
    cannot silently become the thing being recorded."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "CUBIE_SERIAL_PORT": "/dev/null"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "recording /dev/null" in result.stderr


def test_the_port_is_found_by_id_not_by_number() -> None:
    """/dev/ttyACM0 is assigned in enumeration order, so a reset, a reboot or
    another serial device can move it -- and the recorder would then be reading
    something else entirely, or nothing."""
    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "/dev/serial/by-id" in code
    # In the CODE, not the comments -- the comment above the lookup names
    # /dev/ttyACM0 precisely to say why it is not used, and a test that cannot
    # tell those apart would forbid explaining the decision.
    assert "/dev/ttyACM0" not in code


def test_it_restarts_because_a_reset_is_the_moment_worth_catching() -> None:
    """CoreS3 speaks USB-CDC from the ESP32 itself: a reboot makes the USB
    device vanish and reappear, so `cat` exits at exactly the moment worth
    recording. Restart is the design, not error handling -- without it the
    recorder stops permanently the first time he reboots, which is the first
    time it was needed."""
    assert unit_directives("Restart") == ["always"], unit_directives("Restart")


def test_it_never_gives_up_overnight() -> None:
    """systemd's default start-rate limit stops a unit after a few rapid
    restarts. A robot unplugged at 6pm would leave the recorder dead by 6:01
    and the crash at 2am uncaptured."""
    assert unit_directives("StartLimitIntervalSec") == ["0"]


def test_it_writes_to_the_journal_rather_than_its_own_file() -> None:
    """The point is the SHARED clock: a crash is only diagnosable next to the
    turn that preceded it, and `journalctl -u cubie-serial -u cubie-character`
    interleaves them. A private log file cannot be read that way."""
    text = UNIT.read_text()
    for redirect in ("StandardOutput=file:", "StandardOutput=append:"):
        assert redirect not in text, f"{redirect} takes it out of the journal"


def test_it_does_not_run_as_root() -> None:
    """Its entire job is to READ one character device."""
    assert unit_directives("User") == ["sam"], unit_directives("User")
    assert "dialout" in " ".join(unit_directives("SupplementaryGroups"))
