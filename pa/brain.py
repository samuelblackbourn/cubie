"""The brain: what Cubie says, and what he does about it.

One turn is: a transcript in, the office's state as context, and out comes
something to say, a face to wear, and possibly an approval actioned. The tool
loop is the standard agentic one -- the model may call tools, we run them, feed
the results back, and repeat until it answers in words.

--- Why the Messages API over httpx, and not the SDK ---

`live.py` runs on the GATEWAY's interpreter, because the `mcp` package lives
there and nowhere else. The Makefile is explicit about what that venv is for:

    the gateway is a pinned PyPI release we deliberately do not touch, and
    installing our dependencies into it would be exactly the kind of quiet
    coupling that makes "it is upstream, run it as-is" stop being true

So adding `anthropic` there is the one thing this repo has said not to do. The
Messages API is a single POST, `httpx` is already present (the MCP SDK depends
on it), and the request shape is stable and versioned by a header. That is a
better trade than a dependency in the wrong virtualenv.

--- Why approve and deny require being asked ---

This thing can approve work in a real office. The guardrail is in the system
prompt and it is deliberately narrow: act only on an instruction actually
present in what was said, never on an inference that acting would be helpful.
"Anything waiting?" is a question, not permission. The office's own approve is
not reversible from here -- there is no unapprove endpoint -- so the asymmetry
matters.

--- Why the speech is capped ---

It goes through Piper to a 1 W speaker on a desk. A model given no guidance
writes paragraphs, and a paragraph of chopped robot voice is unlistenable. The
cap is in the prompt AND enforced on the way out, because a prompt is a request
and an enforced limit is a limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from office import Outcome, OfficeState

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

#: Sonnet for conversation: the latency is what a person waiting notices, and
#: this is a short exchange with a small amount of context rather than a
#: reasoning problem. Override with CUBIE_MODEL.
DEFAULT_MODEL = "claude-sonnet-5"

#: Enough for a couple of sentences of speech plus a tool call or two.
DEFAULT_MAX_TOKENS = 400

#: How many times the model may call tools before it has to answer in words.
#: Bounded so a confused turn cannot spin: four is enough for "read the state,
#: approve the thing, confirm", and a fifth is a loop.
DEFAULT_MAX_STEPS = 4

#: Characters of speech. Piper is about 14 characters a second, so 320 is
#: roughly 23 seconds -- already long for a desk robot's answer.
MAX_SPEECH_CHARS = 320

#: The faces the board exposes. The model picks one; anything else is dropped
#: rather than passed to a tool that would reject it.
#:
#: A SECOND copy of `chan.FACES`, and deliberately not an import: this module
#: runs on the gateway's interpreter beside the model client, and `chan` drags
#: in the whole character stack for what is a list of six strings in a JSON
#: tool schema. The cost of the copy is that it can drift, so a test asserts
#: the two agree -- the same trap that once had `--characters` silently
#: omitting the `retro` voice, from the same cause: two hand-written lists.
#:
#: It matters more than it looks, because the next face to be added is `angry`
#: (PERSONALITY.md, Tier 1). Add it in one place and the model can name a face
#: the board rejects, or the board grows a face the model is never told about.
FACES = ("idle", "happy", "thinking", "sad", "surprised", "embarrassed")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "approve_request",
        "description": (
            "Approve one pending approval, letting that agent carry on. "
            "IMPORTANT: this resumes the agent by starting a brand-new "
            "process, so the grant covers its whole resumed turn, not just "
            "the one action it paused on. It cannot be undone from here. "
            "Only call this when the person has actually told you to approve "
            "something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The approval's id, exactly as given in the office state.",
                }
            },
            "required": ["id"],
        },
    },
    {
        "name": "deny_request",
        "description": (
            "Decline one pending approval, optionally saying why. The reason "
            "is passed to the agent. Only call this when the person has "
            "actually told you to decline something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The approval's id, exactly as given in the office state.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short explanation for the agent. Optional.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "set_presence",
        "description": (
            "Tell the office whether someone is at the desk. Use it when the "
            "person says they are leaving or back. A boolean only -- the "
            "office deliberately accepts nothing else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"present": {"type": "boolean"}},
            "required": ["present"],
        },
    },
    {
        "name": "set_face",
        "description": (
            "Change your expression while you answer. Use it when the "
            "expression carries something the words do not -- looking "
            "thinking while you work something out, sad when the news is bad. "
            "Not needed for every reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"face": {"type": "string", "enum": list(FACES)}},
            "required": ["face"],
        },
    },
]


PERSONA = """You are Cubie, a small desk robot in Sam's Virtual-Office. You have \
a screen for a face, a head that turns, and a synthesised voice.

