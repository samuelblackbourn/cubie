"""The CLI brain, and the MCP server that gives it its tools.

Nothing here spawns `claude` -- the command line is built by a pure function so
it can be asserted verbatim, which is the same reason the office's own runner
splits `buildCommand` out from the spawning (`agents/claudeCommand.ts`). The
end-to-end path was exercised by hand against the real binary; what a test can
usefully hold is the SHAPE, and above all the three flags that stop a stranger's
voice reaching a shell.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import brain as brain_mod
import brain_tools
import cli_brain
import office_mcp
from office import Outcome


class FakeOffice:
    """Records what it was asked to do and answers however the test wants."""

    def __init__(self, outcome: Outcome = Outcome.OK, detail: str = "") -> None:
        self.outcome = outcome
        self.detail = detail
        self.calls: list[tuple] = []

    async def approve(self, approval_id):
        self.calls.append(("approve", approval_id))
        return (self.outcome, self.detail)

    async def deny(self, approval_id, reason=None):
        self.calls.append(("deny", approval_id, reason))
        return (self.outcome, self.detail)

    async def set_presence(self, present):
        self.calls.append(("presence", present))
        return (self.outcome, self.detail)


def args_for(**kwargs) -> list[str]:
    brain = cli_brain.CliBrain(FakeOffice(), **kwargs)
    return brain.build_args("hello", "you are a robot", Path("/tmp/turn.jsonl"))


# --- the lockdown -------------------------------------------------------
#
# These four are the security surface. Each is asserted on its own rather than
# as one "the args look right" test, so a failure names which layer went.


def test_every_built_in_tool_is_removed():
    args = args_for()
    assert "--tools" in args
    # The empty string is the CLI's own way of saying "none of them"; a missing
    # flag means the default set, which includes Bash.
    assert args[args.index("--tools") + 1] == ""


def test_no_other_mcp_server_can_reach_him():
    assert "--strict-mcp-config" in args_for()


def test_no_settings_file_can_add_a_tool_back():
    args = args_for()
    assert args[args.index("--setting-sources") + 1] == ""


def test_only_the_four_office_tools_are_allowed():
    args = args_for()
    start = args.index("--allowedTools") + 1
    allowed = []
    for value in args[start:]:
        if value.startswith("--"):
            break
        allowed.append(value)
    assert allowed == [
        "mcp__cubie__approve_request",
        "mcp__cubie__deny_request",
        "mcp__cubie__set_presence",
        "mcp__cubie__set_face",
    ]


def test_breaking_the_lockdown_is_visible():
    """The guard above, seen to fail.

    A test that only ever passes proves nothing about what it would catch, so
    this is the same assertion run against a deliberately-wrong argument list.
    If `--tools ""` were ever dropped, the built-in set -- Bash included --
    comes back, and this is the shape of the failure that would report it.
    """
    broken = [a for a in args_for() if a != "--tools"]
    with pytest.raises(ValueError):
        broken.index("--tools")


# --- the invocation -----------------------------------------------------


def test_the_prompt_replaces_the_system_prompt_rather_than_appending():
    args = args_for()
    assert "--system-prompt" in args
    # --append-system-prompt would leave Claude Code's own coding-agent prompt
    # underneath, which is what makes it answer in markdown.
    assert "--append-system-prompt" not in args


def test_the_model_is_only_named_when_asked_for():
    assert "--model" not in args_for()
    assert args_for(model="claude-haiku-4-5")[-1] == "claude-haiku-4-5"


# --- memory -------------------------------------------------------------
#
# He used to forget everything between one sentence and the next: every turn
# was a fresh `claude -p` with `--no-session-persistence`, so "and what about
# the other one?" could not work. Memory is carrying the session id forward and
# resuming it.
#
# The flag that stopped persistence was never about speed. It stopped
# ~/.claude/projects filling with one directory per utterance -- and resuming
# answers that argument rather than ignoring it, because a conversation is one
# directory instead of one per thing anyone ever said.


def test_a_conversation_is_saved_so_it_can_be_resumed():
    """The opposite of what this asserted before, deliberately.

    A session has to be SAVED to be resumable, so memory and
    `--no-session-persistence` are mutually exclusive by construction.
    """
    assert "--no-session-persistence" not in args_for()


def test_turning_memory_off_puts_the_old_flag_back():
    """Because the housekeeping reason is still valid for anyone who does not
    want memory: without it, one directory per utterance."""
    assert "--no-session-persistence" in args_for(memory_s=0)


def test_a_conversation_in_progress_is_resumed():
    brain = cli_brain.CliBrain(FakeOffice())
    args = brain.build_args("hello", "sys", Path("/tmp/t.jsonl"), resume="abc-123")
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "abc-123"


def test_the_two_flags_are_never_both_present():
    """They contradict each other: one says do not save, the other says
    continue what was saved. Either alone is a coherent build; together the
    behaviour depends on which the CLI happens to read last."""
    for kwargs in ({}, {"memory_s": 0}, {"memory_s": 60}):
        brain = cli_brain.CliBrain(FakeOffice(), **kwargs)
        for resume in (None, "abc-123"):
            args = brain.build_args("hi", "sys", Path("/tmp/t.jsonl"), resume=resume)
            assert not ("--resume" in args and "--no-session-persistence" in args), (
                kwargs,
                resume,
            )


def test_memory_off_never_asks_to_resume():
    """`_resumable` is the only thing that decides, so it has to respect the
    switch rather than relying on the caller to."""
    brain = cli_brain.CliBrain(FakeOffice(), memory_s=0)
    brain._session_id = "abc-123"
    assert brain._resumable() is None


def test_a_conversation_ends_when_the_person_walks_away():
    """Otherwise one session runs for weeks, growing slower and more expensive
    every turn until it overruns its context -- a failure that arrives as a
    gradual slowdown rather than as an error."""
    brain = cli_brain.CliBrain(FakeOffice(), memory_s=60)
    brain._session_id = "abc-123"
    brain._last_turn_at = time.monotonic() - 3600
    assert brain._resumable() is None
    assert brain._session_id is None, "the stale conversation must be let go"


def test_a_recent_conversation_is_still_open():
    brain = cli_brain.CliBrain(FakeOffice(), memory_s=1800)
    brain._session_id = "abc-123"
    brain._last_turn_at = time.monotonic() - 5
    assert brain._resumable() == "abc-123"


def test_the_clock_moves_even_when_the_cli_names_no_session():
    """A nameless turn is still someone speaking. Leaving the clock stale would
    expire a conversation that is actually in progress."""
    brain = cli_brain.CliBrain(FakeOffice(), memory_s=1800)
    brain._session_id = "abc-123"
    brain._last_turn_at = 0.0
    brain._remember(None)
    assert brain._session_id == "abc-123"
    assert brain._last_turn_at > 0.0


def test_the_session_is_taken_from_the_cli_not_invented():
    payload = json.dumps(
        {"result": "hello", "is_error": False, "session_id": "sid-from-cli"}
    )
    parsed = cli_brain.CliBrain(FakeOffice())._payload_from(payload.encode())
    assert parsed.get("session_id") == "sid-from-cli"


def test_a_conversation_that_cannot_be_resumed_costs_nobody_an_answer():
    """The CLI is upgraded, ~/.claude is cleared, the session ages out. None of
    those are the speaker's fault and none should produce "I cannot think
    straight" -- the conversation is dropped and the question asked again."""
    brain = cli_brain.CliBrain(FakeOffice())
    brain._session_id = "gone"
    brain._last_turn_at = time.monotonic()
    tried = []

    async def fake_run(transcript, system, resume):
        tried.append(resume)
        if resume is not None:
            raise brain_mod.BrainError("no conversation found with that id")
        return cli_brain._Turn(
            text="here you go", actions=(), face=None, session_id="fresh"
        )

    brain._run = fake_run
    reply = asyncio.run(brain.respond("what about the other one?", None))

    assert tried == ["gone", None], tried
    assert reply.speech == "here you go"
    assert brain._session_id == "fresh", "the new conversation replaces the lost one"


