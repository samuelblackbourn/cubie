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
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hook as hook_mod  # noqa: E402

TOKEN = "t" * 64

#: From the module rather than spelled again here: a test that hardcoded the
#: path would still pass if the route were renamed out from under the office.
STATUS = hook_mod.STATUS_PATH


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

    def get(self, path="/", *, token=TOKEN):
        host, port = self.receiver.address
        request = urllib.request.Request(f"http://{host}:{port}{path}")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
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


def test_a_get_needs_the_token_too(served):
    """`do_POST` checks the token before it looks at the path and `do_GET` does
    the same, so no route on this port is readable without it. An unauthorised
    GET is still an ANSWER rather than a hang, which is all the 405 above was
    ever there to guarantee."""
    r = served()
    assert r.get(token=None)[0] == 401
    assert r.get(STATUS, token=None)[0] == 401
    assert r.get(STATUS, token="w" * 64)[0] == 401


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


# --- connection hygiene -----------------------------------------------------


def raw_post(address, *, body: bytes, token: str | None = TOKEN,
             content_length: int | None = None, keep_alive: bool = True) -> bytes:
    """Send a request over a bare socket and return the raw response.

    `urllib` sends `Connection: close` and hides the response framing, so it
    cannot see either of the properties below. The gateway uses `aiohttp`,
    which keeps connections alive by default -- so this is closer to the real
    client than the convenience wrapper is.
    """
    import socket

    length = len(body) if content_length is None else content_length
    lines = [b"POST /audio HTTP/1.1", b"Host: localhost"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}".encode())
    lines.append(f"Content-Length: {length}".encode())
    lines.append(b"Content-Type: audio/ogg")
    if keep_alive:
        lines.append(b"Connection: keep-alive")
    request = b"\r\n".join(lines) + b"\r\n\r\n" + body

    with socket.create_connection(address, timeout=5) as sock:
        sock.sendall(request)
        # Read exactly one response and stop. Reading to EOF would sit through
        # the socket timeout on every ACCEPTED capture, because a kept-alive
        # connection is the correct answer there -- and that turned the whole
        # file from under two seconds into seven.
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return data
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        declared = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                declared = int(line.split(b":", 1)[1])
        while len(rest) < declared:
            chunk = sock.recv(4096)
            if not chunk:
                break
            rest += chunk
    return head + b"\r\n\r\n" + rest


def test_a_rejection_closes_the_connection(served):
    """A rejection answers without reading the body, so those bytes are still
    queued on a keep-alive connection. Leaving it open would have the next read
    parse the sender's audio as a request line."""
    r = served(max_bytes=16)
    response = raw_post(r.receiver.address, body=b"z" * 512)
    assert b"413" in response.split(b"\r\n")[0]
    assert b"Connection: close" in response


def test_an_accepted_capture_does_not_need_the_connection_closed(served):
    """The body has been read in full by then, so the connection is reusable
    and there is no reason to make the gateway open another."""
    r = served()
    response = raw_post(r.receiver.address, body=b"ogg")
    assert b"202" in response.split(b"\r\n")[0]
    assert b"Connection: close" not in response


def test_a_sender_that_stalls_does_not_hold_a_thread_for_ever():
    """`StreamRequestHandler.timeout` is None by default, so a half-open
    request would keep its thread until the process died. One leaked thread per
    capture is a slow death rather than a visible fault."""
    assert hook_mod._Handler.timeout is not None
    assert hook_mod._Handler.timeout <= 60


# --- the preview route ------------------------------------------------------
#
# Routed by path, where a capture is not. The fall-through is the point: a
# mistyped STACKCHAN_AUDIO_HOOK_URL must still deliver audio, which was the
# reason the receiver accepted any path in the first place.


class Previewer(Receiver):
    """A receiver with a preview callback, passed through the CONSTRUCTOR.

    The first version assigned `receiver.on_preview` after construction, which
    meant `HookReceiver`'s own `on_preview` parameter -- the thing `live.py`
    actually uses -- was never exercised by any test.
    """

    def __init__(self, answer=(True, "spoke", False), **kwargs):
        self.previews = []
        self.answer = answer
        super().__init__(on_preview=self._on_preview, **kwargs)

    def _on_preview(self, settings):
        self.previews.append(settings)
        return self.answer

    def preview(self, body: bytes = b'{"settings": {"pitch": 1.2}}', token=TOKEN):
        return self.post(body, token=token, path="/preview")


def test_a_preview_asks_the_character_stack_to_speak(served):
    r = Previewer()
    try:
        status, text = r.preview()
        assert status == 200
        assert json.loads(text)["spoke"] is True
        assert len(r.previews) == 1
        assert r.previews[0].pitch == 1.2
    finally:
        r.close()