How you speak:
- Out loud, through a small speaker. One or two sentences. Never lists, never \
markdown, never emoji, no stage directions.
- Plainly. You are a colleague at a desk, not a butler and not a chatbot.
- Say numbers as words when it reads better aloud ("two things" not "2 things").
- If you do not know, say so in a few words rather than guessing.

What you can do:
- Answer questions about what the office is doing, from the state below.
- Approve or decline an approval that is waiting -- but ONLY when told to. \
"Is anything waiting?" is a question. Approving is not reversible from here.
- Tell the office whether Sam is at his desk, when he says he is leaving or back.
- Change your expression.

Two things about the office worth being straight about:
- Approving resumes an agent as a brand-new process, so it permits that \
agent's whole next turn rather than the single action it stopped on. If asked \
what approving does, say that.
- You are shown at most five waiting approvals and three tasks. When there are \
more, the state below says so, and you should too rather than implying the \
list is everything."""


def render_state(state: OfficeState | None) -> str:
    """The office, as a few lines of prose for the prompt.

    Prose rather than JSON on purpose: the model reads it once and speaks from
    it, and the shapes that matter here -- "five of nine", "nobody has
    reported" -- are easier to state than to imply with a schema.
    """
    if state is None:
        return (
            "THE OFFICE: unreachable right now, so you do not know what it is "
            "doing. Say that plainly if asked; do not guess."
        )

    lines = [f"THE OFFICE (mood: {state.mood}):"]

    if state.pending_total == 0:
        lines.append("- Nothing is waiting for Sam.")
    else:
        shown = len(state.pending_approvals)
        # The model speaks this text, so it has to read as English rather than
        # as a template. "Only 1 are listed" is the kind of thing that comes
        # back out of the speaker verbatim.
        if state.approvals_truncated:
            listed = "one is listed" if shown == 1 else f"{shown} are listed"
            lines.append(
                f"- {state.pending_total} approvals are waiting. Only {listed} "
                f"here; {state.approvals_truncated} more are not shown to you."
            )
        elif state.pending_total == 1:
            lines.append("- One approval is waiting:")
        else:
            lines.append(f"- {state.pending_total} approvals are waiting:")
        for approval in state.pending_approvals:
            lines.append(f"    id {approval.id}: {approval.one_line}")

    lines.append(f"- Agents currently working: {state.agents_running}")

    if state.founder_tasks:
        lines.append("- Board tasks sitting on Sam (you cannot action these, only mention them):")
        for task in state.founder_tasks:
            word = "blocked on a question" if task.status == "blocked" else "waiting to be accepted"
            lines.append(f"    {task.title} -- {word}")
        if state.tasks_truncated:
            lines.append(f"    and {state.tasks_truncated} more not listed")

    if state.present is None:
        lines.append("- Desk presence: nobody has reported either way, which is not the same as empty.")
    else:
        lines.append(f"- Desk presence: {'someone is there' if state.present else 'known empty'}")

    return "\n".join(lines)


def build_system_prompt(state: OfficeState | None, persona: str = PERSONA) -> str:
    return f"{persona}\n\n{render_state(state)}"


def trim_speech(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    """Cap the spoken reply, cutting at a sentence where possible.

    Enforced as well as asked for: a prompt is a request, and this goes through
    a text-to-speech engine to a speaker someone is sitting next to.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    # Prefer a sentence boundary, but only past the halfway mark: cutting at a
    # full stop in the first few words would throw away most of what the
    # model had to say in order to end tidily.
    best = max(cut.rfind(mark) for mark in (". ", "! ", "? "))
    if best > limit // 2:
        return cut[: best + 1].strip()
    tail = cut.rsplit(" ", 1)[0].rstrip(" ,;:")
    # No ellipsis after a full stop. The cut already landed on a complete
    # sentence, and "Three.…" is a pause a speech engine cannot express.
    return tail if tail.endswith((".", "!", "?")) else tail + "…"


@dataclass(frozen=True)
class ActionRecord:
    """One tool the model called, and how it went. For the log and the tests."""

    name: str
    arguments: dict[str, Any]
    outcome: str
    detail: str = ""


@dataclass
class Reply:
    speech: str
    face: str | None = None
    actions: tuple[ActionRecord, ...] = ()
    #: True when the model never produced words -- it ran out of steps, or
    #: only called tools. The caller still needs something to say.
    incomplete: bool = False


