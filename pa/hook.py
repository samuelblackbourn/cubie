"""Serve `STACKCHAN_AUDIO_HOOK_URL` -- the device's own captures, delivered.

The gateway has two listening paths and they are not equivalent. The `listen`
tool records for a window we choose and transcribes; that is what tap-to-talk
uses, and it always waits the full window whether or not the sentence finished.
The other path is the device's: wake word, button or LCD touch make the
firmware start listening on its own, and the gateway then buffers the Opus
frames, packs them into Ogg and POSTs them here. That path stops when the
speaker stops, which is the difference between a five-second pause and a
conversation.

Until this file existed the gateway logged `device-driven listen.start ignored
(STACKCHAN_AUDIO_HOOK_URL not configured)` and dropped every frame -- so the
wake word could wake him and nothing could come of it.

--- Why it answers 202 before doing the work ---

`push_audio_capture` gives the POST a **10 second total timeout**, and a turn
is transcription (~2.3 s with the `base` model) plus a model call plus speech.
Holding the connection open would make every *successful* turn appear in the
gateway's log as a failed push, and would tie up the gateway task for the whole
exchange. So the body is read, validated as far as its framing, accepted, and
handed to the character stack; the work happens after the socket has closed.

The consequence is honest and worth stating: **a 202 means "received", not
"understood"**. Nothing the receiver returns can report a failed transcription
or a refused brain call, so those are the character stack's to log and, where
they can be, to say out loud.

--- Why the stdlib, and a thread ---

One route, one method, one header to check. `aiohttp` is present in the
gateway's virtualenv, but importing it here would put this module out of reach
of `pa/.venv`, where the tests run -- and the tests are worth more than the
convenience. `http.server` in a thread costs one small bridge into the event
loop and buys a receiver that can be tested over a real socket with no robot,
no gateway and no model.

--- Why almost any path is accepted ---

The failure this file exists to remove is audio silently not arriving, and a
mismatch between the path in `STACKCHAN_AUDIO_HOOK_URL` and the path expected
here would be exactly that failure wearing a different hat. So a POST to any
path that is not a NAMED route is treated as a capture, and the requested path
is logged -- a typo is visible rather than fatal.

There is now one named route, `/preview`, and the fall-through is what keeps the
original property: a mistyped hook URL still delivers audio unless the typo
happens to be exactly `/preview`.

--- Why the preview lives here at all ---

Because this is the only authenticated HTTP surface the character stack has, and
because the decision it has to make is one only the character stack can make.
"Refuse a preview while he is mid-conversation" needs `Conversation.busy`, which
the office cannot see; an office that spawned the voice itself would bypass the
one thing that knows whether he is already talking, and two producers would
stream into the same capture endpoint.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import voice_settings

logger = logging.getLogger("cubie.hook")

#: Loopback, because the gateway runs on the same box as the character stack.
#: Binding wider would expose a speech endpoint to the LAN for no gain.
DEFAULT_HOST = "127.0.0.1"

#: One past the gateway's own three (8765 device, 8766 capture, 8767 MCP).
DEFAULT_PORT = 8768

#: A 15 second auto-stopped capture is about 30 kB at the device's bitrate, so
#: this is not a tight fit. It is sized for the *manual-stop* path instead --
#: an LCD touch records until touched again, with no ceiling in the firmware --
#: which at ~120 kB a minute is a little over half an hour. Past that the body
#: is refused unread rather than buffered.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024

#: The one named route. Everything else that POSTs here is a capture.
PREVIEW_PATH = "/preview"

#: What belongs in STACKCHAN_AUDIO_HOOK_URL when the defaults are used. Built
#: here rather than in two places, because a receiver listening on one URL
#: while the gateway posts to another is the silent no-audio failure again --
#: and `deploy/install-notify.sh` is tested against this exact string.
DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/audio"


@dataclass(frozen=True)
class Capture:
    """One device-driven listen window, as delivered."""

    body: bytes
    session_id: str


class _Handler(BaseHTTPRequestHandler):
    # Set by the server instance.
    receiver: "HookReceiver"

    protocol_version = "HTTP/1.1"
    server_version = "cubie-hook"
    sys_version = ""

    #: A sender that opens a connection and stalls would otherwise hold a
    #: thread for as long as the process lives, because
    #: `StreamRequestHandler.timeout` defaults to None -- checked, not assumed.
    #: The gateway's own POST gives up after 10 s, so anything still hanging
    #: about at 30 is not it.
    timeout = 30

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        receiver = self.receiver

        if not receiver.authorised(self.headers.get("Authorization")):
            # Deliberately terse: the gateway logs the first 200 characters of
            # a non-2xx body, and a reply that discussed the token would put
            # its shape in two logs.
            self._reply(401, "unauthorised")
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._reply(411, "Content-Length required")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._reply(400, "Content-Length is not a number")
            return
        if length < 0:
            self._reply(400, "negative Content-Length")
            return
        if length == 0:
            self._reply(400, "empty body")
            return
        if length > receiver.max_bytes:
            # Refused without reading: the point of a cap is not to buffer it.
            self._reply(413, f"body larger than {receiver.max_bytes} bytes")
            return

        body = self.rfile.read(length)
        if len(body) != length:
            self._reply(400, "body shorter than Content-Length")
            return

        # Named routes first; everything else is a capture. See the module
        # docstring for why the fall-through is the point rather than laziness.
        if self.path.split("?")[0].rstrip("/") == PREVIEW_PATH:
            self._preview(body)
            return

        session_id = self.headers.get("X-StackChan-Session", "") or ""
        logger.info(
            "capture: %d bytes path=%s session=%s",
            len(body), self.path, session_id or "(none)",
        )

        # Accept first. Everything after this point is the character stack's,
        # and it can take far longer than the gateway is willing to wait.
        self._reply(202, "accepted")
        try:
            receiver.on_capture(Capture(body=body, session_id=session_id))
        except Exception:  # noqa: BLE001 - a handoff must never kill the server
            logger.exception("capture handoff raised")

    def _preview(self, body: bytes) -> None:
        """Say a line with the given settings, or say why not.

        Answers synchronously, unlike a capture: the caller is a person waiting
        to hear something, and the only useful reply is whether it happened.
        A refusal has to arrive as a refusal rather than as silence, because
        silence is indistinguishable from the robot being unplugged.
        """
        receiver = self.receiver
        if receiver.on_preview is None:
            self._json(501, spoke=False,
                       detail="previews are not available: no voice on this stack")
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._json(400, spoke=False, detail=f"body is not JSON: {exc}")
            return
        if not isinstance(payload, dict):
            self._json(400, spoke=False, detail="body must be a JSON object")
            return

        settings, notes = voice_settings.coerce(payload.get("settings"))
        for note in notes:
            logger.info("preview settings: %s", note)

        try:
            ok, detail, busy = receiver.on_preview(settings)
        except Exception as exc:  # noqa: BLE001 - a preview is not worth the server
            logger.exception("preview raised")
            self._json(500, spoke=False, detail=f"the preview failed: {exc}", adjusted=notes)
            return

        # 409 only for "he is talking": the request was fine and the state was
        # wrong, which is the distinction the office API already makes for a
        # stale approval. A voice that FAILED is a 500 -- collapsing it into 409
        # would have the panel report "he is busy" when he is actually mute,
        # which is the one failure this whole file is careful about.
        if ok:
            status = 200
        elif busy:
            status = 409
        else:
            status = 500
        self._json(status, spoke=ok, detail=detail, adjusted=notes)

    def _json(self, status: int, **body) -> None:
        """Answer a preview. Always JSON, always the same keys.

        Every branch of `_preview` goes through here: the panel has one shape to
        parse and one place to read a reason from, whether it was refused,
        rejected or unavailable. The first version answered two branches with
        JSON labelled `text/plain` and six with bare prose.
        """
        body.setdefault("adjusted", [])
        self._reply(status, json.dumps(body), content_type="application/json")

    def do_GET(self) -> None:  # noqa: N802
        # Not part of the contract; answered so a human poking at the port
        # gets something other than a hang.
        self._reply(405, "POST an Ogg/Opus capture")

    def _reply(self, status: int, message: str, content_type: str = "text/plain") -> None:
        payload = message.encode("utf-8", "replace")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if status >= 300:
            # Every rejection above answers WITHOUT reading the body -- that is
            # the point of a size cap, and of not touching an unauthorised
            # request at all. On a keep-alive connection those unread bytes are
            # still queued, and the next read would parse them as a request
            # line. So a rejection ends the connection rather than leaving the
            # sender's body to be misread as its next request.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        # BaseHTTPRequestHandler writes to stderr by default, which under
        # systemd means the journal gets every line twice in two formats.
        logger.debug("http: " + fmt, *args)


class HookReceiver:
    """The webhook, on a thread of its own.

    `on_capture` is called **from the server thread** and must not block: from
    `live.py` it is a `call_soon_threadsafe` into the event loop.

    `on_preview` is the exception to that rule, deliberately -- see `_preview`. It is called
    after the response has been written, so raising cannot fail the POST -- it
    is caught and logged instead, because a handoff bug should cost one turn
    rather than the receiver.
    """

    def __init__(
        self,
        token: str,
        on_capture: Callable[[Capture], None],
        *,
        on_preview: "Callable[[voice_settings.VoiceSettings], tuple[bool, str, bool]] | None" = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if not token:
            # A startup refusal, not a warning. The gateway always sends a
            # bearer token -- STACKCHAN_AUDIO_HOOK_TOKEN, or STACKCHAN_TOKEN
            # as its own documented fallback -- so an empty token here means
            # our configuration is wrong, and the alternative to failing is an
            # endpoint that makes the robot speak for anyone who can reach it.
            raise ValueError(
                "the audio hook needs a token to check against; "
                "STACKCHAN_TOKEN is what the gateway signs with by default"
            )
        self._token = token
        self.on_capture = on_capture
        #: Called on the server thread for a `/preview` POST, and unlike
        #: `on_capture` it MAY block: the caller is a person waiting to hear
        #: something, and the reply is the answer. Returns
        #: (spoke, detail, was_busy) -- the third distinguishes "he is talking"
        #: from "the voice broke", which are 409 and 500 and must not be
        #: conflated: one is a state and the other is him being mute.
        #: None disables the route, which is what happens when there is no
        #: brain and so no voice to preview with.
        self.on_preview = on_preview
        self.max_bytes = max_bytes
        handler = type("_BoundHandler", (_Handler,), {"receiver": self})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """Where it actually bound -- port 0 resolves here, which tests use."""
        return self._server.server_address[:2]

    @property
    def url(self) -> str:
        """What to put in `STACKCHAN_AUDIO_HOOK_URL` for THIS receiver.

        Not `DEFAULT_URL`: port 0 is only resolved by binding, and the tests
        rely on asking a bound receiver where it actually is.
        """
        host, port = self.address
        return f"http://{host}:{port}/audio"

    def authorised(self, header: str | None) -> bool:
        """True when `header` carries our bearer token.

        Compared with `hmac.compare_digest` rather than `==`: the token is a
        64-character secret and an early-exit comparison leaks its prefix to
        anything that can time a request.
        """
        if not header:
            return False
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return False
        return hmac.compare_digest(presented.strip(), self._token)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            # A shorter poll interval than the 0.5 s default, because
            # `shutdown()` waits up to one interval and the tests stop and
            # start a receiver per case: at the default that was 8.6 s of the
            # green bar spent waiting on sockets, and a slow bar stops being
            # run. Ten wakeups a second on an otherwise idle thread is not a
            # cost worth keeping it for.
            target=lambda: self._server.serve_forever(poll_interval=0.1),
            name="cubie-hook",
            daemon=True,
        )
        self._thread.start()
        logger.info("audio hook listening on %s", self.url)

    def stop(self) -> None:
        # `shutdown()` waits on a flag that only `serve_forever` sets, so
        # calling it on a receiver that was never started blocks forever. That
        # is not a hypothetical ordering: `live.py` constructs the receiver and
        # starts it as two statements, and the reconnect loop's `finally` runs
        # whatever happened in between.
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()