def test_a_preview_while_he_is_talking_is_refused_with_a_reason(served):
    """409 rather than 503: the request was fine and the state was wrong, which
    is the distinction the office API already makes for a stale approval."""
    r = Previewer(answer=(False, "he is mid-conversation", True))
    try:
        status, text = r.preview()
        assert status == 409
        assert "mid-conversation" in text
    finally:
        r.close()


def test_a_preview_clamps_and_says_what_it_changed(served):
    """Silently clamping is how a person decides the robot ignores them."""
    r = Previewer()
    try:
        status, text = r.preview(b'{"settings": {"robot": 9}}')
        assert status == 200
        body = json.loads(text)
        assert any("clamped" in note for note in body["adjusted"])
        assert r.previews[0].robot == 1.0
    finally:
        r.close()


def test_a_capture_still_works_on_a_path_that_is_not_the_preview(served):
    """The whole reason any path is accepted. A typo in the hook URL must not
    become the silent no-audio failure the receiver exists to remove."""
    r = served()
    assert r.post(b"ogg bytes", path="/audioo")[0] == 202
    assert r.arrived.wait(timeout=5)
    assert r.captures[0].body == b"ogg bytes"


def test_the_preview_path_is_not_treated_as_a_capture(served):
    r = served()
    status, _ = r.post(b'{"settings": {}}', path="/preview")
    assert status == 501, "no on_preview installed, so it must say so"
    assert not r.captures, "a preview must never be mistaken for audio"


def test_a_preview_with_no_voice_available_says_so_rather_than_failing_quietly(served):
    """`on_preview` is None when there is no brain, and therefore no voice."""
    r = served()
    status, text = r.post(b'{"settings": {}}', path="/preview")
    assert status == 501
    assert "not available" in text


def test_a_preview_body_that_is_not_json_is_a_400(served):
    r = Previewer()
    try:
        assert r.preview(b"pitch=1.2")[0] == 400
        assert not r.previews
    finally:
        r.close()


def test_a_preview_still_needs_the_token(served):
    r = Previewer()
    try:
        assert r.preview(token="w" * 64)[0] == 401
        assert not r.previews
    finally:
        r.close()


def test_the_query_string_does_not_stop_a_preview_being_routed(served):
    r = Previewer()
    try:
        assert r.post(b'{"settings": {}}', token=TOKEN, path="/preview?from=panel")[0] == 200
        assert len(r.previews) == 1
    finally:
        r.close()


def test_a_voice_failure_is_not_reported_as_him_being_busy():
    """409 and 500 are different answers to different questions. Collapsing
    them -- which the first version did -- has the panel say "he is busy" when
    he is actually mute, and mute is the failure this whole path guards."""
    r = Previewer(answer=(False, "the voice failed; see the log", False))
    try:
        status, text = r.preview()
        assert status == 500
        body = json.loads(text)
        assert body["spoke"] is False
        assert "voice failed" in body["detail"]
    finally:
        r.close()


def test_every_preview_answer_is_json_with_the_same_keys():
    """The panel has one shape to parse and one place to read a reason. Six of
    the eight branches used to answer bare prose, and the two that did answer
    JSON were labelled text/plain."""
    cases = []
    r = Previewer()
    try:
        cases.append(r.preview())                                    # 200
        cases.append(r.preview(b"not json"))                         # 400
        cases.append(r.preview(b'"a string"'))                       # 400
        cases.append(r.preview(token="w" * 64))                      # 401 -- not a preview reply
    finally:
        r.close()
    plain = served_preview_unavailable()
    cases.append(plain)

    for status, text in cases:
        if status == 401:
            continue  # auth is refused before the route is known; prose is right there
        body = json.loads(text)
        assert set(body) >= {"spoke", "detail", "adjusted"}, body
        assert body["spoke"] is (status == 200)


def served_preview_unavailable():
    r = Receiver()
    try:
        return r.post(b'{"settings": {}}', path="/preview")
    finally:
        r.close()


def test_a_preview_that_raises_answers_500_rather_than_dropping_the_connection():
    """An unhandled exception on the server thread closes the socket with no
    reply, which the panel cannot tell from the robot being unplugged."""
    class Exploding(Previewer):
        def _on_preview(self, settings):
            raise RuntimeError("the loop is closed")

    r = Exploding()
    try:
        status, text = r.preview()
        assert status == 500
        assert "the preview failed" in json.loads(text)["detail"]
        # And it is still serving.
        assert r.post(b"ogg", path="/audio")[0] == 202
    finally:
        r.close()


# --- the status route -------------------------------------------------------
#
# The read half. Everything above can make him speak; until these existed
# nothing could ask what he was doing, because every GET was a 405.


AWAKE_AND_IDLE = hook_mod.Status(
    awake=True, status="standby", face="idle", speech="",
    busy=False, turns=3, calls_failed=0, calls_dropped=0,
)


