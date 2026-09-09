"""Tests for the webhook, driven over a real socket.

The receiver is stdlib-only precisely so this can happen: every test below
binds a port, sends a genuine HTTP request through `urllib`, and reads a
genuine response. No mock of `BaseHTTPRequestHandler`, so the things that
actually break -- a missing Content-Length, a header case, a response written
before the work starts -- are exercised rather than modelled.

Port 0 throughout, so the tests never collide with the deployed receiver or
with each other.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hook as hook_mod  # noqa: E402

TOKEN = "t" * 64


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """Wait for something the SERVER THREAD does.

    Needed because the receiver answers 202 before running the handoff -- that
    is the point of it -- so a POST returning is not evidence that the capture
    has been handed on yet. Asserting straight after the POST is a race, and
    it is the race the design creates deliberately.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class Receiver:
    """A started receiver plus the captures it handed on."""

    def __init__(self, token: str = TOKEN, **kwargs):
        self.captures: list[hook_mod.Capture] = []
        self.arrived = threading.Event()
        self.receiver = hook_mod.HookReceiver(
            token, self._on_capture, host="127.0.0.1", port=0, **kwargs
        )
        self.receiver.start()

    def _on_capture(self, capture):
        self.captures.append(capture)
        self.arrived.set()

    def post(self, body=b"body", *, token=TOKEN, headers=None, path="/audio",
             content_length=None):
        host, port = self.receiver.address
        request = urllib.request.Request(
            f"http://{host}:{port}{path}", data=body, method="POST"
        )
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "audio/ogg")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if content_length is not None:
            request.add_header("Content-Length", str(content_length))
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def get(self):
        host, port = self.receiver.address
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def close(self):
        self.receiver.stop()


@pytest.fixture
def served():
    made: list[Receiver] = []

    def make(**kwargs):
        r = Receiver(**kwargs)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


# --- the happy path ---------------------------------------------------------


def test_a_capture_is_accepted_and_handed_on(served):
    r = served()
    status, text = r.post(b"ogg bytes here", headers={"X-StackChan-Session": "abc123"})
    assert status == 202
    assert "accepted" in text
    assert r.arrived.wait(timeout=5)
    assert len(r.captures) == 1
    assert r.captures[0].body == b"ogg bytes here"
    assert r.captures[0].session_id == "abc123"


def test_a_missing_session_header_is_an_empty_string_not_a_crash(served):
    """The gateway sends `X-StackChan-Session` with an empty value when the
    device has no session id, and `push_audio_capture` sets the header
    unconditionally -- so the absent case has to be as ordinary as the present
    one."""
    r = served()
    assert r.post(b"x")[0] == 202
    assert r.arrived.wait(timeout=5)
    assert r.captures[0].session_id == ""


def test_any_path_is_accepted(served):
    """Deliberate. A mismatch between the path in STACKCHAN_AUDIO_HOOK_URL and
    the path expected here would be the silent no-audio failure this whole file
    exists to remove."""
    r = served()
    assert r.post(b"x", path="/somewhere/else")[0] == 202


def test_the_response_does_not_wait_for_the_work(served):
    """The gateway gives the POST 10 seconds and a turn takes longer, so a 202
    has to mean "received", not "answered". If the handler ever awaited the
    handoff, this would take a second and a half."""
    started = threading.Event()

    class Slow(Receiver):
        def _on_capture(self, capture):
            started.set()
            time.sleep(1.5)
            super()._on_capture(capture)

    r = Slow()
    try:
        before = time.monotonic()
        status, _ = r.post(b"x")
        elapsed = time.monotonic() - before
        assert status == 202
        assert started.wait(timeout=5), "the handoff never ran"
        assert elapsed < 1.0, f"the response waited for the work ({elapsed:.2f}s)"
    finally:
        r.close()


# --- auth -------------------------------------------------------------------


def test_no_authorization_header_is_rejected(served):
    r = served()
    status, _ = r.post(b"x", token=None)
    assert status == 401
    assert not r.captures


def test_the_wrong_token_is_rejected(served):
    r = served()
    assert r.post(b"x", token="w" * 64)[0] == 401
    assert not r.captures


def test_a_token_of_the_right_length_but_wrong_content_is_rejected(served):
    """`hmac.compare_digest` is used for the timing property, but it must still
    actually compare."""
    r = served()
    assert r.post(b"x", token="t" * 63 + "u")[0] == 401


def test_the_bearer_scheme_is_matched_case_insensitively(served):
    """Nothing in HTTP promises the case of an auth scheme, and the cost of
    being strict is a 401 nobody can explain."""
    r = served()
    host, port = r.receiver.address
    request = urllib.request.Request(f"http://{host}:{port}/audio", data=b"x", method="POST")
    request.add_header("Authorization", f"BEARER {TOKEN}")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 202