def test_a_second_failure_is_a_real_fault_and_surfaces():
    """Retrying forever would turn one broken turn into an unbounded loop, and
    hide the actual error behind it."""
    brain = cli_brain.CliBrain(FakeOffice())
    brain._session_id = "gone"
    brain._last_turn_at = time.monotonic()

    async def always_fails(transcript, system, resume):
        raise brain_mod.BrainError("the brain is genuinely broken")

    brain._run = always_fails
    with pytest.raises(brain_mod.BrainError, match="genuinely broken"):
        asyncio.run(brain.respond("hello", None))


def test_the_mcp_config_names_the_server_and_the_log():
    brain = cli_brain.CliBrain(FakeOffice())
    config = brain.mcp_config(Path("/tmp/t.jsonl"))
    server = config["mcpServers"]["cubie"]
    assert server["args"][0].endswith("office_mcp.py")
    assert server["env"]["CUBIE_TURN_LOG"] == "/tmp/t.jsonl"


def test_the_mcp_server_is_not_given_the_gateway_token(monkeypatch):
    """It has no business reaching the gateway, so it never learns how."""
    monkeypatch.setenv("STACKCHAN_TOKEN", "a" * 64)
    env = cli_brain.CliBrain(FakeOffice()).mcp_config(Path("/tmp/t"))["mcpServers"][
        "cubie"
    ]["env"]
    assert "STACKCHAN_TOKEN" not in env


