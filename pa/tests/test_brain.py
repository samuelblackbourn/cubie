"""Tests for the brain, with a scripted model and no network.

The tool loop and the guardrails are what matter here: this thing can approve
work in a real office, and the office's approve is not reversible from Cubie.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain  # noqa: E402
import office  # noqa: E402
from office import Outcome  # noqa: E402

STATE = office.parse_office_state(
    {
        "pendingApprovals": [
            {"id": "c1", "oneLine": "deploy the API to prod"},
            {"id": "c2", "oneLine": "delete the staging database"},
        ],
        "pendingTotal": 2,
        "agentsRunning": 1,
        "needsYou": True,
        "founderTasks": [{"id": "t1", "title": "sign off the invoice", "status": "review"}],
        "founderTasksTotal": 1,
        "mood": "attention",
        "ts": 1787173749556,
    }
)


class FakeOffice:
    """Records what was actioned, and can be told to fail."""

    def __init__(self, outcome=Outcome.OK, detail="ok"):
        self.outcome = outcome
        self.detail = detail
        self.calls: list[tuple] = []

    async def approve(self, ident):
        self.calls.append(("approve", ident))
        return self.outcome, self.detail

    async def deny(self, ident, reason=None):
        self.calls.append(("deny", ident, reason))
        return self.outcome, self.detail

    async def set_presence(self, present):
        self.calls.append(("presence", present))
        return self.outcome, self.detail


def scripted(*responses):
    """A transport that returns each response in turn, recording the requests."""
    sent: list[dict] = []
    remaining = list(responses)

    async def transport(body):
        sent.append(body)
        return remaining.pop(0) if remaining else {"content": [{"type": "text", "text": "ok"}]}

    transport.sent = sent  # type: ignore[attr-defined]
    return transport


def text(message):
    return {"content": [{"type": "text", "text": message}]}


def tool(name, arguments, call_id="u1"):
    return {"content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}]}


def run(b, transcript="hello", state=STATE):
    return asyncio.run(b.respond(transcript, state))


# ----------------------------------------------------------- the prompt --
def test_the_prompt_carries_the_office_state_and_the_ids():
    prompt = brain.build_system_prompt(STATE)
    assert "deploy the API to prod" in prompt
    assert "id c1" in prompt
    assert "sign off the invoice" in prompt


def test_the_prompt_says_the_office_is_unreachable_rather_than_omitting_it():
    """Silence would let the model answer confidently from nothing."""
    prompt = brain.build_system_prompt(None)
    assert "unreachable" in prompt
    assert "do not guess" in prompt


def test_the_prompt_states_what_approving_actually_does():
    """It resumes an agent as a brand-new process, so it grants the whole next
    turn rather than the one refused action. An assistant that said "I allowed
    that one action" would be describing something the office does not do."""
    assert "whole next turn" in brain.build_system_prompt(STATE)


def test_the_prompt_forbids_acting_without_being_asked():
    prompt = brain.build_system_prompt(STATE)
    assert "ONLY when told to" in prompt
    assert "not reversible" in prompt


def test_the_prompt_says_the_lists_may_be_truncated():
    assert "at most five" in brain.build_system_prompt(STATE)


def test_speech_guidance_rules_out_what_cannot_be_spoken():
    prompt = brain.build_system_prompt(STATE)
    for banned in ("markdown", "emoji", "lists"):
        assert banned in prompt


# ------------------------------------------------------------ the loop --
def test_a_plain_answer_comes_straight_back():
    b = brain.Brain("k", FakeOffice(), transport=scripted(text("Two things are waiting.")))
    reply = run(b)
    assert reply.speech == "Two things are waiting."
    assert reply.actions == ()
    assert not reply.incomplete


def test_a_tool_call_is_executed_and_the_result_fed_back():
    fake = FakeOffice()
    transport = scripted(tool("approve_request", {"id": "c1"}), text("Approved."))
    b = brain.Brain("k", fake, transport=transport)
    reply = run(b, "approve the deploy")

    assert fake.calls == [("approve", "c1")]
    assert reply.speech == "Approved."
    assert reply.actions[0].name == "approve_request"
    assert reply.actions[0].outcome == "ok"

    # The result went back as a tool_result the model could read.
    second = transport.sent[1]["messages"][-1]["content"][0]
    assert second["type"] == "tool_result"
    assert second["tool_use_id"] == "u1"


def test_a_stale_approval_is_reported_to_the_model_as_already_handled():
    """Not as a failure, and explicitly not to be retried -- the office
    documents 409 as a state mismatch."""
    fake = FakeOffice(Outcome.STALE, "not awaiting approval")
    transport = scripted(tool("approve_request", {"id": "c1"}), text("That was already done."))
    reply = run(brain.Brain("k", fake, transport=transport), "approve it")

    fed_back = transport.sent[1]["messages"][-1]["content"][0]["content"]
    assert "already handled" in fed_back
    assert "do not retry" in fed_back.lower()
    assert reply.actions[0].outcome == "stale"


