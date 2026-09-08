"""Tests for the companion API client.

The parser is where a wrong answer becomes a confident one, so most of these
are about refusing bad input rather than accepting good input.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import office  # noqa: E402
from office import Outcome  # noqa: E402

GOOD = {
    "pendingApprovals": [{"id": "c1", "oneLine": "deploy to prod?"}],
    "pendingTotal": 3,
    "agentsRunning": 2,
    "needsYou": True,
    "founderTasks": [{"id": "t1", "title": "sign off", "status": "review"}],
    "founderTasksTotal": 4,
    "mood": "attention",
    "ts": 1787173749556,
}


def test_the_live_office_payload_parses():
    state = office.parse_office_state(GOOD)
    assert state is not None
    assert state.mood == "attention"
    assert state.pending_approvals[0].id == "c1"
    assert state.founder_tasks[0].status == "review"


def test_ts_is_epoch_milliseconds_and_survives_it():
    """The bug the K10's whole test suite missed: ts is ~1.8e12, and reading it
    into a 32-bit type saturated on the device while every host test passed."""
    state = office.parse_office_state(GOOD)
    assert state.ts == 1787173749556
    assert state.ts > 2**31


def test_truncation_is_reported_not_hidden():
    """The list is capped at five and pendingTotal carries the truth. An
    assistant reading out five of nine as though that were everything would be
    telling its owner they are on top of things."""
    state = office.parse_office_state(GOOD)
    assert state.approvals_truncated == 2
    assert state.tasks_truncated == 3


def test_present_absent_is_not_present_false():
    """Absent means nothing has reported; false means something looked and
    found nobody. They route differently."""
    assert office.parse_office_state(GOOD).present is None
    assert office.parse_office_state({**GOOD, "present": False}).present is False
    assert office.parse_office_state({**GOOD, "present": True}).present is True


def test_a_non_boolean_present_is_treated_as_unknown_not_coerced():
    assert office.parse_office_state({**GOOD, "present": "yes"}).present is None


def test_extra_fields_are_tolerated():
    """The office's payload has already grown twice. Refusing to run over a
    field we do not read would be a robot that stops working for no reason."""
    assert office.parse_office_state({**GOOD, "somethingNew": [1, 2, 3]}) is not None


def test_a_field_we_read_with_the_wrong_type_is_drift_and_refused():
    for bad in (
        {"mood": "frantic"},
        {"needsYou": "yes"},
        {"pendingTotal": "3"},
        {"ts": None},
        {"pendingApprovals": {}},
        {"founderTasks": "none"},
    ):
        assert office.parse_office_state({**GOOD, **bad}) is None, bad


def test_booleans_are_not_accepted_as_counts():
    """bool is an int in Python, and True is not a number of approvals."""
    assert office.parse_office_state({**GOOD, "agentsRunning": True}) is None


def test_a_malformed_list_element_is_refused_rather_than_cast():
    """Stricter than the bridge's parser on purpose: these ids go back to the
    office in an approve call and the text goes into a model's prompt."""
    for bad in (
        [{"id": "", "oneLine": "x"}],
        [{"id": "c1"}],
        [{"id": 42, "oneLine": "x"}],
        ["not an object"],
    ):
        assert office.parse_office_state({**GOOD, "pendingApprovals": bad}) is None, bad


def test_a_task_with_an_unknown_status_is_refused():
    """blocked and review are different actions, not shades of one. A third
    value is drift, not a task to guess about."""
    bad = [{"id": "t1", "title": "x", "status": "done"}]
    assert office.parse_office_state({**GOOD, "founderTasks": bad}) is None


def test_a_non_object_body_is_refused():
    for bad in (None, [], "ok", 7):
        assert office.parse_office_state(bad) is None


# ------------------------------------------------------------- the client --
class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def client_with(monkey_response, recorder=None):
    """An OfficeClient whose HTTP layer is a stub."""
    import httpx

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            if recorder is not None:
                recorder.append(("GET", url, None))
            return monkey_response

        async def post(self, url, json=None, headers=None):
            if recorder is not None:
                recorder.append(("POST", url, json))
            return monkey_response

    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    return original


def test_a_409_is_a_state_mismatch_not_a_failure():
    """The office's own comment: a well-formed press that lost a race with the
    wall panel is a STATE mismatch, and the device should re-poll rather than
    treat its own request as malformed."""
    import asyncio

    import httpx

    original = client_with(FakeResponse(409, {"error": "not awaiting approval"}))
    try:
        outcome, detail = asyncio.run(office.OfficeClient(token="t").approve("c1"))
    finally:
        httpx.AsyncClient = original
    assert outcome is Outcome.STALE
    assert "awaiting" in detail


def test_a_400_is_our_bug_and_says_so():
    import asyncio

    import httpx

    original = client_with(FakeResponse(400, {"error": "id must be a non-empty string"}))
    try:
        outcome, _ = asyncio.run(office.OfficeClient(token="t").approve("c1"))
    finally:
        httpx.AsyncClient = original
    assert outcome is Outcome.REJECTED


def test_a_401_reads_as_unreachable_because_that_is_what_it_means_here():
    import asyncio

    import httpx

    original = client_with(FakeResponse(401, {"error": "unauthorized"}))
    try:
        outcome, _ = asyncio.run(office.OfficeClient(token="t").approve("c1"))
    finally:
        httpx.AsyncClient = original
    assert outcome is Outcome.UNREACHABLE


def test_a_blank_deny_reason_is_omitted_rather_than_sent_empty():
    """The office trims and treats a blank reason as absent; sending "" would
    rely on that rather than saying it."""
    import asyncio

    import httpx

    sent: list = []
    original = client_with(FakeResponse(200, {"ok": True}), recorder=sent)
    try:
        asyncio.run(office.OfficeClient(token="t").deny("c1", "   "))
        asyncio.run(office.OfficeClient(token="t").deny("c1", "too risky"))
    finally:
        httpx.AsyncClient = original
    assert sent[0][2] == {"id": "c1"}
    assert sent[1][2] == {"id": "c1", "reason": "too risky"}


def test_presence_sends_a_boolean_and_nothing_else():
    """The privacy property rests on nothing image-shaped being transmitted,
    and the office rejects a non-boolean rather than coercing one."""
    import asyncio

    import httpx

    sent: list = []
    original = client_with(FakeResponse(200, {"present": True}), recorder=sent)
    try:
        asyncio.run(office.OfficeClient(token="t").set_presence(1))
    finally:
        httpx.AsyncClient = original
    assert sent[0][2] == {"present": True}
    assert isinstance(sent[0][2]["present"], bool)


def test_a_body_that_is_not_json_is_a_failed_read_not_a_partial_one():
    import asyncio

    import httpx

    original = client_with(FakeResponse(200, None))
    try:
        state, reason = asyncio.run(office.OfficeClient(token="t").read())
    finally:
        httpx.AsyncClient = original
    assert state is None and "JSON" in reason