# --- reading the turn back ---------------------------------------------


def test_the_face_the_model_ended_on_wins(tmp_path):
    log = tmp_path / "actions.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(
                {"name": "set_face", "arguments": {"face": f}, "outcome": "ok", "detail": ""}
            )
            for f in ("sad", "thinking")
        )
        + "\n"
    )
    actions, face = cli_brain.read_turn_log(log)
    assert face == "thinking"
    assert len(actions) == 2


def test_a_rejected_face_is_not_worn(tmp_path):
    log = tmp_path / "actions.jsonl"
    log.write_text(
        json.dumps(
            {
                "name": "set_face",
                "arguments": {"face": "smug"},
                "outcome": "rejected",
                "detail": "unknown face",
            }
        )
        + "\n"
    )
    _, face = cli_brain.read_turn_log(log)
    assert face is None


def test_a_ragged_tail_does_not_lose_the_actions_before_it(tmp_path):
    """A subprocess killed mid-write must not erase an approval that happened."""
    log = tmp_path / "actions.jsonl"
    log.write_text(
        json.dumps(
            {"name": "approve_request", "arguments": {"id": "x"}, "outcome": "ok"}
        )
        + "\n{\"name\": \"set_fa"
    )
    actions, _ = cli_brain.read_turn_log(log)
    assert [a.name for a in actions] == ["approve_request"]


def test_a_missing_log_is_not_an_error(tmp_path):
    assert cli_brain.read_turn_log(tmp_path / "nope.jsonl") == ((), None)


# --- what may reach the speaker ----------------------------------------


def parse(payload: str) -> str:
    return cli_brain.CliBrain(FakeOffice())._text_from(payload.encode())


def test_the_answer_is_taken_from_the_json_result():
    assert parse(json.dumps({"result": " Two waiting. ", "is_error": False})) == (
        "Two waiting."
    )


def test_machine_output_never_reaches_the_speaker():
    """`stdout.strip()` would happily have him read an MCP warning aloud."""
    with pytest.raises(brain_mod.BrainError):
        parse("Client.listTools() called but server does not advertise tools")


def test_an_empty_answer_is_not_an_exception():
    # `incomplete` covers this upstream; the brain must not raise, because
    # something still has to come out of the speaker.
    assert parse(json.dumps({"result": "", "is_error": False})) == ""


def test_a_reported_error_is_raised_rather_than_spoken():
    with pytest.raises(brain_mod.BrainError):
        parse(json.dumps({"result": "Credit balance too low", "is_error": True}))


def test_a_missing_binary_says_so(monkeypatch):
    brain = cli_brain.CliBrain(FakeOffice(), executable="claude-that-is-not-installed")
    with pytest.raises(brain_mod.BrainError, match="not on PATH"):
        asyncio.run(brain.respond("hello", None))


# --- the MCP server -----------------------------------------------------


def call(method: str, params: dict | None = None, ident: int = 1, office=None, log=None):
    message = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return asyncio.run(
        office_mcp.handle(message, office or FakeOffice(), log or office_mcp._TurnLog(None))
    )


def test_the_four_tools_are_advertised():
    result = call("tools/list")["result"]
    assert [t["name"] for t in result["tools"]] == [t["name"] for t in brain_tools.TOOLS]


def test_the_schema_key_is_mcps_not_the_apis():
    """`inputSchema` here, `input_schema` in the Messages API. One list, two shapes."""
    tool = call("tools/list")["result"]["tools"][0]
    assert "inputSchema" in tool and "input_schema" not in tool


def test_a_notification_gets_no_reply():
    """Answering one is a protocol error some clients treat as fatal."""
    assert asyncio.run(
        office_mcp.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            FakeOffice(),
            office_mcp._TurnLog(None),
        )
    ) is None


def test_the_clients_protocol_version_is_echoed():
    reply = call("initialize", {"protocolVersion": "2099-01-01"})
    assert reply["result"]["protocolVersion"] == "2099-01-01"


def test_an_approval_reaches_the_office():
    office = FakeOffice()
    reply = call(
        "tools/call",
        {"name": "approve_request", "arguments": {"id": " abc "}},
        office=office,
    )
    assert office.calls == [("approve", "abc")]
    assert reply["result"]["content"][0]["text"] == "done"