class Reporter(Receiver):
    """A receiver with a status callback, passed through the CONSTRUCTOR.

    Through the constructor for the reason `Previewer` records: assigning the
    attribute afterwards leaves `HookReceiver`'s own parameter -- the one
    `live.py` uses -- unexercised.
    """

    def __init__(self, answer=AWAKE_AND_IDLE, **kwargs):
        self.reads = 0
        self.answer = answer
        super().__init__(on_status=self._on_status, **kwargs)

    def _on_status(self):
        self.reads += 1
        return self.answer

    def status(self, path=STATUS, **kwargs):
        return self.get(path, **kwargs)


def test_status_reports_what_he_is_doing(served):
    r = Reporter()
    try:
        code, text = r.status()
        assert code == 200
        body = json.loads(text)
        assert body["awake"] is True
        assert body["status"] == "standby"
        assert body["face"] == "idle"
        assert body["busy"] is False
        assert body["turns"] == 3
        assert r.reads == 1
    finally:
        r.close()


def test_status_is_json_not_prose():
    """The office parses this. The preview had to learn the same lesson."""
    r = Reporter()
    try:
        host, port = r.receiver.address
        request = urllib.request.Request(f"http://{host}:{port}{STATUS}")
        request.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers.get_content_type() == "application/json"
            json.loads(response.read().decode())
    finally:
        r.close()


def test_what_cannot_be_observed_goes_out_as_null_not_as_a_default(served):
    """The whole point of the route. Without a brain there is no Conversation
    to ask, and answering `busy: false` would tell the office he is definitely
    free when nothing here is tracking whether he is."""
    r = Reporter(answer=hook_mod.Status(
        awake=True, status=None, face="idle", speech="",
        busy=None, turns=None,
    ))
    try:
        body = json.loads(r.status()[1])
        assert body["busy"] is None
        assert body["turns"] is None
        assert body["status"] is None, "no set_status yet is not the same as standby"
        assert body["device"]["calls_failed"] is None
        assert body["device"]["calls_dropped"] is None
        # Present and null, not absent: an absent key invites a guess.
        assert "busy" in body and "turns" in body
    finally:
        r.close()


def test_the_device_link_is_never_claimed(served):
    """The gateway owns the connection to the robot; this process is one of its
    clients and only learns about the device by being refused. A `true` here
    would be inferred from our own process being alive, which is not a reading
    of anything."""
    r = Reporter(answer=hook_mod.Status(
        awake=True, status="standby", face="idle", speech="",
        busy=False, turns=0, calls_failed=0, calls_dropped=0,
    ))
    try:
        body = json.loads(r.status()[1])
        assert body["device"]["connected"] is None, (
            "zero failed calls is not evidence the robot is plugged in"
        )
        # The counters beside it are what is actually known.
        assert body["device"]["calls_failed"] == 0
    finally:
        r.close()


def test_failing_calls_are_reported_so_a_dead_link_is_visible(served):
    r = Reporter(answer=hook_mod.Status(
        awake=True, status="standby", face="idle", speech="",
        busy=False, turns=0, calls_failed=812, calls_dropped=4,
    ))
    try:
        body = json.loads(r.status()[1])
        assert body["device"]["calls_failed"] == 812
        assert body["device"]["calls_dropped"] == 4
        assert body["device"]["connected"] is None, "still not a claim"
    finally:
        r.close()


def test_a_stack_with_nothing_to_report_says_so_rather_than_404(served):
    """501, the same answer the preview gives when there is no voice. A 404
    would read as "wrong URL" and send the caller hunting a typo."""
    r = served()
    code, text = r.get(STATUS)
    assert code == 501
    assert "not available" in json.loads(text)["detail"]


def test_a_status_that_raises_answers_500_rather_than_dropping_the_connection():
    """An unhandled exception on the server thread closes the socket with no
    reply, which the office cannot tell from the robot being unplugged."""
    class Exploding(Reporter):
        def _on_status(self):
            raise RuntimeError("the driver is gone")

    r = Exploding()
    try:
        code, text = r.status()
        assert code == 500
        assert "status read failed" in json.loads(text)["detail"]
        # And it is still serving.
        assert r.post(b"ogg", path="/audio")[0] == 202
    finally:
        r.close()


def test_the_query_string_does_not_stop_status_being_routed(served):
    r = Reporter()
    try:
        assert r.status(path=STATUS + "?t=1")[0] == 200
    finally:
        r.close()


def test_a_post_to_the_status_path_is_still_a_capture(served):
    """`/status` is a GET, so it must not take a path away from the capture
    fall-through -- the property that keeps a mistyped hook URL delivering
    audio instead of silently dropping it."""
    r = served()
    assert r.post(b"ogg bytes", path=STATUS)[0] == 202
    assert r.arrived.wait(timeout=5)
    assert r.captures[0].body == b"ogg bytes"