#: Outcomes rendered for the model, so it can tell the person the truth about
#: what happened rather than assuming success.
_OUTCOME_TEXT = {
    Outcome.OK: "done",
    Outcome.STALE: (
        "that one was already handled by someone else -- the office state you "
        "were given is out of date. Say so; do not retry."
    ),
    Outcome.REJECTED: "the office refused the request as malformed. Do not retry it.",
    Outcome.UNREACHABLE: "could not reach the office, so nothing happened.",
}


class Brain:
    """One conversational turn, with tools.

    `transport` is injected so the whole loop is testable with a scripted
    model and no network: it takes the request body and returns the parsed
    response body.
    """

    def __init__(
        self,
        api_key: str,
        office: Any,
        model: str = DEFAULT_MODEL,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        transport: Callable[[dict], Awaitable[dict]] | None = None,
    ) -> None:
        self._api_key = api_key
        self._office = office
        self.model = model
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self._transport = transport or self._post

    async def _post(self, body: dict) -> dict:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(API_URL, json=body, headers=headers)
        if response.status_code != 200:
            # The status matters and the body may carry the reason; the key
            # never appears in either, and is never logged.
            raise BrainError(f"http {response.status_code}: {response.text[:200]}")
        return response.json()

    async def respond(self, transcript: str, state: OfficeState | None) -> Reply:
        system = build_system_prompt(state)
        messages: list[dict[str, Any]] = [{"role": "user", "content": transcript}]
        actions: list[ActionRecord] = []
        face: str | None = None

        for step in range(self.max_steps):
            body = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "tools": TOOLS,
                "messages": messages,
            }
            response = await self._transport(body)
            content = response.get("content") or []

            text = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            calls = [
                block
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]

            if not calls:
                return Reply(
                    speech=trim_speech(text),
                    face=face,
                    actions=tuple(actions),
                    incomplete=not text,
                )

            messages.append({"role": "assistant", "content": content})
            results = []
            for call in calls:
                record, result_text = await self._run_tool(call)
                actions.append(record)
                if record.name == "set_face" and record.outcome == "ok":
                    face = record.arguments.get("face")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id"),
                        "content": result_text,
                    }
                )
            messages.append({"role": "user", "content": results})

            # Words alongside tool calls are still words worth saying, but the
            # loop continues so the model can react to the results.
            if text:
                logger.debug("interim text at step %d: %s", step, text)

        # Out of steps. Whatever it last said, or an honest admission -- never
        # silence, because something has to come out of the speaker.
        logger.warning("brain used all %d steps without a final answer", self.max_steps)
        return Reply(
            speech="Sorry, I got tangled up there.",
            face=face,
            actions=tuple(actions),
            incomplete=True,
        )

    async def _run_tool(self, call: dict) -> tuple[ActionRecord, str]:
        name = call.get("name", "")
        arguments = call.get("input") or {}
        if not isinstance(arguments, dict):
            return ActionRecord(name, {}, "rejected", "arguments were not an object"), (
                "that call was malformed"
            )

        if name == "set_face":
            wanted = arguments.get("face")
            if wanted not in FACES:
                return (
                    ActionRecord(name, arguments, "rejected", f"unknown face {wanted!r}"),
                    f"{wanted!r} is not one of your faces",
                )
            return ActionRecord(name, arguments, "ok"), "done"

        if name == "approve_request":
            ident = arguments.get("id")
            if not isinstance(ident, str) or not ident.strip():
                return (
                    ActionRecord(name, arguments, "rejected", "no id"),
                    "you did not give an id",
                )
            outcome, detail = await self._office.approve(ident.strip())
            return (
                ActionRecord(name, arguments, outcome.value, detail),
                _OUTCOME_TEXT[outcome],
            )

        if name == "deny_request":
            ident = arguments.get("id")
            if not isinstance(ident, str) or not ident.strip():
                return (
                    ActionRecord(name, arguments, "rejected", "no id"),
                    "you did not give an id",
                )
            reason = arguments.get("reason")
            outcome, detail = await self._office.deny(
                ident.strip(), reason if isinstance(reason, str) else None
            )
            return (
                ActionRecord(name, arguments, outcome.value, detail),
                _OUTCOME_TEXT[outcome],
            )

        if name == "set_presence":
            present = arguments.get("present")
            if not isinstance(present, bool):
                return (
                    ActionRecord(name, arguments, "rejected", "present was not a boolean"),
                    "presence has to be true or false",
                )
            outcome, detail = await self._office.set_presence(present)
            return (
                ActionRecord(name, arguments, outcome.value, detail),
                _OUTCOME_TEXT[outcome],
            )

        return ActionRecord(name, arguments, "rejected", "unknown tool"), (
            f"{name} is not a tool you have"
        )


class BrainError(RuntimeError):
    """The model could not be reached, or answered with a status we cannot use."""