def test_an_unreachable_office_is_reported_as_nothing_having_happened():
    fake = FakeOffice(Outcome.UNREACHABLE, "connect failed")
    transport = scripted(tool("approve_request", {"id": "c1"}), text("I couldn't reach it."))
    run(brain.Brain("k", fake, transport=transport), "approve it")
    fed_back = transport.sent[1]["messages"][-1]["content"][0]["content"]
    assert "nothing happened" in fed_back


def test_deny_passes_the_reason_through():
    fake = FakeOffice()
    transport = scripted(
        tool("deny_request", {"id": "c2", "reason": "too risky"}), text("Declined.")
    )
    run(brain.Brain("k", fake, transport=transport), "say no to the database one")
    assert fake.calls == [("deny", "c2", "too risky")]


def test_presence_is_actioned():
    fake = FakeOffice()
    transport = scripted(tool("set_presence", {"present": False}), text("Noted."))
    run(brain.Brain("k", fake, transport=transport), "I'm heading off")
    assert fake.calls == [("presence", False)]


def test_a_face_change_is_returned_rather_than_sent_to_the_office():
    transport = scripted(tool("set_face", {"face": "thinking"}), text("Let me see."))
    fake = FakeOffice()
    reply = run(brain.Brain("k", fake, transport=transport))
    assert reply.face == "thinking"
    assert fake.calls == []


def test_an_unknown_face_is_refused_rather_than_passed_on():
    """set_avatar rejects an unknown face, and a rejected call is a silent
    no-op at the far end."""
    transport = scripted(tool("set_face", {"face": "smug"}), text("Right."))
    reply = run(brain.Brain("k", FakeOffice(), transport=transport))
    assert reply.face is None
    assert reply.actions[0].outcome == "rejected"


def test_an_approval_without_an_id_is_refused_before_reaching_the_office():
    fake = FakeOffice()
    transport = scripted(tool("approve_request", {}), text("I need to know which one."))
    reply = run(brain.Brain("k", fake, transport=transport))
    assert fake.calls == []
    assert reply.actions[0].outcome == "rejected"


def test_a_tool_it_does_not_have_is_refused_by_name():
    transport = scripted(tool("launch_missiles", {}), text("No."))
    reply = run(brain.Brain("k", FakeOffice(), transport=transport))
    assert reply.actions[0].outcome == "rejected"
    assert "unknown tool" in reply.actions[0].detail


def test_the_loop_is_bounded_and_still_says_something():
    """A confused turn must not spin, and something has to come out of the
    speaker either way."""
    calls = [tool("set_face", {"face": "thinking"}, f"u{i}") for i in range(10)]
    b = brain.Brain("k", FakeOffice(), max_steps=3, transport=scripted(*calls))
    reply = run(b)
    assert reply.incomplete
    assert reply.speech
    assert len(reply.actions) == 3


def test_several_tool_calls_in_one_step_all_run():
    fake = FakeOffice()
    both = {
        "content": [
            {"type": "tool_use", "id": "a", "name": "approve_request", "input": {"id": "c1"}},
            {"type": "tool_use", "id": "b", "name": "deny_request", "input": {"id": "c2"}},
        ]
    }
    transport = scripted(both, text("One approved, one declined."))
    reply = run(brain.Brain("k", fake, transport=transport), "approve the first, deny the second")
    assert fake.calls == [("approve", "c1"), ("deny", "c2", None)]
    assert len(reply.actions) == 2


# ----------------------------------------------------------- the speech --
def test_speech_is_capped_because_it_goes_through_a_speaker():
    long = "This is a sentence. " * 60
    b = brain.Brain("k", FakeOffice(), transport=scripted(text(long)))
    assert len(run(b).speech) <= brain.MAX_SPEECH_CHARS


def test_the_cap_cuts_at_a_sentence_when_one_is_past_the_halfway_mark():
    """Only past halfway: cutting at a full stop in the first few words would
    throw away most of the reply in order to end tidily."""
    spoken = "Two things are waiting for you. The second one looks risky. " + "x" * 400
    trimmed = brain.trim_speech(spoken, limit=70)
    assert trimmed == "Two things are waiting for you. The second one looks risky."


def test_a_word_cut_gets_an_ellipsis_but_a_sentence_cut_does_not():
    """"Three.…" is a pause a speech engine cannot express."""
    assert brain.trim_speech("One. Two. Three. " + "x" * 400, limit=40).endswith(".")
    assert not brain.trim_speech("One. Two. Three. " + "x" * 400, limit=40).endswith("…")
    assert brain.trim_speech("a bb ccc dddd eeeee ffffff ggggggg", limit=20).endswith("…")


def test_a_short_reply_is_left_alone_apart_from_whitespace():
    assert brain.trim_speech("  Two   things\nare waiting. ") == "Two things are waiting."


def test_the_tool_schemas_are_the_four_we_intend():
    assert {t["name"] for t in brain.TOOLS} == {
        "approve_request",
        "deny_request",
        "set_presence",
        "set_face",
    }
    for tool_schema in brain.TOOLS:
        assert tool_schema["input_schema"]["type"] == "object"
        assert "description" in tool_schema


def test_the_approve_tool_warns_the_model_what_it_is_doing():
    approve = next(t for t in brain.TOOLS if t["name"] == "approve_request")
    assert "brand-new" in approve["description"]
    assert "cannot be undone" in approve["description"]
