"""Reading the office, and acting on it. The companion API, as the brain needs it.

Ported from the `bridge/` prototype's `officeClient.ts` and `contract.ts`
(since removed -- see git history), and downstream of
`contract/companion-status.json` in exactly the same way -- if
`make check-contract` fails, the types here are the thing to fix, not a second
source of truth.

--- The four endpoints, and what their failures mean ---

    GET  /api/companion/status      the distilled office state
    POST /api/companion/approve     {id}          -> ok | 400 | 409
    POST /api/companion/deny        {id, reason?} -> ok | 400 | 409
    POST /api/companion/presence    {present}     -> ok | 400

**409 is not an error to retry, and not one to report as a failure.** The
office's own comment says why: a well-formed press that lost a race with the
wall panel is a STATE mismatch, and the right response is to re-poll rather
than to treat our own request as malformed. So it comes back as a distinct
outcome, because the assistant should say "that one's already been handled"
rather than "something went wrong".

**400 means we sent rubbish** -- an empty id, a non-string reason. That is a
bug here, not a state, and it says so.

--- Only pendingApprovals is actionable ---

The office has two approve/deny pairs, and sending the wrong one is a SILENT
NO-OP -- a lesson already paid for in the Passport client. The companion
endpoint deliberately exposes only the first, so this client cannot mis-fire.
`founderTasks` are read-only here for the same reason: they are things to
mention, not things to action.

--- What approve actually does ---

Carried into the brain's system prompt rather than left as folklore: the
office's `approveRequest` resumes by spawning a BRAND-NEW claude process, so no
mid-flow state survives and the grant covers the whole resumed turn rather than
the one call that was refused. An assistant that says "I've allowed that one
action" would be describing something the office does not do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: The office. LAN only, per the design note -- Cubie never takes a tunnel path.
DEFAULT_BASE_URL = "http://office-server.local:8420"

#: One read should not hold up a conversation. The bridge uses 4 s for its
#: poll; a person waiting for an answer is less patient than a beacon.
DEFAULT_TIMEOUT_S = 4.0

MOODS = ("calm", "busy", "attention")
TASK_STATUSES = ("blocked", "review")


class Outcome(Enum):
    """What came back, in the vocabulary the brain needs to speak.

    Four cases rather than a boolean, because they need saying differently:
    done, already handled by someone else, we sent rubbish, or we could not
    reach the office at all.
    """

    OK = "ok"
    #: 409: lost a race with the wall panel. Re-poll, and say so plainly.
    STALE = "stale"
    #: 400: a malformed request. Our bug.
    REJECTED = "rejected"
    #: Unreachable, 401, 5xx, or a body that did not parse.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class PendingApproval:
    id: str
    one_line: str


@dataclass(frozen=True)
class FounderTask:
    id: str
    title: str
    status: str


@dataclass(frozen=True)
class OfficeState:
    """The companion payload. Field names are ours; the wire is camelCase."""

    pending_approvals: tuple[PendingApproval, ...]
    pending_total: int
    agents_running: int
    needs_you: bool
    founder_tasks: tuple[FounderTask, ...]
    founder_tasks_total: int
    mood: str
    ts: int
    #: Absent when nothing has reported, `False` only when something looked and
    #: found nobody. Collapsing the two would make a dead sensor
    #: indistinguishable from an empty room.
    present: bool | None = None

    @property
    def approvals_truncated(self) -> int:
        """How many approvals exist beyond the ones we were sent.

        The list is capped at five and `pendingTotal` carries the truth. An
        assistant that read out five of nine as though that were everything
        would be telling its owner they are on top of things.
        """
        return max(0, self.pending_total - len(self.pending_approvals))

    @property
    def tasks_truncated(self) -> int:
        return max(0, self.founder_tasks_total - len(self.founder_tasks))


def _int(value: Any) -> int | None:
    # bool is an int in Python, and `True` is not a count.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_office_state(body: Any) -> OfficeState | None:
    """Narrow an unknown body to `OfficeState`, or None.

    Tolerant about EXTRA fields and strict about the ones we read -- the
    office's payload has already grown twice, and refusing to run because of a
    field we do not use would be a robot that stops reflecting the office for
    no reason. A field we DO read arriving with the wrong type is drift, and
    should surface as "cannot reach the office" rather than as a wrong answer.

    Stricter than the bridge's `contract.ts` was in one place, deliberately: that
    casts the two arrays without checking their elements, which is fine for
    driving a face. Here the ids go back to the office in an approve call and
    the text goes into a language model's prompt, so a malformed element is
    checked rather than cast.
    """
    if not isinstance(body, dict):
        return None

    pending_total = _int(body.get("pendingTotal"))
    agents_running = _int(body.get("agentsRunning"))
    tasks_total = _int(body.get("founderTasksTotal"))
    ts = _int(body.get("ts"))
    if None in (pending_total, agents_running, tasks_total, ts):
        return None
    if not isinstance(body.get("needsYou"), bool):
        return None
    mood = body.get("mood")
    if mood not in MOODS:
        return None

    raw_approvals = body.get("pendingApprovals")
    raw_tasks = body.get("founderTasks")
    if not isinstance(raw_approvals, list) or not isinstance(raw_tasks, list):
        return None

    approvals = []
    for item in raw_approvals:
        if not isinstance(item, dict):
            return None
        ident, one_line = item.get("id"), item.get("oneLine")
        if not isinstance(ident, str) or not ident or not isinstance(one_line, str):
            return None
        approvals.append(PendingApproval(ident, one_line))

    tasks = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            return None
        ident, title, status = item.get("id"), item.get("title"), item.get("status")
        if not isinstance(ident, str) or not ident:
            return None
        if not isinstance(title, str) or status not in TASK_STATUSES:
            return None
        tasks.append(FounderTask(ident, title, status))

    present = body.get("present")
    return OfficeState(
        pending_approvals=tuple(approvals),
        pending_total=pending_total,
        agents_running=agents_running,
        needs_you=body["needsYou"],
        founder_tasks=tuple(tasks),
        founder_tasks_total=tasks_total,
        mood=mood,
        ts=ts,
        present=present if isinstance(present, bool) else None,
    )


class OfficeClient:
    """One client, four calls, no retries.

    Retrying belongs to the caller: a failed read is just this moment's answer
    being "I don't know", and a retry inside would only delay that answer.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout_s

    async def read(self) -> tuple[OfficeState | None, str]:
        """The office's state, or None with a reason.

        The reason is for the log, not for the prompt -- unreachable, 401,
        malformed all mean the same thing to an assistant on a desk, which is
        that it does not know what the office is doing.
        """
        url = f"{self.base_url}/api/companion/status"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if response.status_code != 200:
            return None, f"http {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return None, "body was not JSON"
        state = parse_office_state(body)
        return (state, "ok") if state else (None, "payload did not match the contract")

    async def _act(self, path: str, payload: dict) -> tuple[Outcome, str]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            return Outcome.UNREACHABLE, f"{type(exc).__name__}: {exc}"

        if response.status_code == 200:
            return Outcome.OK, "ok"
        # 409 before the generic failure: it is a state mismatch, not an error,
        # and the office documents it as "re-poll rather than treat your own
        # request as malformed".
        if response.status_code == 409:
            return Outcome.STALE, _error_text(response) or "already handled"
        if response.status_code == 400:
            return Outcome.REJECTED, _error_text(response) or "malformed request"
        return Outcome.UNREACHABLE, f"http {response.status_code}"

    async def approve(self, approval_id: str) -> tuple[Outcome, str]:
        return await self._act("/api/companion/approve", {"id": approval_id})

    async def deny(self, approval_id: str, reason: str | None = None) -> tuple[Outcome, str]:
        payload: dict[str, Any] = {"id": approval_id}
        # Omitted rather than sent empty: the office trims and treats a blank
        # reason as absent, and sending "" would rely on that.
        if reason and reason.strip():
            payload["reason"] = reason.strip()
        return await self._act("/api/companion/deny", payload)

    async def set_presence(self, present: bool) -> tuple[Outcome, str]:
        """Report whether someone is at the desk.

        A boolean, and nothing that could carry a frame. The office rejects a
        non-boolean rather than coercing one, because the privacy property the
        design rests on is that inference happens on the device and nothing
        image-shaped is transmitted -- and that is only a property of the
        system if the server refuses anything else.
        """
        return await self._act("/api/companion/presence", {"present": bool(present)})


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""