# --- the say route ----------------------------------------------------------
#
# `/preview` speaks PREVIEW_LINE and nothing else, so it is no use to anything
# that has something of its own to say. This is that route.


SAY = hook_mod.SAY_PATH


class Sayer(Receiver):
    """A receiver with a say callback, passed through the CONSTRUCTOR."""

    def __init__(self, answer=(True, "spoke", False), **kwargs):
        self.said = []
        self.answer = answer
        super().__init__(on_say=self._on_say, **kwargs)

    def _on_say(self, text, settings):
        self.said.append((text, settings))
        return self.answer

    def say(self, body=b'{"text": "the build is green"}', token=TOKEN):
        return self.post(body, token=token, path=SAY)


def test_a_say_speaks_the_callers_own_line(served):
    r = Sayer()
    try:
        code, text = r.say()
        assert code == 200
        assert json.loads(text)["spoke"] is True
        assert r.said[0][0] == "the build is green", "it must not say the sample line"
    finally:
        r.close()


def test_a_say_while_he_is_talking_is_refused_with_a_reason(served):
    """409, exactly as the preview does it: the request was fine and the state
    was wrong."""
    r = Sayer(answer=(False, "he is mid-conversation", True))
    try:
        code, text = r.say()
        assert code == 409
        assert "mid-conversation" in text
        assert json.loads(text)["spoke"] is False
    finally:
        r.close()


def test_a_voice_failure_on_say_is_not_reported_as_him_being_busy(served):
    """A broken voice makes him MUTE, not busy. Collapsing 500 into 409 has the
    office report an ordinary state for something nobody will go and look at."""
    r = Sayer(answer=(False, "the voice failed; see the log", False))
    try:
        code, text = r.say()
        assert code == 500
        assert "voice failed" in json.loads(text)["detail"]
    finally:
        r.close()


def test_a_say_with_no_voice_available_says_so_rather_than_failing_quietly(served):
    r = served()
    code, text = r.post(b'{"text": "hello"}', path=SAY)
    assert code == 501
    assert "not available" in json.loads(text)["detail"]


def test_a_say_with_no_usable_text_is_refused_before_anything_is_heard(served):
    r = Sayer()
    try:
        assert r.say(b'{"settings": {}}')[0] == 400
        assert r.say(b'{"text": 7}')[0] == 400
        assert r.say(b'{"text": "   "}')[0] == 400
        assert r.say(b"not json")[0] == 400
        assert r.say(b'"a string"')[0] == 400
        assert not r.said, "nothing may reach the voice from a rejected body"
    finally:
        r.close()


def test_a_line_over_the_cap_is_refused_rather_than_truncated(served):
    """Nothing can stop an utterance once it starts, so the cap is the only
    bound on how long the room is held. Truncating would have him break off
    mid-sentence with the caller told nothing."""
    r = Sayer()
    try:
        code, text = r.say(json.dumps({"text": "a" * (hook_mod.MAX_SAY_CHARS + 1)}).encode())
        assert code == 400
        assert str(hook_mod.MAX_SAY_CHARS) in json.loads(text)["detail"]
        assert not r.said, "a refused line must not be spoken at all"
        # And the cap itself is allowed.
        assert r.say(json.dumps({"text": "a" * hook_mod.MAX_SAY_CHARS}).encode())[0] == 200
    finally:
        r.close()


def test_a_say_carries_voice_settings_like_a_preview_does(served):
    r = Sayer()
    try:
        code, text = r.say(b'{"text": "hello", "settings": {"robot": 9}}')
        assert code == 200
        assert any("clamped" in note for note in json.loads(text)["adjusted"])
        assert r.said[0][1].robot == 1.0
    finally:
        r.close()


def test_a_say_still_needs_the_token(served):
    r = Sayer()
    try:
        assert r.say(token="w" * 64)[0] == 401
        assert not r.said
    finally:
        r.close()


def test_a_say_that_raises_answers_500_rather_than_dropping_the_connection():
    class Exploding(Sayer):
        def _on_say(self, text, settings):
            raise RuntimeError("the loop is closed")

    r = Exploding()
    try:
        code, text = r.say()
        assert code == 500
        assert "speaking failed" in json.loads(text)["detail"]
        # And it is still serving.
        assert r.post(b"ogg", path="/audio")[0] == 202
    finally:
        r.close()


def test_the_say_path_is_not_treated_as_a_capture(served):
    r = served()
    code, _ = r.post(b'{"text": "hello"}', path=SAY)
    assert code == 501, "no on_say installed, so it must say so"
    assert not r.captures, "a line to speak must never be mistaken for audio"


def test_the_query_string_does_not_stop_a_say_being_routed(served):
    r = Sayer()
    try:
        assert r.post(b'{"text": "hi"}', path=SAY + "?v=1")[0] == 200
    finally:
        r.close()