def test_a_bare_token_with_no_scheme_is_rejected(served):
    r = served()
    r_host, r_port = r.receiver.address
    request = urllib.request.Request(
        f"http://{r_host}:{r_port}/audio", data=b"x", method="POST"
    )
    request.add_header("Authorization", TOKEN)
    try:
        urllib.request.urlopen(request, timeout=5)
        pytest.fail("a token with no Bearer scheme was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_the_reply_to_a_bad_token_says_nothing_about_the_token(served):
    """The gateway logs the first 200 characters of a non-2xx body. A reply
    that discussed the token would put its shape in a second log."""
    r = served()
    _, text = r.post(b"x", token="w" * 64)
    assert "w" * 8 not in text
    assert TOKEN[:8] not in text


def test_starting_without_a_token_is_refused(served):
    """A receiver with no auth makes the robot speak for anyone who can reach
    the port. The gateway always sends a bearer token, so an empty one here
    means our own configuration is wrong."""
    with pytest.raises(ValueError, match="token"):
        hook_mod.HookReceiver("", lambda c: None, host="127.0.0.1", port=0)


# --- bodies -----------------------------------------------------------------


def test_an_empty_body_is_rejected(served):
    r = served()
    status, text = r.post(b"")
    assert status == 400
    assert "empty" in text
    assert not r.captures


def test_a_body_over_the_cap_is_rejected_unread(served):
    r = served(max_bytes=1024)
    status, text = r.post(b"z" * 4096)
    assert status == 413
    assert "1024" in text
    assert not r.captures


def test_a_body_exactly_at_the_cap_is_accepted(served):
    """Off-by-one in a size cap means a capture at the boundary is silently
    dropped, which looks like the device not reporting."""
    r = served(max_bytes=64)
    assert r.post(b"z" * 64)[0] == 202


def test_a_get_is_answered_rather_than_hanging(served):
    r = served()
    status, text = r.get()
    assert status == 405
    assert "Ogg" in text


# --- the handoff ------------------------------------------------------------


def test_a_raising_handoff_costs_one_turn_not_the_receiver(served):
    """`on_capture` is `call_soon_threadsafe` in `live.py`, which can raise if
    the loop is closing. That must not take the server down with it."""
    calls = []

    class Raiser(Receiver):
        def _on_capture(self, capture):
            calls.append(capture)
            raise RuntimeError("loop is closed")

    r = Raiser()
    try:
        assert r.post(b"first")[0] == 202
        # Still serving.
        assert r.post(b"second")[0] == 202
        assert wait_for(lambda: len(calls) == 2), f"only {len(calls)} handoffs ran"
    finally:
        r.close()


def test_the_url_it_reports_is_the_one_it_bound(served):
    """What goes into STACKCHAN_AUDIO_HOOK_URL. Port 0 means the value is only
    knowable after binding, and a wrong one here is a silent no-audio."""
    r = served()
    host, port = r.receiver.address
    assert r.receiver.url == f"http://{host}:{port}/audio"
    assert port != 0


def test_stop_releases_the_port():
    """The reconnect loop in `live.py` re-enters `run_once`, which binds again.
    A leaked socket would turn a gateway restart into a dead wake word."""
    first = Receiver()
    host, port = first.receiver.address
    first.close()
    again = hook_mod.HookReceiver(TOKEN, lambda c: None, host=host, port=port)
    try:
        assert again.address[1] == port
    finally:
        again.stop()


def test_stop_is_safe_before_start():
    r = hook_mod.HookReceiver(TOKEN, lambda c: None, host="127.0.0.1", port=0)
    r.stop()


def test_the_default_port_is_not_one_the_gateway_already_uses():
    """8765 device, 8766 capture, 8767 MCP. Binding one of those would break
    the gateway rather than the hook, and the symptom would be somewhere else
    entirely."""
    assert hook_mod.DEFAULT_PORT not in (8765, 8766, 8767)


def test_it_binds_loopback_by_default():
    """A speech endpoint on the LAN is not something to arrive at by accident."""
    assert hook_mod.DEFAULT_HOST == "127.0.0.1"


# --- the installer and the receiver have to agree ---------------------------


def installer() -> str:
    return (Path(hook_mod.__file__).resolve().parent.parent
            / "deploy" / "install-notify.sh").read_text()


def test_the_installer_posts_to_the_url_the_receiver_serves():
    """Three files have to agree on one URL: this receiver's defaults, the
    string the installer writes into the gateway's env, and (at runtime) the
    gateway's own STACKCHAN_AUDIO_HOOK_URL. A mismatch drops every capture
    with no error on either side -- the gateway logs a connection refusal at
    most, and the robot simply never answers."""
    import re

    match = re.search(r'^HOOK_URL="\$\{HOOK_URL:-([^}]+)\}"', installer(), re.M)
    assert match, "could not find HOOK_URL in install-notify.sh"
    assert match.group(1) == hook_mod.DEFAULT_URL


def test_the_default_url_is_built_from_the_default_host_and_port():
    assert hook_mod.DEFAULT_URL == (
        f"http://{hook_mod.DEFAULT_HOST}:{hook_mod.DEFAULT_PORT}/audio"
    )


def test_the_installer_sets_no_hook_token():
    """Deliberately. The gateway signs with STACKCHAN_AUDIO_HOOK_TOKEN when set
    and falls back to STACKCHAN_TOKEN, which both ends already have -- so
    setting one would create a second secret to keep in step for no gain, and
    a mismatched pair would look exactly like an unauthorised sender."""
    assert "STACKCHAN_AUDIO_HOOK_TOKEN=" not in installer()