def test_a_stale_approval_tells_the_model_not_to_retry():
    """The whole reason the outcome table is shared rather than copied."""
    reply = call(
        "tools/call",
        {"name": "approve_request", "arguments": {"id": "abc"}},
        office=FakeOffice(Outcome.STALE),
    )
    text = reply["result"]["content"][0]["text"]
    assert "do not retry" in text.lower()
    assert text == brain_tools.OUTCOME_TEXT[Outcome.STALE]


def test_a_failed_tool_is_a_result_not_a_protocol_error():
    """The refusal text IS the answer; flagging it invites a client to hide it."""
    reply = call(
        "tools/call",
        {"name": "set_face", "arguments": {"face": "smug"}},
    )
    assert "error" not in reply
    assert "not one of your faces" in reply["result"]["content"][0]["text"]


def test_an_unknown_method_is_an_error():
    assert call("resources/list")["error"]["code"] == office_mcp.METHOD_NOT_FOUND


def test_every_call_is_recorded(tmp_path):
    log_path = tmp_path / "actions.jsonl"
    log = office_mcp._TurnLog(str(log_path))
    call(
        "tools/call",
        {"name": "set_presence", "arguments": {"present": True}},
        log=log,
    )
    recorded = json.loads(log_path.read_text().strip())
    assert recorded["name"] == "set_presence"
    assert recorded["outcome"] == "ok"


# --- the credential the CLI brain must not inherit ----------------------
#
# Choosing the CLI brain has to mean choosing the CLI's own login. The CLI
# resolves ANTHROPIC_API_KEY first and says so, so a key left in the env for
# the other brain silently decides this one's credential too.


def test_the_api_key_is_not_passed_to_the_cli():
    env = cli_brain.child_env({"ANTHROPIC_API_KEY": "sk-whatever", "HOME": "/home/sam"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == "/home/sam"


def test_the_auth_token_is_not_passed_either():
    assert "ANTHROPIC_AUTH_TOKEN" not in cli_brain.child_env(
        {"ANTHROPIC_AUTH_TOKEN": "t", "PATH": "/usr/bin"}
    )


def test_everything_else_survives():
    """It is a subtraction, not an allowlist: the CLI needs HOME and PATH."""
    source = {"HOME": "/home/sam", "PATH": "/usr/bin", "LANG": "C", "STACKCHAN_TOKEN": "x"}
    assert cli_brain.child_env(source) == source


def test_a_real_environ_is_not_mutated(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    cli_brain.child_env()
    import os as _os

    assert _os.environ["ANTHROPIC_API_KEY"] == "sk-live"


# --- a timeout must carry its own explanation ---------------------------


class DeadProcess:
    """A killed subprocess with something to say, and one with nothing."""

    def __init__(self, out=b"", err=b""):
        self._pair = (out, err)

    async def communicate(self):
        return self._pair


def test_a_timeout_reports_what_the_process_said():
    said = asyncio.run(cli_brain.CliBrain._drain(DeadProcess(err=b"Invalid API key\n")))
    assert "Invalid API key" in said
    assert said.startswith("stderr=")


def test_a_silent_process_yields_nothing_rather_than_noise():
    assert asyncio.run(cli_brain.CliBrain._drain(DeadProcess())) == ""


def test_draining_never_replaces_the_timeout_with_its_own_error():
    class Hostile:
        async def communicate(self):
            raise OSError("pipe already closed")

    assert asyncio.run(cli_brain.CliBrain._drain(Hostile())) == ""


def test_an_expired_login_says_so_rather_than_looking_broken():
    """This exact failure cost an evening.

    The CLI's claude.ai OAuth session lapses after some weeks. Every turn then
    fails and he falls back to "I cannot think straight just now" -- which
    reads as a robot that has broken rather than a credential that needs
    renewing, and nothing anywhere names the difference.
    """
    payload = json.dumps(
        {
            "is_error": True,
            "result": "Failed to authenticate: OAuth session expired and "
                      "could not be refreshed",
        }
    )
    with pytest.raises(brain_mod.BrainError, match="login on this machine has expired"):
        cli_brain.CliBrain(FakeOffice())._payload_from(payload.encode())


def test_an_ordinary_error_is_still_reported_as_itself():
    """The auth message must not swallow everything else -- an over-eager
    pattern would report every failure as a credential problem and send the
    next person to renew a login that was never the issue."""
    payload = json.dumps({"is_error": True, "result": "model overloaded"})
    with pytest.raises(brain_mod.BrainError, match="model overloaded"):
        cli_brain.CliBrain(FakeOffice())._payload_from(payload.encode())
